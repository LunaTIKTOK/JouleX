"""M6 — Multi-outcome markets (generalized LMSR, N outcomes).

Covers: the generalized LMSR math (and its exact agreement with the
binary functions at N=2), market/outcome validation, the admitted
creation + trading + settlement flow on a 3-outcome market, receipt
shape, binary back-compat, and reconciliation of multi-outcome state
(including tamper detection). All state goes through the real Exchange;
the last test drives the real HTTP API.
"""

from __future__ import annotations

import json
import math
import threading
from datetime import timedelta

import pytest

from joulex.attestation import mint_envelope, payload_hash_for_settlement
from joulex.exchange import Exchange, ExchangeError
from joulex.lmsr import (
    apply_trade,
    cost_function,
    cost_function_multi,
    price_yes,
    prices_multi,
    trade_cost,
    trade_cost_multi,
)
from joulex.markets import new_market, resolve_outcome
from joulex.reconcile import reconcile
from tests.conftest import (
    T0,
    create_market_admitted,
    iso,
    make_market_payload,
    make_order_payload,
)
from tests.test_ledger import open_market, place

OUTCOMES_3 = ["Lakers", "Celtics", "Other"]


def make_multi_payload(**overrides):
    """A public_source market with three outcomes."""
    payload = {
        "question": "Which team wins the 2026 championship?",
        "resolution_type": "public_source",
        "source_url": "https://example.com/league/standings",
        "source_description": "Official league standings page; the listed champion wins.",
        "outcomes": list(OUTCOMES_3),
        "resolves_at": iso(T0 + timedelta(days=1)),
        "b": 100.0,
    }
    payload.update(overrides)
    return payload


def open_multi_market(exchange, bundle, clock, **overrides):
    return create_market_admitted(exchange, bundle, make_multi_payload(**overrides), clock)


def settle_multi(exchange, market_id, *, outcome, evidence, clock):
    """Settle a multi-outcome market under the settlement-auth
    policy (2026-11-11): public_source settlements REQUIRE a valid
    attestation envelope bound to the settlement payload hash."""
    ph = payload_hash_for_settlement(
        market_id=market_id, outcome=outcome,
        observed_value=None, evidence=evidence,
    )
    env = mint_envelope(
        credential_id="joulex-settlement",
        challenge_nonce=f"settle-{market_id}",
        payload_hash=ph,
        now=clock(),
    )
    return exchange.settle_market(
        market_id, outcome=outcome, evidence=evidence,
        attestation=env,
        attestation_binding={
            "credential_id": "joulex-settlement",
            "challenge_nonce": f"settle-{market_id}",
        },
    )


# -- generalized LMSR math ----------------------------------------------


def test_multi_cost_at_zero_is_b_ln_n():
    assert cost_function_multi([0.0, 0.0, 0.0], 100.0) == pytest.approx(100.0 * math.log(3))
    assert cost_function_multi([0.0, 0.0, 0.0, 0.0], 100.0) == pytest.approx(100.0 * math.log(4))


def test_multi_prices_start_equal_and_sum_to_one():
    prices = prices_multi([0.0, 0.0, 0.0], 100.0)
    assert prices == pytest.approx([1 / 3, 1 / 3, 1 / 3])
    assert sum(prices) == pytest.approx(1.0)


def test_multi_prices_sum_to_one_after_trades():
    q = [10.0, 5.0, 0.0]
    assert sum(prices_multi(q, 100.0)) == pytest.approx(1.0)
    q = [1000.0, 0.0, 500.0]
    assert sum(prices_multi(q, 100.0)) == pytest.approx(1.0)


def test_multi_buy_raises_own_price_lowers_others():
    before = prices_multi([0.0, 0.0, 0.0], 100.0)
    after = prices_multi([10.0, 0.0, 0.0], 100.0)
    assert after[0] > before[0]
    assert after[1] < before[1]
    assert after[2] < before[2]
    # The untouched outcomes stay equal to each other.
    assert after[1] == pytest.approx(after[2])


def test_multi_buy_then_sell_round_trip_exact():
    cost_buy = trade_cost_multi([0.0, 0.0, 0.0], 100.0, 0, 10)
    assert cost_buy > 0
    cost_sell = trade_cost_multi([10.0, 0.0, 0.0], 100.0, 0, -10)
    assert cost_sell == -cost_buy


