const fs = require("fs");
const path = require("path");
/* Headless-browser smoke test of the BUILT joulex-sandbox.html.
 * Needs jsdom: npm install jsdom (in /tmp or a scratch dir — do not
 * commit node_modules). Run: node tests/smoke.cjs (from the sandbox
 * dir). The HTML path is resolved relative to this file so the test
 * runs from any checkout directory, including a CI runner. */
const { JSDOM } = require("jsdom");
const html = fs.readFileSync(path.join(__dirname, "..", "joulex-sandbox.html"), "utf-8");

const dom = new JSDOM(html, {
  url: "https://sandbox.example/joulex-sandbox.html",
  runScripts: "dangerously",
  pretendToBeVisual: true,
});
const { window } = dom;
const { document } = window;

let failures = 0;
function check(name, ok, detail) {
  if (!ok) { failures++; console.log("FAIL: " + name + (detail ? " — " + detail : "")); }
  else console.log("ok: " + name);
}
function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

(async () => {
  await sleep(300); // let DOMContentLoaded + init run
  check("banner present", document.querySelector(".banner").textContent.includes("PAPER ONLY"));
  check("3 market cards render", document.querySelectorAll("#markets-open .card").length === 3,
    "got " + document.querySelectorAll("#markets-open .card").length);
  check("clock renders MT time", document.getElementById("clock-mt").textContent.includes("2026"));
  check("balance shows 1000", document.getElementById("agent-balance").textContent.includes("1000.00"));
  check("conservation line OK", document.getElementById("conservation").textContent.includes("conserved"));
  check("HCS log renders", document.querySelectorAll("#log-body tr").length >= 5,
    "rows=" + document.querySelectorAll("#log-body tr").length);
  check("log verifies", document.getElementById("log-status").textContent.includes("verifies"));
  check("live snapshot renders", document.getElementById("live-snapshot").textContent.includes("LIVE SNAPSHOT"));
  check("suppliers panel says 0 onboarded",
    document.body.textContent.includes("0 — NONE YET"));

  // Buy 10 YES on the flex market as you.
  const flexCard = document.getElementById("card-mkt_sandbox_flex1");
  const buyBtn = flexCard.querySelector("button[data-action='buy']");
  buyBtn.click();
  await sleep(50);
  const balAfter = document.getElementById("agent-balance").textContent;
  check("buy reduces balance", !balAfter.includes("1000.00"), balAfter);
  check("position row appears", document.getElementById("positions").textContent.includes("YES"));

  // Jump clock to next resolvable, settle flex.
  document.getElementById("jump-next").click();
  await sleep(50);
  const flexCard2 = document.getElementById("card-mkt_sandbox_flex1");
  const settleBtn = flexCard2.querySelector("button[data-action='settle-aeso']");
  check("settle button appears after jump", !!settleBtn);
  if (settleBtn) {
    settleBtn.click();
    await sleep(50);
    check("flex resolves NO in UI",
      document.getElementById("markets-resolved").textContent.includes("RESOLVED"));
    check("notice reports settlement",
      document.getElementById("notice").textContent.includes("Settled"));
  }

  // Create a market via the form.
  const form = document.getElementById("create-form");
  form.querySelector("[name=question]").value = "Will the pool price exceed $200/MWh on Nov 9?";
  form.querySelector("[name=resolves_at]").value = "2026-11-09T00:00";
  document.getElementById("create-market").click();
  await sleep(50);
  check("created market appears", document.querySelectorAll("#markets-open .card").length >= 3,
    "cards=" + document.querySelectorAll("#markets-open .card").length);

  // Run the scheduler.
  document.getElementById("run-scheduler").click();
  await sleep(1500);
  const sched = document.getElementById("scheduler-result").textContent;
  check("scheduler verdict renders", sched.includes("VERDICT: GO"),
    sched.slice(0, 120));
  check("scheduler auditable note", sched.includes("auditable 100%"));

  // Reset restores seed state.
  document.getElementById("reset-sandbox").click();
  await sleep(50);
  check("reset restores 3 open markets",
    document.querySelectorAll("#markets-open .card").length === 3);
  check("reset restores balance",
    document.getElementById("agent-balance").textContent.includes("1000.00"));

  if (failures > 0) { console.log("SMOKE FAILED: " + failures); process.exit(1); }
  console.log("SMOKE OK: sandbox app works end-to-end in a headless browser.");
  process.exit(0);
})().catch((e) => { console.error("SMOKE ERROR", e); process.exit(1); });
