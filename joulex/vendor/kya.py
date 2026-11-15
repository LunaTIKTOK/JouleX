"""CF-KYA v0.1 — Know Your Agent protocol (the Bind layer, specified to work).

Answers one question a counterparty can check for itself, without trusting
the agent's word: which legal person (human or corporation) stands behind
this agent, for this action, right now.

Design rules (from CF_SPEC.md, implemented here, not aspirational):
- No new blockchain, token, or global registry. Verification needs only
  public keys, the credential, and a freshness-checked revocation list.
- Key discovery copies DKIM: a principal publishes its public key at
  https://<domain>/.well-known/cf-kya.json (see discovery.py). Discovery is
  a separate step; NOTHING in this module performs network I/O, so
  verification itself is offline. Callers fetch keys and revocation lists,
  then verify with plain dicts.
- Signing is Ed25519 (see ed25519.py; the prod path requires the
  `cryptography` library, enforced by the sidecar).
- Credentials are short-lived: validity window at most 24h, enforced at
  issuance AND at verification, so a credential minted elsewhere with a
  longer life never verifies.
- Fail closed, always, for consequential intent classes (PAYMENT, TRADE,
  AUTHORIZATION, SYSTEM_MODIFICATION, DATA_EXPORT): any verification
  failure, expired credential, broken chain, out-of-scope intent, stale or
  missing revocation data means BLOCK.

Assurance levels (copied from TLS certificates, because they work):
- L1 self-asserted: proves key control only. The credential is signed by a
  key the principal controls (issuer == principal, issuer key == principal
  root key). NEVER accepted for consequential actions.
- L2 domain-verified: the principal's root key must match the key published
  at the principal domain's well-known endpoint (pre-fetched keys supplied
  as a dict, so verification stays offline). Proves domain control.
- L3 legal-person verified: an independent issuer (issuer != principal,
  issuer key != principal key) attests the principal against a corporate
  registry, bank, or identity provider, and the issuer must be in the
  verifier's trust store (trusted_issuers dict). ONLY L3 satisfies "tied to
  a person" for consequential actions. The issuer's name and verification
  method are IN the credential, so a verifier judges the issuer.

Presentation: on every consequential request the agent sends the credential
plus a fresh signature over (challenge + payload_hash + timestamp) in the
X-CF-KYA HTTP header, alongside the firewall governance token. The token
proves the action passed the gates; the credential proves whose action it
is. Neither substitutes for the other. Payload binding lives in the
presentation signature: a presentation made for one payload_hash never
verifies for another payload. Credentials themselves are identity, valid
for up to 24h and reusable across payloads — the per-request binding is
the presentation, by design.

Challenge replay: a presentation signature is replayable within its
freshness window unless the server tracks challenges. Callers may pass a
mutable `seen_challenges` set to verify_presentation; a challenge recorded
there after a successful verification is rejected on reuse. Servers in
production MUST use an atomic, persistent challenge store — the in-memory
set here is the interface, not a production store (see threat model).

Interop: CF-KYA is issuer-neutral and self-hostable. It maps onto x401-style
identity presentation and sits alongside x402 payment; it does NOT replace
Visa's Trusted Agent Protocol, Mastercard's Verifiable Intent, or
SailPoint's enterprise agent identity. Those are the network and enterprise
versions of the same idea; CF-KYA is the open protocol a small principal
can adopt without joining a network or buying an enterprise suite.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import ed25519
from delegation import chain_hash, verify_chain_dicts
from delegation import TERMS_MISMATCH as _DELEGATION_TERMS_MISMATCH
from intent_classification import classify_intent
from principal import Principal

KYA_VERSION = "cf-kya-0.1"
HEADER_NAME = "X-CF-KYA"

CONSEQUENTIAL_INTENT_CLASSES = {
    "PAYMENT",
    "TRADE",
    "AUTHORIZATION",
    "SYSTEM_MODIFICATION",
    "DATA_EXPORT",
}

ASSURANCE_LEVELS = {"L1", "L2", "L3"}
PRINCIPAL_TYPES = {"human", "corporation"}
L3_VERIFICATION_METHODS = {
    "corporate_registry_record",
    "bank_attestation",
    "id_provider_attestation",
    # v0.2: Bedrock workload identity as an L3 issuer method (enterprise
    # attestation method, not cross-boundary legal-person binding by
    # itself — see docs/interop_decision.md).
    "bedrock_workload_attestation",
}

MAX_CREDENTIAL_VALIDITY_SECONDS = 24 * 3600
DEFAULT_PRESENTATION_MAX_AGE_SECONDS = 300
DEFAULT_REVOCATION_MAX_AGE_SECONDS = 3600
CLOCK_SKEW_SECONDS = 60

# Stable verification outcomes. Interceptor BLOCK reasons, benchmark outputs,
# and sidecar outputs use these strings verbatim.
OK = "OK"
INVALID_CREDENTIAL = "KYA_INVALID_CREDENTIAL"
UNKNOWN_VERSION = "KYA_UNKNOWN_VERSION"
CREDENTIAL_EXPIRED = "KYA_CREDENTIAL_EXPIRED"
CREDENTIAL_NOT_YET_VALID = "KYA_CREDENTIAL_NOT_YET_VALID"
CREDENTIAL_VALIDITY_TOO_LONG = "KYA_CREDENTIAL_VALIDITY_TOO_LONG"
CREDENTIAL_SIGNATURE_INVALID = "KYA_CREDENTIAL_SIGNATURE_INVALID"
L1_NOT_SELF_SIGNED = "KYA_L1_NOT_SELF_SIGNED"
L2_DOMAIN_MISMATCH = "KYA_L2_DOMAIN_MISMATCH"
L3_ISSUER_UNTRUSTED = "KYA_L3_ISSUER_UNTRUSTED"
L3_NOT_INDEPENDENT = "KYA_L3_NOT_INDEPENDENT"
L3_METHOD_INVALID = "KYA_L3_METHOD_INVALID"
CHAIN_PRINCIPAL_MISMATCH = "KYA_CHAIN_PRINCIPAL_MISMATCH"
AGENT_MISMATCH = "KYA_AGENT_MISMATCH"
CREDENTIAL_SCOPE_EXCEEDS_DELEGATION = "KYA_CREDENTIAL_SCOPE_EXCEEDS_DELEGATION"
SCOPE_EXCEEDED = "KYA_SCOPE_EXCEEDED"
REVOCATION_LIST_MISSING = "KYA_REVOCATION_LIST_MISSING"
REVOCATION_LIST_INVALID = "KYA_REVOCATION_LIST_INVALID"
REVOCATION_LIST_STALE = "KYA_REVOCATION_LIST_STALE"
CREDENTIAL_REVOKED = "KYA_CREDENTIAL_REVOKED"
PRESENTATION_MISSING = "KYA_PRESENTATION_MISSING"
PRESENTATION_INVALID = "KYA_PRESENTATION_INVALID"
PRESENTATION_SIGNATURE_INVALID = "KYA_PRESENTATION_SIGNATURE_INVALID"
PRESENTATION_STALE = "KYA_PRESENTATION_STALE"
PRESENTATION_NOT_YET_VALID = "KYA_PRESENTATION_NOT_YET_VALID"
CHALLENGE_MISMATCH = "KYA_CHALLENGE_MISMATCH"
CHALLENGE_REUSED = "KYA_CHALLENGE_REUSED"
PAYLOAD_MISMATCH = "KYA_PAYLOAD_MISMATCH"
L1_REJECTED_CONSEQUENTIAL = "KYA_L1_REJECTED_CONSEQUENTIAL"
L2_INSUFFICIENT_CONSEQUENTIAL = "KYA_L2_INSUFFICIENT_CONSEQUENTIAL"
TERMS_REQUIRED = "KYA_TERMS_HASH_REQUIRED"


def _canonical_bytes(data: dict[str, Any]) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _parse_time(ts: str) -> datetime:
    parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone information")
    return parsed


def _is_hex(value: Any, length: int) -> bool:
    if not isinstance(value, str) or len(value) != length:
        return False
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# Credential issuance / signature
# ---------------------------------------------------------------------------


def credential_unsigned_dict(credential: dict[str, Any]) -> dict[str, Any]:
    """All credential fields except the signature — the signed content."""
    return {k: v for k, v in credential.items() if k != "signature_hex"}


def issue_credential(
    *,
    credential_id: str,
    principal_id: str,
    principal_type: str,
    display_name: str,
    domain: str,
    agent_id: str,
    agent_public_key_hex: str,
    intent_classes: list[str],
    tools: list[str],
    max_allocation_pct: float,
    spend_cap_usd: float | None,
    issuer_id: str,
    issuer_public_key_hex: str,
    issuer_seed: bytes,
    assurance_level: str,
    verification_method: str,
    issued_at: str,
    expires_at: str | None = None,
    delegation_chain: list[dict[str, Any]],
    terms_hash: str = "",
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Create and sign a CF-KYA credential. The issuer holds the seed; the
    seed is never stored on the credential. Enforced at issuance (and again
    at verification, so a credential minted elsewhere with a longer life or
    a self-signed L3 never verifies):
    - validity window at most 24h (default exactly 24h);
    - L1 must be self-signed: issuer == principal and issuer key == the
      delegation chain's root (principal) key;
    - L3 must be independent: issuer != principal and issuer key != root key.
    """
    if principal_type not in PRINCIPAL_TYPES:
        raise ValueError("principal_type must be human or corporation")
    if assurance_level not in ASSURANCE_LEVELS:
        raise ValueError("assurance_level must be L1, L2, or L3")
    if not credential_id.strip() or not principal_id.strip() or not agent_id.strip() or not issuer_id.strip():
        raise ValueError("credential_id, principal_id, agent_id and issuer_id must be non-empty")
    if not _is_hex(agent_public_key_hex, 64) or not _is_hex(issuer_public_key_hex, 64):
        raise ValueError("agent and issuer public keys must be 64 hex characters (Ed25519)")
    if not (0.0 <= float(max_allocation_pct) <= 100.0):
        raise ValueError("max_allocation_pct must be in [0, 100]")
    if spend_cap_usd is not None and float(spend_cap_usd) < 0:
        raise ValueError("spend_cap_usd must be >= 0 when set")
    if not delegation_chain:
        raise ValueError("delegation_chain must be non-empty")
    root = delegation_chain[0]
    root_key = str(root.get("delegator_public_key_hex", "")).lower()
    if not _is_hex(root_key, 64):
        raise ValueError("delegation chain root must carry a valid delegator public key")
    if assurance_level == "L1":
        if issuer_id != principal_id or issuer_public_key_hex.lower() != root_key:
            raise ValueError("L1 credentials must be self-signed (issuer == principal, issuer key == principal key)")
    if assurance_level == "L3":
        if issuer_id == principal_id or issuer_public_key_hex.lower() == root_key:
            raise ValueError("L3 credentials must come from an independent issuer")
    if terms_hash and not _is_hex(terms_hash, 64):
        raise ValueError("terms_hash must be 64 hex characters (SHA-256) when set")
    issued = _parse_time(issued_at)
    expires = _parse_time(expires_at) if expires_at is not None else issued + timedelta(seconds=MAX_CREDENTIAL_VALIDITY_SECONDS)
    if expires < issued:
        raise ValueError("expires_at must be >= issued_at")
    if (expires - issued).total_seconds() > MAX_CREDENTIAL_VALIDITY_SECONDS:
        raise ValueError("credential validity window must be at most 24h")
    unsigned: dict[str, Any] = {
        "version": KYA_VERSION,
        "credential_id": credential_id,
        "principal": {
            "id": principal_id,
            "type": principal_type,
            "display_name": display_name,
            "domain": domain,
        },
        "agent": {"agent_id": agent_id, "public_key_hex": agent_public_key_hex.lower()},
        "scope": {
            "intent_classes": list(intent_classes),
            "tools": list(tools),
            "max_allocation_pct": float(max_allocation_pct),
            "spend_cap_usd": (float(spend_cap_usd) if spend_cap_usd is not None else None),
        },
        "issuer": {
            "id": issuer_id,
            "public_key_hex": issuer_public_key_hex.lower(),
            "assurance_level": assurance_level,
            "verification_method": verification_method,
        },
        "issued_at": issued_at,
        "expires_at": expires.isoformat(),
        "delegation_chain": delegation_chain,
        "terms_hash": terms_hash,
    }
    if tenant_id is not None:
        # v0.2 multi-tenancy: the tenant is part of the signed content,
        # so a credential cannot be transplanted across tenants. Absent
        # means unscoped (v0.1 behavior); strict verifiers operating
        # multi-tenant REQUIRE it (see strict.py).
        if not tenant_id.strip():
            raise ValueError("tenant_id must be non-empty when set")
        unsigned["tenant_id"] = tenant_id
    signature = ed25519.sign(
        _canonical_bytes(unsigned), issuer_seed, bytes.fromhex(issuer_public_key_hex)
    ).hex()
    return {**unsigned, "signature_hex": signature}


