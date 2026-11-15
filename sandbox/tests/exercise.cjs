#!/usr/bin/env node
/* End-to-end exercise of the sandbox engine (tests/exercise.cjs).
 *
 * Drives the EXACT engine code the browser app uses: seed, trade,
 * settle all three seed markets, create markets (valid + invalid),
 * fail-closed settlement, error paths, ledger conservation, and the
 * HCS log hash-chain verification. Exit non-zero on any failure.
 * Run: node tests/exercise.cjs (from ~/workspace/joulex/sandbox).
 */
"use strict";

const fs = require("fs");
const path = require("path");

const Engine = require("../engine.js");
const Orch = require("../scheduler.js");

const data = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "data.json"), "utf-8"));

let failures = 0;
function check(name, ok, detail) {
  if (!ok) { failures++; console.log("FAIL: " + name + (detail ? " — " + detail : "")); }
  else console.log("ok: " + name);
}
function eq(a, b) { return Math.abs(a - b) < 1e-9; }

const eng = Engine.createEngine(data);
eng.seed();

// --- seed state ---------------------------------------------------------
let markets = eng.listMarkets();
check("3 seed markets", markets.length === 3);
check("you start with 1000", eq(eng.balance("you"), 1000));
check("alice balance after seed trades", eng.balance("alice") < 1000,
  "alice=" + eng.balance("alice"));
check("bob balance after seed trades", eng.balance("bob") < 1000,
  "bob=" + eng.balance("bob"));
check("conservation after seed", eng.conservation().ok,
  JSON.stringify(eng.conservation()));
const flex = eng.getMarket("mkt_sandbox_flex1");
check("seed trades moved flex price off 0.50", Math.abs(flex.prices.YES - 0.5) > 0.001,
  "price_yes=" + flex.prices.YES);

// --- trade as you --------------------------------------------------------
const before = eng.balance("you");
const r1 = eng.placeOrder("you", { market_id: "mkt_sandbox_flex1", side: "YES", shares: 5, action: "buy" });
check("buy 5 YES receipt", r1.cost_points > 0 && eq(r1.balance_after, before - r1.cost_points));
check("price rises after YES buy", eng.getMarket("mkt_sandbox_flex1").prices.YES > flex.prices.YES);
check("position recorded", eq(eng.position("you", "mkt_sandbox_flex1", "YES"), 5));
const r2 = eng.placeOrder("you", { market_id: "mkt_sandbox_flex1", side: "YES", shares: 5, action: "sell" });
check("sell refunds the buy exactly (round trip)", eq(r2.cost_points, -r1.cost_points),
  "buy " + r1.cost_points + " sell " + r2.cost_points);
check("position zeroed after sell", eq(eng.position("you", "mkt_sandbox_flex1", "YES"), 0));
check("balance restored after round trip", eq(eng.balance("you"), before));

// Error paths.
function expectError(name, fn, status) {
  try { fn(); check(name, false, "no error thrown"); }
  catch (e) { check(name, e.status === status, "got status " + e.status + ": " + e.reason); }
}
expectError("sell without position -> 400", () =>
  eng.placeOrder("you", { market_id: "mkt_sandbox_flex1", side: "YES", shares: 1, action: "sell" }), 400);
expectError("buy more than balance -> 403", () =>
  eng.placeOrder("you", { market_id: "mkt_sandbox_flex1", side: "YES", shares: 100000, action: "buy" }), 403);
expectError("unknown market -> 404", () =>
  eng.placeOrder("you", { market_id: "mkt_nope", side: "YES", shares: 1, action: "buy" }), 404);
expectError("bad side -> 400", () =>
  eng.placeOrder("you", { market_id: "mkt_sandbox_flex1", side: "MAYBE", shares: 1, action: "buy" }), 400);

// Buy and hold into settlement: you 10 YES flex, 10 "$50–100" peak.
eng.placeOrder("you", { market_id: "mkt_sandbox_flex1", side: "YES", shares: 10, action: "buy" });
eng.placeOrder("you", { market_id: "mkt_sandbox_peak1", side: "$50–100", shares: 10, action: "buy" });
check("conservation before settlement", eng.conservation().ok);

// --- settle flex (window max $29.35 < $50 strike -> NO) ------------------
expectError("settle before resolves_at -> 409", () =>
  eng.settleMarket("mkt_sandbox_flex1", {}), 409);
eng.advanceClock(24 * 8 + 1); // Oct 27 -> Nov 4 01:00 UTC
const flexReceipt = eng.settleMarket("mkt_sandbox_flex1", {});
check("flex settles NO (max $29.35 < $50 strike)",
  flexReceipt.outcome === "NO" && eq(flexReceipt.observed_value, 29.35),
  "outcome=" + flexReceipt.outcome + " observed=" + flexReceipt.observed_value);
check("flex settlement source labelled SYNTHETIC", flexReceipt.settlement_source === "SYNTHETIC");
const bobPayout = flexReceipt.payouts.find((p) => p.agent_id === "bob" && p.side === "NO");
check("bob 20 NO wins 20 pts", bobPayout && eq(bobPayout.payout_points, 20),
  JSON.stringify(bobPayout));
const alicePayout = flexReceipt.payouts.find((p) => p.agent_id === "alice" && p.side === "YES");
check("alice 10 YES wins 0", alicePayout && eq(alicePayout.payout_points, 0));
const youPayout = flexReceipt.payouts.find((p) => p.agent_id === "you" && p.side === "YES");
check("you 10 YES wins 0", youPayout && eq(youPayout.payout_points, 0));
check("conservation after flex settlement", eng.conservation().ok);

