"""Admission tests: CF-KYA verification at order time.

Every rejection the design promises is tested here: no credential,
expired, revoked, wrong agent key, L1/L2 for TRADE, stale/missing
revocation list, replayed challenge, payload mismatch, out-of-scope
tool, unknown/expired challenge, stale presentation.
"""

from __future__ import annotations

import pytest

import kya as kya_module
from joulex.admission import payload_hash
from tests.conftest import T0, iso


def signed_presentation(bundle, challenge, payload, timestamp):
    """Agent-side: sign (challenge + payload_hash + timestamp)."""
    return kya_module.create_presentation(
        bundle["credential"],
        agent_seed=bundle["agent_seed"],
        challenge=challenge,
        payload_hash=payload_hash(payload),
        timestamp=timestamp,
    )


def verify_ok(verifier, bundle, challenge, payload, clock):
    pres = signed_presentation(bundle, challenge, payload, iso(clock()))
    return verifier.verify_order(pres, payload, agent_id=bundle["agent_id"])


ORDER = {"agent_id": "agent-alice-1", "market_id": "m1", "side": "YES", "shares": 10, "action": "buy"}


def test_valid_l3_presentation_verifies(verifier, agent_a, clock):
    challenge = verifier.issue_challenge()
    ok, reason, info = verify_ok(verifier, agent_a, challenge, ORDER, clock)
    assert ok, reason
    assert reason == "OK"
    assert info["assurance_level"] == "L3"
    assert info["agent_id"] == "agent-alice-1"
    assert info["principal_id"] == "principal-alice-test"


def test_no_presentation_rejected(verifier, agent_a):
    ok, reason, _ = verifier.verify_order(None, ORDER, agent_id="agent-alice-1")
    assert not ok
    assert reason == "KYA_PRESENTATION_MISSING"


def test_malformed_presentation_rejected(verifier):
    ok, reason, _ = verifier.verify_order({"nope": True}, ORDER, agent_id="agent-alice-1")
    assert not ok
    assert reason == "KYA_PRESENTATION_INVALID"


def test_expired_credential_rejected(issuer, verifier, clock):
    bundle = issuer.mint_agent(
        principal_id="principal-exp-test",
        display_name="Expired Test",
        agent_id="agent-exp-1",
        validity_hours=1.0,
    )
    clock.advance(hours=2)  # credential (1h) and delegation both expired
    # Fresh challenge at the new time, so the failure is the credential,
    # not the challenge.
    challenge = verifier.issue_challenge()
    pres = signed_presentation(bundle, challenge, ORDER, iso(clock()))
    ok, reason, _ = verifier.verify_order(pres, ORDER, agent_id="agent-exp-1")
    assert not ok
    assert reason == "KYA_CREDENTIAL_EXPIRED"


def test_revoked_credential_rejected(issuer, verifier, agent_a, clock):
    cred_id = agent_a["credential"]["credential_id"]
    ok, reason = verifier.refresh_revocation_list(
        issuer.revocation_list(revoked_credential_ids=[cred_id])
    )
    assert ok, reason
    challenge = verifier.issue_challenge()
    ok, reason, _ = verify_ok(verifier, agent_a, challenge, ORDER, clock)
    assert not ok
    assert reason == "KYA_CREDENTIAL_REVOKED"


def test_wrong_agent_key_rejected_at_presentation_build(agent_a, agent_b):
    # create_presentation must fail loudly when the seed does not match
    # the credential's agent key — never produce a presentation that can
    # only fail later at verification.
    with pytest.raises(ValueError, match="does not match"):
        kya_module.create_presentation(
            agent_a["credential"],
            agent_seed=agent_b["agent_seed"],
            challenge="x" * 32,
            payload_hash="0" * 64,
            timestamp=iso(T0),
        )


