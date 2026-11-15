"""Attestation envelope — DESIGN.md §7.1/§7.2 (M1, Verifiable Compute).

Honest labelling, stated in the code (DESIGN.md §7, §7.6): the v1
envelope is a SOFTWARE attestation. It proves which code tree minted
the action and binds the action to that code; it is NOT a hardware TEE
attestation (no SEV-SNP/TDX/SGX hardware is involved) and must never
be described as one.

WHAT THE M1 LAYER DOES NOT PROVE (DESIGN.md §7.6, stated once here
and in docs/HEDERA.md): the software envelope proves which code tree
minted the action and binds the action to that code; it does NOT prove
the code ran on genuine hardware, that the host was uncompromised, or
that the agent's judgement was sound. A compromised host can mint a
valid software envelope for bad code — the measurement would differ
from the pinned set, which is the detection, not prevention. The
hardware leg (SEV-SNP CVM) is the answer to that residual and is a
later, owner-decided step.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

ATTEST_VERSION = "joulex-attest-0.1"
MAX_ENVELOPE_TTL_SECONDS = 300

# Kinds this verifier implements. The hardware kinds are RESERVED:
# a verifier MUST reject an envelope whose kind it does not implement,
# so a sev-snp/tdx/... envelope is rejected here until a hardware
# verification path exists (Phase 1, owner-decided).
IMPLEMENTED_KINDS = frozenset({"software"})
RESERVED_KINDS = frozenset(
    {"sev-snp", "tdx", "sgx-dcap", "nitro", "zkvm-risc0", "zkvm-sp1"}
)

# DESIGN.md §7.2 reason strings.
VC_INVALID = "JOULEX_VC_INVALID"
VC_KIND = "JOULEX_VC_KIND"
VC_BINDING = "JOULEX_VC_BINDING"
VC_EXPIRED = "JOULEX_VC_EXPIRED"
VC_MEASUREMENT = "JOULEX_VC_MEASUREMENT"
VC_EVIDENCE = "JOULEX_VC_EVIDENCE"
VC_REQUIRED = "JOULEX_VC_REQUIRED"


def canonical_bytes(data: dict[str, Any]) -> bytes:
    """JCS-style canonical JSON: sorted keys, no whitespace.

    Same byte shape as ``joulex.admission.canonical_bytes`` (kept as a
    separate copy so this module has no import cycle with admission,
    which imports this module for verification)."""
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _parse_time(value: Any) -> datetime:
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def code_manifest(package_dir: str | None = None) -> list[list[str]]:
    """The canonical code manifest: sorted [relative path, sha256] for
    every ``.py`` file in the ``joulex/`` package at mint time. This is
    what "which code ran" means in software: the exact code tree."""
    pkg = package_dir or os.path.dirname(os.path.abspath(__file__))
    entries: list[list[str]] = []
    for root, dirs, files in os.walk(pkg):
        dirs.sort()
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, pkg)
            entries.append([rel, _sha256_file(full)])
    entries.sort(key=lambda e: e[0])
    return entries


def compute_measurement(package_dir: str | None = None) -> str:
    """SHA-256 of the canonical JSON of the code manifest."""
    return hashlib.sha256(canonical_bytes(
        {"manifest": code_manifest(package_dir)}
    )).hexdigest()


def binding_hash(credential_id: str, challenge_nonce: str, payload_hash: str) -> str:
    """REPORT_DATA binding (normative, DESIGN.md §7.1): SHA-256 over
    the JCS canonical bytes of {credential_id, challenge_nonce,
    payload_hash} for THIS action. A report captured from an earlier
    action cannot be stapled to a later one: the challenge nonce and
    payload hash differ."""
    return hashlib.sha256(
        canonical_bytes(
            {
                "credential_id": str(credential_id),
                "challenge_nonce": str(challenge_nonce),
                "payload_hash": str(payload_hash),
            }
        )
    ).hexdigest()


def payload_hash_for_settlement(
    *,
    market_id: str,
    outcome: str | None,
    observed_value: float | None,
    evidence: str | None,
) -> str:
    """The payload hash a settlement attestation binds: SHA-256 over
    the canonical settlement payload. The settler supplies
    credential_id + challenge_nonce alongside the envelope (see
    ``Exchange.settle_market``); the verifier recomputes this hash
    from the live settlement and compares, exactly like an order."""
    return hashlib.sha256(
        canonical_bytes(
            {
                "market_id": str(market_id),
                "outcome": outcome,
                "observed_value": observed_value,
                "evidence": evidence,
            }
        )
    ).hexdigest()


def _default_evidence(measurement: str) -> str:
    """Base64 of the canonical evidence object for a software-minted
    envelope with no provenance link file: names the measurement it
    attests (verified by the verifier against the envelope) and no
    link. A link-backed envelope names ``link_sha256`` instead — see
    ``joulex.provenance.build_link``."""
    return base64.b64encode(
        canonical_bytes({"measurement": measurement, "link_sha256": None})
    ).decode("ascii")


def mint_envelope(
    *,
    credential_id: str,
    challenge_nonce: str,
    payload_hash: str,
    now: datetime,
    measurement: str | None = None,
    evidence: str | None = None,
    kind: str = "software",
    ttl_seconds: int = MAX_ENVELOPE_TTL_SECONDS,
) -> dict[str, Any]:
    """Mint a software attestation envelope bound to THIS action.

    ``expires_at - issued_at`` is capped at 300s (same bound as the
    token TTL — an attestation outlives nothing it attests to).
    ``measurement`` defaults to the live code tree's manifest hash."""
    if ttl_seconds <= 0 or ttl_seconds > MAX_ENVELOPE_TTL_SECONDS:
        raise ValueError(
            f"ttl_seconds must be in (0, {MAX_ENVELOPE_TTL_SECONDS}]"
        )
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    m = measurement or compute_measurement()
    return {
        "version": ATTEST_VERSION,
        "kind": kind,
        "measurement": m,
        "report_data": binding_hash(credential_id, challenge_nonce, payload_hash),
        "evidence": evidence if evidence is not None else _default_evidence(m),
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
    }


