/* Browser-crypto vectors for the server-served UI (M1.5).
 * Loads the EXACT files the browser loads (joulex/webui/ed25519.js,
 * joulex/webui/sha256.js) and checks them against:
 *  - RFC 8032 §7.1 TEST 1 + TEST 2 (seed/pubkey/signature, verified
 *    against Python's ed25519 before hardcoding — see BUILD_LOG);
 *  - SHA-256 of canonical JSON for three fixed objects (hashes
 *    computed with joulex.attestation.canonical_bytes in Python).
 * Prints one SIG line (TEST 2 seed signing a fixed message) that the
 * pytest wrapper verifies with Python ed25519 — JS->Python interop.
 * Exit 0 + "WEBUI CRYPTO OK" iff everything matches.
 */
"use strict";
const path = require("path");
const ROOT = path.resolve(__dirname, "..");
const Ed = require(path.join(ROOT, "joulex", "webui", "ed25519.js"));
const Hash = require(path.join(ROOT, "joulex", "webui", "sha256.js"));

let failures = 0;
function check(name, ok, detail) {
  if (!ok) { failures++; console.log("FAIL: " + name + (detail ? " — " + detail : "")); }
  else console.log("ok: " + name);
}

// RFC 8032 §7.1 TEST 1 (empty message) + TEST 2 (1 byte 0x72).
const VECTORS = [
  {
    seed: "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
    pub: "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
    msg: "",
    sig: "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
  },
  {
    seed: "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
    pub: "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
    msg: "72",
    sig: "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
  },
];
for (const v of VECTORS) {
  const seed = Buffer.from(v.seed, "hex");
  const msg = Buffer.from(v.msg, "hex");
  check("pubkey " + v.seed.slice(0, 8), Buffer.from(Ed.publicKeyFromSeed(seed)).toString("hex") === v.pub);
  check("sign " + v.seed.slice(0, 8),
    Buffer.from(Ed.sign(msg, seed, Buffer.from(v.pub, "hex"))).toString("hex") === v.sig);
}

// Canonical-JSON SHA-256 parity (Python-computed expectations).
const HASH_CASES = [
  [{ action: "buy", agent_id: "agent-demo-alice", market_id: "mkt_x", shares: 10.0, side: "YES" },
   "27469eebbdb538b2f845d8fa5d9afe68a6c5904f14601c919664f9270f661fb0"],
  [{ challenge: "c-1", payload_hash: "ab".repeat(32), timestamp: "2026-11-05T12:00:00+00:00" },
   "ac1683d4f09d7bd22f51d97a1cd7b063f815fdeb1261b5db9acfbaf8f93e0820"],
  [{ a: 100.0, b: 0.5, c: null, d: true, e: "x\ny" },
   "e530a82f679ea186eb24001bcfded3700ea8082bd6ca3c7077391f7300ea061b"],
];
for (const [obj, want] of HASH_CASES) {
  check("canonical hash " + JSON.stringify(obj).slice(0, 40),
    Hash.sha256Hex(Hash.canonical(obj)) === want,
    "got " + Hash.sha256Hex(Hash.canonical(obj)));
}

// One signature for the Python side to verify (JS -> Python interop).
const sig = Ed.sign(Buffer.from("joulex-webui-test", "utf8"),
  Buffer.from(VECTORS[1].seed, "hex"), Buffer.from(VECTORS[1].pub, "hex"));
console.log("SIG " + Buffer.from(sig).toString("hex"));

if (failures > 0) { console.log("WEBUI CRYPTO FAIL: " + failures + " failures"); process.exit(1); }
console.log("WEBUI CRYPTO OK");