def test_tampered_presentation_signature_rejected(verifier, agent_a, clock):
    challenge = verifier.issue_challenge()
    pres = signed_presentation(agent_a, challenge, ORDER, iso(clock()))
    sig = pres["presentation_signature_hex"]
    pres["presentation_signature_hex"] = ("0" if sig[0] != "0" else "1") + sig[1:]
    ok, reason, _ = verifier.verify_order(pres, ORDER, agent_id="agent-alice-1")
    assert not ok
    assert reason == "KYA_PRESENTATION_SIGNATURE_INVALID"


def test_l1_credential_rejected_for_trade(issuer, verifier, clock):
    bundle = issuer.mint_agent(
        principal_id="principal-l1-test",
        display_name="L1 Test",
        agent_id="agent-l1-1",
        assurance_level="L1",
    )
    # The L1 issuer is the principal itself, so its revocation list is
    # signed by the principal key, not the demo issuer key.
    ok, reason = verifier.refresh_revocation_list(
        issuer.revocation_list_for(
            issuer_id=bundle["principal_id"],
            issuer_seed=bundle["principal_seed"],
            issuer_public_key_hex=bundle["principal_public_key_hex"],
        ),
        issuer_public_key_hex=bundle["principal_public_key_hex"],
    )
    assert ok, reason
    challenge = verifier.issue_challenge()
    payload = dict(ORDER, agent_id="agent-l1-1")
    ok, reason, _ = verify_ok(verifier, bundle, challenge, payload, clock)
    assert not ok
    assert reason == "KYA_L1_REJECTED_CONSEQUENTIAL"


def test_l2_credential_rejected_for_trade(issuer, clock):
    from joulex.admission import ExchangeVerifier

    bundle = issuer.mint_agent(
        principal_id="principal-l2-test",
        display_name="L2 Test",
        agent_id="agent-l2-1",
        assurance_level="L2",
        domain="l2test.example.invalid",
    )
    verifier = ExchangeVerifier(
        trusted_issuers=issuer.trust_store,
        domain_keys={"l2test.example.invalid": bundle["principal_public_key_hex"]},
        now_fn=clock,
    )
    ok, reason = verifier.refresh_revocation_list(issuer.revocation_list())
    assert ok, reason
    challenge = verifier.issue_challenge()
    payload = dict(ORDER, agent_id="agent-l2-1")
    ok, reason, _ = verify_ok(verifier, bundle, challenge, payload, clock)
    assert not ok
    assert reason == "KYA_L2_INSUFFICIENT_CONSEQUENTIAL"


def test_stale_revocation_list_rejected(issuer, verifier, agent_a, clock):
    clock.advance(minutes=61)  # revocation list max age is 3600s
    challenge = verifier.issue_challenge()
    pres = signed_presentation(agent_a, challenge, ORDER, iso(clock()))
    ok, reason, _ = verifier.verify_order(pres, ORDER, agent_id="agent-alice-1")
    assert not ok
    assert reason == "KYA_REVOCATION_LIST_STALE"


def test_missing_revocation_list_rejected(issuer, agent_a, clock):
    from joulex.admission import ExchangeVerifier

    verifier = ExchangeVerifier(trusted_issuers=issuer.trust_store, now_fn=clock)
    challenge = verifier.issue_challenge()
    ok, reason, _ = verify_ok(verifier, agent_a, challenge, ORDER, clock)
    assert not ok
    assert reason == "KYA_REVOCATION_LIST_MISSING"


def test_replayed_challenge_rejected(verifier, agent_a, clock):
    challenge = verifier.issue_challenge()
    pres = signed_presentation(agent_a, challenge, ORDER, iso(clock()))
    ok, reason, _ = verifier.verify_order(pres, ORDER, agent_id="agent-alice-1")
    assert ok, reason
    # Same presentation again: the challenge is now seen.
    ok, reason, _ = verifier.verify_order(pres, ORDER, agent_id="agent-alice-1")
    assert not ok
    assert reason == "KYA_CHALLENGE_REUSED"


