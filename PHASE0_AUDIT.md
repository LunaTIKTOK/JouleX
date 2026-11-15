# JouleX — Phase 0 Audit (delivery track)

**Audited:** 2026-11-14 ~15:00 CST, by the delivery-track subagent.
**Scope:** the full mandate Phases 0–7; this document is Phase 0 only.
**Statuses used:** PASS / FAIL / BLOCKED / NOT VERIFIED. Nothing here is
claimed from a config file alone — every Hedera claim was checked
against the public testnet Mirror Node in this run.

## 0.1 Inventory (verified from disk)

| Item | Status | Evidence |
|---|---|---|
| `~/workspace/joulex/` tree (AUDIT_HEDERA_COMMERCIAL_20261112.md, BUILD_LOG.md, DESIGN.md, MILESTONES.md, README.md, VENDOR.md, data/, demo.py, docs/, joulex/, quickstart.py, render.yaml, requirements.txt, sandbox/, scripts/, tests/) | PASS | `ls` 2026-11-14; matches the inventory confirmation |
| Git repo / remote | FAIL (absent) | `git status` → "fatal: not a git repository"; no remote, no history. First-time init + push, not a migration |
| GitHub repo `LunaTIKTOK/joulex` | FAIL (absent) | API `GET /repos/LunaTIKTOK/joulex` → 404 |
| Render service | FAIL (absent) | No Render connector exists in this environment; no Render API key/credential on file; philosophy-tools deploy is likewise still owner-pending (Render dashboard) |
| Test suite | PASS | `python3 -m pytest tests/ -q` → **426 passed in 51.03s** (2026-11-14) |
| `demo.py` | PASS | `DEMO OK` — Alice 1009.750104, Bob 985.124948, conservation 2000.0 |
| `quickstart.py` | PASS | `TRADE OK` — first paper trade in 0.39s (bar 600s) |
| Local server boot + probe | PASS | `python3 -m joulex.server --port 8479` → GET `/` 200 (exchange info), `/health` ok, `/markets` `[]`, `/hcs-log` serves the local chain |
| Sandbox parity | PASS | `node tests/parity.cjs` → PARITY OK (300 LMSR cases, canonical/SHA-256 exact, orchestrator 7.48% vs greedy) |
| Sandbox engine exercise | PASS | `node tests/exercise.cjs` → EXERCISE OK (trade/settle/errors/conservation/log-chain) |
| Sandbox smoke (headless browser) | NOT VERIFIED | `node tests/smoke.cjs` → `Cannot find module 'jsdom'`; jsdom is not installed anywhere in the tree. Fix: `cd sandbox && npm install jsdom` (or add it to a sandbox package.json). Not a code failure. |
| `render.yaml` | PASS | Present; free tier, `python3 -m joulex.server`, health check GET `/`, local-HCS-only, no secrets |
| `data/hcs_config.json` | PASS (exists) | `{"hedera_topic":"0.0.11399235","mode":"hedera","network":"testnet"}` — config only; see §0.2 for what the network actually shows |
| Secrets scan | PASS | No private key, seed, password, or API key value in the tree (grep over `*.py/*.json/*.yaml/*.md`, excluding node_modules). Key material enters only via env vars (`JOULEX_HCS_PRIVATE_KEY`, `JOULEX_DEMO_ISSUER_SEED_HEX`), never stored. `data/trust_store.demo.json` holds public keys + a signed revocation list only, marked DEMO ONLY |

## 0.2 Hedera status — Mirror Node verification (not config)

Checked live 2026-11-14 against `https://testnet.mirrornode.hedera.com`:

