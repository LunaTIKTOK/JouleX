"""Orchestrator simulation — DESIGN.md §8 (M2).

Deterministic, LABELLED simulation. Given a price history (AESO pool
prices fetched via ``joulex.aeso`` and cached, or the labelled
SYNTHETIC series from ``joulex.aeso_synthetic``) and a set of
deferrable GPU jobs, allocate each job to hours before its deadline
and score the allocation against strong baselines computed on the
SAME history and job set.

Cost model: ``cost = pool price ($/MWh) x power draw (MW) x hours``.
Time is hourly slots indexed into the price series. A job is
``{"job_id", "hours", "power_mw", "earliest_start", "deadline"}``
where ``earliest_start`` / ``deadline`` are slot indices and the job
must finish inside ``[earliest_start, deadline)``. A job may be split
across non-contiguous hours (checkpointed batch work — the deferrable
kind Phase 1 §4.5 identifies as shiftable; latency-sensitive
inference is out of scope).

Capacity: the optimising schedulers (greedy, orchestrator) respect a
per-hour power cap (``capacity_mw``, default 1.0 MW — the facility's
power budget). Without a shared cap the allocation problem is
degenerate: every job would independently take its own cheapest
hours and no scheduler could beat another. The cap is what makes
jobs compete for cheap hours and is therefore an explicit modelling
choice, stated here, not a hidden assumption.

Schedulers:
- ``fixed`` — run at arrival: consecutive hours from
  ``earliest_start``, ignoring price, deadlines, and capacity. The
  no-orchestration counterfactual. (It can overload the cap — that
  is the point: it is what happens with no orchestration.)
- ``price_only`` — the cheapest hours in the whole series, ignoring
  deadlines, earliest start, SLA, and capacity. A lower bound on
  cost that cheats on SLA; reported for context, NOT the bar.
- ``greedy`` — the optimisation baseline: earliest-deadline-first,
  each job takes its cheapest feasible hours with remaining capacity.
  This is the strong baseline the orchestrator must beat.
- ``orchestrator`` — the candidate: it evaluates the greedy
  allocation AND a most-constrained-first (smallest slack) allocation,
  improves each by local search (single-hour moves and pairwise
  hour swaps that reduce total cost), and keeps the cheapest. It
  therefore can never cost more than greedy on energy — the honest
  test is whether it beats greedy by >=5% NET of coordination cost,
  not whether it beats greedy at all.

Verdict (V0 targets, DESIGN.md §0): GO iff net improvement vs the
greedy baseline is >=5% AND orchestrator SLA is 100% AND every
allocation decision is audited (100%). Improvement vs ``price_only``
is reported but is NOT part of the verdict: price-only is cheaper
whenever it violates deadlines, so beating it on cost while meeting
SLA is not the bar — meeting SLA at near price-only cost is. If the
orchestrator cannot beat the strong baseline net of costs, the
verdict is NO-GO and the report says so. The simulator is the
falsifier, not a marketing demo.

Coordination cost is an EXPLICIT parameter (default $0.01/decision,
one decision per job allocated). The falsifier is sensitive to it,
so it is a parameter, never hidden.

Forecast-error sensitivity: the orchestrator plans on perturbed
prices (bias / noise) but is SCORED on true prices — planning on a
wrong forecast and paying real prices is the honest sensitivity.

Auditing: every allocation decision is written to the HCS log
(``joulex.hcs.LocalHCS``) with its inputs (job, hours, prices,
power, cost, source label) plus a summary message; the result
carries the log verification report. 0 unauthorised transactions is
N/A in simulation and is recorded as such — there is no money here.

Calibration (Phase 1 §2.3): pure energy arbitrage on 1 MW is worth
tens of thousands per year, NOT hundreds of thousands. The result
carries an annualised saving for the simulated job mix plus saving
per MWh; a number contradicting Phase 1 would have to be explained,
not shipped.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_COORDINATION_COST_PER_DECISION = 0.01
DEFAULT_CAPACITY_MW = 1.0
IMPROVEMENT_TARGET_PCT = 5.0
SLA_TARGET_PCT = 100.0
ORCHESTRATOR_AGENT_ID = "joulex-orchestrator"

Job = dict[str, Any]
PriceSeries = list[tuple[datetime, float]]
Allocation = dict[str, list[int]]

# Default forecast-error perturbations (deterministic seeds).
DEFAULT_PERTURBATIONS: list[dict[str, Any]] = [
    {"name": "+10% price bias", "mode": "bias", "factor": 1.10},
    {"name": "-10% price bias", "mode": "bias", "factor": 0.90},
    {"name": "+/-10% hourly noise (seed 7)", "mode": "noise", "pct": 0.10, "seed": 7},
    {"name": "+/-25% hourly noise (seed 7)", "mode": "noise", "pct": 0.25, "seed": 7},
]


# -- validation ---------------------------------------------------------

def _validate(prices: PriceSeries, jobs: list[Job]) -> None:
    if not prices:
        raise ValueError("no price history: refusing to simulate")
    if any(p < 0 for _, p in prices):
        raise ValueError("negative pool price in history")
    for job in jobs:
        for key in ("job_id", "hours", "power_mw", "earliest_start", "deadline"):
            if key not in job:
                raise ValueError(f"job {job.get('job_id', '?')} missing {key!r}")
        if job["hours"] <= 0 or job["power_mw"] <= 0:
            raise ValueError(f"job {job['job_id']}: hours and power_mw must be > 0")
        if not (0 <= job["earliest_start"] < job["deadline"] <= len(prices)):
            raise ValueError(
                f"job {job['job_id']}: window [{job['earliest_start']}, "
                f"{job['deadline']}) outside the {len(prices)}-hour history"
            )


def _price_at(prices: PriceSeries, hour: int) -> float:
    return prices[hour][1]


# -- cost + SLA -----------------------------------------------------------

def allocation_cost(
    prices: PriceSeries, jobs: list[Job], alloc: Allocation
) -> float:
    """Total energy cost: sum over jobs and assigned hours of
    price ($/MWh) x power draw (MW) x 1h."""
    by_id = {job["job_id"]: job for job in jobs}
    total = 0.0
    for job_id, hours in alloc.items():
        job = by_id[job_id]
        for h in hours:
            total += _price_at(prices, h) * job["power_mw"]
    return total


def allocation_energy_mwh(jobs: list[Job], alloc: Allocation) -> float:
    by_id = {job["job_id"]: job for job in jobs}
    return sum(
        len(hours) * by_id[job_id]["power_mw"]
        for job_id, hours in alloc.items()
    )


def sla_pct(jobs: list[Job], alloc: Allocation) -> float:
    """% of jobs fully scheduled inside [earliest_start, deadline)."""
    if not jobs:
        return 100.0
    met = 0
    for job in jobs:
        hours = alloc.get(job["job_id"], [])
        if len(hours) == job["hours"] and all(
            job["earliest_start"] <= h < job["deadline"] for h in hours
        ):
            met += 1
    return 100.0 * met / len(jobs)


# -- schedulers -------------------------------------------------------------

def schedule_fixed(
    prices: PriceSeries, jobs: list[Job], capacity_mw: float = DEFAULT_CAPACITY_MW
) -> Allocation:
    """Run at arrival: consecutive hours from earliest_start, ignoring
    price, deadlines, and capacity. Truncated at the end of the
    series (a job that runs past the series is incomplete)."""
    _validate(prices, jobs)
    alloc: Allocation = {}
    for job in jobs:
        hours = [
            h
            for h in range(job["earliest_start"], job["earliest_start"] + job["hours"])
            if h < len(prices)
        ]
        alloc[job["job_id"]] = hours
    return alloc


def schedule_price_only(
    prices: PriceSeries, jobs: list[Job], capacity_mw: float = DEFAULT_CAPACITY_MW
) -> Allocation:
    """Cheapest hours in the whole series, ignoring deadlines, earliest
    start, SLA, and capacity. A cost lower bound that cheats on SLA."""
    _validate(prices, jobs)
    cheapest = sorted(range(len(prices)), key=lambda h: (_price_at(prices, h), h))
    alloc: Allocation = {}
    for job in jobs:
        alloc[job["job_id"]] = sorted(cheapest[: job["hours"]])
    return alloc


def _used_mw(alloc: Allocation, jobs: list[Job]) -> dict[int, float]:
    by_id = {job["job_id"]: job for job in jobs}
    used: dict[int, float] = {}
    for job_id, hours in alloc.items():
        for h in hours:
            used[h] = used.get(h, 0.0) + by_id[job_id]["power_mw"]
    return used


def _cheapest_feasible(
    prices: PriceSeries,
    job: Job,
    used: dict[int, float],
    capacity_mw: float,
    k: int,
) -> list[int]:
    """The k cheapest hours in the job's window with room for its
    power draw under the capacity cap. May return fewer than k when
    the window or the cap cannot fit the job (infeasible job)."""
    candidates = [
        h
        for h in range(job["earliest_start"], job["deadline"])
        if used.get(h, 0.0) + job["power_mw"] <= capacity_mw + 1e-9
    ]
    candidates.sort(key=lambda h: (_price_at(prices, h), h))
    return sorted(candidates[:k])


def _assign_in_order(
    prices: PriceSeries,
    jobs: list[Job],
    order: list[Job],
    capacity_mw: float,
) -> Allocation:
    alloc: Allocation = {job["job_id"]: [] for job in jobs}
    used: dict[int, float] = {}
    for job in order:
        hours = _cheapest_feasible(prices, job, used, capacity_mw, job["hours"])
        alloc[job["job_id"]] = hours
        for h in hours:
            used[h] = used.get(h, 0.0) + job["power_mw"]
    return alloc


def schedule_greedy(
    prices: PriceSeries, jobs: list[Job], capacity_mw: float = DEFAULT_CAPACITY_MW
) -> Allocation:
    """Optimisation baseline: earliest-deadline-first; each job takes
    its cheapest feasible hours with remaining capacity."""
    _validate(prices, jobs)
    order = sorted(jobs, key=lambda j: (j["deadline"], j["job_id"]))
    return _assign_in_order(prices, jobs, order, capacity_mw)


def _improve(
    prices: PriceSeries, jobs: list[Job], alloc: Allocation, capacity_mw: float
) -> Allocation:
    """Deterministic local search: repeat single-hour moves and pairwise
    hour swaps that reduce total cost, until no improving move exists
    (max 20 passes). Only accepts strictly cost-reducing moves, so the
    result is never worse than the input allocation."""
    alloc = {job_id: sorted(hours) for job_id, hours in alloc.items()}
    by_id = {job["job_id"]: job for job in jobs}
    for _ in range(20):
        improved = False
        used = _used_mw(alloc, jobs)
        # Single-hour moves: replace an assigned hour with a cheaper
        # feasible hour that has capacity room.
        for job in sorted(jobs, key=lambda j: j["job_id"]):
            job_id = job["job_id"]
            for h in list(alloc[job_id]):
                for cand in range(job["earliest_start"], job["deadline"]):
                    if cand in alloc[job_id]:
                        continue
                    if _price_at(prices, cand) >= _price_at(prices, h):
                        continue
                    room = used.get(cand, 0.0) + job["power_mw"]
                    if room > capacity_mw + 1e-9:
                        continue
                    alloc[job_id].remove(h)
                    alloc[job_id].append(cand)
                    alloc[job_id].sort()
                    used[h] -= job["power_mw"]
                    used[cand] = used.get(cand, 0.0) + job["power_mw"]
                    improved = True
                    break
        # Pairwise swaps: two jobs exchange one hour each when the swap
        # reduces total cost (frees a cheap hour the other job can use).
        for i, job_a in enumerate(sorted(jobs, key=lambda j: j["job_id"])):
            for job_b in sorted(jobs, key=lambda j: j["job_id"])[i + 1:]:
                a_id, b_id = job_a["job_id"], job_b["job_id"]
                for h_a in list(alloc[a_id]):
                    for h_b in list(alloc[b_id]):
                        # Earlier swaps in this pass may have moved these
                        # hours already; skip stale snapshot entries.
                        if h_a not in alloc[a_id] or h_b not in alloc[b_id]:
                            continue
                        # A swap must not duplicate an hour a job already
                        # holds (that would double-count its cost).
                        if h_b in alloc[a_id] or h_a in alloc[b_id]:
                            continue
                        if not (
                            job_a["earliest_start"] <= h_b < job_a["deadline"]
                            and job_b["earliest_start"] <= h_a < job_b["deadline"]
                        ):
                            continue
                        before = (
                            _price_at(prices, h_a) * job_a["power_mw"]
                            + _price_at(prices, h_b) * job_b["power_mw"]
                        )
                        after = (
                            _price_at(prices, h_b) * job_a["power_mw"]
                            + _price_at(prices, h_a) * job_b["power_mw"]
                        )
                        if after < before - 1e-9:
                            alloc[a_id].remove(h_a)
                            alloc[a_id].append(h_b)
                            alloc[a_id].sort()
                            alloc[b_id].remove(h_b)
                            alloc[b_id].append(h_a)
                            alloc[b_id].sort()
                            improved = True
        if not improved:
            break
    return alloc


def schedule_orchestrator(
    prices: PriceSeries, jobs: list[Job], capacity_mw: float = DEFAULT_CAPACITY_MW
) -> Allocation:
    """The orchestrator: greedy allocation AND a most-constrained-first
    (smallest slack = deadline - earliest - hours) allocation, each
    improved by local search; keep the cheapest. Never worse than
    greedy on energy by construction."""
    _validate(prices, jobs)
    greedy = schedule_greedy(prices, jobs, capacity_mw)
    slack_order = sorted(
        jobs,
        key=lambda j: (j["deadline"] - j["earliest_start"] - j["hours"], j["job_id"]),
    )
    slack_alloc = _assign_in_order(prices, jobs, slack_order, capacity_mw)
    candidates = [
        _improve(prices, jobs, greedy, capacity_mw),
        _improve(prices, jobs, slack_alloc, capacity_mw),
    ]
    best = min(
        candidates, key=lambda a: (allocation_cost(prices, jobs, a), str(sorted(a)))
    )
    return best


def _orchestrator_strategy(
    prices: PriceSeries, jobs: list[Job], alloc: Allocation, capacity_mw: float
) -> str:
    greedy = _improve(prices, jobs, schedule_greedy(prices, jobs, capacity_mw), capacity_mw)
    if alloc == greedy:
        return "greedy-improved"
    return "slack-order-improved"


# -- default job set ----------------------------------------------------------

def default_job_set(
    n_jobs: int = 24, n_hours: int = 336, seed: int = 20261110
) -> list[Job]:
    """Deterministic deferrable-job set for the demo run: jobs arrive
    across the horizon, each 2-10 hours at 0.15-0.40 MW, deadline
    24-72h after arrival. Every job is feasible by construction."""
    rng = random.Random(seed)
    windows = [w for w in (24, 36, 48, 72) if w <= n_hours]
    if not windows:
        raise ValueError("n_hours too small for any job window")
    jobs: list[Job] = []
    for i in range(n_jobs):
        window = rng.choice(windows)
        earliest = rng.randrange(0, n_hours - window + 1)
        hours = rng.randint(2, min(10, window))
        jobs.append(
            {
                "job_id": f"job-{i + 1:02d}",
                "hours": hours,
                "power_mw": round(rng.uniform(0.15, 0.40), 2),
                "earliest_start": earliest,
                "deadline": earliest + window,
            }
        )
    return jobs


# -- forecast-error sensitivity -------------------------------------------------

def _perturb_prices(
    prices: PriceSeries, perturbation: dict[str, Any]
) -> PriceSeries:
    if perturbation["mode"] == "bias":
        factor = float(perturbation["factor"])
        return [(ts, max(0.0, p * factor)) for ts, p in prices]
    if perturbation["mode"] == "noise":
        rng = random.Random(perturbation.get("seed", 7))
        pct = float(perturbation["pct"])
        out = []
        for ts, p in prices:
            # Multiplicative perturbation: a $0 forecast hour stays $0
            # (conservative — forecast error cannot invent a price
            # where the forecast says $0).
            u = rng.uniform(-pct, pct)
            out.append((ts, max(0.0, p * (1.0 + u))))
        return out
    raise ValueError(f"unknown perturbation mode {perturbation['mode']!r}")


def forecast_sensitivity(
    prices: PriceSeries,
    jobs: list[Job],
    capacity_mw: float = DEFAULT_CAPACITY_MW,
    perturbations: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Plan on perturbed prices, score on TRUE prices. Returns one
    entry per perturbation with the % cost change vs the orchestrator
    planning on true (unperturbed) prices. That baseline is the
    orchestrator's own plan, NOT a proven global optimum — the
    orchestrator is a heuristic (local search), so a perturbed plan
    can occasionally find a better local optimum; that shows up as a
    negative % and is reported, not hidden."""
    _validate(prices, jobs)
    base = allocation_cost(
        prices, jobs, schedule_orchestrator(prices, jobs, capacity_mw)
    )
    entries: list[dict[str, Any]] = []
    for pert in perturbations or DEFAULT_PERTURBATIONS:
        perturbed = _perturb_prices(prices, pert)
        planned = schedule_orchestrator(perturbed, jobs, capacity_mw)
        true_cost = allocation_cost(prices, jobs, planned)
        if base <= 0:
            pct = 0.0 if true_cost <= 0 else 100.0
        else:
            pct = 100.0 * (true_cost - base) / base
        entries.append(
            {
                "perturbation": pert["name"],
                "planned_cost_on_true_prices": round(true_cost, 4),
                "perfect_foresight_cost": round(base, 4),
                "pct_increase_vs_perfect_foresight": round(pct, 2),
            }
        )
    return entries