def test_multi_n2_matches_binary_exactly():
    # The generalized function must be the binary function at N=2 —
    # one code path, not two mechanisms. Exact equality, not approx:
    # the demo's exact balances depend on it.
    for qy, qn in [(0.0, 0.0), (10.0, 0.0), (3.0, 7.0), (1000.0, 500.0)]:
        assert cost_function_multi([qy, qn], 100.0) == cost_function(qy, qn, 100.0)
        assert prices_multi([qy, qn], 100.0) == pytest.approx(
            [price_yes(qy, qn, 100.0), 1.0 - price_yes(qy, qn, 100.0)]
        )
        assert trade_cost_multi([qy, qn], 100.0, 0, 10) == trade_cost(qy, qn, 100.0, "YES", 10)
        assert trade_cost_multi([qy, qn], 100.0, 1, 10) == trade_cost(qy, qn, 100.0, "NO", 10)
    # apply_trade on the binary pair still works (legacy helper).
    assert apply_trade(3.0, 7.0, "NO", 5) == (3.0, 12.0)


def test_multi_cost_bounded_by_shares():
    assert trade_cost_multi([0.0, 0.0, 0.0], 100.0, 0, 10_000) < 10_000


def test_multi_cost_convex_in_size():
    c1 = trade_cost_multi([0.0, 0.0, 0.0], 100.0, 0, 10)
    c2 = trade_cost_multi([0.0, 0.0, 0.0], 100.0, 0, 20)
    assert 0 < c1 < c2 < 20
    assert c2 > 2 * c1


def test_multi_invalid_index_and_b_raise():
    with pytest.raises((ValueError, IndexError)):
        trade_cost_multi([0.0, 0.0, 0.0], 100.0, 3, 10)
    with pytest.raises((ValueError, IndexError)):
        trade_cost_multi([0.0, 0.0, 0.0], 100.0, -1, 10)
    with pytest.raises(ValueError):
        cost_function_multi([0.0, 0.0, 0.0], 0.0)
    with pytest.raises(ValueError):
        prices_multi([0.0, 0.0, 0.0], -5.0)


def test_multi_zero_shares_costs_zero():
    assert trade_cost_multi([5.0, 5.0, 5.0], 100.0, 1, 0) == 0.0


# -- market record + outcome validation ---------------------------------


def test_default_market_is_binary_with_mirrors(clock):
    m = new_market(
        question="Will the pool price exceed $50/MWh in the window?",
        resolution_type="aeso_price",
        contract_type="flex_capacity",
        strike=50.0,
        window_start=iso(T0 + timedelta(days=1)),
        window_end=iso(T0 + timedelta(days=1, hours=6)),
        resolves_at=iso(T0 + timedelta(days=1)), now=clock(),
    )
    assert m["outcomes"] == ["YES", "NO"]
    assert m["q"] == [0.0, 0.0]
    assert m["prices"] == {"YES": 0.5, "NO": 0.5}
    assert m["q_yes"] == 0.0 and m["q_no"] == 0.0 and m["price_yes"] == 0.5


def test_multi_market_record(clock):
    m = new_market(
        question="Which team wins?",
        resolution_type="public_source",
        source_url="https://example.com/x",
        source_description="Standings page.",
        outcomes=list(OUTCOMES_3),
        resolves_at=iso(T0 + timedelta(days=1)), now=clock(),
    )
    assert m["outcomes"] == OUTCOMES_3
    assert m["q"] == [0.0, 0.0, 0.0]
    assert m["prices"] == {"Lakers": pytest.approx(1 / 3),
                           "Celtics": pytest.approx(1 / 3),
                           "Other": pytest.approx(1 / 3)}
    # No binary mirrors on a multi-outcome market.
    assert "q_yes" not in m and "q_no" not in m and "price_yes" not in m


def test_binary_outcomes_normalized_to_yes_no_order(clock):
    m = new_market(
        question="Will it rain?",
        resolution_type="public_source",
        source_url="https://example.com/x",
        source_description="Weather service.",
        outcomes=["NO", "YES"],
        resolves_at=iso(T0 + timedelta(days=1)), now=clock(),
    )
    assert m["outcomes"] == ["YES", "NO"]
    assert m["q_yes"] == 0.0 and m["q_no"] == 0.0


