/* Sandbox exchange engine — the JouleX core rules, ported from
 * joulex/exchange.py + joulex/markets.py + joulex/ledger.py +
 * joulex/hcs.py (LocalHCS) + joulex/aeso.py (oracle rule).
 *
 * What is the SAME as production code:
 *  - LMSR pricing (via JoulexLMSR — the port of joulex/lmsr.py)
 *  - Market validation rules (joulex/markets.py new_market)
 *  - Order checks: open market, side valid, sell-position check,
 *    balance check, cost = C(q+delta) - C(q), 6dp rounding
 *  - Ledger rules (joulex/ledger.py): 1,000-point endowment, pool as
 *    market-maker account, 1 point per winning share at settlement,
 *    conservation invariant sum(balances) + pool == 1000 * agents
 *  - Settlement rules (joulex/markets.py resolve_outcome): aeso_price
 *    YES iff max hourly pool price over the window > strike (tie = NO);
 *    public_source outcome must be one of the market's outcomes
 *  - Oracle fail-closed (joulex/aeso.py): a window the series does
 *    not cover is a NO_DATA refusal — the market stays OPEN, no
 *    outcome is ever guessed
 *  - HCS log schema (joulex/hcs.py LocalHCS): header
 *    {"mode": "local", "hedera_topic": null}, strictly increasing
 *    sequence, consensus timestamp in HCS nanosecond format, sender,
 *    action, payload hash, attestation, provenance, hash chain.
 *    The timestamp is a LOCAL simulation of a consensus timestamp,
 *    never a Hedera consensus timestamp.
 *
 * What is DELIBERATELY different (sandbox only — labelled in the UI):
 *  - NO CF-KYA admission: the visitor trades as a named sandbox
 *    agent with no credential. Production requires L3 admission.
 *  - NO operator key / attestation on settlement: the visitor acts
 *    as sandbox operator. Production /settle requires the operator
 *    key and, for public_source, a valid attestation envelope.
 *  - NO Hedera submission: the log is local-only. The live path is
 *    Hedera testnet topic 0.0.11399235; this sandbox never submits.
 *  - Paper points only. No real money, power, or counterparties.
 *
 * UMD: browser global window.JoulexEngine, Node module.exports.
 * Depends on JoulexLMSR and JoulexHash (required in Node, read from
 * window in the browser). No DOM, no network. Pure state machine.
 */