| Claim | Verdict | Evidence |
|---|---|---|
| Topic `0.0.11399235` exists on testnet | **PASS — VERIFIED** | `GET /api/v1/topics/0.0.11399235` → 200; `created_timestamp` 1794332721 (~2026-11-10), `auto_renew_account` `0.0.11389089`, not deleted, no admin/submit key set |
| Operator account `0.0.11389089` | **PASS — VERIFIED** | Topic response names it as auto-renew account; it is the payer of the one message on the topic |
| Real transactions on the topic | **PASS, n=1** | `GET .../messages?limit=100` → exactly **1 message**, seq 1, consensus `1794335407`, payload `{"action":"test_message","payload":{"purpose":"verify live Hedera wiring",...},"sender":"operator"}`. One real test message; zero production/settlement traffic |
| Smart contracts deployed | **FAIL — none** | No contract ID anywhere in config, docs, or code; nothing to verify. Correctly stated as "NO smart contracts deployed" |
| HTS tokens | **FAIL — none** | No token ID anywhere; none claimed |
| Code uses real Hedera SDK calls | **PASS** | `joulex/hcs.py` `RealHCS` shells to `joulex/hcs_submit.mjs`, which imports `Client, PrivateKey, TopicMessageSubmitTransaction` from `@hashgraph/sdk` (^2.81.0, in root `package.json`, `node_modules` present). Real SDK, real submit path — exercised once (the seq-1 test message). `joulex/transfer.py` has a matching testnet-only HBAR rail (`hcs_transfer.mjs`) |
| Live testnet settlement cycle (M4) | **NOT VERIFIED** | Needs the owner-supplied operator key transiently; never run. The M4 demo proves the cycle with a fake runner only. The local HCS log (`data/hcs_log.jsonl`, 75 entries + header) is a local hash-chained log and is **not** Hedera consensus — the code labels it honestly (`"mode": "local"`, `"hedera_topic": null` in the header line) |
| Mirror-node verification path | **PASS** | `joulex/audit.py` fetches `GET /api/v1/topics/{topic}/messages/{seq}` and compares message hash + payload against the receipt (`verify_settlement` VERIFIED/PARTIAL/NOT_FOUND/FAILED). Unit-tested; live path not yet exercised against a real settlement |

**Phase 0 Hedera verdict:** the testnet topic is real, the SDK path is real,
one real test message exists. Everything beyond that — contracts, tokens,
settlement anchoring, value rail — is built code awaiting its first live
run, not a deployed integration. No mainnet anything exists or is claimed.

## 0.3 Frontend / backend / persisted state

- **Backend:** `joulex/server.py` — stdlib-only `http.server`, ~25 routes
  (`/`, `/health`, `/metrics`, `/time`, `/markets`, `/orders`,
  `/positions`, `/ledger`, `/reconcile`, `/challenge`, `/attestations`,
  `/hcs-log*`, `/aeso/price`, `/audit/*`, `/revocation-lists`,
  `/__demo/*`). No static-file serving, no HTML UI served by the server.
- **Frontend:** `sandbox/joulex-sandbox.html` (123 KB single-file app,
  generated by `build_html.py`, never hand-edit) — a self-contained
  client-side simulation (JS ports of LMSR/orchestrator, local ledger).
  It does **not** talk to the Python server (no `fetch` to the API in
  `app.js`/`engine.js`). The server has no browser UI at all.
- **Persisted state:** `joulex/store.py` `JsonStore` — JSON files +
  append-only JSONL under `data/` (ledger, HCS log, challenges,
  settlements, receipts). No database. Survives process restarts;
  on Render free tier it is wiped on redeploy (render.yaml documents
  this). No backups automation beyond `scripts/backup_data.py`.
- **Pricing/matching model:** LMSR prediction-market pricing
  (`joulex/lmsr.py`, `markets.py`) with `flex_capacity` /
  `compute_forward` / `generic` contract types, AESO oracle module,
  paper ledger with conservation. This is **not** a Phase 2 order-book
  matching engine (no bid/ask books, no price-time priority, no
  partial fills, no positions/obligations beyond LMSR shares).
  The mandate's Phase 1/2 work is a real build, not a relabel.

## 0.4 Gap analysis (what the mandate needs that does not exist yet)