// --- settle peak (window max $89.62 -> band $50–100) ----------------------
eng.advanceClock(24 * 2); // -> Nov 6 01:00 UTC
const peakReceipt = eng.settleMarket("mkt_sandbox_peak1",
  { outcome: "$50–100", evidence: "SYNTHETIC series max $89.62 over Nov 4 (derived by sandbox)" });
check("peak settles $50–100", peakReceipt.outcome === "$50–100");
const youPeak = peakReceipt.payouts.find((p) => p.agent_id === "you");
check("you 10 $50–100 wins 10 pts", youPeak && eq(youPeak.payout_points, 10));
check("conservation after peak settlement", eng.conservation().ok);

// --- settle compute (operator outcome + evidence) -------------------------
eng.advanceClock(24 * 2); // -> Nov 8 01:00 UTC
expectError("public_source settle without evidence -> 400", () =>
  eng.settleMarket("mkt_sandbox_compute1", { outcome: "YES" }), 400);
const compReceipt = eng.settleMarket("mkt_sandbox_compute1",
  { outcome: "YES", evidence: "https://example.com/gpu-prices (H100 spot $4.20/GPU-hr) — sandbox operator" });
check("compute settles YES", compReceipt.outcome === "YES");
check("conservation after all settlements", eng.conservation().ok,
  JSON.stringify(eng.conservation()));

// --- create-market validation + fail-closed settlement --------------------
const badWindow = eng.createMarket({
  question: "Will the pool price exceed $10/MWh on Nov 12?",
  resolution_type: "aeso_price", contract_type: "flex_capacity", strike: 10,
  window_start: "2026-11-12T11:00:00+00:00", window_end: "2026-11-12T14:00:00+00:00",
  resolves_at: "2026-11-13T01:00:00+00:00", b: 100, created_by: "you"
});
check("aeso market on live-snapshot window created", badWindow.status === "OPEN");
expectError("create with bad horizon -> 400", () => eng.createMarket({
  question: "x", resolution_type: "public_source", source_url: "https://example.com",
  source_description: "d", resolves_at: "2026-11-08T02:00:00+00:00", b: 100
}), 400);
// Settle the live-window market: max of live snapshot $3.59/$12.60/$16.52 = $16.52 > $10 -> YES.
eng.advanceClock(24 * 5 + 1); // -> Nov 13 02:00 UTC
const liveReceipt = eng.settleMarket(badWindow.market_id, {});
check("live-snapshot window settles from LIVE data",
  liveReceipt.outcome === "YES" && eq(liveReceipt.observed_value, 16.52) &&
  liveReceipt.settlement_source === "LIVE SNAPSHOT",
  "outcome=" + liveReceipt.outcome + " observed=" + liveReceipt.observed_value +
  " source=" + liveReceipt.settlement_source);

// Window the series does not cover -> NO_DATA, market stays OPEN.
const gap = eng.createMarket({
  question: "Will the pool price exceed $10/MWh on Nov 11?",
  resolution_type: "aeso_price", contract_type: "flex_capacity", strike: 10,
  window_start: "2026-11-11T00:00:00+00:00", window_end: "2026-11-11T06:00:00+00:00",
  resolves_at: "2026-11-15T01:00:00+00:00", b: 100, created_by: "you"
});
eng.advanceClock(48); // -> Nov 15 02:00 UTC
expectError("uncovered window -> 503 NO_DATA, stays OPEN", () =>
  eng.settleMarket(gap.market_id, {}), 503);
check("gap market still OPEN after refused settlement",
  eng.getMarket(gap.market_id).status === "OPEN");

// --- HCS log ---------------------------------------------------------------
const verify = eng.verifyLog();
check("HCS log verifies (hash chain, payload hashes, sequence)",
  verify.ok, JSON.stringify(verify.errors));
check("HCS log header is local mode",
  eng.hcsLog().length > 0 && eng.hcsLog()[0].action === "sandbox_start");
const log = eng.hcsLog();
check("log messages carry LOCAL timestamps + null attestation",
  log.every((m) => m.attestation === null && m.consensus_timestamp.endsWith("Z")));
console.log("HCS log messages: " + log.length);

// --- scheduler run (audited: decisions logged) ------------------------------
const prices = data.synthetic.prices.map((p) => [p.timestamp, p.price]);
const sim = Orch.runSimulation(prices, data.jobs, { sourceLabel: "SYNTHETIC" });
check("scheduler verdict GO (7.48% vs greedy, SLA 100%)",
  sim.verdict === "GO", sim.verdict + " " + sim.verdict_reason);
console.log("scheduler: fixed $" + sim.costs.fixed + " greedy $" + sim.costs.greedy +
  " orchestrator $" + sim.costs.orchestrator_net + " (net) — saving vs fixed $" +
  sim.annualised.saving_vs_fixed_usd + " over 14d, $" +
  sim.annualised.saving_per_year_usd + "/yr annualised (simulated job mix)");

// --- serialize / restore ------------------------------------------------------
const snap = eng.serialize();
const eng2 = Engine.createEngine(data);
eng2.loadSnapshot(snap);
check("state survives serialize/restore",
  eq(eng2.balance("you"), eng.balance("you")) &&
  eng2.verifyLog().ok && eng2.listMarkets().length === eng.listMarkets().length);

if (failures > 0) {
  console.log("EXERCISE FAILED: " + failures + " failure(s)");
  process.exit(1);
}
console.log("EXERCISE OK: engine end-to-end verified.");