(function (root, factory) {
  var LMSR, Hash;
  if (typeof module !== "undefined" && module.exports) {
    LMSR = require("./lmsr.js");
    Hash = require("./sha256.js");
    module.exports = factory(LMSR, Hash);
  } else {
    root.JoulexEngine = factory(root.JoulexLMSR, root.JoulexHash);
  }
})(typeof self !== "undefined" ? self : this, function (LMSR, Hash) {
  "use strict";

  var STARTING_POINTS = 1000.0;
  var GENESIS_PREV_HASH = "0000000000000000000000000000000000000000000000000000000000000000";
  var MIN_RESOLVE_MS = 24 * 3600 * 1000;
  var MAX_RESOLVE_MS = 30 * 24 * 3600 * 1000;
  var RESOLUTION_TYPES = ["aeso_price", "public_source"];
  var CONTRACT_TYPES = ["flex_capacity", "compute_forward", "generic"];
  var BINARY_OUTCOMES = ["YES", "NO"];

  function EngineError(status, reason) {
    var e = new Error(status + ": " + reason);
    e.status = status;
    e.reason = reason;
    return e;
  }

  function r6(v) { return LMSR.roundMoney(v); }

  function parseTime(value) {
    var d = new Date(value);
    if (isNaN(d.getTime())) throw new Error("invalid timestamp: " + value);
    return d;
  }

  // HCS nanosecond format, LOCAL simulation (joulex/hcs.py
  // format_hcs_timestamp): microseconds * 1000, last three digits 0.
  function formatHcsTimestamp(date) {
    function p(n, l) { var s = String(n); while (s.length < l) s = "0" + s; return s; }
    return date.getUTCFullYear() + "-" + p(date.getUTCMonth() + 1, 2) + "-" +
      p(date.getUTCDate(), 2) + "T" + p(date.getUTCHours(), 2) + ":" +
      p(date.getUTCMinutes(), 2) + ":" + p(date.getUTCSeconds(), 2) + "." +
      p(date.getUTCMilliseconds(), 3) + "000000Z";
  }

  // -- market validation (mirrors joulex/markets.py new_market) --------
  function normalizeOutcomes(outcomes) {
    if (outcomes === null || outcomes === undefined) return BINARY_OUTCOMES.slice();
    if (!Array.isArray(outcomes)) throw new Error("outcomes must be a list of outcome labels");
    if (outcomes.length < 2) throw new Error("outcomes must list at least 2 outcomes");
    if (outcomes.length > 10) throw new Error("outcomes must list at most 10 outcomes");
    var labels = outcomes.map(function (label) {
      if (typeof label !== "string") throw new Error("outcome labels must be strings");
      label = label.trim();
      if (!label) throw new Error("outcome labels must be non-empty");
      if (label.length > 64) throw new Error("outcome labels must be at most 64 characters");
      if (label.indexOf("|") !== -1) throw new Error("outcome labels must not contain '|'");
      return label;
    });
    if (new Set(labels).size !== labels.length) throw new Error("outcome labels must be distinct");
    var lowered = labels.map(function (l) { return l.toLowerCase(); });
    if (lowered.length === 2 && lowered.indexOf("yes") !== -1 && lowered.indexOf("no") !== -1) {
      return BINARY_OUTCOMES.slice();
    }
    return labels;
  }

  function newMarket(fields, now) {
    var question = fields.question;
    if (!question || !String(question).trim()) throw new Error("question must be non-empty");
    if (RESOLUTION_TYPES.indexOf(fields.resolution_type) === -1) {
      throw new Error("resolution_type must be one of " + RESOLUTION_TYPES.join(", "));
    }
    var contractType = fields.contract_type || "generic";
    if (CONTRACT_TYPES.indexOf(contractType) === -1) {
      throw new Error("contract_type must be one of " + CONTRACT_TYPES.join(", "));
    }
    var resolves = parseTime(fields.resolves_at);
    var horizon = resolves.getTime() - now.getTime();
    if (horizon < MIN_RESOLVE_MS) throw new Error("resolves_at must be at least 1 day in the future");
    if (horizon > MAX_RESOLVE_MS) throw new Error("resolves_at must be at most 30 days in the future");
    var b = fields.b === undefined ? 100.0 : Number(fields.b);
    if (!(b > 0)) throw new Error("liquidity parameter b must be > 0");
    var outcomes = normalizeOutcomes(fields.outcomes);
    var strike = null, windowStart = null, windowEnd = null;
    var sourceUrl = fields.source_url || null;
    var sourceDesc = fields.source_description || null;
    if (fields.resolution_type === "aeso_price") {
      if (outcomes.join("|") !== BINARY_OUTCOMES.join("|")) {
        throw new Error("aeso_price markets are binary (outcomes YES/NO only)");
      }
      if (fields.strike === null || fields.strike === undefined) {
        throw new Error("aeso_price markets require strike");
      }
      strike = Number(fields.strike);
      if (isNaN(strike)) throw new Error("strike must be a number");
      if (!fields.window_start || !fields.window_end) {
        throw new Error("aeso_price markets require window_start and window_end");
      }
      var start = parseTime(fields.window_start);
      var end = parseTime(fields.window_end);
      if (end <= start) throw new Error("window_end must be after window_start");
      windowStart = start.toISOString();
      windowEnd = end.toISOString();
      sourceUrl = "http://ets.aeso.ca/ets_web/ip/Market/Reports/CSMPriceReportServlet?contentType=csv";
      sourceDesc = "AESO System Marginal Price (minute), averaged to hourly pool " +
        "price over [" + windowStart + ", " + windowEnd + "); YES iff max hourly " +
        "pool price > " + strike + " $/MWh";
    } else {
      if (!sourceUrl || !sourceUrl.trim()) throw new Error("public_source markets require source_url");
      if (!sourceDesc || !sourceDesc.trim()) throw new Error("public_source markets require source_description");
      windowStart = fields.window_start || null;
      windowEnd = fields.window_end || null;
    }
    var n = outcomes.length;
    var q = [];
    for (var i = 0; i < n; i++) q.push(0);
    var prices = LMSR.pricesMulti(q, b);
    var priceMap = {};
    outcomes.forEach(function (o, idx) { priceMap[o] = prices[idx]; });
    var market = {
      market_id: "mkt_" + Math.random().toString(16).slice(2, 14),
      question: String(question).trim(),
      resolution_type: fields.resolution_type,
      contract_type: contractType,
      strike: strike,
      window_start: windowStart,
      window_end: windowEnd,
      resolution_source_url: sourceUrl,
      resolution_source_description: sourceDesc,
      resolves_at: resolves.toISOString(),
      created_at: now.toISOString(),
      b: b,
      outcomes: outcomes,
      q: q,
      prices: priceMap,
      status: "OPEN",
      outcome: null,
      observed_value: null,
      evidence: null,
      settled_at: null,
      created_by: fields.created_by || "sandbox"
    };
    if (outcomes.join("|") === BINARY_OUTCOMES.join("|")) {
      market.q_yes = 0; market.q_no = 0; market.price_yes = priceMap.YES;
    }
    return market;
  }

  function canTrade(market, now) {
    if (market.status !== "OPEN") return [false, "MARKET_" + market.status];
    if (now >= parseTime(market.resolves_at)) return [false, "MARKET_CLOSED"];
    return [true, "OK"];
  }

  function canSettle(market, now) {
    if (market.status !== "OPEN") return [false, "MARKET_" + market.status];
    if (now < parseTime(market.resolves_at)) return [false, "MARKET_NOT_YET_RESOLVABLE"];
    return [true, "OK"];
  }

  function resolveOutcome(market, observedValue, outcome) {
    if (market.resolution_type === "aeso_price") {
      if (observedValue === null || observedValue === undefined) {
        throw new Error("aeso_price settlement requires observed_value");
      }
      return Number(observedValue) > Number(market.strike) ? "YES" : "NO";
    }
    if (market.outcomes.indexOf(outcome) === -1) {
      throw new Error("public_source settlement outcome must be one of " + market.outcomes.join(", "));
    }
    return outcome;
  }

  // -- engine -----------------------------------------------------------
  function createEngine(data) {
    // data: { synthetic: {prices:[{timestamp,price}]}, live_snapshot: {hours:[...]},
    //         seed_markets: [...], seed_trades: [...], sandbox_clock_start }
    var state = {
      clock: data.sandbox_clock_start,
      agents: {},       // agent_id -> {balance}
      positions: {},    // "agent|market|side" -> shares
      pool: 0,
      markets: {},
      trades: [],
      log: [],          // HCS-schema messages (local mode)
      nextSeq: 1,
      prevHash: GENESIS_PREV_HASH,
      marketCounter: 0
    };

    function now() { return new Date(state.clock); }

    function ensureAgent(agentId) {
      if (!state.agents[agentId]) {
        state.agents[agentId] = { balance: STARTING_POINTS };
      }
    }

    function balance(agentId) {
      return state.agents[agentId] ? state.agents[agentId].balance : 0;
    }

    function position(agentId, marketId, side) {
      return state.positions[agentId + "|" + marketId + "|" + side] || 0;
    }

    function positionsFor(agentId) {
      var out = [];
      Object.keys(state.positions).sort().forEach(function (key) {
        var parts = key.split("|");
        if (parts[0] === agentId && Math.abs(state.positions[key]) > 1e-9) {
          out.push({ market_id: parts[1], side: parts[2], shares: state.positions[key] });
        }
      });
      return out;
    }

    // -- HCS local log (joulex/hcs.py LocalHCS schema) -----------------
    function hcsAppend(sender, action, payload, provenance) {
      var ts = formatHcsTimestamp(now());
      var message = {
        sequence: state.nextSeq,
        consensus_timestamp: ts,
        topic: "joulex-actions",
        sender: sender,
        action: action,
        payload: payload,
        payload_hash: Hash.sha256Canonical(payload),
        attestation: null,
        provenance: provenance || { told_by: "sandbox", known_facts: [] },
        prev_hash: state.prevHash
      };
      message.message_hash = Hash.sha256Hex(Hash.canonical(message));
      state.log.push(message);
      state.prevHash = message.message_hash;
      state.nextSeq += 1;
      return message;
    }

    function verifyLog() {
      var errors = [];
      var prev = GENESIS_PREV_HASH;
      for (var i = 0; i < state.log.length; i++) {
        var m = state.log[i];
        if (m.sequence !== i + 1) errors.push("seq break at index " + i);
        if (m.prev_hash !== prev) errors.push("hash-chain break at seq " + m.sequence);
        var body = {};
        Object.keys(m).forEach(function (k) { if (k !== "message_hash") body[k] = m[k]; });
        if (Hash.sha256Hex(Hash.canonical(body)) !== m.message_hash) {
          errors.push("message_hash mismatch at seq " + m.sequence);
        }
        if (Hash.sha256Canonical(m.payload) !== m.payload_hash) {
          errors.push("payload_hash mismatch at seq " + m.sequence);
        }
        if (i > 0 && m.consensus_timestamp < state.log[i - 1].consensus_timestamp) {
          errors.push("timestamp regression at seq " + m.sequence);
        }
        prev = m.message_hash;
      }
      return { ok: errors.length === 0, messages: state.log.length, errors: errors };
    }

    // -- price series lookup (oracle rule, fail-closed) ------------------
    function seriesHours() {
      // Returns [{ts: Date, price, source}] covering synthetic + live
      // snapshot. Synthetic covers 2026-10-27T00:00Z..2026-11-10T00:00Z;
      // live snapshot covers 2026-11-12T11:00Z..2026-11-12T15:00Z.
      var out = data.synthetic.prices.map(function (p) {
        return { ts: new Date(p.timestamp), price: p.price, source: "SYNTHETIC" };
      });
      data.live_snapshot.hours.forEach(function (p) {
        out.push({ ts: new Date(p.timestamp), price: p.price, source: "LIVE SNAPSHOT" });
      });
      return out;
    }

    function maxPriceOverWindow(windowStartIso, windowEndIso) {
      var start = parseTime(windowStartIso);
      var end = parseTime(windowEndIso);
      var hours = seriesHours();
      var covered = [];
      var source = null;
      var cursor = new Date(start);
      cursor.setUTCMinutes(0, 0, 0);
      while (cursor < end) {
        var found = null;
        for (var i = 0; i < hours.length; i++) {
          if (hours[i].ts.getTime() === cursor.getTime()) { found = hours[i]; break; }
        }
        if (!found) {
          throw EngineError(503, "AESO_NO_DATA: no pool price for hour starting " +
            cursor.toISOString() + " (series does not cover the window)");
        }
        if (source === null) source = found.source;
        if (found.source !== source) {
          throw EngineError(503, "AESO_NO_DATA: window spans two price sources " +
            "(" + source + " + " + found.source + ") — settlement refuses mixed sources");
        }
        covered.push(found.price);
        cursor = new Date(cursor.getTime() + 3600000);
      }
      return { max: Math.max.apply(null, covered), source: source, hours: covered.length };
    }

    // -- markets ---------------------------------------------------------
    function createMarket(fields) {
      var market;
      try {
        market = newMarket(fields, now());
      } catch (err) {
        throw EngineError(400, "INVALID_MARKET: " + err.message);
      }
      market.market_id = "mkt_sandbox_" + String(++state.marketCounter).padStart(3, "0") +
        "_" + Math.random().toString(16).slice(2, 8);
      hcsAppend(market.created_by, "create_market", {
        market_id: market.market_id,
        question: market.question,
        resolution_type: market.resolution_type,
        contract_type: market.contract_type,
        strike: market.strike,
        window_start: market.window_start,
        window_end: market.window_end,
        resolves_at: market.resolves_at,
        outcomes: market.outcomes,
        b: market.b
      }, { told_by: "sandbox-operator", known_facts: [] });
      state.markets[market.market_id] = market;
      return JSON.parse(JSON.stringify(market));
    }

    function listMarkets() {
      return Object.keys(state.markets).sort().map(function (id) {
        return JSON.parse(JSON.stringify(state.markets[id]));
      });
    }

    function getMarket(marketId) {
      var m = state.markets[marketId];
      if (!m) throw EngineError(404, "MARKET_NOT_FOUND");
      return JSON.parse(JSON.stringify(m));
    }

    // -- orders ----------------------------------------------------------
    function placeOrder(agentId, payload) {
      var marketId = payload.market_id;
      var side = payload.side;
      var shares = Number(payload.shares);
      var action = payload.action || "buy";
      if (!agentId) throw EngineError(400, "INVALID_ORDER: agent_id required");
      if (!marketId || !side || !(shares > 0)) {
        throw EngineError(400, "INVALID_ORDER: market_id, side, shares required");
      }
      if (action !== "buy" && action !== "sell") {
        throw EngineError(400, "INVALID_ORDER: action must be buy or sell");
      }
      var market = state.markets[marketId];
      if (!market) throw EngineError(404, "MARKET_NOT_FOUND");
      if (market.outcomes.indexOf(side) === -1) {
        throw EngineError(400, "INVALID_ORDER: side must be one of the market outcomes");
      }
      var tradable = canTrade(market, now());
      if (!tradable[0]) throw EngineError(409, tradable[1]);
      var signedShares = action === "buy" ? shares : -shares;
      if (action === "sell" && position(agentId, marketId, side) < shares - 1e-9) {
        throw EngineError(400, "INSUFFICIENT_SHARES");
      }
      var index = market.outcomes.indexOf(side);
      var cost = LMSR.tradeCostMulti(market.q, market.b, index, signedShares);
      var effectiveBalance = state.agents[agentId] ? balance(agentId) : STARTING_POINTS;
      if (cost > 0 && cost > effectiveBalance + 1e-9) {
        throw EngineError(403, "INSUFFICIENT_POINTS");
      }
      // HCS log BEFORE execution (a log failure executes nothing).
      hcsAppend(agentId, "place_order", {
        agent_id: agentId, market_id: marketId, side: side,
        shares: shares, action: action
      }, { told_by: "sandbox", known_facts: [] });
      // Execute.
      var newQ = LMSR.applyTradeMulti(market.q, index, signedShares);
      var pricesAfter = LMSR.pricesMulti(newQ, market.b);
      market.q = newQ;
      market.prices = {};
      market.outcomes.forEach(function (o, idx) { market.prices[o] = pricesAfter[idx]; });
      if (market.outcomes.join("|") === BINARY_OUTCOMES.join("|")) {
        market.q_yes = newQ[0]; market.q_no = newQ[1];
        market.price_yes = pricesAfter[0];
      }
      ensureAgent(agentId);
      state.agents[agentId].balance = r6(state.agents[agentId].balance - cost);
      state.pool = r6(state.pool + cost);
      var key = agentId + "|" + marketId + "|" + side;
      var newShares = r6((state.positions[key] || 0) + signedShares);
      if (Math.abs(newShares) < 1e-9) delete state.positions[key];
      else state.positions[key] = newShares;
      var receipt = {
        trade_id: "trd_" + Math.random().toString(16).slice(2, 14),
        timestamp: now().toISOString(),
        market_id: marketId,
        agent_id: agentId,
        side: side,
        action: action,
        shares: shares,
        cost_points: cost,
        prices_after: market.prices,
        balance_after: balance(agentId)
      };
      state.trades.push(receipt);
      return JSON.parse(JSON.stringify(receipt));
    }

    // -- settlement --------------------------------------------------------
    function settleMarket(marketId, opts) {
      opts = opts || {};
      var market = state.markets[marketId];
      if (!market) throw EngineError(404, "MARKET_NOT_FOUND");
      var settleable = canSettle(market, now());
      if (!settleable[0]) throw EngineError(409, settleable[1]);
      var observedValue = null, evidence = opts.evidence || null, source = null;
      var outcome;
      if (market.resolution_type === "aeso_price") {
        // Oracle path: max hourly pool price over the window. The
        // operator-supplied value is ignored; a window the series does
        // not cover refuses settlement (503) and the market stays OPEN.
        var res = maxPriceOverWindow(market.window_start, market.window_end);
        observedValue = res.max;
        source = res.source;
        evidence = market.resolution_source_url;
        outcome = resolveOutcome(market, observedValue, null);
      } else {
        if (!evidence) throw EngineError(400, "INVALID_SETTLEMENT: public_source settlement requires evidence");
        outcome = resolveOutcome(market, null, opts.outcome);
      }
      hcsAppend("exchange", "settle_market", {
        market_id: marketId, outcome: outcome,
        observed_value: observedValue, evidence: evidence
      }, { told_by: source === "SYNTHETIC" ? "synthetic-series" :
                    source === "LIVE SNAPSHOT" ? "aeso-oracle" : "sandbox-operator",
           known_facts: [] });
      // Payouts: 1 point per winning share, 0 per losing share.
      var payouts = [];
      Object.keys(state.positions).sort().forEach(function (key) {
        var parts = key.split("|");
        if (parts[1] !== marketId) return;
        var shares = state.positions[key];
        delete state.positions[key];
        var payout = parts[2] === outcome ? r6(shares) : 0;
        if (payout) {
          state.agents[parts[0]].balance = r6(state.agents[parts[0]].balance + payout);
          state.pool = r6(state.pool - payout);
        }
        payouts.push({ agent_id: parts[0], market_id: marketId, side: parts[2],
                       shares: shares, payout_points: payout });
      });
      market.status = "RESOLVED";
      market.outcome = outcome;
      market.observed_value = observedValue;
      market.evidence = evidence;
      market.settled_at = now().toISOString();
      market.settlement_source = source;
      return { market_id: marketId, question: market.question,
               resolution_type: market.resolution_type, outcome: outcome,
               observed_value: observedValue, evidence: evidence,
               settlement_source: source, settled_at: market.settled_at,
               payouts: payouts };
    }

    // -- scheduler audit (mirrors joulex/orchestrator.py run_simulation)
    // Every orchestrator allocation decision is appended to the HCS log
    // with its inputs, plus a summary message. Without this the run is
    // not auditable and cannot be GO (same rule as Python).
    function auditScheduler(prices, jobs, alloc, result) {
      var seriesPairs = prices.map(function (p) { return [p[0], p[1]]; });
      var seriesHash = Hash.sha256Hex(Hash.canonical(seriesPairs));
      var sourceLabel = result.source_label || "SYNTHETIC";
      var toldBy = sourceLabel === "REAL" ? "aeso-oracle" : "synthetic-series";
      var logged = 0;
      jobs.slice().sort(function (a, b) {
        return a.job_id < b.job_id ? -1 : a.job_id > b.job_id ? 1 : 0;
      }).forEach(function (job) {
        var hours = (alloc[job.job_id] || []).slice().sort(function (a, b) { return a - b; });
        if (!hours.length) return;
        var jobCost = 0;
        hours.forEach(function (h) { jobCost += prices[h][1] * job.power_mw; });
        hcsAppend("joulex-orchestrator", "orchestrator_allocate", {
          job_id: job.job_id,
          hours: hours,
          hour_starts: hours.map(function (h) { return prices[h][0]; }),
          prices: hours.map(function (h) { return prices[h][1]; }),
          power_mw: job.power_mw,
          cost: r6(jobCost),
          source_label: sourceLabel,
          price_series_hash: seriesHash,
          scheduler: "orchestrator"
        }, { told_by: toldBy, known_facts: [seriesHash] });
        logged += 1;
      });
      hcsAppend("joulex-orchestrator", "orchestrator_summary", {
        n_jobs: jobs.length,
        decisions: result.coordination_cost ? result.coordination_cost.decisions : logged,
        energy_cost: result.costs ? result.costs.orchestrator_energy : null,
        coordination_cost: result.coordination_cost ? result.coordination_cost.total : null,
        net_cost: result.costs ? result.costs.orchestrator_net : null,
        sla_pct: result.sla_pct ? result.sla_pct.orchestrator : null,
        source_label: sourceLabel,
        price_series_hash: seriesHash
      }, { told_by: toldBy, known_facts: [seriesHash] });
      return logged;
    }

    // -- views -------------------------------------------------------------
    function conservation() {
      var total = r6(Object.keys(state.agents).reduce(function (s, a) {
        return s + state.agents[a].balance;
      }, 0) + state.pool);
      var expected = r6(STARTING_POINTS * Object.keys(state.agents).length);
      return { ok: Math.abs(total - expected) < 1e-6, total: total,
               expected: expected, pool: r6(state.pool),
               agents: Object.keys(state.agents).length };
    }

    function ledgerView() {
      var balances = {};
      Object.keys(state.agents).forEach(function (a) { balances[a] = state.agents[a].balance; });
      return { balances: balances, pool: r6(state.pool), conservation: conservation() };
    }

    function advanceClock(hours) {
      state.clock = new Date(now().getTime() + hours * 3600000).toISOString();
      return state.clock;
    }

    function serialize() { return JSON.stringify(state); }

    function loadSnapshot(json) {
      var parsed = JSON.parse(json);
      Object.keys(parsed).forEach(function (k) { state[k] = parsed[k]; });
    }

    function getState() { return JSON.parse(JSON.stringify(state)); }

    // -- seed --------------------------------------------------------------
    function seed() {
      // Seed markets keep their stable IDs from data.json.
      data.seed_markets.forEach(function (sm) {
        var market = newMarket({
          question: sm.question,
          resolution_type: sm.resolution_type,
          contract_type: sm.contract_type,
          strike: sm.strike,
          window_start: sm.window_start,
          window_end: sm.window_end,
          source_url: sm.source_url,
          source_description: sm.source_description,
          resolves_at: sm.resolves_at,
          outcomes: sm.outcomes,
          b: sm.b,
          created_by: "sandbox-seed"
        }, new Date(data.sandbox_clock_start));
        market.market_id = sm.market_id;
        state.markets[market.market_id] = market;
        state.marketCounter += 1;
      });
      hcsAppend("sandbox", "sandbox_start", {
        clock: data.sandbox_clock_start,
        markets: data.seed_markets.map(function (m) { return m.market_id; }),
        note: "SANDBOX — paper only. Local HCS log; not a Hedera consensus log."
      }, { told_by: "sandbox", known_facts: [] });
      data.seed_trades.forEach(function (t) {
        // Seed trades bypass the clock (markets are OPEN at seed time).
        placeOrder(t.agent_id, { market_id: t.market_id, side: t.side,
                                 shares: t.shares, action: "buy" });
      });
      // The sandbox visitor trades as "you".
      ensureAgent("you");
      return getState();
    }

    return {
      STARTING_POINTS: STARTING_POINTS,
      now: now,
      seed: seed,
      createMarket: createMarket,
      listMarkets: listMarkets,
      getMarket: getMarket,
      placeOrder: placeOrder,
      settleMarket: settleMarket,
      advanceClock: advanceClock,
      balance: balance,
      position: position,
      positionsFor: positionsFor,
      ledgerView: ledgerView,
      conservation: conservation,
      hcsLog: function () { return JSON.parse(JSON.stringify(state.log)); },
      verifyLog: verifyLog,
      serialize: serialize,
      loadSnapshot: loadSnapshot,
      getState: getState,
      seriesHours: seriesHours,
      maxPriceOverWindow: maxPriceOverWindow,
      auditScheduler: auditScheduler,
      EngineError: EngineError
    };
  }

  return { createEngine: createEngine, EngineError: EngineError,
           STARTING_POINTS: STARTING_POINTS };
});
