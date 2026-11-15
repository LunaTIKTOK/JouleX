"""M1 — attestation envelope tests (test-first).

Covers DESIGN.md §7.1/§7.2: envelope mint/verify, REPORT_DATA binding,
expiry, unknown-kind rejection, policy required/advisory paths,
measurement computation + pinned-set rejection, evidence checks.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from joulex.attestation import (
    ATTEST_VERSION,
    MAX_ENVELOPE_TTL_SECONDS,
    binding_hash,
    canonical_bytes,
    compute_measurement,
    default_policy,
    load_pinned_measurements,
    mint_envelope,
    verify_envelope,
    verify_with_policy,
)

T0 = datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc)


def _env(**overrides):
    env = mint_envelope(
        credential_id="cred-1",
        challenge_nonce="nonce-abc",
        payload_hash="a" * 64,
        now=T0,
    )
    env.update(overrides)
    return env


def _ctx(env):
    return dict(
        credential_id="cred-1",
        challenge_nonce="nonce-abc",
        payload_hash="a" * 64,
    )


def test_mint_verify_roundtrip():
    env = _env()
    assert env["version"] == ATTEST_VERSION
    assert env["kind"] == "software"
    assert len(env["measurement"]) == 64
    assert env["report_data"] == binding_hash("cred-1", "nonce-abc", "a" * 64)
    ok, reason, info = verify_envelope(env, **_ctx(env), now=T0)
    assert ok, reason
    assert reason == "OK"
    assert info["verified"] is True
    assert info["measurement"] == env["measurement"]


def test_binding_mismatch_rejected():
    env = _env()
    # Same envelope, different live payload hash: a replay stapled to
    # another action must fail on the binding.
    ok, reason, _ = verify_envelope(
        env, credential_id="cred-1", challenge_nonce="nonce-abc",
        payload_hash="b" * 64, now=T0,
    )
    assert not ok
    assert reason == "JOULEX_VC_BINDING"
    # Same envelope, different challenge nonce: also a binding failure.
    ok, reason, _ = verify_envelope(
        env, credential_id="cred-1", challenge_nonce="nonce-other",
        payload_hash="a" * 64, now=T0,
    )
    assert not ok
    assert reason == "JOULEX_VC_BINDING"


def test_binding_hash_is_jcs_canonical():
    expected = hashlib.sha256(
        json.dumps(
            {"credential_id": "cred-1", "challenge_nonce": "n", "payload_hash": "p"},
            sort_keys=True, separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert binding_hash("cred-1", "n", "p") == expected


def test_expired_envelope_rejected():
    env = _env()
    ok, reason, _ = verify_envelope(
        env, **_ctx(env), now=T0 + timedelta(seconds=301)
    )
    assert not ok
    assert reason == "JOULEX_VC_EXPIRED"


def test_envelope_ttl_bound_enforced_at_mint():
    with pytest.raises(ValueError):
        mint_envelope(
            credential_id="c", challenge_nonce="n", payload_hash="p",
            now=T0, ttl_seconds=MAX_ENVELOPE_TTL_SECONDS + 1,
        )
    env = mint_envelope(
        credential_id="c", challenge_nonce="n", payload_hash="p",
        now=T0, ttl_seconds=60,
    )
    ok, reason, _ = verify_envelope(
        env, credential_id="c", challenge_nonce="n", payload_hash="p",
        now=T0 + timedelta(seconds=61),
    )
    assert not ok and reason == "JOULEX_VC_EXPIRED"


def test_unknown_kind_rejected():
    # Reserved hardware kinds are NOT implemented by this verifier —
    # a verifier MUST reject a kind it does not implement.
    for kind in ("sev-snp", "tdx", "sgx-dcap", "nitro", "zkvm-risc0", "zkvm-sp1", "bogus"):
        env = _env(kind=kind)
        ok, reason, _ = verify_envelope(env, **_ctx(env), now=T0)
        assert not ok, kind
        assert reason == "JOULEX_VC_KIND", kind


def test_malformed_envelope_rejected():
    for bad in (None, "x", {}, {"version": "joulex-attest-9.9"}, {"version": ATTEST_VERSION}):
        ok, reason, _ = verify_envelope(bad, **_ctx({}), now=T0)
        assert not ok and reason == "JOULEX_VC_INVALID", bad


def test_policy_required_missing_envelope():
    policy = {"required": True, "kinds": ["software"], "measurements": [], "max_age_seconds": 300}
    ok, reason, _ = verify_with_policy(
        None, credential_id="c", challenge_nonce="n", payload_hash="p",
        policy=policy, now=T0,
    )
    assert not ok
    assert reason == "JOULEX_VC_REQUIRED"


def test_policy_required_valid_envelope_accepted():
    policy = {"required": True, "kinds": ["software"], "measurements": [], "max_age_seconds": 300}
    env = _env()
    ok, reason, _ = verify_with_policy(
        env, **_ctx(env), policy=policy, now=T0,
    )
    assert ok, reason


def test_policy_advisory_paths():
    policy = default_policy()
    assert policy["required"] is False
    # Absent -> plain verification passes.
    ok, reason, info = verify_with_policy(
        None, credential_id="c", challenge_nonce="n", payload_hash="p",
        policy=policy, now=T0,
    )
    assert ok and info["attestation"] == "absent"
    # Present + valid -> recorded.
    env = _env()
    ok, reason, info = verify_with_policy(env, **_ctx(env), policy=policy, now=T0)
    assert ok and info["attestation"] == "verified"
    # Present + invalid -> REJECTED, never ignored.
    ok, reason, _ = verify_with_policy(
        env, credential_id="c", challenge_nonce="n", payload_hash="f" * 64,
        policy=policy, now=T0,
    )
    assert not ok and reason == "JOULEX_VC_BINDING"


def test_policy_kind_not_allowed():
    policy = {"required": False, "kinds": ["sev-snp"], "measurements": [], "max_age_seconds": 300}
    env = _env()  # kind software
    ok, reason, _ = verify_with_policy(env, **_ctx(env), policy=policy, now=T0)
    assert not ok and reason == "JOULEX_VC_KIND"


def test_policy_max_age():
    policy = {"required": False, "kinds": ["software"], "measurements": [], "max_age_seconds": 60}
    env = _env()
    ok, reason, _ = verify_with_policy(
        env, **_ctx(env), policy=policy, now=T0 + timedelta(seconds=61),
    )
    assert not ok and reason == "JOULEX_VC_EXPIRED"


def test_measurement_pinned_set_rejection():
    measurement = compute_measurement()
    env = _env()
    assert env["measurement"] == measurement
    # Policy pins a different measurement: reject.
    policy = {
        "required": True, "kinds": ["software"],
        "measurements": ["0" * 64], "max_age_seconds": 300,
    }
    ok, reason, _ = verify_with_policy(env, **_ctx(env), policy=policy, now=T0)
    assert not ok and reason == "JOULEX_VC_MEASUREMENT"
    # Policy pins the real measurement: accept.
    policy["measurements"] = [measurement]
    ok, reason, _ = verify_with_policy(env, **_ctx(env), policy=policy, now=T0)
    assert ok, reason
    # Verifier-level pinned set (no policy measurements) also enforces.
    ok, reason, _ = verify_envelope(
        env, **_ctx(env), now=T0, pinned_measurements=["0" * 64],
    )
    assert not ok and reason == "JOULEX_VC_MEASUREMENT"


def test_compute_measurement_deterministic_and_sensitive(tmp_path):
    pkg = tmp_path / "joulex"
    pkg.mkdir()
    (pkg / "a.py").write_text("x = 1\n")
    (pkg / "b.py").write_text("y = 2\n")
    m1 = compute_measurement(package_dir=str(pkg))
    m2 = compute_measurement(package_dir=str(pkg))
    assert m1 == m2 and len(m1) == 64
    (pkg / "b.py").write_text("y = 3\n")
    assert compute_measurement(package_dir=str(pkg)) != m1
    (pkg / "c.py").write_text("z = 4\n")
    assert compute_measurement(package_dir=str(pkg)) != m1


def test_evidence_tamper_rejected():
    env = _env()
    env["evidence"] = base64.b64encode(b"not-json").decode()
    ok, reason, _ = verify_envelope(env, **_ctx(env), now=T0)
    assert not ok and reason == "JOULEX_VC_EVIDENCE"
    # Evidence naming a different measurement than the envelope's.
    other = {"measurement": "f" * 64, "link_sha256": None}
    env2 = _env(evidence=base64.b64encode(canonical_bytes(other)).decode())
    ok, reason, _ = verify_envelope(env2, **_ctx(env2), now=T0)
    assert not ok and reason == "JOULEX_VC_EVIDENCE"


def test_load_pinned_measurements_missing_is_empty(tmp_path):
    assert load_pinned_measurements(str(tmp_path)) == []
    (tmp_path / "measurements.json").write_text(
        json.dumps({"measurements": ["ab" * 32]})
    )
    assert load_pinned_measurements(str(tmp_path)) == ["ab" * 32]


def test_load_pinned_measurements_corrupt_fails_closed(tmp_path):
    (tmp_path / "measurements.json").write_text("{not json")
    with pytest.raises(RuntimeError):
        load_pinned_measurements(str(tmp_path))