def verify_credential_signature(credential: dict[str, Any]) -> bool:
    try:
        issuer_key = str(credential["issuer"]["public_key_hex"])
        signature = str(credential["signature_hex"])
    except (KeyError, TypeError):
        return False
    return ed25519.verify_hex(signature, _canonical_bytes(credential_unsigned_dict(credential)), issuer_key)


# ---------------------------------------------------------------------------
# Revocation lists (signed, timestamped, published by the issuer)
# ---------------------------------------------------------------------------


def issue_revocation_list(
    *,
    issuer_id: str,
    issuer_seed: bytes,
    issuer_public_key_hex: str,
    revoked_credential_ids: list[str],
    revoked_delegation_ids: list[str],
    issued_at: str,
) -> dict[str, Any]:
    """Publish a signed revocation list. Short credential life bounds the
    damage of a missed entry, but a missed or stale list never verifies —
    see verify_credential, which rejects when the list is absent or stale
    for the credential's issuer."""
    unsigned: dict[str, Any] = {
        "issuer_id": issuer_id,
        "issued_at": issued_at,
        "revoked_credential_ids": sorted(revoked_credential_ids),
        "revoked_delegation_ids": sorted(revoked_delegation_ids),
    }
    signature = ed25519.sign(
        _canonical_bytes(unsigned), issuer_seed, bytes.fromhex(issuer_public_key_hex)
    ).hex()
    return {**unsigned, "signature_hex": signature}


