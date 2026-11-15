"""Tests for the labelled synthetic AESO price series (M2).

DESIGN.md §5: the synthetic series is built from the Phase 1 verified
price statistics (2025 avg $43.68, 929 zero-price hours, 8h at cap)
and labelled SYNTHETIC in every output that uses it. Synthetic data
is never presented as observed data.
"""

from __future__ import annotations

from joulex.aeso_synthetic import (
    SYNTHETIC_LABEL,
    synthetic_price_series,
    synthetic_record,
)


def test_synthetic_label_is_synthetic():
    assert SYNTHETIC_LABEL == "SYNTHETIC"
    rec = synthetic_record()
    assert rec["label"] == "SYNTHETIC"
    assert "SYNTHETIC" in rec["source"]
    assert "synthetic" in rec["source"].lower()


def test_synthetic_series_length_and_order():
    series = synthetic_price_series(days=14)
    assert len(series) == 14 * 24
    stamps = [ts for ts, _ in series]
    assert stamps == sorted(stamps)
    # consecutive hourly steps
    for a, b in zip(stamps, stamps[1:]):
        assert (b - a).total_seconds() == 3600


def test_synthetic_mean_matches_phase1_average():
    # Phase 1 §2.2 [V]: 2025 average pool price $43.68/MWh.
    series = synthetic_price_series(days=14)
    mean = sum(p for _, p in series) / len(series)
    assert abs(mean - 43.68) < 1.0, f"mean {mean} far from Phase 1 avg $43.68"


def test_synthetic_zero_price_share_matches_phase1():
    # Phase 1 §2.2 [V]: 929 zero-price hours in 2025 = 10.6%.
    series = synthetic_price_series(days=14)
    zeros = sum(1 for _, p in series if p == 0.0)
    share = zeros / len(series)
    assert 0.08 <= share <= 0.13, f"zero share {share:.3f} far from 10.6%"


def test_synthetic_has_cap_hour_and_floor():
    # Phase 1 §2.2 [V]: offer cap $999.99/MWh, floor $0/MWh.
    series = synthetic_price_series(days=14)
    prices = [p for _, p in series]
    assert max(prices) == 999.99
    assert min(prices) == 0.0
    assert all(0.0 <= p <= 999.99 for p in prices)


def test_synthetic_is_deterministic():
    a = synthetic_price_series(days=14, seed=20261110)
    b = synthetic_price_series(days=14, seed=20261110)
    assert a == b
    c = synthetic_price_series(days=14, seed=7)
    assert c != a


def test_synthetic_record_carries_stats_and_warning():
    rec = synthetic_record(days=14)
    assert rec["label"] == "SYNTHETIC"
    assert rec["n_hours"] == 336
    assert "mean" in rec["stats"]
    assert "zero_hours" in rec["stats"]
    assert "cap_hours" in rec["stats"]
    assert "never presented as observed AESO data" in rec["warning"].lower() or "not observed" in rec["warning"].lower()
