"""M1 — exchange wiring tests (test-first).

Covers DESIGN.md §7: envelope on action records + receipts, verifier
in the order / market-creation / settlement paths, HCS append on every
action including attestation verification results, attestation policy
on the credential (inside the signed content, re-signed by the demo
issuer).
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

import kya as kya_module
from joulex.admission import creation_payload, creation_payload_hash, payload_hash
from joulex.attestation import (
    compute_measurement,
    mint_envelope,
    payload_hash_for_settlement,
)
from joulex.exchange import ExchangeError
from tests.conftest import (
    create_market_admitted,
    make_market_payload,
    make_order_payload,
    signed_creation,
)


def _order_with_attestation(exchange, bundle, market_id, clock, *, attestation=None):
    """Place one order, optionally attaching an attestation envelope to
    the presentation (the envelope rides on the presentation dict)."""
    challenge = exchange.issue_challenge()["challenge"]
    payload = make_order_payload(bundle["agent_id"], market_id)
    pres = kya_module.create_presentation(
        bundle["credential"],
        agent_seed=bundle["agent_seed"],
        challenge=challenge,
        payload_hash=payload_hash(payload),
        timestamp=clock().isoformat(),
    )
    if attestation is not None:
        pres["attestation"] = attestation
    return exchange.place_order(pres, payload)


def _valid_envelope(exchange, bundle, challenge, payload_hash, clock):
    return mint_envelope(
        credential_id=bundle["credential_id"],
        challenge_nonce=challenge,
        payload_hash=payload_hash,
        now=clock(),
    )


def test_order_without_attestation_still_trades(exchange, agent_a, clock):
    """Default is advisory: no envelope -> plain CF-KYA verification."""
    market = create_market_admitted(exchange, agent_a, make_market_payload(), clock)
    receipt = _order_with_attestation(exchange, agent_a, market["market_id"], clock)
    assert receipt["attestation"] is None
    assert receipt["attestation_status"] == "absent"


def test_order_with_valid_attestation_recorded(exchange, agent_a, clock):
    market = create_market_admitted(exchange, agent_a, make_market_payload(), clock)
    challenge = exchange.issue_challenge()["challenge"]
    payload = make_order_payload(agent_a["agent_id"], market["market_id"])
    from joulex.admission import payload_hash

    env = _valid_envelope(exchange, agent_a, challenge, payload_hash(payload), clock)
    pres = kya_module.create_presentation(
        agent_a["credential"],
        agent_seed=agent_a["agent_seed"],
        challenge=challenge,
        payload_hash=payload_hash(payload),
        timestamp=clock().isoformat(),
    )
    pres["attestation"] = env
    receipt = exchange.place_order(pres, payload)
    assert receipt["attestation"] == env
    assert receipt["attestation_status"] == "verified"
    assert receipt["attestation"]["measurement"] == compute_measurement()
    # The HCS log recorded the action with its attestation.
    msgs = exchange.hcs.messages()
    assert msgs[-1]["action"] == "place_order"
    assert msgs[-1]["attestation"] == env
    assert msgs[-1]["provenance"]["told_by"] == "exchange"


def test_order_with_binding_mismatch_rejected(exchange, agent_a, clock):
    market = create_market_admitted(exchange, agent_a, make_market_payload(), clock)
    challenge = exchange.issue_challenge()["challenge"]
    payload = make_order_payload(agent_a["agent_id"], market["market_id"])
    from joulex.admission import payload_hash

    # Envelope minted for a DIFFERENT payload hash: replayed report.
    env = mint_envelope(
        credential_id=agent_a["credential_id"],
        challenge_nonce=challenge,
        payload_hash="f" * 64,
        now=clock(),
    )
    pres = kya_module.create_presentation(
        agent_a["credential"],
        agent_seed=agent_a["agent_seed"],
        challenge=challenge,
        payload_hash=payload_hash(payload),
        timestamp=clock().isoformat(),
    )
    pres["attestation"] = env
    with pytest.raises(ExchangeError) as exc:
        exchange.place_order(pres, payload)
    assert exc.value.status == 401
    assert exc.value.reason == "JOULEX_VC_BINDING"
    # The failed attestation verification is itself in the HCS log.
    msgs = exchange.hcs.messages()
    assert msgs[-1]["action"] == "attestation_verification"
    assert msgs[-1]["payload"]["result"] == "rejected"
    assert msgs[-1]["payload"]["reason"] == "JOULEX_VC_BINDING"


def test_required_policy_without_envelope_rejected(
    issuer, agent_a, exchange, clock, tmp_path
):
    """A credential whose attestation_policy.required=true cannot trade
    bare: absence is JOULEX_VC_REQUIRED."""
    from joulex.demo_issuer import DemoIssuer

    policy = {
        "required": True,
        "kinds": ["software"],
        "measurements": [compute_measurement()],
        "max_age_seconds": 300,
    }
    bundle = issuer.mint_agent(
        principal_id="principal-carol-test",
        display_name="Carol Test Principal",
        agent_id="agent-carol-1",
        attestation_policy=policy,
    )
    # The policy is inside the signed credential content: tampering
    # with it after issuance must break the issuer signature.
    tampered = dict(bundle["credential"])
    tampered["attestation_policy"] = {"required": False}
    assert not kya_module.verify_credential_signature(tampered)
    assert kya_module.verify_credential_signature(bundle["credential"])

    market = create_market_admitted(exchange, agent_a, make_market_payload(), clock)
    # Bare order (no envelope) -> required rejection. (The market was
    # created by agent_a, whose credential carries no policy — Carol's
    # required policy bites on HER actions, including creation.)
    challenge = exchange.issue_challenge()["challenge"]
    payload = make_order_payload(bundle["agent_id"], market["market_id"])
    from joulex.admission import payload_hash

    pres = kya_module.create_presentation(
        bundle["credential"],
        agent_seed=bundle["agent_seed"],
        challenge=challenge,
        payload_hash=payload_hash(payload),
        timestamp=clock().isoformat(),
    )
    with pytest.raises(ExchangeError) as exc:
        exchange.place_order(pres, payload)
    assert exc.value.status == 401
    assert exc.value.reason == "JOULEX_VC_REQUIRED"
    # Same order WITH a valid envelope trades.
    challenge = exchange.issue_challenge()["challenge"]
    env = _valid_envelope(exchange, bundle, challenge, payload_hash(payload), clock)
    pres = kya_module.create_presentation(
        bundle["credential"],
        agent_seed=bundle["agent_seed"],
        challenge=challenge,
        payload_hash=payload_hash(payload),
        timestamp=clock().isoformat(),
    )
    pres["attestation"] = env
    receipt = exchange.place_order(pres, payload)
    assert receipt["attestation_status"] == "verified"


def test_market_creation_records_attestation(exchange, agent_a, clock):
    fields = make_market_payload()
    challenge = exchange.issue_challenge()["challenge"]
    from joulex.admission import creation_payload_hash, creation_payload

    payload = creation_payload(agent_id=agent_a["agent_id"], **fields)
    env = mint_envelope(
        credential_id=agent_a["credential_id"],
        challenge_nonce=challenge,
        payload_hash=creation_payload_hash(payload),
        now=clock(),
    )
    pres = signed_creation(agent_a, challenge, fields, clock().isoformat())
    pres["attestation"] = env
    market = exchange.create_market(pres, agent_id=agent_a["agent_id"], **fields)
    assert market["attestation"] == env
    msgs = exchange.hcs.messages()
    assert msgs[-1]["action"] == "create_market"
    assert msgs[-1]["attestation"] == env


def test_settlement_records_attestation(exchange, agent_a, agent_b, clock):
    market = create_market_admitted(exchange, agent_a, make_market_payload(), clock)
    payload = make_order_payload(agent_a["agent_id"], market["market_id"])
    _order_with_attestation(exchange, agent_a, market["market_id"], clock)
    clock.advance(days=1, minutes=1)
    # Settlement attestation: the settler mints an envelope bound to
    # the settlement payload hash with its own binding inputs.
    ph = payload_hash_for_settlement(
        market_id=market["market_id"], outcome="YES",
        observed_value=62.5, evidence=None,
    )
    env = mint_envelope(
        credential_id="joulex-settlement",
        challenge_nonce=f"settle-{market['market_id']}",
        payload_hash=ph,
        now=clock(),
    )
    receipt = exchange.settle_market(
        market["market_id"],
        observed_value=62.5,
        attestation=env,
        attestation_binding={
            "credential_id": "joulex-settlement",
            "challenge_nonce": f"settle-{market['market_id']}",
        },
    )
    assert receipt["outcome"] == "YES"
    assert receipt["attestation"] == env
    assert receipt["attestation_status"] == "verified"
    msgs = exchange.hcs.messages()
    assert msgs[-1]["action"] == "settle_market"
    assert msgs[-1]["attestation"] == env


def test_settlement_with_invalid_attestation_rejected(exchange, agent_a, clock):
    market = create_market_admitted(exchange, agent_a, make_market_payload(), clock)
    clock.advance(days=1, minutes=1)
    ph = payload_hash_for_settlement(
        market_id=market["market_id"], outcome="YES",
        observed_value=62.5, evidence=None,
    )
    env = mint_envelope(
        credential_id="joulex-settlement",
        challenge_nonce="settle-other",
        payload_hash=ph,
        now=clock(),
    )
    with pytest.raises(ExchangeError) as exc:
        exchange.settle_market(
            market["market_id"],
            observed_value=62.5,
            attestation=env,
            attestation_binding={
                "credential_id": "joulex-settlement",
                "challenge_nonce": f"settle-{market['market_id']}",
            },
        )
    assert exc.value.status == 401
    assert exc.value.reason == "JOULEX_VC_BINDING"
    # Market stays OPEN: no outcome was guessed.
    assert exchange.get_market(market["market_id"])["status"] == "OPEN"


def test_hcs_log_covers_full_cycle(exchange, agent_a, agent_b, clock):
    market = create_market_admitted(exchange, agent_a, make_market_payload(), clock)
    _order_with_attestation(exchange, agent_a, market["market_id"], clock)
    clock.advance(days=1, minutes=1)
    exchange.settle_market(market["market_id"], observed_value=62.5)
    actions = [m["action"] for m in exchange.hcs.messages()]
    assert actions == ["create_market", "place_order", "settle_market"]
    assert exchange.hcs.verify_log()["ok"] is True
    # Replay answers what agent-a knew at settlement time.
    known = exchange.hcs.what_did_agent_know("agent-alice-1", clock())
    assert known["messages"], "agent-a acted, so it knew something"


def test_exchange_refuses_start_on_corrupt_hcs_log(tmp_path, issuer, clock):
    from joulex.exchange import Exchange

    data_dir = str(tmp_path / "data")
    ex = Exchange(data_dir=data_dir, now_fn=clock, trusted_issuers=issuer.trust_store)
    assert ex.hcs.verify_log()["ok"] is True
    log = tmp_path / "data" / "hcs_log.jsonl"
    log.write_text('{"mode": "local", "hedera_topic": null}\n{broken\n')
    with pytest.raises(RuntimeError):
        Exchange(data_dir=data_dir, now_fn=clock, trusted_issuers=issuer.trust_store)