@pytest.mark.parametrize("cased", [["yes", "no"], ["Yes", "No"], ["YES", "no"], ["no", "YES"]])
def test_binary_outcomes_normalized_regardless_of_case(clock, cased):
    # YES/NO are the canonical binary labels: any casing of the pair
    # is the same binary market (with mirrors), not a distinct
    # 2-outcome market with lowercase labels.
    m = new_market(
        question="Will it rain?",
        resolution_type="public_source",
        source_url="https://example.com/x",
        source_description="Weather service.",
        outcomes=cased,
        resolves_at=iso(T0 + timedelta(days=1)), now=clock(),
    )
    assert m["outcomes"] == ["YES", "NO"]
    assert m["q_yes"] == 0.0 and m["q_no"] == 0.0 and m["price_yes"] == 0.5


@pytest.mark.parametrize("bad", [
    ["OnlyOne"],
    [],
    ["A", "A"],
    ["A", "  "],
    ["A", "B|C"],
    ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K"],
    ["A", ""],
    ["A", "x" * 65],
])
def test_invalid_outcomes_rejected(clock, bad):
    with pytest.raises(ValueError):
        new_market(
            question="Which?",
            resolution_type="public_source",
            source_url="https://example.com/x",
            source_description="Source.",
            outcomes=bad,
            resolves_at=iso(T0 + timedelta(days=1)), now=clock(),
        )


def test_outcomes_must_be_a_list(clock):
    with pytest.raises(ValueError):
        new_market(
            question="Which?",
            resolution_type="public_source",
            source_url="https://example.com/x",
            source_description="Source.",
            outcomes="Lakers",
            resolves_at=iso(T0 + timedelta(days=1)), now=clock(),
        )


def test_aeso_price_rejects_multi_outcomes(clock):
    with pytest.raises(ValueError):
        new_market(
            question="Which?",
            resolution_type="aeso_price",
            contract_type="flex_capacity",
            strike=50.0,
            window_start=iso(T0 + timedelta(days=1)),
            window_end=iso(T0 + timedelta(days=1, hours=6)),
            outcomes=list(OUTCOMES_3),
            resolves_at=iso(T0 + timedelta(days=1)), now=clock(),
        )


def test_outcome_labels_are_stripped(clock):
    m = new_market(
        question="Which?",
        resolution_type="public_source",
        source_url="https://example.com/x",
        source_description="Source.",
        outcomes=[" Lakers ", "Celtics"],
        resolves_at=iso(T0 + timedelta(days=1)), now=clock(),
    )
    assert m["outcomes"] == ["Lakers", "Celtics"]
    assert m["prices"] == {"Lakers": 0.5, "Celtics": 0.5}


def test_resolve_outcome_multi(clock):
    m = new_market(
        question="Which?",
        resolution_type="public_source",
        source_url="https://example.com/x",
        source_description="Source.",
        outcomes=list(OUTCOMES_3),
        resolves_at=iso(T0 + timedelta(days=1)), now=clock(),
    )
    assert resolve_outcome(m, outcome="Celtics") == "Celtics"
    assert resolve_outcome(m, outcome="Other") == "Other"
    for bad in ("YES", "NO", "Knicks", None):
        with pytest.raises(ValueError):
            resolve_outcome(m, outcome=bad)


def test_resolve_outcome_legacy_market_without_outcomes(clock):
    # State written before M6 has no outcomes field: it is binary.
    m = new_market(
        question="Will it rain?",
        resolution_type="public_source",
        source_url="https://example.com/x",
        source_description="Weather service.",
        resolves_at=iso(T0 + timedelta(days=1)), now=clock(),
    )
    del m["outcomes"]
    assert resolve_outcome(m, outcome="YES") == "YES"
    with pytest.raises(ValueError):
        resolve_outcome(m, outcome="Celtics")


# -- exchange flow: creation, trading, settlement ----------------------


def test_create_multi_market_via_admitted_path(exchange, agent_a, clock):
    m = open_multi_market(exchange, agent_a, clock)
    assert m["outcomes"] == OUTCOMES_3
    assert m["q"] == [0.0, 0.0, 0.0]
    assert m["created_by"] == agent_a["agent_id"]
    assert m["status"] == "OPEN"


def test_create_multi_market_via_operator_key(tmp_path, issuer, clock):
    ex = Exchange(
        data_dir=str(tmp_path / "data"), now_fn=clock,
        trusted_issuers=issuer.trust_store, operator_key="op-secret",
    )
    m = ex.create_market(operator_key="op-secret", **make_multi_payload())
    assert m["outcomes"] == OUTCOMES_3
    assert m["created_by"] == "operator"


