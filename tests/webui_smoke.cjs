/* Headless-browser smoke of the SERVED UI (M1.5).
 * Usage: node tests/webui_smoke.cjs <base_url> <market_id>
 * Loads <base_url>/app in jsdom with Node's fetch injected, then
 * drives the DOM the way a stranger would: markets render from the
 * live API, load the demo agent, place a trade through the order
 * form, positions/ledger/audit update. Needs jsdom (npm install
 * --no-save jsdom in the repo root; not committed).
 * Exit 0 + "WEBUI SMOKE OK" iff every check passes.
 */
"use strict";
const { JSDOM } = require("jsdom");

const [baseUrl, marketId] = process.argv.slice(2);
if (!baseUrl || !marketId) { console.error("usage: webui_smoke.cjs <base_url> <market_id>"); process.exit(2); }

let failures = 0;
function check(name, ok, detail) {
  if (!ok) { failures++; console.log("FAIL: " + name + (detail ? " — " + detail : "")); }
  else console.log("ok: " + name);
}
function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }
async function waitFor(fn, timeoutMs, label) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try { if (fn()) return true; } catch { /* keep waiting */ }
    await sleep(100);
  }
  console.log("TIMEOUT waiting for: " + label);
  return false;
}

(async () => {
  const res = await fetch(baseUrl + "/app");
  const html = await res.text();
  const dom = new JSDOM(html, {
    url: baseUrl + "/app",
    runScripts: "dangerously",
    resources: "usable", // load the page's <script src> files from the server
    pretendToBeVisual: true,
    beforeParse(window) {
      // jsdom has no fetch; inject Node's, resolving relative URLs
      // against the page URL the way a browser does.
      window.fetch = (url, opts) => fetch(new URL(url, window.location.href), opts);
    },
  });
  const { document } = dom.window;

  await sleep(500); // let the app's initial fetches land
  check("banner present", document.querySelector("#banner").textContent.includes("PAPER ONLY"));
  check("health renders", document.getElementById("health-line").textContent.includes("ok") ||
    document.getElementById("health-line").textContent.includes("markets"));
  const card = await waitFor(
    () => document.querySelector(`.market-card[data-market-id="${marketId}"]`),
    5000, "market card " + marketId);
  check("seeded market renders from live API", !!card);

  // Load the demo agent (demo-clock server only).
  document.getElementById("demo-agent-btn").click();
  const agentLoaded = await waitFor(
    () => document.getElementById("agent-status").textContent.includes("agent-demo-alice"),
    5000, "demo agent load");
  check("demo agent loads", agentLoaded,
    document.getElementById("agent-status").textContent);

  // Place a trade through the DOM order form.
  const select = document.getElementById("order-market");
  select.value = marketId;
  document.getElementById("order-side").value = "YES";
  document.getElementById("order-shares").value = "10";
  document.getElementById("place-order-btn").click();
  const receiptShown = await waitFor(
    () => document.getElementById("order-result").textContent.includes("trd_"),
    5000, "order receipt");
  check("order form places a trade", receiptShown,
    document.getElementById("order-result").textContent.slice(0, 160));
  const posShown = await waitFor(
    () => document.getElementById("positions-list").textContent.includes("YES"),
    5000, "positions update");
  check("positions update after trade", posShown,
    document.getElementById("positions-list").textContent.slice(0, 160));
  check("ledger view conserves", document.getElementById("ledger-view").textContent.includes("conserved"),
    document.getElementById("ledger-view").textContent.slice(0, 160));
  const auditRows = document.querySelectorAll("#audit-log tbody tr").length;
  check("audit log renders rows", auditRows >= 1, "rows=" + auditRows);

  if (failures > 0) { console.log("WEBUI SMOKE FAIL: " + failures + " failures"); process.exit(1); }
  console.log("WEBUI SMOKE OK");
})().catch((e) => { console.log("WEBUI SMOKE ERROR: " + (e && e.stack || e)); process.exit(1); });
