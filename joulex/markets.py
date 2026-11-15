"""Markets: creation validation and resolution rules.

A market is a plain dict (JSON-native, so it persists as-is). Two
resolution types in v1:

- ``aeso_price``: "Will the Alberta pool price exceed <strike> $/MWh at
  any point during window [window_start, window_end]?" Settlement is
  deterministic from the observed MAX hourly pool price over the
  window: YES iff max > strike. A tie resolves NO. Binary only.
  The observed series (or its max) and source URL are recorded on the
  market at settlement, so any third party can re-derive the outcome.
- ``public_source``: any other event, binary or multi-outcome.
  Creation must name a public source URL and a one-line resolution
  rule. Settlement is operator-attested in v1 (outcome + evidence
  recorded on the market); the outcome must be one of the market's
  ``outcomes``.

Contract types (``contract_type``, stated at creation): ``flex_capacity``
(a flexibility / curtailment contract, normally ``aeso_price``),
``compute_forward`` (a forward price contract on GPU-hours, normally
``public_source``), ``generic`` (any other event, ``public_source``
only in practice). The type is descriptive in v1 — it does not change
the resolution rule — but it is bound by the creation presentation,
so a market cannot be re-typed after the fact.

Multi-outcome: a market carries an ``outcomes`` list (2–10 labels), a
share vector ``q`` parallel to it, and a ``prices`` map. Binary markets
also carry the legacy mirrors ``q_yes`` / ``q_no`` / ``price_yes``.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from .lmsr import DEFAULT_B, prices_multi

RESOLUTION_TYPES = ("aeso_price", "public_source")
CONTRACT_TYPES = ("flex_capacity", "compute_forward", "generic")
MIN_RESOLVE = timedelta(days=1)
MAX_RESOLVE = timedelta(days=30)
MIN_OUTCOMES = 2
MAX_OUTCOMES = 10
MAX_LABEL_LEN = 64
BINARY_OUTCOMES = ["YES", "NO"]


def normalize_outcomes(outcomes: Any) -> list[str]:
    """Validate + normalize an ``outcomes`` list (M6).

    ``None`` means the default binary market. Labels are stripped;
    a binary pair is normalized to ``["YES", "NO"]`` order so the
    share vector always lines up ``q[0] = YES, q[1] = NO``. Raises
    ValueError on any invalid shape. This is the single normalization
    used by ``new_market`` and by the creation-payload binding in
    ``admission.py`` — the two must never diverge.
    """
    if outcomes is None:
        return list(BINARY_OUTCOMES)
    if not isinstance(outcomes, list):
        raise ValueError("outcomes must be a list of outcome labels")
    if len(outcomes) < MIN_OUTCOMES:
        raise ValueError(f"outcomes must list at least {MIN_OUTCOMES} outcomes")
    if len(outcomes) > MAX_OUTCOMES:
        raise ValueError(f"outcomes must list at most {MAX_OUTCOMES} outcomes")
    labels: list[str] = []
    for label in outcomes:
        if not isinstance(label, str):
            raise ValueError("outcome labels must be strings")
        label = label.strip()
        if not label:
            raise ValueError("outcome labels must be non-empty")
        if len(label) > MAX_LABEL_LEN:
            raise ValueError(f"outcome labels must be at most {MAX_LABEL_LEN} characters")
        if "|" in label:
            # The ledger keys positions by agent_id|market_id|side.
            raise ValueError("outcome labels must not contain '|'")
        labels.append(label)
    if len(set(labels)) != len(labels):
        raise ValueError("outcome labels must be distinct")
    # YES/NO are the canonical binary labels: any casing of the pair
    # ("yes"/"no", "Yes"/"No", ...) is the same binary market. Other
    # labels keep their case ("Lakers" stays "Lakers").
    if {label.casefold() for label in labels} == {"yes", "no"}:
        return list(BINARY_OUTCOMES)
    return labels


def market_outcomes(market: dict[str, Any]) -> list[str]:
    """A market's outcomes, defaulting to binary for records written
    before M6 (which carry no ``outcomes`` field)."""
    outcomes = market.get("outcomes")
    if outcomes is None:
        return list(BINARY_OUTCOMES)
    return [str(o) for o in outcomes]


def is_binary_market(market: dict[str, Any]) -> bool:
    return market_outcomes(market) == BINARY_OUTCOMES


def market_q(market: dict[str, Any]) -> list[float]:
    """A market's share vector, rebuilt from the binary mirrors for
    records written before M6."""
    q = market.get("q")
    if q is not None:
        return [float(x) for x in q]
    return [float(market.get("q_yes", 0.0)), float(market.get("q_no", 0.0))]


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone information")
    return parsed


def new_market(
    *,
    question: str,
    resolution_type: str,
    resolves_at: str,
    now: datetime,
    contract_type: str | None = None,
    strike: float | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
    source_url: str | None = None,
    source_description: str | None = None,
    outcomes: list[str] | None = None,
    b: float = DEFAULT_B,
) -> dict[str, Any]:
    """Validate and build a market record. Raises ValueError on any
    invalid field; the Exchange maps that to a 400."""
    if not question or not question.strip():
        raise ValueError("question must be non-empty")
    if resolution_type not in RESOLUTION_TYPES:
        raise ValueError(f"resolution_type must be one of {RESOLUTION_TYPES}")
    if contract_type is None:
        contract_type = "generic"
    if contract_type not in CONTRACT_TYPES:
        raise ValueError(f"contract_type must be one of {CONTRACT_TYPES}")
    try:
        resolves = parse_time(resolves_at)
    except ValueError:
        raise ValueError("resolves_at must be an ISO-8601 timestamp with timezone") from None
    horizon = resolves - now
    if horizon < MIN_RESOLVE:
        raise ValueError("resolves_at must be at least 1 day in the future")
    if horizon > MAX_RESOLVE:
        raise ValueError("resolves_at must be at most 30 days in the future")
    if b <= 0:
        raise ValueError("liquidity parameter b must be > 0")
    outcomes = normalize_outcomes(outcomes)

    if resolution_type == "aeso_price":
        if outcomes != BINARY_OUTCOMES:
            raise ValueError("aeso_price markets are binary (outcomes YES/NO only)")
        if strike is None:
            raise ValueError("aeso_price markets require strike")
        try:
            strike = float(strike)
        except (TypeError, ValueError):
            raise ValueError("strike must be a number") from None
        if not window_start or not window_end:
            raise ValueError("aeso_price markets require window_start and window_end")
        try:
            start = parse_time(window_start)
            end = parse_time(window_end)
        except ValueError:
            raise ValueError(
                "window_start and window_end must be ISO-8601 timestamps with timezone"
            ) from None
        if end <= start:
            raise ValueError("window_end must be after window_start")
        window_start = start.isoformat()
        window_end = end.isoformat()
        # Function-level import: aeso.py imports resolve_outcome from
        # this module, so a top-level import would be circular.
        from .aeso import SMP_CSV_URL

        source_url = SMP_CSV_URL
        source_description = (
            "AESO System Marginal Price (minute), averaged to hourly pool "
            f"price over [{window_start}, {window_end}); YES iff max hourly "
            f"pool price > {strike:g} $/MWh"
        )
    else:  # public_source
        if not source_url or not source_url.strip():
            raise ValueError("public_source markets require source_url")
        if not source_description or not source_description.strip():
            raise ValueError("public_source markets require source_description")
        window_start = window_start or None
        window_end = window_end or None

    market_id = "mkt_" + secrets.token_hex(6)
    n = len(outcomes)
    q = [0.0] * n
    prices = dict(zip(outcomes, prices_multi(q, float(b))))
    record: dict[str, Any] = {
        "market_id": market_id,
        "question": question.strip(),
        "resolution_type": resolution_type,
        "contract_type": contract_type,
        "strike": strike,
        "window_start": window_start,
        "window_end": window_end,
        "resolution_source_url": source_url,
        "resolution_source_description": source_description,
        "resolves_at": resolves.isoformat(),
        "created_at": now.isoformat(),
        "b": float(b),
        "outcomes": outcomes,
        "q": q,
        "prices": prices,
        "status": "OPEN",
        "outcome": None,
        "observed_value": None,
        "evidence": None,
        "settled_at": None,
    }
    if outcomes == BINARY_OUTCOMES:
        # Legacy mirrors, so existing clients, the demo, and the
        # reconciler's binary path keep working unchanged.
        record["q_yes"] = 0.0
        record["q_no"] = 0.0
        record["price_yes"] = prices["YES"]
    return record


def can_trade(market: dict[str, Any], now: datetime) -> tuple[bool, str]:
    """Trading is allowed only while the market is OPEN and before
    resolves_at. Returns (ok, reason)."""
    if market["status"] != "OPEN":
        return False, f"MARKET_{market['status']}"
    if now >= parse_time(market["resolves_at"]):
        return False, "MARKET_CLOSED"
    return True, "OK"


def can_settle(market: dict[str, Any], now: datetime) -> tuple[bool, str]:
    if market["status"] != "OPEN":
        return False, f"MARKET_{market['status']}"
    if now < parse_time(market["resolves_at"]):
        return False, "MARKET_NOT_YET_RESOLVABLE"
    return True, "OK"


def resolve_outcome(
    market: dict[str, Any],
    *,
    observed_value: float | None = None,
    outcome: str | None = None,
) -> str:
    """Deterministic outcome from the market's stated rule.

    aeso_price: computed from the observed MAX hourly pool price over
    the window (``observed_value``): YES iff max > strike, NO on a tie
    or below. public_source: the operator-attested outcome — it must
    be one of the market's outcomes (YES/NO for a binary market).
    """
    if market["resolution_type"] == "aeso_price":
        if observed_value is None:
            raise ValueError("aeso_price settlement requires observed_value")
        try:
            value = float(observed_value)
        except (TypeError, ValueError):
            raise ValueError("observed_value must be a number") from None
        strike = float(market["strike"])
        return "YES" if value > strike else "NO"
    if outcome not in market_outcomes(market):
        raise ValueError(
            "public_source settlement outcome must be one of "
            f"{market_outcomes(market)}"
        )
    return outcome
