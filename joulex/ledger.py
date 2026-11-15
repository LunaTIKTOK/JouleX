"""Paper-points ledger.

Rules (see DESIGN.md §6):
- Every agent starts with 1,000 points, credited lazily on first
  verified order (account keyed by agent_id from the credential).
- Buying debits the LMSR cost from the agent and credits the exchange
  pool; selling refunds from the pool. The pool is the market-maker
  account: it absorbs the bounded LMSR subsidy and can go negative.
- Settlement pays exactly 1 point per winning share, 0 per losing
  share, debited from the pool; positions are then zeroed.
- Conservation invariant: sum(balances) + pool == 1,000 * agents,
  at all times. Points are never created or destroyed.
"""

from __future__ import annotations

from typing import Any

STARTING_POINTS = 1000.0
_MONEY_DP = 6


def _r(value: float) -> float:
    return round(float(value), _MONEY_DP)


class Ledger:
    def __init__(self, state: dict[str, Any] | None = None):
        state = state or {}
        self.balances: dict[str, float] = {
            k: float(v) for k, v in state.get("balances", {}).items()
        }
        # positions: {"agent_id|market_id|side": shares}
        self.positions: dict[str, float] = {
            k: float(v) for k, v in state.get("positions", {}).items()
        }
        self.pool: float = float(state.get("pool", 0.0))
        self.agents: list[str] = list(state.get("agents", []))

    def to_dict(self) -> dict[str, Any]:
        return {
            "balances": dict(self.balances),
            "positions": dict(self.positions),
            "pool": self.pool,
            "agents": list(self.agents),
        }

    # -- accounts ------------------------------------------------------
    def ensure_agent(self, agent_id: str) -> None:
        if agent_id not in self.agents:
            self.agents.append(agent_id)
            self.balances[agent_id] = STARTING_POINTS

    def balance(self, agent_id: str) -> float:
        return self.balances.get(agent_id, 0.0)

    # -- positions -----------------------------------------------------
    @staticmethod
    def _pos_key(agent_id: str, market_id: str, side: str) -> str:
        return f"{agent_id}|{market_id}|{side}"

    def position(self, agent_id: str, market_id: str, side: str) -> float:
        return self.positions.get(self._pos_key(agent_id, market_id, side), 0.0)

    def positions_for(self, agent_id: str) -> list[dict[str, Any]]:
        out = []
        for key, shares in sorted(self.positions.items()):
            a, m, s = key.split("|", 2)
            if a == agent_id and abs(shares) > 1e-9:
                out.append({"market_id": m, "side": s, "shares": shares})
        return out

    # -- trades --------------------------------------------------------
    def apply_trade(
        self, *, agent_id: str, market_id: str, side: str, shares: float, cost: float
    ) -> None:
        """Record an executed trade. ``cost`` is signed: positive when
        the agent pays (buy), negative on a sell refund."""
        self.ensure_agent(agent_id)
        self.balances[agent_id] = _r(self.balances[agent_id] - cost)
        self.pool = _r(self.pool + cost)
        key = self._pos_key(agent_id, market_id, side)
        new_shares = _r(self.positions.get(key, 0.0) + shares)
        if abs(new_shares) < 1e-9:
            self.positions.pop(key, None)
        else:
            self.positions[key] = new_shares

    # -- settlement ----------------------------------------------------
    def settle_market(self, market_id: str, outcome: str) -> list[dict[str, Any]]:
        """Pay out one market. Winners get 1 point/share, losers 0.
        Positions for the market are zeroed. Returns payout records."""
        payouts: list[dict[str, Any]] = []
        doomed = [k for k in self.positions if k.split("|", 2)[1] == market_id]
        for key in sorted(doomed):
            agent_id, _, side = key.split("|", 2)
            shares = self.positions.pop(key)
            payout = _r(shares) if side == outcome else 0.0
            if payout:
                self.balances[agent_id] = _r(self.balances.get(agent_id, 0.0) + payout)
                self.pool = _r(self.pool - payout)
            payouts.append(
                {
                    "agent_id": agent_id,
                    "market_id": market_id,
                    "side": side,
                    "shares": shares,
                    "payout_points": payout,
                }
            )
        return payouts

    # -- invariant -----------------------------------------------------
    def conservation(self) -> dict[str, Any]:
        total = _r(sum(self.balances.values()) + self.pool)
        expected = _r(STARTING_POINTS * len(self.agents))
        return {
            "ok": abs(total - expected) < 1e-6,
            "total": total,
            "expected": expected,
            "pool": self.pool,
            "agents": len(self.agents),
        }