def default_policy() -> dict[str, Any]:
    """Default attestation policy (DESIGN.md §7.2): advisory —
    required=false, software only, no pinned measurements, 300s max age."""
    return {
        "required": False,
        "kinds": ["software"],
        "measurements": [],
        "max_age_seconds": MAX_ENVELOPE_TTL_SECONDS,
    }


def load_pinned_measurements(data_dir: str) -> list[str]:
    """The operator-maintained pinned measurement set from
    ``<data_dir>/measurements.json`` (``{"measurements": [...]}``).
    Missing file means no pinning is configured (empty set). A corrupt
    file refuses startup (fail closed) — a verifier must never trade
    against a pinning set it cannot read."""
    path = os.path.join(data_dir, "measurements.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (ValueError, OSError) as exc:
        raise RuntimeError(
            f"refusing to start: unreadable pinned measurements: {exc}"
        ) from exc
    if not isinstance(state, dict) or not isinstance(state.get("measurements"), list):
        raise RuntimeError(
            "refusing to start: corrupt pinned measurements "
            "(not an object with a measurements list)"
        )
    return [str(m) for m in state["measurements"]]


def _verify_evidence(
    envelope: dict[str, Any], *, provenance_dir: str | None
) -> tuple[bool, str]:
    """Re-verify the evidence from scratch; never trust the agent's
    assertion. For software envelopes the evidence is base64 canonical
    JSON naming the measurement (and, when link-backed, the sha256 of
    the provenance link file). A named link must exist under
    ``provenance_dir`` and its product hash must equal the envelope's
    measurement."""
    try:
        raw = base64.b64decode(str(envelope["evidence"]), validate=True)
        evidence = json.loads(raw)
    except (ValueError, TypeError):
        return False, VC_EVIDENCE
    if not isinstance(evidence, dict):
        return False, VC_EVIDENCE
    if evidence.get("measurement") != envelope.get("measurement"):
        return False, VC_EVIDENCE
    link_sha = evidence.get("link_sha256")
    if link_sha is not None:
        if provenance_dir is None:
            return False, VC_EVIDENCE
        link_path = os.path.join(provenance_dir, f"{link_sha}.link.json")
        if not os.path.exists(link_path):
            return False, VC_EVIDENCE
        try:
            with open(link_path, "r", encoding="utf-8") as f:
                link = json.load(f)
            product = link["products"]["code_manifest_hash"]["sha256"]
        except (ValueError, KeyError, OSError, TypeError):
            return False, VC_EVIDENCE
        if product != envelope.get("measurement"):
            return False, VC_EVIDENCE
    return True, "OK"


def verify_envelope(
    envelope: dict[str, Any] | None,
    *,
    credential_id: str,
    challenge_nonce: str,
    payload_hash: str,
    policy: dict[str, Any] | None = None,
    pinned_measurements: list[str] | None = None,
    now: datetime | None = None,
    provenance_dir: str | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Verify one attestation envelope against the live action.

    Order: shape/version -> kind implemented -> kind allowed by
    policy -> TTL + expiry + policy max age -> REPORT_DATA binding ->
    measurement pinning -> evidence. Returns (ok, reason, info);
    info carries kind/measurement/verified on success."""
    empty: dict[str, Any] = {}
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if not isinstance(envelope, dict):
        return False, VC_INVALID, empty
    if envelope.get("version") != ATTEST_VERSION:
        return False, VC_INVALID, empty
    for field in ("kind", "measurement", "report_data", "evidence", "issued_at", "expires_at"):
        if field not in envelope:
            return False, VC_INVALID, empty
    kind = str(envelope["kind"])
    if kind not in IMPLEMENTED_KINDS:
        # Reserved hardware kinds included: this verifier implements
        # software only, and MUST reject a kind it does not implement.
        return False, VC_KIND, empty
    if policy is not None:
        kinds = policy.get("kinds")
        if isinstance(kinds, list) and kinds and kind not in [str(k) for k in kinds]:
            return False, VC_KIND, empty
    try:
        issued = _parse_time(envelope["issued_at"])
        expires = _parse_time(envelope["expires_at"])
    except ValueError:
        return False, VC_INVALID, empty
    if (expires - issued).total_seconds() > MAX_ENVELOPE_TTL_SECONDS:
        return False, VC_INVALID, empty
    if current < issued or current > expires:
        return False, VC_EXPIRED, empty
    if policy is not None:
        max_age = policy.get("max_age_seconds")
        if isinstance(max_age, (int, float)) and (current - issued).total_seconds() > float(max_age):
            return False, VC_EXPIRED, empty
    expected = binding_hash(credential_id, challenge_nonce, payload_hash)
    if str(envelope["report_data"]) != expected:
        return False, VC_BINDING, empty
    # Measurement pinning: the policy's list wins when non-empty;
    # otherwise the verifier's configured pinned set applies. An
    # empty set in both places means no pinning is enforced.
    allowed: list[str] | None = None
    if policy is not None:
        measurements = policy.get("measurements")
        if isinstance(measurements, list) and measurements:
            allowed = [str(m) for m in measurements]
    if allowed is None and pinned_measurements:
        allowed = [str(m) for m in pinned_measurements]
    if allowed is not None and str(envelope["measurement"]) not in allowed:
        return False, VC_MEASUREMENT, empty
    ok, reason = _verify_evidence(envelope, provenance_dir=provenance_dir)
    if not ok:
        return False, reason, empty
    return True, "OK", {
        "kind": kind,
        "measurement": str(envelope["measurement"]),
        "verified": True,
    }


def verify_with_policy(
    envelope: dict[str, Any] | None,
    *,
    credential_id: str,
    challenge_nonce: str,
    payload_hash: str,
    policy: dict[str, Any] | None = None,
    pinned_measurements: list[str] | None = None,
    now: datetime | None = None,
    provenance_dir: str | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Apply an attestation policy to one action (DESIGN.md §7.2).

    - policy.required=true and no envelope -> JOULEX_VC_REQUIRED.
    - required=false (default, advisory): absent -> OK (plain CF-KYA
      verification); present+valid -> OK, recorded; present+invalid
      -> rejected (an invalid attestation is evidence of attack,
      never ignored).
    - A malformed policy is a fail-closed rejection (JOULEX_VC_INVALID):
      the quorum-signed policy is part of what the issuer attests to,
      so a policy the verifier cannot parse cannot be honoured."""
    if policy is not None and not isinstance(policy, dict):
        return False, VC_INVALID, {}
    effective = policy if policy is not None else default_policy()
    if envelope is None:
        if effective.get("required") is True:
            return False, VC_REQUIRED, {}
        return True, "OK", {"attestation": "absent"}
    ok, reason, info = verify_envelope(
        envelope,
        credential_id=credential_id,
        challenge_nonce=challenge_nonce,
        payload_hash=payload_hash,
        policy=effective,
        pinned_measurements=pinned_measurements,
        now=now,
        provenance_dir=provenance_dir,
    )
    if not ok:
        return False, reason, {}
    return True, "OK", {"attestation": "verified", **info}