def verify_revocation_list(
    revocation_list: dict[str, Any],
    *,
    issuer_public_key_hex: str,
    issuer_id: str | None = None,
    max_age_seconds: int = DEFAULT_REVOCATION_MAX_AGE_SECONDS,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Verify a signed revocation list: shape, issuer match, signature, and
    freshness. A list older than max_age_seconds is STALE, not usable —
    fail closed for consequential actions."""
    current = now or datetime.now(timezone.utc)
    if not isinstance(revocation_list, dict):
        return False, REVOCATION_LIST_INVALID
    try:
        list_issuer = str(revocation_list["issuer_id"])
        issued_at = str(revocation_list["issued_at"])
        revoked_creds = revocation_list["revoked_credential_ids"]
        revoked_dels = revocation_list["revoked_delegation_ids"]
        signature = str(revocation_list["signature_hex"])
    except (KeyError, TypeError):
        return False, REVOCATION_LIST_INVALID
    if issuer_id is not None and list_issuer != issuer_id:
        return False, REVOCATION_LIST_INVALID
    if not isinstance(revoked_creds, list) or not isinstance(revoked_dels, list):
        return False, REVOCATION_LIST_INVALID
    unsigned = {k: v for k, v in revocation_list.items() if k != "signature_hex"}
    if not ed25519.verify_hex(signature, _canonical_bytes(unsigned), issuer_public_key_hex):
        return False, REVOCATION_LIST_INVALID
    try:
        issued = _parse_time(issued_at)
    except ValueError:
        return False, REVOCATION_LIST_INVALID
    if issued > current + timedelta(seconds=CLOCK_SKEW_SECONDS):
        return False, REVOCATION_LIST_INVALID
    if (current - issued).total_seconds() > max_age_seconds:
        return False, REVOCATION_LIST_STALE
    return True, OK


# ---------------------------------------------------------------------------
# Credential verification (offline: keys and lists supplied as dicts)
# ---------------------------------------------------------------------------


def _scope_subset(cred_scope: dict[str, Any], leaf: Any) -> bool:
    """True when the credential scope fits inside the delegation leaf scope.
    '*' on either side means all; a credential '*' requires a leaf '*'."""
    cred_classes = cred_scope.get("intent_classes", [])
    leaf_classes = list(leaf.intent_classes)
    if "*" in cred_classes:
        if "*" not in leaf_classes:
            return False
    elif "*" not in leaf_classes and not set(cred_classes) <= set(leaf_classes):
        return False
    cred_tools = cred_scope.get("tools", [])
    leaf_tools = list(leaf.tools)
    if "*" in cred_tools:
        if "*" not in leaf_tools:
            return False
    elif "*" not in leaf_tools and not set(cred_tools) <= set(leaf_tools):
        return False
    if float(cred_scope.get("max_allocation_pct", 0.0)) > float(leaf.max_allocation_pct):
        return False
    return True


def _scope_covers_action(
    scope: dict[str, Any],
    *,
    intent_class: str,
    tool_name: str,
    allocation_pct: float | None,
    spend_amount_usd: float | None,
) -> bool:
    classes = scope.get("intent_classes", [])
    if "*" not in classes and intent_class not in classes:
        return False
    tools = scope.get("tools", [])
    if "*" not in tools and tool_name not in tools:
        return False
    if allocation_pct is not None and float(allocation_pct) > float(scope.get("max_allocation_pct", 0.0)):
        return False
    cap = scope.get("spend_cap_usd")
    if cap is not None and spend_amount_usd is not None and float(spend_amount_usd) > float(cap):
        return False
    return True


def verify_credential(
    credential: dict[str, Any],
    *,
    intent_class: str,
    tool_name: str,
    agent_id: str,
    allocation_pct: float | None = None,
    spend_amount_usd: float | None = None,
    agent_public_keys: dict[str, str] | None = None,
    trusted_issuers: dict[str, str] | None = None,
    domain_keys: dict[str, str] | None = None,
    revocation_list: dict[str, Any] | None = None,
    revoked_delegations: list[str] | None = None,
    revoked_principals: list[str] | None = None,
    max_revocation_age_seconds: int = DEFAULT_REVOCATION_MAX_AGE_SECONDS,
    require_terms_hash: bool = False,
    now: datetime | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Verify a CF-KYA credential for one action. Offline: every key and
    list is supplied by the caller (trusted_issuers, domain_keys,
    revocation_list); this function performs no network I/O.

    Returns (ok, reason, info). On success info carries principal_id,
    credential_id, agent_id, leaf_delegation_id, delegation_chain_hash,
    assurance_level, and issuer_id. Any failure returns an empty info dict.
    """
    current = now or datetime.now(timezone.utc)
    empty: dict[str, Any] = {}
    if not isinstance(credential, dict):
        return False, INVALID_CREDENTIAL, empty
    if credential.get("version") != KYA_VERSION:
        return False, UNKNOWN_VERSION, empty
    try:
        credential_id = str(credential["credential_id"])
        principal = credential["principal"]
        principal_id = str(principal["id"])
        principal_type = str(principal["type"])
        principal_domain = str(principal.get("domain", ""))
        agent = credential["agent"]
        cred_agent_id = str(agent["agent_id"])
        cred_agent_key = str(agent["public_key_hex"]).lower()
        scope = credential["scope"]
        issuer = credential["issuer"]
        issuer_id = str(issuer["id"])
        issuer_key = str(issuer["public_key_hex"]).lower()
        assurance = str(issuer["assurance_level"])
        method = str(issuer["verification_method"])
        issued_at = str(credential["issued_at"])
        expires_at = str(credential["expires_at"])
        chain = credential["delegation_chain"]
    except (KeyError, TypeError):
        return False, INVALID_CREDENTIAL, empty
    if principal_type not in PRINCIPAL_TYPES or assurance not in ASSURANCE_LEVELS:
        return False, INVALID_CREDENTIAL, empty
    if not credential_id.strip() or not principal_id.strip() or not cred_agent_id.strip():
        return False, INVALID_CREDENTIAL, empty
    if not _is_hex(cred_agent_key, 64) or not _is_hex(issuer_key, 64):
        return False, INVALID_CREDENTIAL, empty
    if not isinstance(chain, list) or not chain:
        return False, INVALID_CREDENTIAL, empty
    try:
        root_key = str(chain[0]["delegator_public_key_hex"]).lower()
        root_principal = str(chain[0]["principal_id"])
    except (KeyError, TypeError):
        return False, INVALID_CREDENTIAL, empty
    if not _is_hex(root_key, 64):
        return False, INVALID_CREDENTIAL, empty
    if root_principal != principal_id:
        return False, CHAIN_PRINCIPAL_MISMATCH, empty
    # Time window: structural (too-long) defects reported before expiry so a
    # long-lived credential is never mistaken for merely expired.
    try:
        issued = _parse_time(issued_at)
        expires = _parse_time(expires_at)
    except ValueError:
        return False, INVALID_CREDENTIAL, empty
    if (expires - issued).total_seconds() > MAX_CREDENTIAL_VALIDITY_SECONDS:
        return False, CREDENTIAL_VALIDITY_TOO_LONG, empty
    if current < issued:
        return False, CREDENTIAL_NOT_YET_VALID, empty
    if current > expires:
        return False, CREDENTIAL_EXPIRED, empty
    if not verify_credential_signature(credential):
        return False, CREDENTIAL_SIGNATURE_INVALID, empty
    # Assurance internal consistency (consequential suitability — L3 only —
    # is enforced by verify_presentation / gate_check, not here).
    if assurance == "L1":
        if issuer_id != principal_id or issuer_key != root_key:
            return False, L1_NOT_SELF_SIGNED, empty
    elif assurance == "L2":
        domain_keys = domain_keys or {}
        if not principal_domain or domain_keys.get(principal_domain) != root_key:
            return False, L2_DOMAIN_MISMATCH, empty
    elif assurance == "L3":
        if issuer_id == principal_id or issuer_key == root_key:
            return False, L3_NOT_INDEPENDENT, empty
        trusted_issuers = trusted_issuers or {}
        if trusted_issuers.get(issuer_id) != issuer_key:
            return False, L3_ISSUER_UNTRUSTED, empty
        if method not in L3_VERIFICATION_METHODS:
            return False, L3_METHOD_INVALID, empty
    # Revocation: a fresh signed list from this credential's issuer is
    # REQUIRED. Missing or stale revocation data means reject — the one
    # non-offline step is the caller refreshing this list.
    if revocation_list is None:
        return False, REVOCATION_LIST_MISSING, empty
    list_ok, list_reason = verify_revocation_list(
        revocation_list,
        issuer_public_key_hex=issuer_key,
        issuer_id=issuer_id,
        max_age_seconds=max_revocation_age_seconds,
        now=current,
    )
    if not list_ok:
        return False, list_reason, empty
    if credential_id in list(revocation_list.get("revoked_credential_ids") or []):
        return False, CREDENTIAL_REVOKED, empty
    # Delegation chain, verified link by link, with the credential's own
    # principal as the chain root (evidence reference: the credential id).
    try:
        principal_obj = Principal(
            principal_id=principal_id,
            principal_type=principal_type,
            display_name=str(principal.get("display_name", "")),
            verification_method=method,
            verification_issuer=issuer_id,
            verified_at=issued_at,
            evidence_ref=f"kya-credential:{credential_id}",
            public_key_hex=root_key,
        )
    except (TypeError, ValueError):
        return False, INVALID_CREDENTIAL, empty
    merged_revoked = list(revocation_list.get("revoked_delegation_ids") or []) + list(revoked_delegations or [])
    chain_ok, chain_reason, leaf = verify_chain_dicts(
        chain,
        principals={principal_id: principal_obj},
        agent_public_keys=agent_public_keys or {},
        intent_class=intent_class,
        tool_name=tool_name,
        agent_id=agent_id,
        allocation_pct=allocation_pct,
        revoked_delegations=merged_revoked,
        revoked_principals=revoked_principals or [],
        now=current,
    )
    if not chain_ok or leaf is None:
        return False, chain_reason, empty
    if leaf.delegatee_id != cred_agent_id:
        return False, AGENT_MISMATCH, empty
    # Contractual layer: the credential's terms hash must name the same
    # agreement as the chain (chain verification already forces all links
    # to agree). A counterparty that requires contractual binding passes
    # require_terms_hash=True and rejects agreement-less credentials.
    cred_terms = str(credential.get("terms_hash", ""))
    if cred_terms != str(chain[0].get("terms_hash", "")):
        return False, _DELEGATION_TERMS_MISMATCH, empty
    if require_terms_hash and not cred_terms:
        return False, TERMS_REQUIRED, empty
    if not _scope_subset(scope, leaf):
        return False, CREDENTIAL_SCOPE_EXCEEDS_DELEGATION, empty
    if not _scope_covers_action(
        scope,
        intent_class=intent_class,
        tool_name=tool_name,
        allocation_pct=allocation_pct,
        spend_amount_usd=spend_amount_usd,
    ):
        return False, SCOPE_EXCEEDED, empty
    info = {
        "principal_id": principal_id,
        "credential_id": credential_id,
        "agent_id": cred_agent_id,
        "leaf_delegation_id": leaf.delegation_id,
        "delegation_chain_hash": chain_hash(chain),
        "assurance_level": assurance,
        "issuer_id": issuer_id,
    }
    return True, OK, info


# ---------------------------------------------------------------------------
# Presentation: credential + fresh signature over (challenge + payload + time)
# ---------------------------------------------------------------------------


def create_presentation(
    credential: dict[str, Any],
    *,
    agent_seed: bytes,
    challenge: str,
    payload_hash: str,
    timestamp: str,
) -> dict[str, Any]:
    """Build a presentation for one request. The agent signs (challenge +
    payload_hash + timestamp) with the key named in the credential; a seed
    that does not match the credential's agent key raises loudly instead of
    producing a presentation that can never verify."""
    if not challenge or not payload_hash or not timestamp:
        raise ValueError("challenge, payload_hash and timestamp must be non-empty")
    try:
        agent_key = bytes.fromhex(str(credential["agent"]["public_key_hex"]))
    except (KeyError, TypeError):
        raise ValueError("credential has no agent public key")
    # Validate the seed against the credential's agent key HERE, not
    # inside ed25519.sign: the cryptography backend validates, the
    # pure-python fallback does not, and presentation creation must fail
    # loudly on both backends (a wrong seed must never produce a
    # presentation that can only fail later at verification).
    if ed25519.public_key_from_seed(agent_seed).hex() != agent_key.hex():
        raise ValueError("agent_seed does not match the credential's agent key")
    signed_content = {"challenge": challenge, "payload_hash": payload_hash, "timestamp": timestamp}
    signature = ed25519.sign(_canonical_bytes(signed_content), agent_seed, agent_key).hex()
    return {
        "credential": credential,
        "challenge": challenge,
        "payload_hash": payload_hash,
        "timestamp": timestamp,
        "presentation_signature_hex": signature,
    }


def verify_presentation(
    presentation: dict[str, Any],
    *,
    expected_payload_hash: str,
    expected_challenge: str | None = None,
    intent_class: str,
    tool_name: str,
    agent_id: str,
    allocation_pct: float | None = None,
    spend_amount_usd: float | None = None,
    agent_public_keys: dict[str, str] | None = None,
    trusted_issuers: dict[str, str] | None = None,
    domain_keys: dict[str, str] | None = None,
    revocation_list: dict[str, Any] | None = None,
    revoked_delegations: list[str] | None = None,
    revoked_principals: list[str] | None = None,
    require_l3: bool = False,
    presentation_max_age_seconds: int = DEFAULT_PRESENTATION_MAX_AGE_SECONDS,
    seen_challenges: set[str] | None = None,
    max_revocation_age_seconds: int = DEFAULT_REVOCATION_MAX_AGE_SECONDS,
    require_terms_hash: bool = False,
    now: datetime | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Verify a presentation for one action. Checks, in order: shape,
    timestamp freshness, payload binding, challenge match and reuse,
    presentation signature (agent key control), then the full credential
    (issuer signature, assurance, revocation freshness, chain, scope).
    With require_l3=True (consequential actions), L1 and L2 are rejected
    even when otherwise valid. The challenge is recorded in seen_challenges
    only on full success, so a failed attempt never burns a challenge."""
    current = now or datetime.now(timezone.utc)
    empty: dict[str, Any] = {}
    if not isinstance(presentation, dict):
        return False, PRESENTATION_INVALID, empty
    try:
        credential = presentation["credential"]
        challenge = str(presentation["challenge"])
        payload_hash = str(presentation["payload_hash"])
        timestamp = str(presentation["timestamp"])
        pres_signature = str(presentation["presentation_signature_hex"])
    except (KeyError, TypeError):
        return False, PRESENTATION_INVALID, empty
    if not challenge or not payload_hash or not timestamp or not pres_signature:
        return False, PRESENTATION_INVALID, empty
    try:
        ts = _parse_time(timestamp)
    except ValueError:
        return False, PRESENTATION_INVALID, empty
    if ts > current + timedelta(seconds=CLOCK_SKEW_SECONDS):
        return False, PRESENTATION_NOT_YET_VALID, empty
    if (current - ts).total_seconds() > presentation_max_age_seconds:
        return False, PRESENTATION_STALE, empty
    if payload_hash != expected_payload_hash:
        return False, PAYLOAD_MISMATCH, empty
    if expected_challenge is not None and challenge != expected_challenge:
        return False, CHALLENGE_MISMATCH, empty
    if seen_challenges is not None and challenge in seen_challenges:
        return False, CHALLENGE_REUSED, empty
    try:
        agent_key_hex = str(credential["agent"]["public_key_hex"])
    except (KeyError, TypeError):
        return False, PRESENTATION_INVALID, empty
    signed_content = {"challenge": challenge, "payload_hash": payload_hash, "timestamp": timestamp}
    if not ed25519.verify_hex(pres_signature, _canonical_bytes(signed_content), agent_key_hex):
        return False, PRESENTATION_SIGNATURE_INVALID, empty
    cred_ok, cred_reason, info = verify_credential(
        credential,
        intent_class=intent_class,
        tool_name=tool_name,
        agent_id=agent_id,
        allocation_pct=allocation_pct,
        spend_amount_usd=spend_amount_usd,
        agent_public_keys=agent_public_keys,
        trusted_issuers=trusted_issuers,
        domain_keys=domain_keys,
        revocation_list=revocation_list,
        revoked_delegations=revoked_delegations,
        revoked_principals=revoked_principals,
        max_revocation_age_seconds=max_revocation_age_seconds,
        require_terms_hash=require_terms_hash,
        now=current,
    )
    if not cred_ok:
        return False, cred_reason, empty
    if require_l3:
        if info["assurance_level"] == "L1":
            return False, L1_REJECTED_CONSEQUENTIAL, empty
        if info["assurance_level"] == "L2":
            return False, L2_INSUFFICIENT_CONSEQUENTIAL, empty
    if seen_challenges is not None:
        seen_challenges.add(challenge)
    return True, OK, info


# ---------------------------------------------------------------------------
# HTTP header transport
# ---------------------------------------------------------------------------


def presentation_to_header(presentation: dict[str, Any]) -> str:
    """Encode a presentation as the X-CF-KYA header value: base64url of the
    canonical JSON. The header carries the credential, so no separate
    credential fetch is needed to verify the request."""
    raw = _canonical_bytes(presentation)
    return base64.urlsafe_b64encode(raw).decode("ascii")


def presentation_from_header(value: str) -> dict[str, Any]:
    """Decode an X-CF-KYA header value. Raises ValueError on bad encoding
    or non-object JSON; shape problems are left to verify_presentation."""
    try:
        raw = base64.urlsafe_b64decode(value.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("X-CF-KYA header is not valid base64url JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("X-CF-KYA header must decode to a JSON object")
    return data


# ---------------------------------------------------------------------------
# Gate integration (interceptor + sidecar share this, so the rule is one)
# ---------------------------------------------------------------------------


def gate_check(
    actor_context: dict[str, Any],
    *,
    intent_name: str,
    tool_name: str,
    payload_hash: str,
    allocation_pct: float | None = None,
    spend_amount_usd: float | None = None,
    now: datetime | None = None,
) -> tuple[str, bool, str, dict[str, Any]]:
    """CF-KYA gate for one action. Returns (mode, ok, reason, info).

    - Presentation present: it is ALWAYS verified (a presented credential
      that fails verification blocks, for any intent class). Consequential
      intents additionally require L3.
    - Consequential intent without a presentation: BLOCK
      (KYA_PRESENTATION_MISSING) — a delegation alone is not enough for
      consequential actions under CF-KYA.
    - Non-consequential intent without a presentation: mode "defer" — the
      caller runs the pre-KYA delegation check (check_bind) instead. This
      keeps DATA_ACCESS-style actions working on delegation alone while
      consequential actions move to credentials.
    """
    intent_class = classify_intent(tool_name, intent_name)
    presentation = actor_context.get("kya_presentation")
    if presentation is not None:
        ok, reason, info = verify_presentation(
            presentation,
            expected_payload_hash=payload_hash,
            expected_challenge=actor_context.get("kya_expected_challenge"),
            intent_class=intent_class,
            tool_name=tool_name,
            agent_id=str(actor_context.get("agent_id", "")),
            allocation_pct=allocation_pct,
            spend_amount_usd=spend_amount_usd,
            agent_public_keys=actor_context.get("agent_public_keys") or {},
            trusted_issuers=actor_context.get("kya_trusted_issuers") or {},
            domain_keys=actor_context.get("kya_domain_keys"),
            revocation_list=actor_context.get("kya_revocation_list"),
            revoked_delegations=actor_context.get("revoked_delegations") or [],
            revoked_principals=actor_context.get("revoked_principals") or [],
            require_l3=intent_class in CONSEQUENTIAL_INTENT_CLASSES,
            seen_challenges=actor_context.get("kya_seen_challenges"),
            require_terms_hash=bool(actor_context.get("kya_require_terms_hash", False)),
            now=now,
        )
        return "kya", ok, reason, info
    if intent_class in CONSEQUENTIAL_INTENT_CLASSES:
        return "kya", False, PRESENTATION_MISSING, {}
    return "defer", True, OK, {}
