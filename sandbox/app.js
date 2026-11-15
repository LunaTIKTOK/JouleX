/* JouleX Sandbox — browser UI layer.
 *
 * Thin DOM binding over JoulexEngine (the tested engine). No pricing
 * math here: quotes use JoulexLMSR directly on the market's own q/b,
 * trades and settlements go through the engine. State persists per
 * visitor in localStorage; nothing leaves the browser.
 */
(function () {
  "use strict";

  var LS_KEY = "joulex-sandbox-v1";
  var eng = null;
  var activeAgent = "you";
  var autoTimer = null;
  var chartMarketId = "mkt_sandbox_flex1";
  var lastNotice = "";

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function fmt(n, dp) { return Number(n).toFixed(dp === undefined ? 2 : dp); }
  function fmtMT(iso) {
    try {
      return new Intl.DateTimeFormat("en-CA", {
        timeZone: "America/Edmonton", year: "numeric", month: "short",
        day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false
      }).format(new Date(iso)) + " MT";
    } catch (e) { return iso; }
  }
  function fmtUTC(iso) {
    var d = new Date(iso);
    return d.toISOString().replace("T", " ").slice(0, 16) + " UTC";
  }

  function notice(msg, isError) {
    lastNotice = msg;
    var n = $("notice");
    n.textContent = msg;
    n.className = isError ? "notice err" : "notice ok";
    n.style.display = "block";
  }

  function save() {
    try { localStorage.setItem(LS_KEY, eng.serialize()); } catch (e) { /* private mode */ }
  }

  function init() {
    eng = JoulexEngine.createEngine(JOULEX_DATA);
    var restored = false;
    try {
      var snap = localStorage.getItem(LS_KEY);
      if (snap) { eng.loadSnapshot(snap); restored = true; }
    } catch (e) { restored = false; }
    if (!restored || eng.listMarkets().length === 0) {
      eng = JoulexEngine.createEngine(JOULEX_DATA);
      eng.seed();
    }
    renderAll();
  }

  // -- clock -------------------------------------------------------------
  function renderClock() {
    var now = eng.now();
    $("clock-mt").textContent = fmtMT(now.toISOString());
    $("clock-utc").textContent = fmtUTC(now.toISOString());
    var open = eng.listMarkets().filter(function (m) { return m.status === "OPEN"; });
    var next = null;
    open.forEach(function (m) {
      var r = new Date(m.resolves_at);
      if (r > now && (!next || r < next)) next = r;
    });
    $("clock-next").textContent = next ?
      "Next market resolvable: " + fmtMT(next.toISOString()) : "No open markets.";
  }

  // -- markets -------------------------------------------------------------
  function marketCard(m) {
    var now = eng.now();
    var resolvable = m.status === "OPEN" && now >= new Date(m.resolves_at);
    var rows = m.outcomes.map(function (o) {
      var p = m.prices[o];
      var pos = eng.position(activeAgent, m.market_id, o);
      return "<div class='outcome'><span class='olabel'>" + esc(o) + "</span>" +
        "<div class='bar'><div class='fill' style='width:" + (p * 100).toFixed(1) + "%'></div></div>" +
        "<span class='oprice'>" + (p * 100).toFixed(1) + "¢</span>" +
        (pos ? "<span class='opos'>you hold " + fmt(pos, 2) + "</span>" : "") +
        "</div>";
    }).join("");
    var trade = "";
    if (m.status === "OPEN" && now < new Date(m.resolves_at)) {
      var opts = m.outcomes.map(function (o) {
        return "<option value='" + esc(o) + "'>" + esc(o) + "</option>";
      }).join("");
      trade = "<div class='trade' data-market='" + m.market_id + "'>" +
        "<select class='tside'>" + opts + "</select>" +
        "<input class='tshares' type='number' min='0.01' step='0.01' value='10' aria-label='shares'>" +
        "<button data-action='buy' data-market='" + m.market_id + "'>Buy</button>" +
        "<button data-action='sell' data-market='" + m.market_id + "'>Sell</button>" +
        "<span class='quote'></span></div>";
    }
    var settle = "";
    if (resolvable) {
      if (m.resolution_type === "aeso_price") {
        settle = "<button class='settle' data-action='settle-aeso' data-market='" + m.market_id + "'>" +
          "Settle from price series</button>" +
          "<span class='settle-note'>Max hourly pool price over the window vs $" +
          fmt(m.strike, 0) + "/MWh strike.</span>";
      } else {
        var oopts = m.outcomes.map(function (o) {
          return "<option value='" + esc(o) + "'>" + esc(o) + "</option>";
        }).join("");
        settle = "<div class='settle-box'><select class='settle-outcome'>" + oopts + "</select>" +
          "<input class='settle-evidence' type='text' placeholder='Evidence URL / source note' " +
          "value='" + esc(m.resolution_source_url || "") + "'>" +
          "<button data-action='settle-public' data-market='" + m.market_id + "'>Settle</button></div>" +
          "<span class='settle-note'>Sandbox operator settlement — production requires " +
          "operator key + valid attestation.</span>";
      }
    }
    var resolved = "";
    if (m.status === "RESOLVED") {
      resolved = "<div class='resolved'>RESOLVED: <b>" + esc(m.outcome) + "</b>" +
        (m.observed_value !== null && m.observed_value !== undefined ?
          " — observed max $" + fmt(m.observed_value, 2) + "/MWh" : "") +
        (m.settlement_source ? " (" + esc(m.settlement_source) + ")" : "") +
        "<br><span class='dim'>Evidence: " + esc(m.evidence || "—") + "</span></div>";
    }
    return "<div class='card' id='card-" + m.market_id + "'>" +
      "<div class='mkt-q'>" + esc(m.question) + "</div>" +
      "<div class='mkt-meta'>" + esc(m.contract_type) + " · " + esc(m.resolution_type) +
      (m.strike !== null && m.strike !== undefined ? " · strike $" + fmt(m.strike, 0) + "/MWh" : "") +
      " · b=" + fmt(m.b, 0) + " · " + esc(m.status) +
      " · resolves " + fmtMT(m.resolves_at) + "</div>" +
      (m.window_start ? "<div class='dim'>Window: " + fmtMT(m.window_start) + " → " + fmtMT(m.window_end) + "</div>" : "") +
      "<div class='dim'>Source: " + esc(m.resolution_source_description || m.resolution_source_url || "—") + "</div>" +
      rows + trade + settle + resolved +
      "<div class='chart-link'><button data-action='chart' data-market='" + m.market_id + "'>" +
      "Show on price chart</button></div>" +
      "</div>";
  }

  function renderMarkets() {
    var markets = eng.listMarkets();
    var open = markets.filter(function (m) { return m.status === "OPEN"; });
    var done = markets.filter(function (m) { return m.status !== "OPEN"; });
    $("markets-open").innerHTML = open.map(marketCard).join("") ||
      "<p class='dim'>No open markets. Create one below.</p>";
    $("markets-resolved").innerHTML = done.map(marketCard).join("") ||
      "<p class='dim'>Nothing settled yet.</p>";
    // Cost previews.
    document.querySelectorAll(".trade").forEach(function (box) {
      var mid = box.getAttribute("data-market");
      updateQuote(box, mid);
    });
  }

  function updateQuote(box, mid) {
    var m;
    try { m = eng.getMarket(mid); } catch (e) { return; }
    var side = box.querySelector(".tside").value;
    var shares = parseFloat(box.querySelector(".tshares").value);
    var q = box.querySelector(".quote");
    if (!isFinite(shares) || shares <= 0) { q.textContent = ""; return; }
    var idx = m.outcomes.indexOf(side);
    try {
      var cost = JoulexLMSR.tradeCostMulti(m.q, m.b, idx, shares);
      q.textContent = "Buy " + fmt(shares, 2) + " " + side + " ≈ " + fmt(cost, 4) + " pts" +
        " (price " + (m.prices[side] * 100).toFixed(1) + "¢)";
    } catch (e) { q.textContent = ""; }
  }

  // -- portfolio + ledger ---------------------------------------------------
  function renderPortfolio() {
    var bal = eng.balance(activeAgent);
    $("agent-name").textContent = activeAgent;
    $("agent-balance").textContent = fmt(bal, 2) + " pts";
    var pos = eng.positionsFor(activeAgent);
    $("positions").innerHTML = pos.length ? pos.map(function (p) {
      var m = null;
      try { m = eng.getMarket(p.market_id); } catch (e) { return ""; }
      var value = p.shares * (m.status === "RESOLVED" ?
        (p.side === m.outcome ? 1 : 0) : m.prices[p.side]);
      return "<tr><td>" + esc(m.question.slice(0, 60)) + "…</td><td>" + esc(p.side) +
        "</td><td>" + fmt(p.shares, 2) + "</td><td>" + fmt(value, 2) + " pts</td></tr>";
    }).join("") : "<tr><td colspan='4' class='dim'>No open positions.</td></tr>";
    var lv = eng.ledgerView();
    $("ledger").innerHTML = Object.keys(lv.balances).sort().map(function (a) {
      return "<tr><td>" + esc(a) + "</td><td>" + fmt(lv.balances[a], 2) + "</td></tr>";
    }).join("") + "<tr><td>pool (market maker)</td><td>" + fmt(lv.pool, 2) + "</td></tr>";
    var c = lv.conservation;
    $("conservation").textContent = c.ok ? "✓ conserved: Σ balances + pool = " +
      fmt(c.total, 2) + " = " + fmt(c.expected, 2) + " (1,000 × " + c.agents + " agents)" :
      "✗ BROKEN: total " + fmt(c.total, 2) + " ≠ expected " + fmt(c.expected, 2);
    $("conservation").className = c.ok ? "ok" : "err";
  }

  // -- AESO chart ------------------------------------------------------------
  function renderChart() {
    var s = JOULEX_DATA.synthetic.stats;
    $("price-stats").textContent = "SYNTHETIC series: mean $" + s.mean + "/MWh · " +
      s.zero_hours + " zero-price hours (10.7%) · " + s.cap_hours + " cap hour at $999.99 · " +
      "max $" + s.max + " · min $" + s.min + ". Built from verified 2025 AESO statistics " +
      "(avg $43.68/MWh, 10.6% zero-price hours, $999.99 cap). NOT observed AESO data.";
    var live = JOULEX_DATA.live_snapshot;
    $("live-snapshot").innerHTML = "<b>LIVE SNAPSHOT — observed AESO data</b> · fetched " +
      fmtMT(live.fetched_at) + " from the public ETS report<br>" +
      live.hours.map(function (h) {
        return fmtMT(h.timestamp) + ": <b>$" + fmt(h.price, 2) + "/MWh</b>";
      }).join(" · ") +
      "<br><span class='dim'>Source: " + esc(live.source_url) + "<br>" +
      "The live report covers only the recent ~3 hours; the historical report returns HTTP 403. " +
      "A browser cannot fetch this endpoint directly (no CORS headers, plain HTTP) — hence the embedded snapshot.</span>";
    var cv = $("price-chart");
    if (!cv || !cv.getContext) return;
    var ctx = null;
    try { ctx = cv.getContext("2d"); } catch (e) { ctx = null; }
    if (!ctx) return; // headless / no canvas: text panels above still render
    var W = cv.width, H = cv.height;
    ctx.clearRect(0, 0, W, H);
    var series = JOULEX_DATA.synthetic.prices;
    var maxShow = 150; // cap hour $999.99 clipped; labelled below
    var minT = new Date(series[0].timestamp).getTime();
    var maxT = new Date(series[series.length - 1].timestamp).getTime();
    function X(ts) { return 40 + (new Date(ts).getTime() - minT) / (maxT - minT) * (W - 50); }
    function Y(p) { return H - 25 - Math.min(p, maxShow) / maxShow * (H - 45); }
    // Window shading for selected market.
    var sel = null;
    try { sel = eng.getMarket(chartMarketId); } catch (e) { sel = null; }
    if (sel && sel.window_start) {
      ctx.fillStyle = "rgba(255, 200, 0, 0.18)";
      ctx.fillRect(X(sel.window_start), 10, X(sel.window_end) - X(sel.window_start), H - 35);
      ctx.fillStyle = "#8a6d00";
      ctx.font = "11px sans-serif";
      ctx.fillText("window: " + sel.market_id, X(sel.window_start) + 4, 22);
    }
    // Gridlines + labels.
    ctx.strokeStyle = "#e5e5e5"; ctx.fillStyle = "#666"; ctx.font = "11px sans-serif";
    [0, 50, 100, 150].forEach(function (v) {
      ctx.beginPath(); ctx.moveTo(40, Y(v)); ctx.lineTo(W - 10, Y(v)); ctx.stroke();
      ctx.fillText("$" + v, 4, Y(v) + 4);
    });
    // Synthetic line.
    ctx.strokeStyle = "#2563eb"; ctx.lineWidth = 1.5; ctx.beginPath();
    series.forEach(function (p, i) {
      var x = X(p.timestamp), y = Y(p.price);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
    // Cap hour marker.
    series.forEach(function (p) {
      if (p.price >= 999.99) {
        ctx.fillStyle = "#dc2626";
        ctx.beginPath(); ctx.arc(X(p.timestamp), Y(150), 4, 0, 7); ctx.fill();
      }
    });
    // Live snapshot points.
    ctx.fillStyle = "#16a34a";
    JOULEX_DATA.live_snapshot.hours.forEach(function (p) {
      ctx.beginPath(); ctx.arc(X(p.timestamp), Y(p.price), 4, 0, 7); ctx.fill();
    });
    ctx.fillStyle = "#333"; ctx.font = "12px sans-serif";
    ctx.fillText("Blue: SYNTHETIC 14-day series (seed 20261110) · Green dots: LIVE SNAPSHOT Nov 12, 05:00–08:00 MT · Red dot: $999.99 cap hour (clipped at $150 for display)",
      40, H - 6);
  }

  // -- HCS log ---------------------------------------------------------------
  function renderLog() {
    var log = eng.hcsLog();
    var v = eng.verifyLog();
    $("log-status").textContent = v.ok ?
      "✓ Log verifies: " + v.messages + " messages, hash chain intact, sequence continuous." :
      "✗ LOG BROKEN: " + v.errors.join("; ");
    $("log-status").className = v.ok ? "ok" : "err";
    var rows = log.slice(-30).reverse().map(function (m) {
      return "<tr><td>" + m.sequence + "</td><td>" + esc(m.consensus_timestamp.slice(0, 19)) +
        "Z (LOCAL)</td><td>" + esc(m.sender) + "</td><td>" + esc(m.action) + "</td>" +
        "<td class='mono'>" + esc(m.payload_hash.slice(0, 12)) + "…</td></tr>";
    }).join("");
    $("log-body").innerHTML = rows || "<tr><td colspan='5' class='dim'>No messages yet.</td></tr>";
    $("log-count").textContent = log.length + " messages (showing latest 30)";
  }

  // -- scheduler ---------------------------------------------------------------
  function runScheduler() {
    var btn = $("run-scheduler");
    btn.disabled = true;
    btn.textContent = "Running…";
    setTimeout(function () {
      try {
        var prices = JOULEX_DATA.synthetic.prices.map(function (p) { return [p.timestamp, p.price]; });
        var sim = JoulexOrchestrator.runSimulation(prices, JOULEX_DATA.jobs, { sourceLabel: "SYNTHETIC" });
        var logged = eng.auditScheduler(prices, JOULEX_DATA.jobs, sim.allocations.orchestrator, sim);
        save();
        $("scheduler-result").innerHTML =
          "<table><tr><th>Scheduler</th><th>Energy cost</th><th>SLA</th></tr>" +
          "<tr><td>fixed (run at arrival)</td><td>$" + fmt(sim.costs.fixed, 2) + "</td><td>" + fmt(sim.sla_pct.fixed, 1) + "%</td></tr>" +
          "<tr><td>greedy (earliest-deadline-first)</td><td>$" + fmt(sim.costs.greedy, 2) + "</td><td>" + fmt(sim.sla_pct.greedy, 1) + "%</td></tr>" +
          "<tr><td>orchestrator (net of coordination)</td><td>$" + fmt(sim.costs.orchestrator_net, 2) + "</td><td>" + fmt(sim.sla_pct.orchestrator, 1) + "%</td></tr></table>" +
          "<p>Improvement vs greedy: <b>" + fmt(sim.improvement_pct.vs_greedy, 2) + "%</b> (target ≥ 5%) · " +
          "vs fixed: " + fmt(sim.improvement_pct.vs_fixed, 2) + "% · coordination: " +
          sim.coordination_cost.decisions + " decisions × $0.01 = $" + fmt(sim.coordination_cost.total, 2) + "</p>" +
          "<p>Annualised saving vs fixed (this simulated job mix): <b>$" +
          fmt(sim.annualised.saving_per_year_usd, 0) + "/yr</b> ($" +
          fmt(sim.annualised.saving_per_mwh, 2) + "/MWh over " + fmt(sim.annualised.energy_mwh, 1) +
          " MWh) — Phase 1 §2.3 band for 1 MW pure arbitrage is tens of thousands/yr, not hundreds of thousands.</p>" +
          "<p><b>VERDICT: " + sim.verdict + "</b> — " + esc(sim.verdict_reason) + "</p>" +
          "<p class='dim'>" + logged + " allocation decisions + summary written to the HCS audit log " +
          "(auditable 100%). price-only is not the bar: it cheats on SLA (" +
          fmt(sim.sla_pct.price_only, 1) + "%).</p>";
        notice("Scheduler run complete: " + sim.verdict + " — " + sim.improvement_pct.vs_greedy.toFixed(2) + "% vs greedy.");
        renderAll();
      } catch (e) {
        notice("Scheduler failed: " + e.message, true);
      }
      btn.disabled = false;
      btn.textContent = "Run scheduler on SYNTHETIC series";
    }, 30);
  }

  // -- actions ---------------------------------------------------------------
  // One failing panel must never brick the rest: each renderer is
  // isolated, and handlers are wired before init() runs, so a render
  // error cannot leave the page unresponsive.
  function renderAll() {
    ["renderClock", "renderMarkets", "renderPortfolio", "renderChart", "renderLog"].forEach(function (fn) {
      try {
        ({ renderClock: renderClock, renderMarkets: renderMarkets,
           renderPortfolio: renderPortfolio, renderChart: renderChart,
           renderLog: renderLog })[fn]();
      } catch (e) {
        if (window.console && console.error) console.error(fn + " failed: " + e.message);
      }
    });
  }

  function doTrade(marketId, action) {
    var box = document.querySelector(".trade[data-market='" + marketId + "']");
    if (!box) return;
    var side = box.querySelector(".tside").value;
    var shares = parseFloat(box.querySelector(".tshares").value);
    try {
      var r = eng.placeOrder(activeAgent, { market_id: marketId, side: side, shares: shares, action: action });
      save();
      notice(action + " " + fmt(shares, 2) + " " + side + " on " + marketId +
        " for " + fmt(Math.abs(r.cost_points), 4) + " pts. Balance: " + fmt(r.balance_after, 2) + " pts.");
      renderAll();
    } catch (e) {
      notice("Order refused (" + (e.status || "?") + "): " + (e.reason || e.message), true);
    }
  }

  function doSettle(marketId, kind, card) {
    try {
      var receipt;
      if (kind === "aeso") {
        receipt = eng.settleMarket(marketId, {});
      } else {
        var outcome = card.querySelector(".settle-outcome").value;
        var evidence = card.querySelector(".settle-evidence").value.trim();
        receipt = eng.settleMarket(marketId, { outcome: outcome, evidence: evidence });
      }
      save();
      notice("Settled " + marketId + ": " + receipt.outcome +
        (receipt.observed_value !== null && receipt.observed_value !== undefined ?
          " (observed max $" + fmt(receipt.observed_value, 2) + "/MWh, " + receipt.settlement_source + ")" : "") +
        ". Payouts: " + receipt.payouts.filter(function (p) { return p.payout_points > 0; })
          .map(function (p) { return p.agent_id + " +" + fmt(p.payout_points, 2); }).join(", "));
      renderAll();
    } catch (e) {
      notice("Settlement refused (" + (e.status || "?") + "): " + (e.reason || e.message), true);
    }
  }

  function doCreateMarket() {
    var f = $("create-form");
    var type = f.querySelector("[name=rectype]").value;
    var fields = {
      question: f.querySelector("[name=question]").value,
      resolution_type: type === "aeso" ? "aeso_price" : "public_source",
      contract_type: type === "aeso" ? "flex_capacity" :
        (type === "compute" ? "compute_forward" : "generic"),
      resolves_at: new Date(f.querySelector("[name=resolves_at]").value).toISOString(),
      b: parseFloat(f.querySelector("[name=b]").value) || 100,
      created_by: activeAgent
    };
    if (type === "aeso") {
      fields.strike = parseFloat(f.querySelector("[name=strike]").value);
      fields.window_start = new Date(f.querySelector("[name=window_start]").value).toISOString();
      fields.window_end = new Date(f.querySelector("[name=window_end]").value).toISOString();
    } else {
      fields.source_url = f.querySelector("[name=source_url]").value;
      fields.source_description = f.querySelector("[name=source_description]").value;
      var oc = f.querySelector("[name=outcomes]").value.trim();
      if (oc) fields.outcomes = oc.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
    }
    try {
      var m = eng.createMarket(fields);
      save();
      notice("Market created: " + m.market_id + " — " + m.question);
      f.reset();
      renderAll();
    } catch (e) {
      notice("Market creation refused (" + (e.status || "?") + "): " + (e.reason || e.message), true);
    }
  }

  function toggleCreateFields() {
    var type = document.querySelector("#create-form [name=rectype]").value;
    document.querySelectorAll("#create-form .aeso-only").forEach(function (el) {
      el.style.display = type === "aeso" ? "block" : "none";
    });
    document.querySelectorAll("#create-form .public-only").forEach(function (el) {
      el.style.display = type === "aeso" ? "none" : "block";
    });
  }

  function jumpNext() {
    var now = eng.now();
    var next = null;
    eng.listMarkets().forEach(function (m) {
      if (m.status !== "OPEN") return;
      var r = new Date(m.resolves_at);
      if (r > now && (!next || r < next)) next = r;
    });
    if (!next) { notice("No open market left to jump to."); return; }
    var hours = Math.ceil((next.getTime() - now.getTime()) / 3600000) + 1;
    eng.advanceClock(hours);
    save();
    notice("Clock advanced " + hours + "h to " + fmtMT(eng.now().toISOString()) + ".");
    renderAll();
  }

  function autoTrade() {
    var markets = eng.listMarkets().filter(function (m) {
      return m.status === "OPEN" && eng.now() < new Date(m.resolves_at);
    });
    if (!markets.length) return;
    var m = markets[Math.floor(Math.random() * markets.length)];
    var agent = Math.random() < 0.5 ? "alice" : "bob";
    var side = m.outcomes[Math.floor(Math.random() * m.outcomes.length)];
    var shares = 1 + Math.floor(Math.random() * 5);
    try {
      eng.placeOrder(agent, { market_id: m.market_id, side: side, shares: shares, action: "buy" });
      save();
      renderMarkets(); renderPortfolio(); renderLog();
    } catch (e) { /* insufficient points etc. — skip quietly */ }
  }

  // -- wire up ---------------------------------------------------------------
  document.addEventListener("DOMContentLoaded", function () {
    // Wire handlers FIRST: init() renders, and a render failure must
    // never leave the page without working controls.
    toggleCreateFields();
    document.body.addEventListener("click", function (ev) {
      var t = ev.target;
      var action = t.getAttribute && t.getAttribute("data-action");
      if (!action) return;
      if (action === "buy" || action === "sell") doTrade(t.getAttribute("data-market"), action);
      if (action === "settle-aeso") doSettle(t.getAttribute("data-market"), "aeso", t.closest(".card"));
      if (action === "settle-public") doSettle(t.getAttribute("data-market"), "public", t.closest(".card"));
      if (action === "chart") {
        chartMarketId = t.getAttribute("data-market");
        renderChart();
        document.getElementById("price-panel").scrollIntoView({ behavior: "smooth" });
      }
    });
    document.body.addEventListener("input", function (ev) {
      var box = ev.target.closest && ev.target.closest(".trade");
      if (box) updateQuote(box, box.getAttribute("data-market"));
    });
    document.body.addEventListener("change", function (ev) {
      if (ev.target && ev.target.name === "rectype") toggleCreateFields();
      if (ev.target && ev.target.id === "auto-trader") {
        if (ev.target.checked) { autoTimer = setInterval(autoTrade, 5000); }
        else if (autoTimer) { clearInterval(autoTimer); autoTimer = null; }
      }
    });
    $("advance-1h").addEventListener("click", function () { eng.advanceClock(1); save(); renderAll(); });
    $("advance-6h").addEventListener("click", function () { eng.advanceClock(6); save(); renderAll(); });
    $("advance-24h").addEventListener("click", function () { eng.advanceClock(24); save(); renderAll(); });
    $("jump-next").addEventListener("click", jumpNext);
    $("run-scheduler").addEventListener("click", runScheduler);
    $("create-market").addEventListener("click", doCreateMarket);
    $("reset-sandbox").addEventListener("click", function () {
      try { localStorage.removeItem(LS_KEY); } catch (e) {}
      eng = JoulexEngine.createEngine(JOULEX_DATA);
      eng.seed();
      save();
      notice("Sandbox reset to seed state.");
      renderAll();
    });
    document.querySelectorAll("[data-agent]").forEach(function (b) {
      b.addEventListener("click", function () {
        activeAgent = b.getAttribute("data-agent");
        document.querySelectorAll("[data-agent]").forEach(function (x) {
          x.classList.toggle("active", x === b);
        });
        renderMarkets(); renderPortfolio();
      });
    });
    init();
  });
})();
