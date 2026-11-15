"""Tests for the M2 orchestrator simulation (DESIGN.md §8).

Test-first: these tests were written before joulex/orchestrator.py
existed. Rules under test: allocation respects deadlines + earliest
start; cost = price x MW x hours; every baseline on the SAME history
and job set; improvement vs baselines net of coordination cost; SLA %;
forecast-error sensitivity; SYNTHETIC labelling everywhere; every
allocation decision in the HCS log with its inputs; the falsifier
path reports NO-GO honestly instead of inflating.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from joulex.aeso_synthetic import synthetic_price_series
from joulex.orchestrator import (
    DEFAULT_COORDINATION_COST_PER_DECISION,
    allocation_cost,
    default_job_set,
    forecast_sensitivity,
    run_simulation,
    schedule_fixed,
    schedule_greedy,
    schedule_orchestrator,
    schedule_price_only,
    sla_pct,
)

H0 = datetime(2026, 10, 27, 0, 0, tzinfo=timezone.utc)


def _prices(values):
    return [(H0 + timedelta(hours=i), float(v)) for i, v in enumerate(values)]


def _job(job_id, hours, power_mw, earliest, deadline):
    return {
        "job_id": job_id,
        "hours": hours,
        "power_mw": power_mw,
        "earliest_start": earliest,
        "deadline": deadline,
    }


@pytest.fixture()
def small_prices():
    # 12 hours: $50 at 0-2, $0 at 3-5 (the cheap dip AFTER arrival),
    # $50 at 6-8, spike $200 at 9-11. Fixed runs at arrival into $50
    # hours; greedy moves into the $0 dip — so fixed > greedy here.
    return _prices([50, 50, 50, 0, 0, 0, 50, 50, 50, 200, 200, 200])


@pytest.fixture()
def small_jobs():
    return [
        _job("job-a", 3, 0.5, 0, 12),
        _job("job-b", 2, 0.5, 3, 9),
    ]


# -- cost arithmetic ---------------------------------------------------

def test_cost_arithmetic_price_times_mw_times_hours(small_prices):
    alloc = {"job-a": [9, 10, 11]}
    jobs = [_job("job-a", 3, 0.5, 0, 12)]
    # 3h x 0.5 MW x $200/MWh = $300.
    assert allocation_cost(small_prices, jobs, alloc) == pytest.approx(300.0)


def test_cost_arithmetic_zero_price_hours_are_free(small_prices):
    alloc = {"job-a": [3, 4, 5]}
    jobs = [_job("job-a", 3, 0.5, 0, 12)]
    assert allocation_cost(small_prices, jobs, alloc) == pytest.approx(0.0)


# -- window discipline ---------------------------------------------------

def test_allocation_respects_deadlines_and_earliest_start(small_prices, small_jobs):
    for fn in (schedule_greedy, schedule_orchestrator):
        alloc = fn(small_prices, small_jobs, capacity_mw=1.0)
        for job in small_jobs:
            for h in alloc[job["job_id"]]:
                assert job["earliest_start"] <= h < job["deadline"], (
                    f"{fn.__name__} put {job['job_id']} at hour {h}, "
                    f"outside [{job['earliest_start']}, {job['deadline']})"
                )


def test_no_duplicate_hours_per_job_in_any_scheduler():
    # Regression: the local-search swap pass once appended an hour a
    # job already held, double-counting its cost in the HCS log.
    prices = synthetic_price_series(days=14)
    jobs = default_job_set(n_jobs=24, n_hours=len(prices))
    for fn in (schedule_fixed, schedule_price_only, schedule_greedy,
               schedule_orchestrator):
        alloc = fn(prices, jobs, capacity_mw=1.0)
        for job in jobs:
            hours = alloc[job["job_id"]]
            assert len(hours) == len(set(hours)), (
                f"{fn.__name__} duplicated hours for {job['job_id']}: {hours}"
            )


def test_capacity_respected_by_optimising_schedulers(small_prices):
    jobs = [
        _job("job-a", 3, 0.6, 0, 6),
        _job("job-b", 3, 0.6, 0, 6),
        _job("job-c", 3, 0.6, 0, 6),
    ]
    for fn in (schedule_greedy, schedule_orchestrator):
        alloc = fn(small_prices, jobs, capacity_mw=1.0)
        for h in range(12):
            used = sum(
                job["power_mw"] for job in jobs if h in alloc[job["job_id"]]
            )
            assert used <= 1.0 + 1e-9, f"{fn.__name__} exceeds capacity at hour {h}"


def test_greedy_and_orchestrator_place_cheapest_feasible_hours(small_prices, small_jobs):
    # job-a (window 0-12) should land on the $0 hours 3-5; job-b
    # (window 3-9) on the $0 hours 3-4, never the $200 spike.
    alloc = schedule_orchestrator(small_prices, small_jobs, capacity_mw=1.0)
    assert sorted(alloc["job-a"]) == [3, 4, 5]
    assert sorted(alloc["job-b"]) == [3, 4]


# -- baselines on the same inputs ----------------------------------------

def test_baselines_computed_on_same_history_and_job_set(small_prices, small_jobs):
    result = run_simulation(
        small_prices, small_jobs, source_label="SYNTHETIC", capacity_mw=1.0
    )
    assert set(result["costs"]) >= {"fixed", "price_only", "greedy", "orchestrator_net"}
    # Same inputs: fixed (arrival, mid+spike hours) must cost more
    # than the deadline-respecting greedy here.
    assert result["costs"]["fixed"] > result["costs"]["greedy"]
    # price-only cheats on deadlines so it is cheapest here — and its
    # SLA is what exposes the cheat (see test below).
    assert result["costs"]["price_only"] <= result["costs"]["greedy"]
    assert result["n_hours"] == 12
    assert result["n_jobs"] == 2
    assert result["source_label"] == "SYNTHETIC"


def test_fixed_baseline_runs_at_arrival(small_prices, small_jobs):
    alloc = schedule_fixed(small_prices, small_jobs, capacity_mw=1.0)
    assert sorted(alloc["job-a"]) == [0, 1, 2]
    assert sorted(alloc["job-b"]) == [3, 4]


def test_price_only_ignores_deadlines(small_prices):
    jobs = [_job("job-late", 2, 0.5, 9, 12)]  # window is all spike hours
    alloc = schedule_price_only(small_prices, jobs, capacity_mw=1.0)
    # Cheapest hours overall are the $0 hours 3-5, outside the window.
    assert sorted(alloc["job-late"]) == [3, 4]


# -- improvement net of coordination cost --------------------------------

def test_improvement_deducts_coordination_cost():
    # Inline fixture with a NONZERO greedy cost (the shared small
    # fixture has greedy $0, which makes improvement undefined).
    prices = _prices([20, 20, 20, 50, 50, 50, 50, 50, 50, 200, 200, 200])
    jobs = [_job("job-a", 3, 0.5, 0, 12), _job("job-b", 2, 0.5, 3, 9)]
    result = run_simulation(
        prices,
        jobs,
        source_label="SYNTHETIC",
        capacity_mw=1.0,
        coordination_cost_per_decision=0.01,
    )
    cc = result["coordination_cost"]
    assert cc["per_decision"] == 0.01
    assert cc["decisions"] == 2  # one decision per job allocated
    assert cc["total"] == pytest.approx(0.02)
    energy = result["costs"]["orchestrator_energy"]
    assert result["costs"]["orchestrator_net"] == pytest.approx(energy + 0.02)
    # Improvement vs greedy is computed on the NET cost (rounded to 2dp
    # in the result, so compare with a rounding-width tolerance).
    greedy = result["costs"]["greedy"]
    expected = 100.0 * (greedy - result["costs"]["orchestrator_net"]) / greedy
    assert result["improvement_pct"]["vs_greedy"] == pytest.approx(expected, abs=0.01)


def test_orchestrator_never_worse_than_greedy_net_of_coordination():
    prices = synthetic_price_series(days=14)
    jobs = default_job_set(n_jobs=24, n_hours=len(prices))
    result = run_simulation(
        prices, jobs, source_label="SYNTHETIC", capacity_mw=1.0
    )
    # The orchestrator considers the greedy allocation as a candidate,
    # so its energy cost can never exceed greedy's.
    assert result["costs"]["orchestrator_energy"] <= result["costs"]["greedy"] + 1e-9


# -- SLA -------------------------------------------------------------------

def test_sla_pct_counts_deadline_completion(small_prices):
    jobs = [
        _job("job-ok", 2, 0.5, 0, 12),
        _job("job-late", 2, 0.5, 9, 12),
    ]
    alloc_fixed = schedule_fixed(small_prices, jobs, capacity_mw=1.0)
    assert sla_pct(jobs, alloc_fixed) == pytest.approx(100.0)
    alloc_cheap = schedule_price_only(small_prices, jobs, capacity_mw=1.0)
    # price-only puts job-late on hours 0-1, outside its window.
    assert sla_pct(jobs, alloc_cheap) == pytest.approx(50.0)


def test_infeasible_job_reports_sla_miss_honestly(small_prices):
    # 5 hours of work in a 3-hour window: no scheduler can meet it.
    jobs = [_job("job-impossible", 5, 0.5, 9, 12)]
    result = run_simulation(
        small_prices, jobs, source_label="SYNTHETIC", capacity_mw=1.0
    )
    assert result["sla_pct"]["orchestrator"] == pytest.approx(0.0)
    assert result["verdict"] == "NO-GO"


# -- falsifier path ---------------------------------------------------------

def test_falsifier_low_contention_job_set_reports_no_go_honestly():
    # One tiny job on a flat price series: nothing to optimise, so the
    # orchestrator cannot beat greedy by 5% — the report must say
    # NO-GO with the true (near-zero) improvement, not inflate it.
    prices = _prices([50.0] * 48)
    jobs = [_job("job-only", 2, 0.1, 0, 48)]
    result = run_simulation(
        prices, jobs, source_label="SYNTHETIC", capacity_mw=1.0
    )
    assert result["improvement_pct"]["vs_greedy"] < 5.0
    assert result["verdict"] == "NO-GO"
    assert "5%" in result["verdict_reason"] or "5.0%" in result["verdict_reason"]


# -- forecast-error sensitivity ---------------------------------------------

def test_forecast_error_sensitivity_runs_and_is_deterministic(small_prices, small_jobs):
    result = run_simulation(
        small_prices,
        small_jobs,
        source_label="SYNTHETIC",
        capacity_mw=1.0,
        sensitivity=True,
    )
    sens = result["sensitivity"]
    assert len(sens) >= 2
    for entry in sens:
        assert "perturbation" in entry
        assert "planned_cost_on_true_prices" in entry
        assert "pct_increase_vs_perfect_foresight" in entry
    # Deterministic: same inputs, same sensitivity numbers.
    result2 = run_simulation(
        small_prices,
        small_jobs,
        source_label="SYNTHETIC",
        capacity_mw=1.0,
        sensitivity=True,
    )
    assert result2["sensitivity"] == sens


def test_forecast_error_sensitivity_is_reported_honestly(small_prices, small_jobs):
    # Sensitivity entries are computed and finite. No claim that
    # perturbed planning can never beat unperturbed planning: the
    # orchestrator is a heuristic (local search), not a global
    # optimiser, so a perturbed plan can occasionally find a better
    # local optimum — that is reported as a negative % increase, not
    # hidden.
    sens = forecast_sensitivity(small_prices, small_jobs, capacity_mw=1.0)
    for entry in sens:
        assert isinstance(entry["pct_increase_vs_perfect_foresight"], float)
        assert entry["planned_cost_on_true_prices"] >= 0


# -- SYNTHETIC labelling ------------------------------------------------------

def test_synthetic_label_present_everywhere_in_result():
    prices = synthetic_price_series(days=2)
    jobs = default_job_set(n_jobs=4, n_hours=len(prices))
    result = run_simulation(
        prices, jobs, source_label="SYNTHETIC", capacity_mw=1.0
    )
    assert result["source_label"] == "SYNTHETIC"
    blob = json.dumps(result, default=str)
    assert "SYNTHETIC" in blob
    assert "REAL" not in result["source_label"]


def test_real_label_preserved_when_source_is_real(small_prices, small_jobs):
    result = run_simulation(
        small_prices, small_jobs, source_label="REAL", capacity_mw=1.0
    )
    assert result["source_label"] == "REAL"


# -- HCS audit ---------------------------------------------------------------

def test_every_allocation_decision_lands_in_hcs_log(tmp_path, small_prices, small_jobs):
    from joulex.hcs import LocalHCS

    hcs = LocalHCS(str(tmp_path / "data"))
    result = run_simulation(
        small_prices,
        small_jobs,
        source_label="SYNTHETIC",
        capacity_mw=1.0,
        hcs=hcs,
    )
    alloc_msgs = [
        m for m in hcs.messages() if m["action"] == "orchestrator_allocate"
    ]
    assert len(alloc_msgs) == 2  # one decision per job
    for msg in alloc_msgs:
        payload = msg["payload"]
        assert payload["job_id"] in ("job-a", "job-b")
        assert "hours" in payload and "prices" in payload
        assert "power_mw" in payload and "cost" in payload
        assert payload["source_label"] == "SYNTHETIC"
        assert msg["provenance"]["told_by"] == "synthetic-series"
    assert result["hcs"]["messages"] == len(hcs.messages())
    assert result["hcs"]["verify"]["ok"] is True
    assert result["auditable_pct"] == pytest.approx(100.0)


def test_no_hcs_means_not_auditable(small_prices, small_jobs):
    result = run_simulation(
        small_prices, small_jobs, source_label="SYNTHETIC", capacity_mw=1.0
    )
    assert result["auditable_pct"] == pytest.approx(0.0)
    assert result["verdict"] == "NO-GO"


# -- default job set + verdict shape ------------------------------------------

def test_default_job_set_is_feasible_and_deterministic():
    prices = synthetic_price_series(days=14)
    jobs_a = default_job_set(n_jobs=24, n_hours=len(prices))
    jobs_b = default_job_set(n_jobs=24, n_hours=len(prices))
    assert jobs_a == jobs_b
    assert len(jobs_a) == 24
    for job in jobs_a:
        assert 0 <= job["earliest_start"] < job["deadline"] <= len(prices)
        assert job["hours"] <= job["deadline"] - job["earliest_start"]
        assert job["hours"] > 0 and job["power_mw"] > 0


def test_simulation_result_has_verdict_and_targets():
    prices = synthetic_price_series(days=14)
    jobs = default_job_set(n_jobs=24, n_hours=len(prices))
    result = run_simulation(
        prices, jobs, source_label="SYNTHETIC", capacity_mw=1.0
    )
    assert result["verdict"] in ("GO", "NO-GO")
    assert result["targets"]["improvement_vs_greedy_pct"] == 5.0
    assert result["targets"]["sla_pct"] == 100.0
    assert result["targets"]["unauthorised_transactions"] == "N/A - simulation"
    assert result["targets"]["auditable_pct"] == 100.0
    # Annualised calibration vs Phase 1 §2.3: pure energy arbitrage on
    # 1 MW is tens of thousands per year, not hundreds of thousands.
    ann = result["annualised"]
    assert ann["saving_per_mwh"] > 0
    assert ann["saving_per_year_usd"] < 200_000, (
        f"annualised saving ${ann['saving_per_year_usd']:,.0f} contradicts "
        "Phase 1 §2.3 (tens of thousands, not hundreds of thousands)"
    )
