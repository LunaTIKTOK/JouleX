"""M3: persistent challenge store.

The replay defence must survive a restart: a challenge seen before the
restart is still rejected after it (KYA_CHALLENGE_REUSED, not silently
forgotten), and an issued-but-unused challenge still verifies after it.
State lives in ``data/challenges.json`` via atomic writes. A failed
verification never burns a challenge; expired entries are pruned on
load; a corrupt store refuses to start (fail closed).
"""

from __future__ import annotations

import json

import pytest

import kya as kya_module
from joulex.admission import ExchangeVerifier, payload_hash
from joulex.challenges import PersistentChallengeStore
from joulex.exchange import Exchange, ExchangeError
from joulex.store import JsonStore
from tests.conftest import (
    create_market_admitted,
    iso,
    make_market_payload,
    make_order_payload,
)

ORDER = {"agent_id": "agent-alice-1", "market_id": "m1", "side": "YES", "shares": 10, "action": "buy"}


def signed_presentation(bundle, challenge, payload, timestamp):
    return kya_module.create_presentation(
        bundle["credential"],
        agent_seed=bundle["agent_seed"],
        challenge=challenge,
        payload_hash=payload_hash(payload),
        timestamp=timestamp,
    )


def make_verifier(tmp_path, issuer, clock):
    """Verifier backed by a persistent store in a temp data dir."""
    store = PersistentChallengeStore(JsonStore(str(tmp_path / "data")), now_fn=clock)
    v = ExchangeVerifier(trusted_issuers=issuer.trust_store, now_fn=clock, challenge_store=store)
    ok, reason = v.refresh_revocation_list(issuer.revocation_list())
    assert ok, reason
    return v


def test_seen_challenge_survives_restart(tmp_path, issuer, agent_a, clock):
    v1 = make_verifier(tmp_path, issuer, clock)
    challenge = v1.issue_challenge()
    pres = signed_presentation(agent_a, challenge, ORDER, iso(clock()))
    ok, reason, _ = v1.verify_order(pres, ORDER, agent_id="agent-alice-1")
    assert ok, reason
    # Restart: a brand-new verifier on the same data dir.
    v2 = make_verifier(tmp_path, issuer, clock)
    ok, reason, _ = v2.verify_order(pres, ORDER, agent_id="agent-alice-1")
    assert not ok
    assert reason == "KYA_CHALLENGE_REUSED"


def test_issued_challenge_survives_restart(tmp_path, issuer, agent_a, clock):
    v1 = make_verifier(tmp_path, issuer, clock)
    challenge = v1.issue_challenge()
    # Restart before the challenge is used.
    v2 = make_verifier(tmp_path, issuer, clock)
    pres = signed_presentation(agent_a, challenge, ORDER, iso(clock()))
    ok, reason, _ = v2.verify_order(pres, ORDER, agent_id="agent-alice-1")
    assert ok, reason


def test_expired_challenge_pruned_on_restart(tmp_path, issuer, agent_a, clock):
    v1 = make_verifier(tmp_path, issuer, clock)
    challenge = v1.issue_challenge()
    clock.advance(seconds=301)
    v2 = make_verifier(tmp_path, issuer, clock)
    pres = signed_presentation(agent_a, challenge, ORDER, iso(clock()))
    ok, reason, _ = v2.verify_order(pres, ORDER, agent_id="agent-alice-1")
    assert not ok
    assert reason == "EXCHANGE_CHALLENGE_UNKNOWN"


def test_failed_verification_does_not_burn_persisted_challenge(tmp_path, issuer, agent_a, clock):
    v1 = make_verifier(tmp_path, issuer, clock)
    challenge = v1.issue_challenge()
    # Sign for a different payload: verification must fail, challenge stays live.
    other = dict(ORDER, shares=999)
    pres = signed_presentation(agent_a, challenge, other, iso(clock()))
    ok, reason, _ = v1.verify_order(pres, ORDER, agent_id="agent-alice-1")
    assert not ok
    assert reason == "KYA_PAYLOAD_MISMATCH"
    # Restart, then use the same challenge correctly.
    v2 = make_verifier(tmp_path, issuer, clock)
    pres = signed_presentation(agent_a, challenge, ORDER, iso(clock()))
    ok, reason, _ = v2.verify_order(pres, ORDER, agent_id="agent-alice-1")
    assert ok, reason


def test_challenges_file_is_atomic_json(tmp_path, issuer, clock):
    v = make_verifier(tmp_path, issuer, clock)
    challenge = v.issue_challenge()
    path = tmp_path / "data" / "challenges.json"
    assert path.exists()
    assert not (tmp_path / "data" / "challenges.json.tmp").exists()
    state = json.loads(path.read_text())
    assert challenge in state["issued"]
    assert state["seen"] == {}


def test_corrupt_challenge_store_refuses_start(tmp_path, issuer, clock):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "challenges.json").write_text("{not json")
    with pytest.raises(RuntimeError, match="challenge store"):
        make_verifier(tmp_path, issuer, clock)


def test_exchange_restart_rejects_replayed_order(tmp_path, issuer, agent_a, clock):
    """End to end: an order placed, server restarted, same presentation
    replayed against the new instance must still be a replay."""
    data_dir = str(tmp_path / "data")

    def build_exchange():
        ex = Exchange(data_dir=data_dir, now_fn=clock, trusted_issuers=issuer.trust_store)
        ok, reason = ex.verifier.refresh_revocation_list(issuer.revocation_list())
        assert ok, reason
        return ex

    ex1 = build_exchange()
    market = create_market_admitted(ex1, agent_a, make_market_payload(), clock)
    payload = make_order_payload(agent_a["agent_id"], market["market_id"])
    challenge = ex1.issue_challenge()["challenge"]
    pres = signed_presentation(agent_a, challenge, payload, iso(clock()))
    receipt = ex1.place_order(pres, payload)
    assert receipt["agent_id"] == agent_a["agent_id"]

    # Restart: new Exchange instance, same data dir, same clock.
    ex2 = build_exchange()
    with pytest.raises(ExchangeError) as excinfo:
        ex2.place_order(pres, payload)
    assert excinfo.value.status == 401
    assert excinfo.value.reason == "KYA_CHALLENGE_REUSED"