def test_buy_multi_outcome_moves_prices(exchange, agent_a, clock):
    m = open_multi_market(exchange, agent_a, clock)
    mid = m["market_id"]
    receipt = place(exchange, agent_a,
                    make_order_payload(agent_a["agent_id"], mid, "Lakers", 10), clock)
    assert receipt["cost_points"] > 0
    assert receipt["prices_after"]["Lakers"] > 1 / 3
    assert receipt["prices_after"]["Celtics"] < 1 / 3
    assert receipt["prices_after"]["Other"] == pytest.approx(receipt["prices_after"]["Celtics"])
    assert abs(sum(receipt["prices_after"].values()) - 1.0) < 1e-9
    # Binary-only field is absent on a multi-outcome receipt.
    assert "price_yes_after" not in receipt
    stored = exchange.get_market(mid)
    assert stored["q"] == [10.0, 0.0, 0.0]
    assert stored["prices"] == receipt["prices_after"]


def test_buy_cost_matches_generalized_lmsr(exchange, agent_a, clock):
    m = open_multi_market(exchange, agent_a, clock)
    receipt = place(exchange, agent_a,
                    make_order_payload(agent_a["agent_id"], m["market_id"], "Celtics", 10), clock)
    assert receipt["cost_points"] == trade_cost_multi([0.0, 0.0, 0.0], 100.0, 1, 10)


def test_sell_multi_outcome_refunds_exactly(exchange, agent_a, clock):
    m = open_multi_market(exchange, agent_a, clock)
    mid = m["market_id"]
    buy = place(exchange, agent_a,
                make_order_payload(agent_a["agent_id"], mid, "Lakers", 10), clock)
    sell = place(exchange, agent_a,
                 make_order_payload(agent_a["agent_id"], mid, "Lakers", 10, action="sell"), clock)
    assert sell["cost_points"] == -buy["cost_points"]
    assert exchange.ledger.balance(agent_a["agent_id"]) == pytest.approx(1000.0)
    assert exchange.get_market(mid)["q"] == [0.0, 0.0, 0.0]
    assert exchange.ledger.position(agent_a["agent_id"], mid, "Lakers") == 0.0


def test_order_for_unknown_outcome_rejected(exchange, agent_a, clock):
    m = open_multi_market(exchange, agent_a, clock)
    with pytest.raises(ExchangeError) as exc:
        place(exchange, agent_a,
              make_order_payload(agent_a["agent_id"], m["market_id"], "Knicks", 10), clock)
    assert exc.value.status == 400
    assert "INVALID_ORDER" in exc.value.reason


def test_binary_market_rejects_multi_outcome_side(exchange, agent_a, clock):
    m = open_market(exchange, agent_a, clock)
    with pytest.raises(ExchangeError) as exc:
        place(exchange, agent_a,
              make_order_payload(agent_a["agent_id"], m["market_id"], "Lakers", 10), clock)
    assert exc.value.status == 400


def test_sell_more_than_held_rejected(exchange, agent_a, clock):
    m = open_multi_market(exchange, agent_a, clock)
    place(exchange, agent_a,
          make_order_payload(agent_a["agent_id"], m["market_id"], "Lakers", 5), clock)
    with pytest.raises(ExchangeError) as exc:
        place(exchange, agent_a,
              make_order_payload(agent_a["agent_id"], m["market_id"], "Lakers", 6, action="sell"), clock)
    assert exc.value.status == 400
    assert "INSUFFICIENT_SHARES" in exc.value.reason


def test_binary_receipt_still_has_price_yes_after(exchange, agent_a, clock):
    m = open_market(exchange, agent_a, clock)
    receipt = place(exchange, agent_a,
                    make_order_payload(agent_a["agent_id"], m["market_id"], "YES", 10), clock)
    assert receipt["price_yes_after"] > 0.5
    assert receipt["prices_after"]["YES"] == pytest.approx(receipt["price_yes_after"])
    assert receipt["prices_after"]["NO"] == pytest.approx(1.0 - receipt["price_yes_after"])