# -- the simulation ---------------------------------------------------------------

def _series_hash(prices: PriceSeries) -> str:
    blob = json.dumps(
        [[ts.isoformat(), p] for ts, p in prices], sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _improvement_pct(baseline: float, net: float) -> float:
    if baseline <= 0:
        return 0.0
    return 100.0 * (baseline - net) / baseline


def run_simulation(
    prices: PriceSeries,
    jobs: list[Job],
    *,
    source_label: str,
    capacity_mw: float = DEFAULT_CAPACITY_MW,
    coordination_cost_per_decision: float = DEFAULT_COORDINATION_COST_PER_DECISION,
    hcs=None,
    agent_id: str = ORCHESTRATOR_AGENT_ID,
    sensitivity: bool = False,
) -> dict[str, Any]:
    """Run all schedulers on the same history and job set and score.

    ``hcs`` is a ``LocalHCS`` (or compatible) audit log; when given,
    every orchestrator allocation decision is appended with its
    inputs. Without it the run is not auditable and cannot be GO.
    """
    _validate(prices, jobs)
    allocs = {
        "fixed": schedule_fixed(prices, jobs, capacity_mw),
        "price_only": schedule_price_only(prices, jobs, capacity_mw),
        "greedy": schedule_greedy(prices, jobs, capacity_mw),
        "orchestrator": schedule_orchestrator(prices, jobs, capacity_mw),
    }
    costs = {
        name: allocation_cost(prices, jobs, alloc)
        for name, alloc in allocs.items()
    }
    slas = {name: sla_pct(jobs, alloc) for name, alloc in allocs.items()}
    orch_alloc = allocs["orchestrator"]
    decisions = sum(1 for hours in orch_alloc.values() if hours)
    coord_total = decisions * coordination_cost_per_decision
    energy = costs["orchestrator"]
    net = energy + coord_total
    costs["orchestrator_energy"] = energy
    costs["orchestrator_net"] = net
    improvement = {
        "vs_fixed": _improvement_pct(costs["fixed"], net),
        "vs_price_only": _improvement_pct(costs["price_only"], net),
        "vs_greedy": _improvement_pct(costs["greedy"], net),
    }

    # -- audit: every allocation decision to the HCS log ---------------
    hcs_report: dict[str, Any] = {"messages": 0, "verify": {"ok": False, "messages": 0, "errors": ["no HCS log"]}}
    auditable_pct = 0.0
    if hcs is not None:
        series_hash = _series_hash(prices)
        told_by = "aeso-oracle" if source_label == "REAL" else "synthetic-series"
        by_id = {job["job_id"]: job for job in jobs}
        logged = 0
        for job in sorted(jobs, key=lambda j: j["job_id"]):
            hours = orch_alloc.get(job["job_id"], [])
            if not hours:
                continue
            job_cost = sum(_price_at(prices, h) * job["power_mw"] for h in hours)
            hcs.append(
                sender=agent_id,
                action="orchestrator_allocate",
                payload={
                    "job_id": job["job_id"],
                    "hours": sorted(hours),
                    "hour_starts": [prices[h][0].isoformat() for h in sorted(hours)],
                    "prices": [_price_at(prices, h) for h in sorted(hours)],
                    "power_mw": job["power_mw"],
                    "cost": round(job_cost, 6),
                    "source_label": source_label,
                    "price_series_hash": series_hash,
                    "scheduler": "orchestrator",
                },
                provenance={"told_by": told_by, "known_facts": [series_hash]},
            )
            logged += 1
        hcs.append(
            sender=agent_id,
            action="orchestrator_summary",
            payload={
                "n_jobs": len(jobs),
                "decisions": decisions,
                "energy_cost": round(energy, 6),
                "coordination_cost": round(coord_total, 6),
                "net_cost": round(net, 6),
                "sla_pct": slas["orchestrator"],
                "source_label": source_label,
                "price_series_hash": series_hash,
            },
            provenance={"told_by": told_by, "known_facts": [series_hash]},
        )
        verify = hcs.verify_log()
        hcs_report = {"messages": len(hcs.messages()), "verify": verify}
        auditable_pct = 100.0 * logged / len(jobs) if jobs else 100.0

    # -- annualised calibration vs Phase 1 §2.3 -------------------------
    period_days = len(prices) / 24.0
    energy_mwh = allocation_energy_mwh(jobs, orch_alloc)
    saving_vs_fixed = costs["fixed"] - net
    saving_per_mwh = (saving_vs_fixed / energy_mwh) if energy_mwh > 0 else 0.0
    annualised = {
        "period_days": period_days,
        "energy_mwh": round(energy_mwh, 2),
        "saving_vs_fixed_usd": round(saving_vs_fixed, 2),
        "saving_per_mwh": round(saving_per_mwh, 2),
        "saving_per_year_usd": round(saving_vs_fixed * (365.0 / period_days), 2),
        "phase1_band_note": (
            "Phase 1 §2.3: pure energy arbitrage on 1 MW is tens of "
            "thousands per year, not hundreds of thousands. This "
            "annualised figure is for the simulated job mix only "
            "(~%.2f MWh over %.0f days); scale by flexible MWh, not by "
            "wishful thinking." % (energy_mwh, period_days)
        ),
    }

    # -- verdict ---------------------------------------------------------
    reasons: list[str] = []
    go = True
    if improvement["vs_greedy"] < IMPROVEMENT_TARGET_PCT:
        go = False
        reasons.append(
            f"improvement vs greedy {improvement['vs_greedy']:.2f}% < "
            f"{IMPROVEMENT_TARGET_PCT}% target"
        )
    if slas["orchestrator"] < SLA_TARGET_PCT:
        go = False
        reasons.append(
            f"orchestrator SLA {slas['orchestrator']:.1f}% < "
            f"{SLA_TARGET_PCT:.0f}% target"
        )
    if auditable_pct < 100.0:
        go = False
        reasons.append(f"auditable decisions {auditable_pct:.1f}% < 100% target")
    verdict = "GO" if go else "NO-GO"
    verdict_reason = (
        "meets all V0 targets: improvement vs greedy "
        f"{improvement['vs_greedy']:.2f}% >= 5%, SLA 100%, 100% auditable"
        if go
        else "; ".join(reasons)
    )

    result: dict[str, Any] = {
        "source_label": source_label,
        "n_hours": len(prices),
        "n_jobs": len(jobs),
        "capacity_mw": capacity_mw,
        "costs": {k: round(v, 6) for k, v in costs.items()},
        "coordination_cost": {
            "per_decision": coordination_cost_per_decision,
            "decisions": decisions,
            "total": round(coord_total, 6),
        },
        "improvement_pct": {k: round(v, 2) for k, v in improvement.items()},
        "sla_pct": slas,
        "auditable_pct": auditable_pct,
        "targets": {
            "improvement_vs_greedy_pct": IMPROVEMENT_TARGET_PCT,
            "sla_pct": SLA_TARGET_PCT,
            "unauthorised_transactions": "N/A - simulation",
            "auditable_pct": 100.0,
        },
        "verdict": verdict,
        "verdict_reason": verdict_reason,
        "strategy": _orchestrator_strategy(prices, jobs, orch_alloc, capacity_mw),
        "annualised": annualised,
        "sensitivity": (
            forecast_sensitivity(prices, jobs, capacity_mw) if sensitivity else []
        ),
        "hcs": hcs_report,
        "allocations": {
            name: {jid: sorted(h) for jid, h in alloc.items()}
            for name, alloc in allocs.items()
        },
    }
    return result


# -- price-history loading + CLI ---------------------------------------------

def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def load_price_history(
    *,
    days: int,
    synthetic: bool,
    data_dir: Path | None = None,
    cache_path: Path | None = None,
) -> tuple[PriceSeries, str, str]:
    """Load a price history for the simulator.

    Order: (1) cached REAL history in ``data/aeso_prices.json`` if it
    covers the requested window; (2) a live fetch of the recent SMP
    hours (cached, but it only covers ~3h — never enough for a
    multi-day run); (3) the labelled SYNTHETIC series, ONLY when
    ``synthetic=True``; otherwise refuse (DESIGN.md §11).
    Returns (series, source_label, how_loaded)."""
    root = _repo_root()
    data_dir = data_dir or (root / "data")
    cache_path = cache_path or (data_dir / "aeso_prices.json")
    need = days * 24
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            hours = cached.get("hours", [])
            if len(hours) >= need and cached.get("label") == "REAL":
                series = [
                    (datetime.fromisoformat(h["timestamp"]), float(h["price"]))
                    for h in hours[:need]
                ]
                return series, "REAL", f"cache {cache_path} ({len(hours)}h)"
        except (ValueError, KeyError, TypeError):
            pass
    if synthetic:
        from .aeso_synthetic import synthetic_price_series

        return (
            synthetic_price_series(days=days),
            "SYNTHETIC",
            "labelled synthetic series (joulex.aeso_synthetic)",
        )
    raise SystemExit(
        "no multi-day price history available: the cached AESO history "
        f"({cache_path}) does not cover {need}h and the live SMP report "
        "covers only the recent ~3h (PoolPriceReportServlet returns HTTP "
        "403 from this VM). Re-run with --synthetic to use the labelled "
        "synthetic series, or supply a longer REAL cache."
    )


def format_table(result: dict[str, Any]) -> str:
    c = result["costs"]
    lines = [
        "JouleX orchestrator simulation (M2) — "
        f"source: {result['source_label']}",
        f"hours={result['n_hours']} jobs={result['n_jobs']} "
        f"capacity={result['capacity_mw']} MW strategy={result['strategy']}",
        "",
        f"{'scheduler':<14} {'energy cost':>12} {'SLA %':>7}",
        f"{'fixed':<14} ${c['fixed']:>11.2f} {result['sla_pct']['fixed']:>6.1f}",
        f"{'price-only':<14} ${c['price_only']:>11.2f} {result['sla_pct']['price_only']:>6.1f}",
        f"{'greedy':<14} ${c['greedy']:>11.2f} {result['sla_pct']['greedy']:>6.1f}",
        f"{'orchestrator':<14} ${c['orchestrator_energy']:>11.2f} {result['sla_pct']['orchestrator']:>6.1f}",
        f"{'  +coord cost':<14} ${result['coordination_cost']['total']:>11.2f} "
        f"({result['coordination_cost']['decisions']} decisions @ "
        f"${result['coordination_cost']['per_decision']:.2f})",
        f"{'orchestrator net':<14} ${c['orchestrator_net']:>11.2f}",
        "",
        "improvement (net) vs fixed: "
        f"{result['improvement_pct']['vs_fixed']:.2f}%",
        "improvement (net) vs price-only: "
        f"{result['improvement_pct']['vs_price_only']:.2f}% (price-only cheats on SLA — not the bar)",
        "improvement (net) vs greedy: "
        f"{result['improvement_pct']['vs_greedy']:.2f}% (target >= 5%)",
        "",
        f"annualised saving vs fixed (this job mix): "
        f"${result['annualised']['saving_per_year_usd']:,.2f}/yr "
        f"(${result['annualised']['saving_per_mwh']:.2f}/MWh over "
        f"{result['annualised']['energy_mwh']:.1f} MWh) — Phase 1 §2.3 "
        "band for 1 MW pure arbitrage is tens of thousands/yr",
    ]
    if result["sensitivity"]:
        lines.append("")
        lines.append("forecast-error sensitivity (planned on perturbed prices, scored on true prices):")
        for entry in result["sensitivity"]:
            lines.append(
                f"  {entry['perturbation']}: "
                f"${entry['planned_cost_on_true_prices']:.2f} "
                f"({entry['pct_increase_vs_perfect_foresight']:+.2f}% vs perfect foresight)"
            )
    lines.append("")
    lines.append(f"auditable decisions: {result['auditable_pct']:.0f}%")
    lines.append(f"VERDICT: {result['verdict']} — {result['verdict_reason']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="JouleX M2 orchestrator simulation (labelled, deterministic)."
    )
    parser.add_argument("--synthetic", action="store_true",
                        help="use the labelled SYNTHETIC price series")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--jobs", type=int, default=24)
    parser.add_argument("--capacity", type=float, default=DEFAULT_CAPACITY_MW)
    parser.add_argument("--coord-cost", type=float,
                        default=DEFAULT_COORDINATION_COST_PER_DECISION)
    parser.add_argument("--sensitivity", action="store_true",
                        help="run forecast-error sensitivity")
    parser.add_argument("--data-dir", type=str, default=None,
                        help="data dir for the HCS audit log (default: <repo>/data)")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir) if args.data_dir else (_repo_root() / "data")
    prices, label, how = load_price_history(
        days=args.days, synthetic=args.synthetic, data_dir=data_dir
    )
    jobs = default_job_set(n_jobs=args.jobs, n_hours=len(prices))
    from .hcs import LocalHCS

    hcs = LocalHCS(str(data_dir))
    result = run_simulation(
        prices,
        jobs,
        source_label=label,
        capacity_mw=args.capacity,
        coordination_cost_per_decision=args.coord_cost,
        hcs=hcs,
        sensitivity=args.sensitivity,
    )
    print(f"price history: {how}")
    print(format_table(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
