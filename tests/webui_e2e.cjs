/* End-to-end browser flow against the live server (M1.5).
 * Usage: node tests/webui_e2e.cjs <base_url> <market_id>
 * Loads the EXACT browser files (joulex/webui/ed25519.js,
 * joulex/webui/sha256.js) and drives the API the way app.js does:
 * fetch demo agents -> GET /challenge -> sign the order presentation
 * in JS -> POST /orders with X-CF-KYA -> verify receipt, positions,
 * ledger conservation and the HCS audit log. No Python client is
 * used for the order itself — the signature is pure JS.
 * Exit 0 + "WEBUI E2E OK" iff every step matches the live server.
 */
"use strict";
const path = require("path");
const ROOT = path.resolve(__dirname, "..");
const Ed = require(path.join(ROOT, "joulex", "webui", "ed25519.js"));
const Hash = require(path.join(ROOT, "joulex", "webui", "sha256.js"));

const [baseUrl, marketId] = process.argv.slice(2);
if (!baseUrl || !marketId) { console.error("usage: webui_e2e.cjs <base_url> <market_id>"); process.exit(2); }

let failures = 0;
function check(name, ok, detail) {
  if (!ok) { failures++; console.log("FAIL: " + name + (detail ? " — " + detail : "")); }
  else console.log("ok: " + name);
}

async function req(method, p, body, headers) {
  const res = await fetch(baseUrl + p, {
    method,
    headers: { "Content-Type": "application/json", ...(headers || {}) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  let json = null;
  try { json = JSON.parse(text); } catch { /* non-JSON body */ }
  return { status: res.status, json, text };
}

function b64url(buf) { return Buffer.from(buf).toString("base64url"); }

(async () => {
  // The served shell itself.
  const app = await req("GET", "/app");
  check("GET /app 200 html", app.status === 200 && app.text.includes("PAPER ONLY"), app.status + " " + app.text.slice(0, 80));

  // Demo agent bundle (demo-clock server only, labelled DEMO ONLY).
  const agents = await req("GET", "/__demo/agents");
  check("GET /__demo/agents 200", agents.status === 200 && agents.json && agents.json.alice, agents.status);
  const alice = agents.json.alice;
  const seed = Buffer.from(alice.agent_seed_hex, "hex");
  const pub = Buffer.from(alice.credential.agent.public_key_hex, "hex");
  check("seed matches credential key",
    Buffer.from(Ed.publicKeyFromSeed(seed)).toString("hex") === alice.credential.agent.public_key_hex);

  // Challenge -> sign the order presentation in JS.
  const ch = await req("GET", "/challenge");
  check("GET /challenge 200", ch.status === 200 && ch.json.challenge, ch.status);
  const payload = {
    agent_id: alice.agent_id,
    market_id: marketId,
    shares: 10.0,
    side: "YES",
    action: "buy",
  };
  const payloadHash = Hash.sha256Hex(Hash.canonical(payload));
  const signed = { challenge: ch.json.challenge, payload_hash: payloadHash, timestamp: ch.json.server_time };
  const sig = Buffer.from(Ed.sign(Buffer.from(Hash.canonical(signed), "utf8"), seed, pub)).toString("hex");
  const presentation = {
    credential: alice.credential,
    challenge: ch.json.challenge,
    payload_hash: payloadHash,
    timestamp: ch.json.server_time,
    presentation_signature_hex: sig,
  };
  const order = await req("POST", "/orders", payload,
    { "X-CF-KYA": b64url(Buffer.from(Hash.canonical(presentation), "utf8")) });
  check("POST /orders 200 receipt", order.status === 200 && order.json && order.json.trade_id,
    order.status + " " + (order.text || "").slice(0, 200));
  check("receipt is a buy of 10 YES",
    order.json && order.json.side === "YES" && order.json.shares === 10,
    JSON.stringify(order.json));

  // Positions, ledger, audit trail — all from the live API.
  const pos = await req("GET", "/positions?agent_id=" + encodeURIComponent(alice.agent_id));
  check("positions show 10 YES", pos.status === 200 && pos.json.positions &&
    pos.json.positions.some((p) => p.shares === 10 && p.side === "YES"),
    pos.status + " " + (pos.text || "").slice(0, 160));
  const ledger = await req("GET", "/ledger");
  check("ledger conserves (total == expected)", ledger.status === 200 && ledger.json.conservation &&
    ledger.json.conservation.ok === true &&
    Math.abs(ledger.json.conservation.total - ledger.json.conservation.expected) < 1e-9,
    ledger.status + " " + (ledger.text || "").slice(0, 160));
  const log = await req("GET", "/hcs-log?limit=200");
  const logMessages = (log.json && log.json.messages) || log.json;
  check("hcs-log carries the order", log.status === 200 && Array.isArray(logMessages) &&
    logMessages.some((m) => m.action === "place_order" && m.sender === alice.agent_id &&
      m.payload && m.payload.market_id === marketId &&
      m.payload.shares === 10 && m.payload.side === "YES"),
    log.status + " " + (log.text || "").slice(0, 160));
  const verify = await req("GET", "/hcs-log/verify");
  check("hcs-log verifies", verify.status === 200 && verify.json && verify.json.ok === true,
    verify.status + " " + (verify.text || "").slice(0, 160));

  if (failures > 0) { console.log("WEBUI E2E FAIL: " + failures + " failures"); process.exit(1); }
  console.log("WEBUI E2E OK");
})().catch((e) => { console.log("WEBUI E2E ERROR: " + (e && e.stack || e)); process.exit(1); });
