/* Orchestrator port — line-for-line from joulex/orchestrator.py (M2).
 *
 * Same schedulers, same rules, same verdict: GO iff improvement vs
 * greedy >= 5% net of coordination cost AND SLA 100% AND every
 * allocation decision audited (100% — in the sandbox the audit is the
 * local HCS log; the engine records each decision there).
 *
 * Prices are [timestampISO, price] pairs; jobs are
 * {job_id, hours, power_mw, earliest_start, deadline} with slot
 * indices into the price series. Capacity is a per-hour power cap
 * (default 1.0 MW). Coordination cost $0.01/decision (one decision
 * per allocated job). All comparisons mirror the Python exactly,
 * including sort orders and tie-breaks, so results match the Python
 * run_simulation on the same inputs (verified in tests/parity.mjs).
 *
 * UMD: browser global window.JoulexOrchestrator, Node module.exports.
 */
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) {
    module.exports = factory();
  } else {
    root.JoulexOrchestrator = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var DEFAULT_COORDINATION_COST_PER_DECISION = 0.01;
  var DEFAULT_CAPACITY_MW = 1.0;
  var IMPROVEMENT_TARGET_PCT = 5.0;
  var SLA_TARGET_PCT = 100.0;

  function validate(prices, jobs) {
    if (!prices || prices.length === 0) {
      throw new Error("no price history: refusing to simulate");
    }
    for (var i = 0; i < prices.length; i++) {
      if (prices[i][1] < 0) throw new Error("negative pool price in history");
    }
    jobs.forEach(function (job) {
      ["job_id", "hours", "power_mw", "earliest_start", "deadline"].forEach(function (k) {
        if (!(k in job)) throw new Error("job " + (job.job_id || "?") + " missing '" + k + "'");
      });
      if (job.hours <= 0 || job.power_mw <= 0) {
        throw new Error("job " + job.job_id + ": hours and power_mw must be > 0");
      }
      if (!(job.earliest_start >= 0 && job.earliest_start < job.deadline &&
            job.deadline <= prices.length)) {
        throw new Error("job " + job.job_id + ": window [" + job.earliest_start + ", " +
          job.deadline + ") outside the " + prices.length + "-hour history");
      }
    });
  }

  function priceAt(prices, hour) { return prices[hour][1]; }

  function allocationCost(prices, jobs, alloc) {
    var byId = {};
    jobs.forEach(function (j) { byId[j.job_id] = j; });
    var total = 0;
    Object.keys(alloc).forEach(function (jobId) {
      var job = byId[jobId];
      alloc[jobId].forEach(function (h) { total += priceAt(prices, h) * job.power_mw; });
    });
    return total;
  }

  function allocationEnergyMwh(jobs, alloc) {
    var byId = {};
    jobs.forEach(function (j) { byId[j.job_id] = j; });
    var total = 0;
    Object.keys(alloc).forEach(function (jobId) {
      total += alloc[jobId].length * byId[jobId].power_mw;
    });
    return total;
  }

  function slaPct(jobs, alloc) {
    if (jobs.length === 0) return 100.0;
    var met = 0;
    jobs.forEach(function (job) {
      var hours = alloc[job.job_id] || [];
      var ok = hours.length === job.hours && hours.every(function (h) {
        return h >= job.earliest_start && h < job.deadline;
      });
      if (ok) met++;
    });
    return 100.0 * met / jobs.length;
  }

  function scheduleFixed(prices, jobs, capacityMw) {
    validate(prices, jobs);
    var alloc = {};
    jobs.forEach(function (job) {
      var hours = [];
      for (var h = job.earliest_start; h < job.earliest_start + job.hours; h++) {
        if (h < prices.length) hours.push(h);
      }
      alloc[job.job_id] = hours;
    });
    return alloc;
  }

  function schedulePriceOnly(prices, jobs, capacityMw) {
    validate(prices, jobs);
    var cheapest = [];
    for (var h = 0; h < prices.length; h++) cheapest.push(h);
    cheapest.sort(function (a, b) {
      var d = priceAt(prices, a) - priceAt(prices, b);
      return d !== 0 ? d : a - b;
    });
    var alloc = {};
    jobs.forEach(function (job) {
      alloc[job.job_id] = cheapest.slice(0, job.hours).slice().sort(function (a, b) { return a - b; });
    });
    return alloc;
  }

  function usedMw(alloc, jobs) {
    var byId = {};
    jobs.forEach(function (j) { byId[j.job_id] = j; });
    var used = {};
    Object.keys(alloc).forEach(function (jobId) {
      alloc[jobId].forEach(function (h) {
        used[h] = (used[h] || 0) + byId[jobId].power_mw;
      });
    });
    return used;
  }

  function cheapestFeasible(prices, job, used, capacityMw, k) {
    var candidates = [];
    for (var h = job.earliest_start; h < job.deadline; h++) {
      if ((used[h] || 0) + job.power_mw <= capacityMw + 1e-9) candidates.push(h);
    }
    candidates.sort(function (a, b) {
      var d = priceAt(prices, a) - priceAt(prices, b);
      return d !== 0 ? d : a - b;
    });
    return candidates.slice(0, k).sort(function (a, b) { return a - b; });
  }

  function assignInOrder(prices, jobs, order, capacityMw) {
    var alloc = {};
    jobs.forEach(function (j) { alloc[j.job_id] = []; });
    var used = {};
    order.forEach(function (job) {
      var hours = cheapestFeasible(prices, job, used, capacityMw, job.hours);
      alloc[job.job_id] = hours;
      hours.forEach(function (h) { used[h] = (used[h] || 0) + job.power_mw; });
    });
    return alloc;
  }

  function scheduleGreedy(prices, jobs, capacityMw) {
    if (capacityMw === undefined) capacityMw = DEFAULT_CAPACITY_MW;
    validate(prices, jobs);
    var order = jobs.slice().sort(function (a, b) {
      if (a.deadline !== b.deadline) return a.deadline - b.deadline;
      return a.job_id < b.job_id ? -1 : a.job_id > b.job_id ? 1 : 0;
    });
    return assignInOrder(prices, jobs, order, capacityMw);
  }

  function improve(prices, jobs, alloc, capacityMw) {
    var out = {};
    Object.keys(alloc).forEach(function (k) { out[k] = alloc[k].slice().sort(function (a, b) { return a - b; }); });
    var byId = {};
    jobs.forEach(function (j) { byId[j.job_id] = j; });
    var sortedJobs = jobs.slice().sort(function (a, b) {
      return a.job_id < b.job_id ? -1 : a.job_id > b.job_id ? 1 : 0;
    });
    for (var pass = 0; pass < 20; pass++) {
      var improved = false;
      var used = usedMw(out, jobs);
      // Single-hour moves.
      sortedJobs.forEach(function (job) {
        var jobId = job.job_id;
        out[jobId].slice().forEach(function (h) {
          for (var cand = job.earliest_start; cand < job.deadline; cand++) {
            if (out[jobId].indexOf(cand) !== -1) continue;
            if (priceAt(prices, cand) >= priceAt(prices, h)) continue;
            if ((used[cand] || 0) + job.power_mw > capacityMw + 1e-9) continue;
            out[jobId].splice(out[jobId].indexOf(h), 1);
            out[jobId].push(cand);
            out[jobId].sort(function (a, b) { return a - b; });
            used[h] -= job.power_mw;
            used[cand] = (used[cand] || 0) + job.power_mw;
            improved = true;
            break;
          }
        });
      });
      // Pairwise swaps.
      for (var i = 0; i < sortedJobs.length; i++) {
        for (var j = i + 1; j < sortedJobs.length; j++) {
          var jobA = sortedJobs[i], jobB = sortedJobs[j];
          var aId = jobA.job_id, bId = jobB.job_id;
          out[aId].slice().forEach(function (hA) {
            out[bId].slice().forEach(function (hB) {
              if (out[aId].indexOf(hA) === -1 || out[bId].indexOf(hB) === -1) return;
              if (out[aId].indexOf(hB) !== -1 || out[bId].indexOf(hA) !== -1) return;
              if (!(jobA.earliest_start <= hB && hB < jobA.deadline &&
                    jobB.earliest_start <= hA && hA < jobB.deadline)) return;
              var before = priceAt(prices, hA) * jobA.power_mw +
                           priceAt(prices, hB) * jobB.power_mw;
              var after = priceAt(prices, hB) * jobA.power_mw +
                          priceAt(prices, hA) * jobB.power_mw;
              if (after < before - 1e-9) {
                out[aId].splice(out[aId].indexOf(hA), 1);
                out[aId].push(hB);
                out[aId].sort(function (a, b) { return a - b; });
                out[bId].splice(out[bId].indexOf(hB), 1);
                out[bId].push(hA);
                out[bId].sort(function (a, b) { return a - b; });
                improved = true;
              }
            });
          });
        }
      }
      if (!improved) break;
    }
    return out;
  }

  function scheduleOrchestrator(prices, jobs, capacityMw) {
    if (capacityMw === undefined) capacityMw = DEFAULT_CAPACITY_MW;
    validate(prices, jobs);
    var greedy = scheduleGreedy(prices, jobs, capacityMw);
    var slackOrder = jobs.slice().sort(function (a, b) {
      var sa = a.deadline - a.earliest_start - a.hours;
      var sb = b.deadline - b.earliest_start - b.hours;
      if (sa !== sb) return sa - sb;
      return a.job_id < b.job_id ? -1 : a.job_id > b.job_id ? 1 : 0;
    });
    var slackAlloc = assignInOrder(prices, jobs, slackOrder, capacityMw);
    var candidates = [
      improve(prices, jobs, greedy, capacityMw),
      improve(prices, jobs, slackAlloc, capacityMw)
    ];
    var best = candidates[0];
    var bestCost = allocationCost(prices, jobs, best);
    var bestKey = JSON.stringify(Object.keys(best).sort().map(function (k) { return [k, best[k]]; }));
    for (var i = 1; i < candidates.length; i++) {
      var c = allocationCost(prices, jobs, candidates[i]);
      var cKey = JSON.stringify(Object.keys(candidates[i]).sort().map(function (k) { return [k, candidates[i][k]]; }));
      if (c < bestCost || (c === bestCost && cKey < bestKey)) {
        best = candidates[i]; bestCost = c; bestKey = cKey;
      }
    }
    return best;
  }

  function round6(x) { return Math.round(x * 1e6) / 1e6; }

  function improvementPct(baseline, net) {
    if (baseline <= 0) return 0.0;
    return 100.0 * (baseline - net) / baseline;
  }

  // Full simulation scoring — mirrors orchestrator.run_simulation
  // minus the HCS audit (the engine logs decisions separately).
  function runSimulation(prices, jobs, opts) {
    opts = opts || {};
    var capacityMw = opts.capacityMw === undefined ? DEFAULT_CAPACITY_MW : opts.capacityMw;
    var coordPerDecision = opts.coordinationCostPerDecision === undefined ?
      DEFAULT_COORDINATION_COST_PER_DECISION : opts.coordinationCostPerDecision;
    validate(prices, jobs);
    var allocs = {
      fixed: scheduleFixed(prices, jobs, capacityMw),
      price_only: schedulePriceOnly(prices, jobs, capacityMw),
      greedy: scheduleGreedy(prices, jobs, capacityMw),
      orchestrator: scheduleOrchestrator(prices, jobs, capacityMw)
    };
    var costs = {};
    Object.keys(allocs).forEach(function (name) {
      costs[name] = allocationCost(prices, jobs, allocs[name]);
    });
    var slas = {};
    Object.keys(allocs).forEach(function (name) {
      slas[name] = slaPct(jobs, allocs[name]);
    });
    var orchAlloc = allocs.orchestrator;
    var decisions = 0;
    Object.keys(orchAlloc).forEach(function (k) { if (orchAlloc[k].length) decisions++; });
    var coordTotal = decisions * coordPerDecision;
    var energy = costs.orchestrator;
    var net = energy + coordTotal;
    costs.orchestrator_energy = energy;
    costs.orchestrator_net = net;
    var improvement = {
      vs_fixed: improvementPct(costs.fixed, net),
      vs_price_only: improvementPct(costs.price_only, net),
      vs_greedy: improvementPct(costs.greedy, net)
    };
    var periodDays = prices.length / 24.0;
    var energyMwh = allocationEnergyMwh(jobs, orchAlloc);
    var savingVsFixed = costs.fixed - net;
    var savingPerMwh = energyMwh > 0 ? savingVsFixed / energyMwh : 0.0;
    var reasons = [];
    var go = true;
    if (improvement.vs_greedy < IMPROVEMENT_TARGET_PCT) {
      go = false;
      reasons.push("improvement vs greedy " + improvement.vs_greedy.toFixed(2) +
        "% < " + IMPROVEMENT_TARGET_PCT + "% target");
    }
    if (slas.orchestrator < SLA_TARGET_PCT) {
      go = false;
      reasons.push("orchestrator SLA " + slas.orchestrator.toFixed(1) + "% < " +
        SLA_TARGET_PCT + "% target");
    }
    var verdict = go ? "GO" : "NO-GO";
    var verdictReason = go ?
      "meets all V0 targets: improvement vs greedy " +
        improvement.vs_greedy.toFixed(2) + "% >= 5%, SLA 100%, 100% auditable" :
      reasons.join("; ");
    return {
      source_label: opts.sourceLabel || "SYNTHETIC",
      n_hours: prices.length,
      n_jobs: jobs.length,
      capacity_mw: capacityMw,
      costs: { fixed: round6(costs.fixed), price_only: round6(costs.price_only),
               greedy: round6(costs.greedy), orchestrator: round6(energy),
               orchestrator_energy: round6(energy),
               orchestrator_net: round6(net) },
      coordination_cost: { per_decision: coordPerDecision, decisions: decisions,
                           total: round6(coordTotal) },
      improvement_pct: { vs_fixed: Math.round(improvement.vs_fixed * 100) / 100,
                         vs_price_only: Math.round(improvement.vs_price_only * 100) / 100,
                         vs_greedy: Math.round(improvement.vs_greedy * 100) / 100 },
      sla_pct: slas,
      verdict: verdict,
      verdict_reason: verdictReason,
      annualised: {
        period_days: periodDays,
        energy_mwh: Math.round(energyMwh * 100) / 100,
        saving_vs_fixed_usd: Math.round(savingVsFixed * 100) / 100,
        saving_per_mwh: Math.round(savingPerMwh * 100) / 100,
        saving_per_year_usd: Math.round(savingVsFixed * (365.0 / periodDays) * 100) / 100
      },
      allocations: allocs
    };
  }

  return {
    DEFAULT_COORDINATION_COST_PER_DECISION: DEFAULT_COORDINATION_COST_PER_DECISION,
    DEFAULT_CAPACITY_MW: DEFAULT_CAPACITY_MW,
    IMPROVEMENT_TARGET_PCT: IMPROVEMENT_TARGET_PCT,
    SLA_TARGET_PCT: SLA_TARGET_PCT,
    validate: validate,
    allocationCost: allocationCost,
    allocationEnergyMwh: allocationEnergyMwh,
    slaPct: slaPct,
    scheduleFixed: scheduleFixed,
    schedulePriceOnly: schedulePriceOnly,
    scheduleGreedy: scheduleGreedy,
    scheduleOrchestrator: scheduleOrchestrator,
    runSimulation: runSimulation
  };
});
