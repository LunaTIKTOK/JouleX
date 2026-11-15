"""Ledger + exchange integration tests.

Conservation across trades and settlement, exact winner/loser payouts,
insufficient-points rejection, no trading after resolution, idempotent
settlement, and market-creation validation.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from joulex.admission import payload_hash
from joulex.attestation import mint_envelope, payload_hash_for_settlement
from joulex.exchange import ExchangeError
from tests.conftest import (
    T0,
    create_market_admitted,
    iso,
    make_market_payload,
    make_order_payload,
)
from tests.test_admission import signed_presentation


def place(exchange, bundle, payload, clock):
    """Full order path: challenge -> presentation -> place_order."""
    challenge = exchange.issue_challenge()["challenge"]
    pres = signed_presentation(bundle, challenge, payload, iso(clock()))
    return exchange.place_order(pres, payload)


def open_market(exchange, bundle, clock, **overrides):
    """Create a market through the admitted (M2) path."""
    return create_market_admitted(exchange, bundle, make_market_payload(**overrides), clock)


def test_first_order_endows_agent_with_1000_points(exchange, agent_a, clock):
    m = open_market(exchange, agent_a, clock)
    receipt = place(exchange, agent_a, make_order_payload("agent-alice-1", m["market_id"]), clock)
    assert receipt["balance_after"] == pytest.approx(1000.0 - receipt["cost_points"])
    assert exchange.ledger.balance("agent-alice-1") == pytest.approx(1000.0 - receipt["cost_points"])


def test_ledger_conserves_points_across_trades_and_settlement(exchange, agent_a, agent_b, clock):
    m = open_market(exchange, agent_a, clock)
    mid = m["market_id"]
    place(exchange, agent_a, make_order_payload("agent-alice-1", mid, "YES", 10), clock)
    place(exchange, agent_b, make_order_payload("agent-bob-1", mid, "NO", 20), clock)
    place(exchange, agent_a, make_order_payload("agent-alice-1", mid, "YES", 5), clock)
    # Sell some back: Alice sells 5 YES.
    place(exchange, agent_a, make_order_payload("agent-alice-1", mid, "YES", 5, action="sell"), clock)

    check = exchange.ledger.conservation()
    assert check["ok"]
    assert check["total"] == pytest.approx(2000.0)
    assert check["agents"] == 2

    clock.advance(days=1, minutes=1)
    receipt = exchange.settle_market(mid, observed_value=101000)
    assert receipt["outcome"] == "YES"
    check = exchange.ledger.conservation()
    assert check["ok"]
    assert check["total"] == pytest.approx(2000.0)


def test_settlement_pays_winners_exactly_1_per_share_losers_0(exchange, agent_a, agent_b, clock):
    m = open_market(exchange, agent_a, clock)
    mid = m["market_id"]
    place(exchange, agent_a, make_order_payload("agent-alice-1", mid, "YES", 10), clock)
    place(exchange, agent_b, make_order_payload("agent-bob-1", mid, "NO", 20), clock)
    alice_before = exchange.ledger.balance("agent-alice-1")
    bob_before = exchange.ledger.balance("agent-bob-1")

    clock.advance(days=1, minutes=1)
    receipt = exchange.settle_market(mid, observed_value=101000)
    assert receipt["outcome"] == "YES"
    payouts = {p["agent_id"]: p["payout_points"] for p in receipt["payouts"]}
    assert payouts["agent-alice-1"] == pytest.approx(10.0)  # 10 YES shares x 1
    assert payouts["agent-bob-1"] == pytest.approx(0.0)  # 20 NO shares x 0
    assert exchange.ledger.balance("agent-alice-1") == pytest.approx(alice_before + 10.0)
    assert exchange.ledger.balance("agent-bob-1") == pytest.approx(bob_before)
    # Positions are gone after settlement.
    assert exchange.positions("agent-alice-1")["positions"] == []
    assert exchange.positions("agent-bob-1")["positions"] == []


def test_settlement_below_strike_resolves_no(exchange, agent_a, clock):
    m = open_market(exchange, agent_a, clock)
    mid = m["market_id"]
    place(exchange, agent_a, make_order_payload("agent-alice-1", mid, "NO", 10), clock)
    clock.advance(days=1, minutes=1)
    receipt = exchange.settle_market(mid, observed_value=40.0)
    assert receipt["outcome"] == "NO"
    # Alice held 10 NO shares, NO won: +10 points on top of post-trade balance.
    assert receipt["payouts"][0]["payout_points"] == pytest.approx(10.0)


def test_tie_resolves_no(exchange, agent_a, clock):
    m = open_market(exchange, agent_a, clock)
    mid = m["market_id"]
    place(exchange, agent_a, make_order_payload("agent-alice-1", mid, "YES", 10), clock)
    clock.advance(days=1, minutes=1)
    receipt = exchange.settle_market(mid, observed_value=50.0)  # exactly the strike
    assert receipt["outcome"] == "NO"


def test_insufficient_points_rejected_without_state_change(exchange, agent_a, clock):
    m = open_market(exchange, agent_a, clock)
    mid = m["market_id"]
    q_before = (m["q_yes"], m["q_no"])
    with pytest.raises(ExchangeError) as exc:
        place(exchange, agent_a, make_order_payload("agent-alice-1", mid, "YES", 100_000), clock)
    assert exc.value.status == 403
    assert "INSUFFICIENT_POINTS" in exc.value.reason
    after = exchange.get_market(mid)
    assert (after["q_yes"], after["q_no"]) == q_before
    # Agent was never even endowed: no successful order, no account.
    assert exchange.ledger.balance("agent-alice-1") == 0.0


def test_no_order_without_credential(exchange, agent_a, clock):
    m = open_market(exchange, agent_a, clock)
    payload = make_order_payload("agent-alice-1", m["market_id"])
    with pytest.raises(ExchangeError) as exc:
        exchange.place_order(None, payload)
    assert exc.value.status == 401
    assert exc.value.reason == "KYA_PRESENTATION_MISSING"


def test_market_cannot_be_traded_after_resolves_at(exchange, agent_a, clock):
    m = open_market(exchange, agent_a, clock)
    mid = m["market_id"]
    clock.advance(days=1, minutes=1)  # past resolves_at, not yet settled
    with pytest.raises(ExchangeError) as exc:
        place(exchange, agent_a, make_order_payload("agent-alice-1", mid), clock)
    assert exc.value.status == 409
    assert "MARKET_CLOSED" in exc.value.reason


def test_market_cannot_be_traded_after_settlement(exchange, agent_a, clock):
    m = open_market(exchange, agent_a, clock)
    mid = m["market_id"]
    place(exchange, agent_a, make_order_payload("agent-alice-1", mid), clock)
    clock.advance(days=1, minutes=1)
    exchange.settle_market(mid, observed_value=101000)
    with pytest.raises(ExchangeError) as exc:
        place(exchange, agent_a, make_order_payload("agent-alice-1", mid), clock)
    assert exc.value.status == 409


def test_settle_before_resolves_at_rejected(exchange, agent_a, clock):
    m = open_market(exchange, agent_a, clock)
    with pytest.raises(ExchangeError) as exc:
        exchange.settle_market(m["market_id"], observed_value=101000)
    assert exc.value.status == 409


def test_double_settlement_rejected_without_double_payout(exchange, agent_a, clock):
    m = open_market(exchange, agent_a, clock)
    mid = m["market_id"]
    place(exchange, agent_a, make_order_payload("agent-alice-1", mid, "YES", 10), clock)
    clock.advance(days=1, minutes=1)
    first = exchange.settle_market(mid, observed_value=101000)
    balance_after_first = exchange.ledger.balance("agent-alice-1")
    with pytest.raises(ExchangeError) as exc:
        exchange.settle_market(mid, observed_value=101000)
    assert exc.value.status == 409
    assert exchange.ledger.balance("agent-alice-1") == pytest.approx(balance_after_first)
    assert first["payouts"][0]["payout_points"] == pytest.approx(10.0)


def test_sell_more_than_held_rejected(exchange, agent_a, clock):
    m = open_market(exchange, agent_a, clock)
    mid = m["market_id"]
    place(exchange, agent_a, make_order_payload("agent-alice-1", mid, "YES", 10), clock)
    with pytest.raises(ExchangeError) as exc:
        place(exchange, agent_a, make_order_payload("agent-alice-1", mid, "YES", 11, action="sell"), clock)
    assert exc.value.status == 400
    assert exchange.ledger.position("agent-alice-1", mid, "YES") == pytest.approx(10.0)


def test_price_moves_with_trades(exchange, agent_a, agent_b, clock):
    m = open_market(exchange, agent_a, clock)
    mid = m["market_id"]
    assert m["price_yes"] == pytest.approx(0.5)
    r1 = place(exchange, agent_a, make_order_payload("agent-alice-1", mid, "YES", 10), clock)
    assert r1["price_yes_after"] > 0.5
    r2 = place(exchange, agent_b, make_order_payload("agent-bob-1", mid, "NO", 10), clock)
    assert r2["price_yes_after"] == pytest.approx(0.5)
    # Bob buys the cheap side (NO at ~0.475 after Alice moved YES up),
    # so his 10 shares cost less than Alice's 10 at 0.50.
    assert r2["cost_points"] < r1["cost_points"]


def test_market_creation_validation(exchange, agent_a, clock):
    # Invalid fields 400 BEFORE admission runs (M2 ordering): a signed
    # presentation over malformed fields is rejected on validation, and
    # the 400 never burns the challenge (proved in
    # test_market_creation.py). A bare call with no presentation at all
    # is 401 before any validation detail leaks.
    from tests.conftest import signed_creation

    def create_invalid(**overrides):
        fields = make_market_payload(**overrides)
        challenge = exchange.issue_challenge()["challenge"]
        pres = signed_creation(agent_a, challenge, fields, iso(clock()))
        return exchange.create_market(pres, agent_id="agent-alice-1", **fields)

    # resolves_at less than 1 day out
    with pytest.raises(ExchangeError) as exc:
        create_invalid(resolves_at=iso(T0))
    assert exc.value.status == 400
    # resolves_at more than 30 days out (JouleX horizon is 1–30d)
    with pytest.raises(ExchangeError) as exc:
        create_invalid(resolves_at=iso(T0 + timedelta(days=31)))
    assert exc.value.status == 400
    # bad contract_type
    with pytest.raises(ExchangeError):
        create_invalid(contract_type="nuclear_futures")
    # public_source market requires a source URL
    with pytest.raises(ExchangeError):
        fields = {
            "question": "Will it rain in Calgary tomorrow?",
            "resolution_type": "public_source",
            "resolves_at": iso(T0.replace(day=6)),
        }
        challenge = exchange.issue_challenge()["challenge"]
        pres = signed_creation(agent_a, challenge, fields, iso(clock()))
        exchange.create_market(pres, agent_id="agent-alice-1", **fields)
    ok = create_market_admitted(
        exchange,
        agent_a,
        {
            "question": "Will it rain in Calgary tomorrow?",
            "resolution_type": "public_source",
            "source_url": "https://example.invalid/weather/calgary",
            "source_description": "Environment Canada Calgary forecast, precipitation > 0mm",
            "resolves_at": iso(T0.replace(day=6)),
        },
        clock,
    )
    assert ok["status"] == "OPEN"


def test_public_source_settlement_is_operator_attested(exchange, agent_a, clock):
    m = create_market_admitted(
        exchange,
        agent_a,
        {
            "question": "Will it rain in Calgary tomorrow?",
            "resolution_type": "public_source",
            "source_url": "https://example.invalid/weather/calgary",
            "source_description": "Environment Canada Calgary forecast",
            "resolves_at": iso(T0.replace(day=6)),
        },
        clock,
    )
    mid = m["market_id"]
    place(exchange, agent_a, make_order_payload("agent-alice-1", mid, "YES", 10), clock)
    clock.advance(days=1, minutes=1)
    # Settlement-auth policy (2026-11-11): a public_source settle
    # must carry a valid attestation envelope bound to the
    # settlement payload hash — the "operator attested" in this
    # test's name is now literal.
    evidence = "https://example.invalid/weather/calgary observed rain"
    env = mint_envelope(
        credential_id="joulex-settlement",
        challenge_nonce=f"settle-{mid}",
        payload_hash=payload_hash_for_settlement(
            market_id=mid, outcome="YES", observed_value=None,
            evidence=evidence,
        ),
        now=clock(),
    )
    receipt = exchange.settle_market(
        mid, outcome="YES", evidence=evidence,
        attestation=env,
        attestation_binding={
            "credential_id": "joulex-settlement",
            "challenge_nonce": f"settle-{mid}",
        },
    )
    assert receipt["outcome"] == "YES"
    assert receipt["attestation_status"] == "verified"
    assert receipt["payouts"][0]["payout_points"] == pytest.approx(10.0)


def test_state_persists_and_reloads(tmp_path, issuer, clock):
    from joulex.exchange import Exchange

    data_dir = str(tmp_path / "data")
    ex1 = Exchange(data_dir=data_dir, now_fn=clock, trusted_issuers=issuer.trust_store)
    ex1.verifier.refresh_revocation_list(issuer.revocation_list())
    bundle = issuer.mint_agent(
        principal_id="principal-persist-test",
        display_name="Persist Test",
        agent_id="agent-persist-1",
    )
    m = create_market_admitted(ex1, bundle, make_market_payload(), clock)
    place(ex1, bundle, make_order_payload("agent-persist-1", m["market_id"]), clock)
    ex1.save()

    ex2 = Exchange(data_dir=data_dir, now_fn=clock, trusted_issuers=issuer.trust_store)
    assert ex2.get_market(m["market_id"])["q_yes"] == pytest.approx(10.0)
    assert ex2.ledger.balance("agent-persist-1") == pytest.approx(1000.0 - 5.0, abs=1.0)
    assert ex2.ledger.conservation()["ok"]


def test_order_for_unknown_market_rejected(exchange, agent_a, clock):
    with pytest.raises(ExchangeError) as exc:
        place(exchange, agent_a, make_order_payload("agent-alice-1", "no-such-market"), clock)
    assert exc.value.status == 404
