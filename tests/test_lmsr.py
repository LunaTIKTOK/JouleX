"""LMSR invariant tests.

The cost function is the whole matching engine, so it gets the strictest
tests: known values, round-trip exactness, price monotonicity, bounds.
"""

from __future__ import annotations

import math

import pytest

from joulex.lmsr import (
    DEFAULT_B,
    apply_trade,
    cost_function,
    price_yes,
    trade_cost,
)


def test_cost_at_zero_is_b_ln2():
    assert cost_function(0.0, 0.0, 100.0) == pytest.approx(100.0 * math.log(2))


def test_price_at_zero_is_half():
    assert price_yes(0.0, 0.0, 100.0) == pytest.approx(0.5)


def test_price_rises_after_yes_buy():
    qy, qn = apply_trade(0.0, 0.0, "YES", 10)
    assert price_yes(qy, qn, 100.0) > 0.5
    qy2, qn2 = apply_trade(qy, qn, "NO", 10)
    assert price_yes(qy2, qn2, 100.0) == pytest.approx(0.5)


def test_buy_then_sell_same_shares_returns_cost_exactly():
    # The headline invariant: no other trades in between, the round trip
    # costs exactly zero (within float rounding, asserted exactly here
    # because the two cost evaluations are exact negations).
    cost_buy = trade_cost(0.0, 0.0, 100.0, "YES", 10)
    assert cost_buy > 0
    qy, qn = apply_trade(0.0, 0.0, "YES", 10)
    cost_sell = trade_cost(qy, qn, 100.0, "YES", -10)
    assert cost_sell == -cost_buy


def test_buy_cost_increases_with_size():
    c1 = trade_cost(0.0, 0.0, 100.0, "YES", 10)
    c2 = trade_cost(0.0, 0.0, 100.0, "YES", 20)
    assert 0 < c1 < c2 < 20  # never more than 1 point/share
    # LMSR cost is CONVEX in size (average price rises as you buy), so
    # 20 shares cost MORE than twice 10 shares.
    assert c2 > 2 * c1


def test_price_bounded_strictly_inside_unit_interval():
    qy, qn = apply_trade(0.0, 0.0, "YES", 1_000)
    assert 0.5 < price_yes(qy, qn, 100.0) < 1.0
    qy, qn = apply_trade(0.0, 0.0, "NO", 1_000)
    assert 0.0 < price_yes(qy, qn, 100.0) < 0.5


def test_large_buy_cost_bounded_by_shares():
    # Even a huge buy can never cost more than 1 point per share.
    assert trade_cost(0.0, 0.0, 100.0, "YES", 10_000) < 10_000


def test_sell_from_a_position_mirrors_the_buy():
    # Selling 10 YES out of a book holding 10 YES refunds exactly the
    # buy cost (same two cost evaluations, opposite sign). Selling from
    # a FLAT book is a different trade (it moves q below zero) and is
    # not symmetric — the exchange forbids it at the ledger layer.
    assert trade_cost(10.0, 0.0, 100.0, "YES", -10) == -trade_cost(
        0.0, 0.0, 100.0, "YES", 10
    )


def test_apply_trade_updates_only_the_traded_side():
    qy, qn = apply_trade(3.0, 7.0, "NO", 5)
    assert (qy, qn) == (3.0, 12.0)
    qy, qn = apply_trade(3.0, 7.0, "YES", -2)
    assert (qy, qn) == (1.0, 7.0)


def test_invalid_side_rejected():
    with pytest.raises(ValueError):
        trade_cost(0.0, 0.0, 100.0, "MAYBE", 10)
    with pytest.raises(ValueError):
        apply_trade(0.0, 0.0, "MAYBE", 10)


def test_zero_shares_costs_zero():
    assert trade_cost(5.0, 5.0, 100.0, "YES", 0) == 0.0


def test_default_b_is_100():
    assert DEFAULT_B == 100.0
    # Maker worst-case loss b*ln2 at b=100 is ~69.31 points.
    assert 100.0 * math.log(2) == pytest.approx(69.31471805599453)
