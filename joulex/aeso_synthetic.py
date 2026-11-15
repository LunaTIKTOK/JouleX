"""Labelled synthetic AESO price series — M2 price-history fallback.

DESIGN.md §5: when no multi-day live history can be fetched, the
simulator uses this labelled synthetic series, built from the Phase 1
verified price statistics (``research_notes/joulex-phase1-20261109.md``
§2.2, all [V]):

- 2025 average pool price $43.68/MWh;
- 929 zero-price hours in 2025 = 10.6% of hours;
- offer cap $999.99/MWh (8h04m at cap in 2025), floor $0/MWh;
- within-day shape: evening peak around HE 21, midday trough
  around HE 13 (Q2 2025 average by hour-ending).

SYNTHETIC — this series is NOT observed AESO data. It is never
presented as real AESO data: every record, result, and CLI line that
uses it carries the ``SYNTHETIC`` label. The live state (verified
2026-11-10): the SMP report covers only the recent ~3 hours and the
historical PoolPriceReportServlet returns HTTP 403 from this VM, so
no multi-day live history exists to run the simulator on. The few
real hours that were fetched are cached in ``data/aeso_prices.json``.

Construction (deterministic, seeded): an hourly shape (evening peak,
midday trough) times lognormal noise, clipped to [$0, $999.99];
then the lowest 10.6% of hours are set to exactly $0, one hour is
set to the $999.99 cap (a stress tail — 1 hour in 14 days is 0.3%,
above the 2025 annual rate of 0.09%, and is labelled as such), and
the remaining prices are rescaled so the overall mean is $43.68.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from typing import Any

SYNTHETIC_LABEL = "SYNTHETIC"

SYNTHETIC_SOURCE = (
    "SYNTHETIC — labelled synthetic series built from Phase 1 verified "
    "AESO statistics (2025 avg $43.68/MWh, 10.6% zero-price hours, "
    "$999.99/MWh cap). NOT observed AESO data."
)

# 2025 verified statistics (Phase 1 §2.2).
PHASE1_MEAN_USD_PER_MWH = 43.68
PHASE1_ZERO_SHARE = 0.106
PRICE_CAP_USD_PER_MWH = 999.99
PRICE_FLOOR_USD_PER_MWH = 0.0

# Hourly shape factors (hour of day 0-23), normalised to mean 1.0.
# Evening peak around HE 21, midday trough around HE 13 (Phase 1 §2.2:
# Q2 2025 average peaked at HE 21 ~$113/MWh, bottomed at HE 13 ~$2.73).
_HOURLY_SHAPE_RAW = [
    0.55, 0.50, 0.48, 0.50, 0.55, 0.70, 0.90, 1.05,
    1.15, 1.10, 0.90, 0.60, 0.30, 0.15, 0.25, 0.50,
    0.80, 1.10, 1.40, 1.70, 1.90, 1.80, 1.30, 0.85,
]
_SHAPE_MEAN = sum(_HOURLY_SHAPE_RAW) / len(_HOURLY_SHAPE_RAW)
HOURLY_SHAPE = [v / _SHAPE_MEAN for v in _HOURLY_SHAPE_RAW]

DEFAULT_START = datetime(2026, 10, 27, 0, 0, tzinfo=timezone.utc)
DEFAULT_SEED = 20261110


def synthetic_price_series(
    days: int = 14,
    seed: int = DEFAULT_SEED,
    start: datetime | None = None,
) -> list[tuple[datetime, float]]:
    """Deterministic synthetic hourly pool prices: ``days * 24`` entries.

    Returns [(hour_start, price $/MWh), ...], oldest first. Same
    ``(days, seed, start)`` always returns the same series. The series
    is SYNTHETIC — see the module docstring; callers must carry the
    label into every output.
    """
    if days < 1:
        raise ValueError("days must be >= 1")
    n = days * 24
    t0 = start or DEFAULT_START
    rng = random.Random(seed)
    raw: list[float] = []
    for i in range(n):
        hour_of_day = (t0 + timedelta(hours=i)).hour
        noise = math.exp(rng.gauss(0.0, 0.55))
        noise = min(max(noise, 0.05), 6.0)
        price = PHASE1_MEAN_USD_PER_MWH * HOURLY_SHAPE[hour_of_day] * noise
        raw.append(min(max(price, PRICE_FLOOR_USD_PER_MWH), PRICE_CAP_USD_PER_MWH))
    # Zero-price hours: the lowest 10.6% become exactly $0.
    n_zero = round(PHASE1_ZERO_SHARE * n)
    order = sorted(range(n), key=lambda i: (raw[i], i))
    zero_idx = set(order[:n_zero])
    # Cap hour: the highest remaining hour hits the $999.99 cap (stress
    # tail — 1 in 336h is 0.3%, above the 2025 annual 0.09% rate).
    cap_idx = order[-1]
    # Rescale the non-zero, non-cap prices so the overall mean is the
    # Phase 1 average exactly (before 2dp rounding).
    rest = [i for i in range(n) if i not in zero_idx and i != cap_idx]
    rest_sum = sum(raw[i] for i in rest)
    target_rest_sum = PHASE1_MEAN_USD_PER_MWH * n - PRICE_CAP_USD_PER_MWH
    scale = target_rest_sum / rest_sum if rest_sum > 0 else 0.0
    prices: list[float] = []
    for i in range(n):
        if i in zero_idx:
            prices.append(0.0)
        elif i == cap_idx:
            prices.append(PRICE_CAP_USD_PER_MWH)
        else:
            prices.append(round(min(raw[i] * scale, PRICE_CAP_USD_PER_MWH), 2))
    return [(t0 + timedelta(hours=i), prices[i]) for i in range(n)]


def synthetic_record(
    days: int = 14,
    seed: int = DEFAULT_SEED,
    start: datetime | None = None,
) -> dict[str, Any]:
    """The full labelled record for a synthetic series: label, source,
    stats, and the series itself (as ISO timestamp / price pairs)."""
    series = synthetic_price_series(days=days, seed=seed, start=start)
    prices = [p for _, p in series]
    record = {
        "label": SYNTHETIC_LABEL,
        "source": SYNTHETIC_SOURCE,
        "seed": seed,
        "start": series[0][0].isoformat(),
        "n_hours": len(series),
        "stats": {
            "mean": round(sum(prices) / len(prices), 2),
            "zero_hours": sum(1 for p in prices if p == 0.0),
            "cap_hours": sum(1 for p in prices if p == PRICE_CAP_USD_PER_MWH),
            "max": max(prices),
            "min": min(prices),
        },
        "warning": (
            "SYNTHETIC data — not observed AESO data, never presented "
            "as observed AESO data. Built from Phase 1 verified 2025 "
            "statistics; the single cap hour over-represents the 2025 "
            "annual cap rate (0.3% of this window vs 0.09% of 2025) "
            "as a deliberate stress tail."
        ),
        "prices": [
            {"timestamp": ts.isoformat(), "price": p} for ts, p in series
        ],
    }
    return record