| # | Gap | Mandate phase | Severity |
|---|---|---|---|
| G1 | **No human account system.** No `/accounts`, `/register`, `/signup`, `/users` route or code anywhere. Admission is CF-KYA agent credentials (L3) minted by a demo issuer script; a stranger cannot "create an account" in a browser today. | M1 / Phase 1 | **Blocking for the end goal** |
| G2 | **No server-served web UI.** Server returns JSON only; the sandbox is a disconnected static sim. A stranger with a URL gets JSON, not an app. | M1 / Phase 7 | **Blocking for the end goal** |
| G3 | **No GitHub repo, no CI, no deploy.** 403 on repo creation (see §0.5). No Render service. | M1 / Phase 7 | **Blocking for the end goal** |
| G4 | **No durable storage.** JSON files on ephemeral disk; wiped on redeploy. | Phase 1 / Phase 7 | High — must be named honestly on the live deploy |
| G5 | **No order-book matching engine.** LMSR only; no bid/ask, price-time priority, partial fills, expiry, positions/obligations, fees, margin/collateral. | Phase 2 | High — core of the mandate |
| G6 | **No compute-market scheduling with power constraints.** Orchestrator sim exists (M2, SYNTHETIC, verdict GO 7.48%) but there is no live compute reservation market, no PUE/utilisation/facility-power constraint enforcement, no unified electricity+compute availability check. | Phase 1 | High |
| G7 | **No agent API with scoped credentials / limits / disablement.** CF-KYA admission + attestation exist, but no scoped agent keys, spending/exposure limits, rate limits, or emergency disablement as a product surface. | Phase 4 | Medium |
| G8 | **No smart contracts / escrow / HTS.** Designed (M4 value rail is testnet-only, not auto-wired); nothing deployed. Live settlement cycle never run. | Phase 3 | Medium — testnet only, owner key needed |
| G9 | **No scenario test suite.** No shortage/congestion/curtailment/outage/collateral/manipulation scenarios; no matching-engine fuzz/property tests. | Phase 5 | Medium |
| G10 | **No compliance research artifact.** AESO/AUC/derivatives/securities/privacy classification not written; DESIGN.md states simulation boundaries but there is no Phase 6 memo. | Phase 6 | Medium — required before any live-readiness claim |
| G11 | **Sandbox smoke test unrunnable** (missing jsdom). | Phase 0 | Low |

## 0.5 Blockers hit in this run

1. **GitHub repo creation — BLOCKED, 403.** `github_api.py create-repo
   joulex` → `{"status": 403, "detail": {"message": "Resource not
   accessible by personal access token"}}`. The saved fine-grained PAT
   is scoped to selected repositories with Contents read/write; it
   cannot create repositories. **Fix (owner step, ~2 min):** in the
   GitHub UI create private repo `LunaTIKTOK/joulex`, then widen the
   PAT's repository scope to include it (or issue a PAT with Contents
   write on that repo). After that, `push-dir` can land the tree.
   Auth itself is healthy (`whoami` → 200, `LunaTIKTOK`, id 197450726).
2. **Render service — BLOCKED, no credential path.** No Render
   connector exists in this environment and no Render API key is on
   file. The philosophy-tools deploy is in the same state (owner
   creates the Web Service in the Render dashboard). **Fix (owner
   step):** Render dashboard → New → Web Service → connect the repo
   once pushed → render.yaml is detected → Free plan → Create. No
   secret env vars needed for the first deploy; no card required.
   Alternatively the owner can supply a Render API key and the deploy
   can be attempted via the Render REST API.
3. **Local git init — DONE in this run** (see §0.6) so the push is a
   single command once the repo exists.

## 0.6 Preserved components (usable as-is)

Exchange core + LMSR (M0), Verifiable Compute envelope + HCS-schema log
(M1), orchestrator sim (M2), docs/demo/quickstart (M3), settlement layer
+ auditability + testnet value rail (M4), sandbox JS ports with parity
proof, AESO oracle module, CF-KYA L3 admission, attestation + provenance
links. All green under the 426-test suite run in this audit.

## 0.7 Phase 0 verdict

**PASS with named gaps.** The prototype is real, tested, and honestly
labelled; the Hedera topic is real with one real test message; nothing
is fabricated. The delivery track (repo → deploy → account → core
workflow) is blocked on two owner steps (§0.5) and the account/UI gaps
(G1–G2) are real build work, not configuration.
