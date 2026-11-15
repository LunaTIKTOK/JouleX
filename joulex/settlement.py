"""Settlement hooks.

The settlement itself (paying winners 1 point/share, losers 0) lives in
``Ledger.settle_market``. Hooks observe the settlement receipt for
side effects. v1 ships exactly one live hook — the paper one.

- ``PaperSettlementHook``: appends the receipt to
  ``data/settlements.jsonl``. Moves paper points only, inside the ledger.
- ``X402USDCSettlementHook``: DESIGNED BUT INERT. The class exists so
  the adapter shape is on record (payee, USDC amount, x402
  payment-required flow). Every method raises RuntimeError. No keys, no
  wallet, no network calls to any payment rail, no code path that can
  move real money. There is no flag that turns it live; making real
  settlement possible is a code change plus a deliberate human arming
  step, never a runtime toggle.
"""

from __future__ import annotations

from typing import Any


class SettlementHook:
    """Observe a settlement. Called after the ledger has paid out."""

    def on_settlement(self, receipt: dict[str, Any]) -> None:
        raise NotImplementedError


class PaperSettlementHook(SettlementHook):
    def __init__(self, store):
        self._store = store

    def on_settlement(self, receipt: dict[str, Any]) -> None:
        self._store.append_jsonl("settlements.jsonl", receipt)


class X402USDCSettlementHook(SettlementHook):
    """DESIGNED BUT INERT — see module docstring. Never moves money.

    Intended future shape (not implemented): for each payout, convert
    paper points to USDC at a posted rate, build an x402
    payment-required response naming the agent's payee address, and
    settle on-chain only after the x402 facilitator confirms payment.
    None of that exists here on purpose.
    """

    def __init__(self, *args: Any, **kwargs: Any):
        raise RuntimeError(
            "X402USDCSettlementHook is inert in v1: real-money settlement "
            "is not implemented, has no keys, and cannot be enabled by "
            "configuration. See DESIGN.md §12."
        )

    def on_settlement(self, receipt: dict[str, Any]) -> None:
        raise RuntimeError("X402USDCSettlementHook is inert in v1.")