def test_settle_multi_pays_winner_1_per_share(exchange, agent_a, agent_b, clock):
    m = open_multi_market(exchange, agent_a, clock)
    mid = m["market_id"]
    place(exchange, agent_a, make_order_payload(agent_a["agent_id"], mid, "Lakers", 10), clock)
    place(exchange, agent_b, make_order_payload(agent_b["agent_id"], mid, "Celtics", 20), clock)
    alice_before = exchange.ledger.balance(agent_a["agent_id"])
    bob_before = exchange.ledger.balance(agent_b["agent_id"])
    clock.advance(days=1, minutes=1)
    receipt = settle_multi(
        exchange, mid, outcome="Celtics",
        evidence="https://example.com/league/standings", clock=clock)
    assert receipt["outcome"] == "Celtics"
    payouts = {p["agent_id"]: p["payout_points"] for p in receipt["payouts"]}
    assert payouts[agent_a["agent_id"]] == pytest.approx(0.0)   # 10 Lakers x 0
    assert payouts[agent_b["agent_id"]] == pytest.approx(20.0)  # 20 Celtics x 1
    assert exchange.ledger.balance(agent_a["agent_id"]) == pytest.approx(alice_before)
    assert exchange.ledger.balance(agent_b["agent_id"]) == pytest.approx(bob_before + 20.0)
    assert exchange.ledger.position(agent_a["agent_id"], mid, "Lakers") == 0.0
    assert exchange.ledger.position(agent_b["agent_id"], mid, "Celtics") == 0.0
    assert exchange.ledger.conservation()["ok"]


def test_settle_multi_invalid_outcome_rejected(exchange, agent_a, clock):
    m = open_multi_market(exchange, agent_a, clock)
    place(exchange, agent_a,
          make_order_payload(agent_a["agent_id"], m["market_id"], "Lakers", 10), clock)
    clock.advance(days=1, minutes=1)
    with pytest.raises(ExchangeError) as exc:
        exchange.settle_market(m["market_id"], outcome="Knicks",
                               evidence="https://example.com/league/standings")
    assert exc.value.status == 400
    assert "INVALID_SETTLEMENT" in exc.value.reason
    assert exchange.get_market(m["market_id"])["status"] == "OPEN"


def test_settle_multi_twice_rejected(exchange, agent_a, clock):
    m = open_multi_market(exchange, agent_a, clock)
    place(exchange, agent_a,
          make_order_payload(agent_a["agent_id"], m["market_id"], "Lakers", 10), clock)
    clock.advance(days=1, minutes=1)
    settle_multi(exchange, m["market_id"], outcome="Lakers",
                 evidence="https://example.com/league/standings", clock=clock)
    # Second settle: the already-resolved 409 fires before the
    # attestation gate, so this bare call still reads 409.
    with pytest.raises(ExchangeError) as exc:
        exchange.settle_market(m["market_id"], outcome="Lakers",
                               evidence="https://example.com/league/standings")
    assert exc.value.status == 409


def test_creation_payload_binds_outcomes(exchange, agent_a, clock):
    # Sign a creation for one outcome set, submit another: the bound
    # payload must not match, so admission fails (401), nothing created.
    from tests.conftest import signed_creation

    fields_a = make_multi_payload()
    fields_b = make_multi_payload(outcomes=["Lakers", "Celtics", "Knicks"])
    challenge = exchange.issue_challenge()["challenge"]
    pres = signed_creation(agent_a, challenge, fields_a, iso(clock()))
    with pytest.raises(ExchangeError) as exc:
        exchange.create_market(pres, agent_id=agent_a["agent_id"], **fields_b)
    assert exc.value.status == 401
    assert "KYA_PAYLOAD_MISMATCH" in exc.value.reason
    assert exchange.list_markets() == []


def test_conservation_across_multi_trades_and_settlement(exchange, agent_a, agent_b, clock):
    m = open_multi_market(exchange, agent_a, clock)
    mid = m["market_id"]
    place(exchange, agent_a, make_order_payload(agent_a["agent_id"], mid, "Lakers", 10), clock)
    place(exchange, agent_b, make_order_payload(agent_b["agent_id"], mid, "Celtics", 20), clock)
    place(exchange, agent_a, make_order_payload(agent_a["agent_id"], mid, "Other", 5), clock)
    place(exchange, agent_a, make_order_payload(agent_a["agent_id"], mid, "Lakers", 4, action="sell"), clock)
    check = exchange.ledger.conservation()
    assert check["ok"] and check["total"] == pytest.approx(2000.0)
    clock.advance(days=1, minutes=1)
    settle_multi(exchange, mid, outcome="Other",
                 evidence="https://example.com/league/standings", clock=clock)
    check = exchange.ledger.conservation()
    assert check["ok"] and check["total"] == pytest.approx(2000.0)


# -- reconciliation -----------------------------------------------------


def _multi_trade_and_settle(exchange, agent_a, agent_b, clock):
    m = open_multi_market(exchange, agent_a, clock)
    mid = m["market_id"]
    place(exchange, agent_a, make_order_payload(agent_a["agent_id"], mid, "Lakers", 10), clock)
    place(exchange, agent_b, make_order_payload(agent_b["agent_id"], mid, "Celtics", 20), clock)
    clock.advance(days=1, minutes=1)
    receipt = settle_multi(exchange, mid, outcome="Celtics",
                           evidence="https://example.com/league/standings",
                           clock=clock)
    assert receipt["outcome"] == "Celtics"
    return m