def test_payload_mismatch_rejected(verifier, agent_a, clock):
    challenge = verifier.issue_challenge()
    other = dict(ORDER, shares=999)
    pres = signed_presentation(agent_a, challenge, other, iso(clock()))
    ok, reason, _ = verifier.verify_order(pres, ORDER, agent_id="agent-alice-1")
    assert not ok
    assert reason == "KYA_PAYLOAD_MISMATCH"


def test_out_of_scope_tool_rejected(issuer, verifier, clock):
    bundle = issuer.mint_agent(
        principal_id="principal-scope-test",
        display_name="Scope Test",
        agent_id="agent-scope-1",
        tools=["joulex.cancel_order"],
        intent_classes=["TRADE"],
    )
    challenge = verifier.issue_challenge()
    payload = dict(ORDER, agent_id="agent-scope-1")
    ok, reason, _ = verify_ok(verifier, bundle, challenge, payload, clock)
    assert not ok
    # The delegation chain itself does not cover joulex.place_order,
    # so the chain check fires first (delegation reason string).
    assert reason == "DELEGATION_SCOPE_EXCEEDED"


def test_credential_narrower_than_delegation_rejected(issuer, verifier, clock):
    # Delegation covers place_order, but the credential is scoped to
    # cancel_order only: the kya-level scope check fires.
    bundle = issuer.mint_agent(
        principal_id="principal-scope2-test",
        display_name="Scope Test 2",
        agent_id="agent-scope2-1",
        tools=["joulex.place_order", "joulex.cancel_order"],
        credential_tools=["joulex.cancel_order"],
        intent_classes=["TRADE"],
    )
    challenge = verifier.issue_challenge()
    payload = dict(ORDER, agent_id="agent-scope2-1")
    ok, reason, _ = verify_ok(verifier, bundle, challenge, payload, clock)
    assert not ok
    assert reason == "KYA_SCOPE_EXCEEDED"


def test_challenge_not_issued_by_server_rejected(verifier, agent_a, clock):
    pres = signed_presentation(agent_a, "deadbeef" * 4, ORDER, iso(clock()))
    ok, reason, _ = verifier.verify_order(pres, ORDER, agent_id="agent-alice-1")
    assert not ok
    assert reason == "EXCHANGE_CHALLENGE_UNKNOWN"


def test_expired_challenge_rejected(verifier, agent_a, clock):
    challenge = verifier.issue_challenge()
    clock.advance(seconds=301)
    pres = signed_presentation(agent_a, challenge, ORDER, iso(clock()))
    ok, reason, _ = verifier.verify_order(pres, ORDER, agent_id="agent-alice-1")
    assert not ok
    assert reason == "EXCHANGE_CHALLENGE_EXPIRED"


def test_stale_presentation_rejected(verifier, agent_a, clock):
    clock.advance(seconds=301)
    challenge = verifier.issue_challenge()  # fresh challenge at the new time
    old_ts = iso(T0)  # 301s old: past the 300s presentation window
    pres = signed_presentation(agent_a, challenge, ORDER, old_ts)
    ok, reason, _ = verifier.verify_order(pres, ORDER, agent_id="agent-alice-1")
    assert not ok
    assert reason == "KYA_PRESENTATION_STALE"


def test_failed_attempt_does_not_burn_challenge(verifier, agent_a, clock):
    # A failed verification (payload mismatch) must not consume the
    # challenge, so the agent can retry with a correct presentation.
    challenge = verifier.issue_challenge()
    other = dict(ORDER, shares=999)
    pres_bad = signed_presentation(agent_a, challenge, other, iso(clock()))
    ok, _, _ = verifier.verify_order(pres_bad, ORDER, agent_id="agent-alice-1")
    assert not ok
    pres_good = signed_presentation(agent_a, challenge, ORDER, iso(clock()))
    ok, reason, _ = verifier.verify_order(pres_good, ORDER, agent_id="agent-alice-1")
    assert ok, reason
