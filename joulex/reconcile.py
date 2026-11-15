"""Reconciliation + audit trail (M5).

The append-only logs (``trades.jsonl`` + ``settlements.jsonl``) are the
audit trail: every executed order and every settlement leaves one
receipt line. ``reconcile()`` re-derives the whole money state from
those logs — balances, positions, pool, per-market share counts — and
compares it against the stored ``ledger.json`` / ``markets.json``.
Any divergence is a mismatch, reported loudly (the CLI exits non-zero).

Checks, in replay order:
- every trade receipt has the required fields, a unique ``trade_id``,
  a known market, a valid side/action, and shares > 0; the side must
  be one of the market's outcomes;
- each trade's ``cost_points`` equals the generalized-LMSR cost
  recomputed from the replayed share vector (``trade_cost_multi``),
  its price claim(s) equal the recomputed prices (``price_yes_after``
  on a binary market, ``prices_after`` on a multi-outcome market —
  and ``prices_after`` whenever present on a binary receipt too),
  and its ``balance_after`` equals the replayed balance;
- no trade for a market after that market's ``settled_at``, and no
  sell that drives a replayed position negative;
- exactly one settlement receipt per settled market, and every
  RESOLVED market has one; the receipt's outcome is re-derived from
  the market's stated rule (``resolve_outcome`` — deterministic for
  ``aeso_price``; for ``public_source`` the outcome must be one of
  the market's outcomes), its outcome / observed value / evidence /
  settled_at match the market record, and its payouts match the
  replayed settlement exactly (1 point per winning share, 0 per
  losing share);
- final replayed balances / positions / pool / agent set equal the
  stored ledger, and per-market share vector ``q`` / ``prices`` (plus
  the binary mirrors ``q_yes`` / ``q_no`` / ``price_yes`` where the
  market carries them) equal the replayed share counts.

Unreadable or corrupt state files raise ``ReconciliationError`` — the
reconciler refuses to bless state it cannot read, the same fail-closed
posture as the exchange refusing to start on corrupt state. A
well-formed log that disagrees with stored state is a *mismatch*,
not an error: it is collected and reported, never raised, so one
tampered line cannot hide the others.

Replay order note: all trades are replayed first, then all
settlements. That is equivalent to the live interleaving because a
settlement touches only its own market's positions, and no trade for
a market may occur after that market settles (checked above).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any

from .ledger import STARTING_POINTS, Ledger
from .lmsr import apply_trade_multi, prices_multi, trade_cost_multi
from .markets import BINARY_OUTCOMES, market_outcomes, market_q, parse_time, resolve_outcome
from .store import JsonStore

_TOL = 1e-6
# Required on every trade receipt. Price claims are conditional on the
# market shape (binary: price_yes_after; multi-outcome: prices_after)
# and are checked after the market is known — see the replay loop.
_TRADE_FIELDS = (
    "trade_id", "timestamp", "market_id", "agent_id", "side", "action",
    "shares", "cost_points", "balance_after",
)
_SETTLEMENT_FIELDS = (
    "market_id", "outcome", "observed_value", "evidence", "settled_at",
    "payouts",
)


class ReconciliationError(Exception):
    """State files are unreadable or corrupt — reconciliation cannot run."""


def _close(a: float, b: float) -> bool:
    return abs(float(a) - float(b)) <= _TOL


def _load_or_raise(store: JsonStore, name: str, default: Any) -> Any:
    try:
        return store.load_json(name, default)
    except (ValueError, OSError) as exc:
        raise ReconciliationError(f"cannot read {name}: {exc}") from exc


def _read_jsonl_or_raise(store: JsonStore, name: str) -> list[Any]:
    try:
        return store.read_jsonl(name)
    except (ValueError, OSError) as exc:
        raise ReconciliationError(f"cannot read {name}: {exc}") from exc


def reconcile(data_dir: str) -> dict[str, Any]:
    """Re-derive money state from the logs and compare with stored state.

    Returns a report ``{"ok", "checked_at", "counts", "mismatches",
    "conservation"}``. ``ok`` is True only when the logs re-derive the
    stored state exactly (within 1e-6) with no other mismatch.
    Raises ``ReconciliationError`` when the state files cannot be read.
    """
    store = JsonStore(data_dir)
    mismatches: list[str] = []

    ledger_state = _load_or_raise(store, "ledger.json", {})
    markets_state = _load_or_raise(store, "markets.json", {})
    if not isinstance(ledger_state, dict):
        raise ReconciliationError("ledger.json is not an object")
    if not isinstance(markets_state, dict):
        raise ReconciliationError("markets.json is not an object")
    trades = _read_jsonl_or_raise(store, "trades.jsonl")
    settlements = _read_jsonl_or_raise(store, "settlements.jsonl")

    replay = Ledger()
    # Per-market replayed share vectors, from all zeros like a new
    # market. Sized per market from its outcomes (legacy binary
    # records default to ["YES", "NO"]).
    q_state: dict[str, list[float]] = {}

    # -- replay trades -------------------------------------------------
    seen_trade_ids: set[str] = set()
    for n, trade in enumerate(trades):
        if not isinstance(trade, dict):
            mismatches.append(f"trade line {n}: not a JSON object")
            continue
        tid = trade.get("trade_id", f"<line {n}>")
        missing = [f for f in _TRADE_FIELDS if f not in trade]
        if missing:
            mismatches.append(f"trade {tid}: missing fields {sorted(missing)}")
            continue
        if tid in seen_trade_ids:
            mismatches.append(f"duplicate trade_id {tid}: receipt appears twice")
            continue
        seen_trade_ids.add(tid)
        market_id = str(trade["market_id"])
        market = markets_state.get(market_id)
        if market is None:
            mismatches.append(f"trade {tid}: unknown market {market_id}")
            continue
        outcomes = market_outcomes(market)
        is_binary = outcomes == BINARY_OUTCOMES
        # Price claims are conditional on the market shape: a binary
        # trade must carry price_yes_after; a multi-outcome trade must
        # carry prices_after. (The exchange also emits prices_after on
        # binary receipts; when present it is checked too.)
        price_fields = ["price_yes_after"] if is_binary else ["prices_after"]
        missing_price = [f for f in price_fields if f not in trade]
        if missing_price:
            mismatches.append(f"trade {tid}: missing fields {sorted(missing_price)}")
            continue
        agent_id = str(trade["agent_id"])
        side = str(trade["side"])
        action = str(trade["action"])
        try:
            shares = float(trade["shares"])
            cost = float(trade["cost_points"])
            balance_after = float(trade["balance_after"])
            traded_at = parse_time(str(trade["timestamp"]))
            price_after = float(trade["price_yes_after"]) if is_binary else None
            prices_after = trade.get("prices_after")
            if prices_after is not None:
                if not isinstance(prices_after, dict):
                    raise ValueError("prices_after is not an object")
                prices_after = {str(k): float(v) for k, v in prices_after.items()}
        except (TypeError, ValueError):
            mismatches.append(f"trade {tid}: non-numeric shares/cost/price/balance or bad timestamp")
            continue
        if side not in outcomes:
            mismatches.append(f"trade {tid}: invalid side {side!r} for outcomes {outcomes}")
            continue
        if action not in ("buy", "sell"):
            mismatches.append(f"trade {tid}: invalid action {action!r}")
            continue
        if shares <= 0:
            mismatches.append(f"trade {tid}: shares must be > 0, got {shares}")
            continue
        # No trade for a market after it settled. A RESOLVED market
        # with no settled_at is itself corrupt: the trade cannot be
        # proven to predate settlement, so it is a mismatch rather
        # than silently replayed.
        status = market.get("status")
        settled_at = market.get("settled_at")
        if status != "OPEN":
            if not settled_at:
                mismatches.append(
                    f"trade {tid} for market {market_id} with status {status} "
                    f"but no settled_at"
                )
                continue
            try:
                if traded_at > parse_time(str(settled_at)):
                    mismatches.append(
                        f"trade {tid} for market {market_id} after settlement at {settled_at}"
                    )
                    continue
            except ValueError:
                mismatches.append(f"market {market_id}: bad settled_at {settled_at!r}")
                continue
        signed_shares = shares if action == "buy" else -shares
        # A sell must not drive the replayed position negative.
        if action == "sell" and replay.position(agent_id, market_id, side) < shares - _TOL:
            mismatches.append(
                f"trade {tid}: sell of {shares} {side} exceeds replayed position "
                f"{replay.position(agent_id, market_id, side)}"
            )
            continue
        q = q_state.get(market_id, [0.0] * len(outcomes))
        b = float(market["b"])
        index = outcomes.index(side)
        expected_cost = trade_cost_multi(q, b, index, signed_shares)
        if not _close(cost, expected_cost):
            mismatches.append(
                f"trade {tid}: cost mismatch: cost_points {cost} != LMSR cost {expected_cost} "
                f"from replayed q={q}"
            )
            continue
        # Apply to the replay ledger, then check the receipt's own
        # price/balance claims against the replayed state.
        q_state[market_id] = apply_trade_multi(q, index, signed_shares)
        replay.apply_trade(
            agent_id=agent_id, market_id=market_id, side=side,
            shares=signed_shares, cost=cost,
        )
        # The exchange refuses orders the agent cannot afford (403),
        # so a negative replayed balance is always a violation — a
        # log line that spends money the agent never had.
        if replay.balance(agent_id) < -_TOL:
            mismatches.append(
                f"trade {tid}: negative balance {replay.balance(agent_id)} "
                f"for agent {agent_id}"
            )
        expected_prices = dict(zip(outcomes, prices_multi(q_state[market_id], b)))
        if is_binary and price_after is not None:
            if not _close(price_after, expected_prices["YES"]):
                mismatches.append(
                    f"trade {tid}: price_yes_after {price_after} != recomputed "
                    f"{expected_prices['YES']}"
                )
        if prices_after is not None:
            bad = [
                o for o in outcomes
                if o not in prices_after or not _close(prices_after[o], expected_prices[o])
            ]
            if bad:
                mismatches.append(
                    f"trade {tid}: prices_after mismatch for {bad}: "
                    f"{prices_after} != recomputed {expected_prices}"
                )
        if not _close(balance_after, replay.balance(agent_id)):
            mismatches.append(
                f"trade {tid}: balance_after {balance_after} != replayed balance "
                f"{replay.balance(agent_id)} for agent {agent_id}"
            )

    # -- replay settlements --------------------------------------------
    seen_settlements: set[str] = set()
    for n, receipt in enumerate(settlements):
        if not isinstance(receipt, dict):
            mismatches.append(f"settlement line {n}: not a JSON object")
            continue
        market_id = str(receipt.get("market_id", f"<line {n}>"))
        missing = [f for f in _SETTLEMENT_FIELDS if f not in receipt]
        if missing:
            mismatches.append(f"settlement {market_id}: missing fields {sorted(missing)}")
            continue
        if market_id in seen_settlements:
            mismatches.append(f"duplicate settlement receipt for market {market_id}")
            continue
        seen_settlements.add(market_id)
        market = markets_state.get(market_id)
        if market is None:
            mismatches.append(f"settlement {market_id}: unknown market")
            continue
        if market.get("status") != "RESOLVED":
            mismatches.append(
                f"settlement {market_id}: market status is {market.get('status')}, not RESOLVED"
            )
            continue
        # The receipt must agree with the market record.
        for field in ("outcome", "observed_value", "evidence", "settled_at"):
            if receipt.get(field) != market.get(field):
                mismatches.append(
                    f"settlement {market_id}: {field} mismatch: receipt {receipt.get(field)!r} "
                    f"!= market record {market.get(field)!r}"
                )
        # The outcome must follow the market's stated rule. For
        # aeso_price it is deterministic from the observed max pool
        # price; for public_source the rule is the operator-attested
        # outcome, so only membership in the market's outcomes can be
        # re-derived (resolve_outcome enforces it).
        try:
            derived = resolve_outcome(
                market,
                observed_value=receipt.get("observed_value"),
                outcome=receipt.get("outcome"),
            )
        except ValueError as exc:
            mismatches.append(f"settlement {market_id}: outcome cannot be re-derived: {exc}")
            continue
        if derived != receipt.get("outcome"):
            mismatches.append(
                f"settlement {market_id}: outcome mismatch: receipt {receipt.get('outcome')!r} "
                f"!= derived {derived!r} from observed_value {receipt.get('observed_value')!r}"
            )
            continue
        # Settling before resolves_at is impossible through the API.
        try:
            if parse_time(str(receipt["settled_at"])) < parse_time(str(market["resolves_at"])):
                mismatches.append(
                    f"settlement {market_id}: settled_at {receipt['settled_at']} is before "
                    f"resolves_at {market['resolves_at']}"
                )
        except ValueError:
            mismatches.append(f"settlement {market_id}: bad settled_at/resolves_at timestamp")
        # Payouts must match the replayed settlement exactly.
        expected_payouts = replay.settle_market(market_id, str(receipt["outcome"]))
        recorded = receipt.get("payouts")
        if not isinstance(recorded, list):
            mismatches.append(f"settlement {market_id}: payouts is not a list")
            continue
        key = lambda p: (str(p.get("agent_id")), str(p.get("side")))
        got = sorted(
            (
                (str(p["agent_id"]), str(p["side"]), float(p["shares"]), float(p["payout_points"]))
                for p in expected_payouts
            ),
            key=lambda t: (t[0], t[1]),
        )
        try:
            rec = sorted(
                (
                    (str(p["agent_id"]), str(p["side"]), float(p["shares"]), float(p["payout_points"]))
                    for p in recorded
                ),
                key=lambda t: (t[0], t[1]),
            )
        except (KeyError, TypeError, ValueError):
            mismatches.append(f"settlement {market_id}: malformed payout entries")
            continue
        if len(rec) != len(got) or any(
            g[0] != r[0] or g[1] != r[1] or not _close(g[2], r[2]) or not _close(g[3], r[3])
            for g, r in zip(got, rec)
        ):
            mismatches.append(
                f"settlement {market_id}: payout mismatch: receipt payouts {recorded} "
                f"!= replayed payouts {expected_payouts}"
            )

    # Every RESOLVED market must have exactly one settlement receipt.
    for market_id, market in markets_state.items():
        if market.get("status") == "RESOLVED" and market_id not in seen_settlements:
            mismatches.append(f"market {market_id} is RESOLVED but has no settlement receipt")

    # -- final state comparison ----------------------------------------
    stored = Ledger(ledger_state)
    for agent_id, balance in stored.balances.items():
        # The pool may legitimately go negative (bounded LMSR subsidy);
        # an agent balance may not — the exchange refuses unaffordable
        # orders, so a stored negative balance is tampering or a bug.
        if balance < -_TOL:
            mismatches.append(f"negative balance for agent {agent_id}: stored {balance}")
    for agent_id, balance in stored.balances.items():
        if agent_id not in replay.balances:
            mismatches.append(f"balance mismatch for agent {agent_id}: stored {balance} but no replayed account")
        elif not _close(balance, replay.balance(agent_id)):
            mismatches.append(
                f"balance mismatch for agent {agent_id}: stored {balance} != replayed {replay.balance(agent_id)}"
            )
    for agent_id in replay.balances:
        if agent_id not in stored.balances:
            mismatches.append(f"balance mismatch for agent {agent_id}: replayed {replay.balance(agent_id)} but not stored")
    if sorted(stored.agents) != sorted(replay.agents):
        mismatches.append(
            f"agent set mismatch: stored {sorted(stored.agents)} != replayed {sorted(replay.agents)}"
        )
    if not _close(stored.pool, replay.pool):
        mismatches.append(f"pool mismatch: stored {stored.pool} != replayed {replay.pool}")
    for key, shares in stored.positions.items():
        if key not in replay.positions:
            mismatches.append(f"position mismatch for {key}: stored {shares} but replayed 0")
        elif not _close(shares, replay.positions[key]):
            mismatches.append(
                f"position mismatch for {key}: stored {shares} != replayed {replay.positions[key]}"
            )
    for key in replay.positions:
        if key not in stored.positions:
            mismatches.append(f"position mismatch for {key}: replayed {replay.positions[key]} but not stored")
    for market_id, market in markets_state.items():
        outcomes = market_outcomes(market)
        q = q_state.get(market_id, [0.0] * len(outcomes))
        stored_q = market.get("q")
        if stored_q is not None:
            if len(stored_q) != len(q) or any(
                not _close(float(s), r) for s, r in zip(stored_q, q)
            ):
                mismatches.append(
                    f"market {market_id}: q mismatch: stored {stored_q} != replayed {q}"
                )
        expected_prices = dict(zip(outcomes, prices_multi(q, float(market["b"]))))
        stored_prices = market.get("prices")
        if stored_prices is not None:
            bad = [
                o for o in outcomes
                if o not in stored_prices or not _close(float(stored_prices[o]), expected_prices[o])
            ]
            if bad:
                mismatches.append(
                    f"market {market_id}: prices mismatch for {bad}: "
                    f"stored {stored_prices} != replayed {expected_prices}"
                )
        # Binary mirrors, where the market carries them (all binary
        # markets do; multi-outcome markets do not).
        if outcomes == BINARY_OUTCOMES and "q_yes" in market:
            if not _close(market.get("q_yes", 0.0), q[0]):
                mismatches.append(
                    f"market {market_id}: q_yes mismatch: stored {market.get('q_yes')} != replayed {q[0]}"
                )
            if not _close(market.get("q_no", 0.0), q[1]):
                mismatches.append(
                    f"market {market_id}: q_no mismatch: stored {market.get('q_no')} != replayed {q[1]}"
                )
            if not _close(market.get("price_yes", 0.5), expected_prices["YES"]):
                mismatches.append(
                    f"market {market_id}: price_yes mismatch: stored {market.get('price_yes')} "
                    f"!= replayed {expected_prices['YES']}"
                )

    conservation = replay.conservation()
    return {
        "ok": not mismatches,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "counts": {
            "trades": len(trades),
            "settlements": len(settlements),
            "markets": len(markets_state),
            "agents": len(replay.agents),
        },
        "mismatches": mismatches,
        "conservation": conservation,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile the agent exchange: re-derive every balance "
        "from trades.jsonl + settlements.jsonl and fail loudly on any "
        "mismatch with the stored ledger/markets."
    )
    parser.add_argument("--data-dir", default="data",
                        help="exchange data directory (default: ./data)")
    args = parser.parse_args(argv)
    try:
        report = reconcile(args.data_dir)
    except ReconciliationError as exc:
        print(f"RECONCILIATION ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    if not report["ok"]:
        print(
            f"RECONCILIATION FAILED: {len(report['mismatches'])} mismatch(es)",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