def _read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True) + "\n")


def test_multi_state_reconciles(exchange, agent_a, agent_b, clock):
    _multi_trade_and_settle(exchange, agent_a, agent_b, clock)
    report = reconcile(exchange.store.data_dir)
    assert report["ok"] is True
    assert report["mismatches"] == []
    assert report["counts"] == {"trades": 2, "settlements": 1, "markets": 1, "agents": 2}
    assert report["conservation"]["ok"] is True


def test_tampered_multi_cost_detected(exchange, agent_a, agent_b, clock):
    _multi_trade_and_settle(exchange, agent_a, agent_b, clock)
    path = f"{exchange.store.data_dir}/trades.jsonl"
    trades = _read_jsonl(path)
    trades[0]["cost_points"] += 1.0
    _write_jsonl(path, trades)
    report = reconcile(exchange.store.data_dir)
    assert report["ok"] is False
    assert any("cost mismatch" in m for m in report["mismatches"])


def test_tampered_multi_prices_detected(exchange, agent_a, agent_b, clock):
    _multi_trade_and_settle(exchange, agent_a, agent_b, clock)
    path = f"{exchange.store.data_dir}/trades.jsonl"
    trades = _read_jsonl(path)
    trades[0]["prices_after"]["Lakers"] = 0.99
    _write_jsonl(path, trades)
    report = reconcile(exchange.store.data_dir)
    assert report["ok"] is False
    assert any("prices_after" in m for m in report["mismatches"])


def test_tampered_multi_q_detected(exchange, agent_a, agent_b, clock):
    _multi_trade_and_settle(exchange, agent_a, agent_b, clock)
    path = f"{exchange.store.data_dir}/markets.json"
    with open(path, encoding="utf-8") as f:
        markets = json.load(f)
    mid = next(iter(markets))
    markets[mid]["q"][0] += 1.0
    with open(path, "w", encoding="utf-8") as f:
        json.dump(markets, f, indent=2, sort_keys=True)
    report = reconcile(exchange.store.data_dir)
    assert report["ok"] is False
    assert any("q mismatch" in m for m in report["mismatches"])


# -- live HTTP ----------------------------------------------------------

def test_full_http_flow_multi_market(tmp_path, issuer, clock, agent_a, agent_b):
    """Create + trade + settle a 3-outcome market over the real API,
    then check GET /reconcile on the live server."""
    from joulex.client import ExchangeClient
    from joulex.server import make_server

    ex = Exchange(data_dir=str(tmp_path / "data"), now_fn=clock,
                  trusted_issuers=issuer.trust_store)
    ok, reason = ex.verifier.refresh_revocation_list(issuer.revocation_list())
    assert ok, reason
    server = make_server(ex, host="127.0.0.1", port=0, demo_clock=clock)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = ExchangeClient(f"http://127.0.0.1:{server.server_address[1]}")
        market = client.create_market(
            agent_a["credential"], agent_a["agent_seed"], **make_multi_payload())
        mid = market["market_id"]
        assert market["outcomes"] == OUTCOMES_3
        assert market["prices"] == {"Lakers": pytest.approx(1 / 3),
                                    "Celtics": pytest.approx(1 / 3),
                                    "Other": pytest.approx(1 / 3)}
        r = client.place_order(agent_a["credential"], agent_a["agent_seed"],
                               {"agent_id": agent_a["agent_id"], "market_id": mid,
                                "side": "Lakers", "shares": 10, "action": "buy"})
        assert r["prices_after"]["Lakers"] > 1 / 3
        clock.advance(days=1, minutes=1)
        client.refresh_revocation_list(issuer.revocation_list())
        receipt = client.settle(mid, outcome="Lakers",
                                evidence="https://example.com/league/standings",
                                credential=agent_a["credential"], attested=True)
        assert receipt["outcome"] == "Lakers"
        payouts = {p["agent_id"]: p["payout_points"] for p in receipt["payouts"]}
        assert payouts[agent_a["agent_id"]] == pytest.approx(10.0)
        import urllib.request

        with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_address[1]}/reconcile", timeout=10) as resp:
            body = json.loads(resp.read())
        assert body["ok"] is True
    finally:
        server.shutdown()
        server.server_close()
