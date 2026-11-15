"""Decision-basis capture for settlement (M4 chunk 2).

A settlement receipt is only auditable if a third party can re-derive
what the settler knew and which inputs the outcome depended on. The
decision basis records, at settlement time:

- ``oracle``: an HONEST label for where the observed value came from
  (``AESO-live`` / ``SYNTHETIC`` / ``OPERATOR_VALUE`` /
  ``PUBLIC_SOURCE``) plus the snapshot the oracle actually returned;
- ``credential_id``: who attested the settlement (or ``"operator"``);
- ``market_state_hash``: SHA-256 of the canonical market state at
  settlement time, so a later reader can tell whether the market
  record has been rewritten since;
- ``inputs_hash``: SHA-256 of the canonical bytes of the whole basis
  excluding ``inputs_hash`` itself — anyone holding the stored basis
  can recompute it.

Honest labels only: synthetic data is never labelled live, and a
timestamp the oracle never supplied is never invented (``fetched_at``
is ``None`` when the oracle does not expose one).
"""

from __future__ import annotations

import hashlib
from typing import Any

from .attestation import canonical_bytes


def market_state_hash(market: dict[str, Any]) -> str:
    """SHA-256 hex of the canonical JSON bytes of the market state
    that settlement depends on: id, outcomes, share vector, prices,
    status — at settlement time (the market is still OPEN here)."""
    state = {
        "market_id": market.get("market_id"),
        "outcomes": market.get("outcomes"),
        "q": market.get("q"),
        "prices": market.get("prices"),
        "status": market.get("status"),
    }
    return hashlib.sha256(canonical_bytes(state)).hexdigest()


def capture_decision_basis(
    *,
    oracle_label: str,
    oracle_snapshot: dict[str, Any],
    credential_id: str,
    market: dict[str, Any],
) -> dict[str, Any]:
    """Build the decision basis for one settlement.

    ``inputs_hash`` is computed over the basis WITHOUT itself, then
    added — so the stored basis is self-verifying: pop
    ``inputs_hash`` and re-hash the rest."""
    basis: dict[str, Any] = {
        "oracle": {
            "label": str(oracle_label),
            "snapshot": dict(oracle_snapshot),
        },
        "credential_id": str(credential_id),
        "market_state_hash": market_state_hash(market),
    }
    basis["inputs_hash"] = hashlib.sha256(
        canonical_bytes(basis)
    ).hexdigest()
    return basis
