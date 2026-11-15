"""LMSR (logarithmic market scoring rule) automated market maker.

One deterministic mechanism for all markets, binary or multi-outcome.
Prices start at 1/N each with zero outstanding shares. Buying shares
of an outcome costs ``C(q + delta) - C(q)``; selling refunds exactly
that difference (no other trades in between). The market maker's
worst-case loss per market is bounded by ``b * ln N``.

The binary market is the N=2 case of the generalized rule — the
``*_multi`` functions below are the single code path, and the binary
helpers are exact specializations of them (see
tests/test_multi_outcome.py::test_multi_n2_matches_binary_exactly).

All point amounts are rounded to 6 decimal places at the trade boundary
so a buy-then-sell round trip returns the cost exactly.
"""

from __future__ import annotations

import math
from typing import Sequence

DEFAULT_B = 100.0
MONEY_DP = 6
SIDES = ("YES", "NO")


def _round_money(value: float) -> float:
    return round(float(value), MONEY_DP)


def cost_function(q_yes: float, q_no: float, b: float = DEFAULT_B) -> float:
    """C(q) = b * ln(exp(q_yes/b) + exp(q_no/b)), overflow-safe."""
    if b <= 0:
        raise ValueError("liquidity parameter b must be > 0")
    zy, zn = q_yes / b, q_no / b
    m = max(zy, zn)
    return b * (m + math.log(math.exp(zy - m) + math.exp(zn - m)))


def price_yes(q_yes: float, q_no: float, b: float = DEFAULT_B) -> float:
    """Implied probability of YES. Sigmoid form: stable for large q."""
    if b <= 0:
        raise ValueError("liquidity parameter b must be > 0")
    z = (q_yes - q_no) / b
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    ez = math.exp(z)
    return ez / (1.0 + ez)


def price(q_yes: float, q_no: float, b: float, side: str) -> float:
    if side == "YES":
        return price_yes(q_yes, q_no, b)
    if side == "NO":
        return 1.0 - price_yes(q_yes, q_no, b)
    raise ValueError(f"side must be YES or NO, got {side!r}")


def apply_trade(q_yes: float, q_no: float, side: str, shares: float) -> tuple[float, float]:
    """Outstanding shares after trading ``shares`` of ``side``.

    Positive shares = buy, negative = sell. Does not check whether the
    seller holds the shares — the ledger layer enforces that.
    """
    if side == "YES":
        return q_yes + shares, q_no
    if side == "NO":
        return q_yes, q_no + shares
    raise ValueError(f"side must be YES or NO, got {side!r}")


def trade_cost(q_yes: float, q_no: float, b: float, side: str, shares: float) -> float:
    """Signed point cost of trading ``shares`` of ``side``.

    Positive = the trader pays; negative = the trader is refunded
    (a sell). Rounded to 6dp; a buy followed by the exact reverse sell
    returns the cost exactly.
    """
    new_qy, new_qn = apply_trade(q_yes, q_no, side, shares)
    return _round_money(cost_function(new_qy, new_qn, b) - cost_function(q_yes, q_no, b))


# -- generalized LMSR (M6): N outcomes ---------------------------------

def cost_function_multi(q: Sequence[float], b: float = DEFAULT_B) -> float:
    """C(q) = b * ln(sum_i exp(q_i/b)), overflow-safe (max-shift trick).

    ``q`` is the outstanding-share vector, one entry per outcome.
    At N=2 this is exactly ``cost_function`` (same arithmetic).
    """
    if b <= 0:
        raise ValueError("liquidity parameter b must be > 0")
    if len(q) < 2:
        raise ValueError("multi-outcome LMSR needs at least 2 outcomes")
    m = max(float(x) / b for x in q)
    return b * (m + math.log(sum(math.exp(float(x) / b - m) for x in q)))


def prices_multi(q: Sequence[float], b: float = DEFAULT_B) -> list[float]:
    """Implied probability of each outcome (softmax). Sums to 1."""
    if b <= 0:
        raise ValueError("liquidity parameter b must be > 0")
    if len(q) < 2:
        raise ValueError("multi-outcome LMSR needs at least 2 outcomes")
    m = max(float(x) / b for x in q)
    weights = [math.exp(float(x) / b - m) for x in q]
    total = sum(weights)
    return [w / total for w in weights]


def apply_trade_multi(
    q: Sequence[float], index: int, shares: float
) -> list[float]:
    """Outstanding shares after trading ``shares`` of outcome ``index``.

    Positive shares = buy, negative = sell. Returns a NEW list; the
    input is never mutated. Does not check whether the seller holds
    the shares — the ledger layer enforces that.
    """
    if not 0 <= index < len(q):
        raise ValueError(f"outcome index {index} out of range for {len(q)} outcomes")
    out = [float(x) for x in q]
    out[index] += shares
    return out


def trade_cost_multi(
    q: Sequence[float], b: float, index: int, shares: float
) -> float:
    """Signed point cost of trading ``shares`` of outcome ``index``.

    Positive = the trader pays; negative = the trader is refunded
    (a sell). Rounded to 6dp; a buy followed by the exact reverse sell
    returns the cost exactly. At N=2 this equals ``trade_cost`` exactly.
    """
    if not 0 <= index < len(q):
        raise ValueError(f"outcome index {index} out of range for {len(q)} outcomes")
    new_q = apply_trade_multi(q, index, shares)
    return _round_money(cost_function_multi(new_q, b) - cost_function_multi(q, b))
