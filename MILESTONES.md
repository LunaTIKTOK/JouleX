# JouleX — Milestones

## The quality bar (owner order Nov 9 23:52 — "real commercial autonomous AI agent exchange", same bar as the agent exchange: "commercial grade, as if you are Vlad Tenev")

Robinhood-grade: democratized access, dead-simple onboarding, rock-solid
execution, trust through transparency. These are **gates, not aspirations**
— a milestone is not done until it meets them:

1. **10-minute first trade.** An agent goes from discovery to first paper
   trade in ≤10 minutes with zero hand-holding (clear API contract +
   client library + quickstart doc).
2. **Auditable money, end to end.** Order → fill → settlement → receipt,
   with reconciliation that catches a mismatch. Every settlement receipt
   records the fetched value + source so a third party can re-derive it.
3. **Fail-closed admission.** CF-KYA bound credential, L3 only, no
   credential = no trade, no exceptions. No keys or secrets in code.
   No demo-grade shortcuts: no hardcoded fills, no fake liquidity, no
   untested paths.
4. **Test bar.** Every milestone lands with tests; the suite stays green.
   Error/retry semantics specified in writing: duplicate order, timeout,
   partial fill, oracle down, double settlement, attestation failure,
   HCS log failure.
5. **Docs a stranger can use.** API reference, quickstart, one-page
   "how admission works", and a Hedera doc that states exactly what
   the owner must do to go live — written for an agent reading it cold.
6. **No public launch until the paper book proves itself.** Settlement
   stays paper. Real money (x402/USDC) and live Hedera transactions are
   separate, later, owner-ordered decisions — never armed by this build
   loop.
7. **Honest labels.** The software attestation envelope is never
   described as hardware attestation. The local HCS log is never
   described as Hedera consensus. Synthetic price data is always
   labelled SYNTHETIC. No buyer, licence, or AESO commercial use is
   ever claimed.

## Milestone queue (ordered)

| # | Milestone | Status | Test evidence |
|---|-----------|--------|---------------|
| M0 | **Exchange core** — port of the agent-exchange core (LMSR, CF-KYA L3 admission, paper ledger, JSON persistence, HTTP API on port 8472, demo issuer, demo.py) with JouleX market vocabulary: `flex_capacity` / `compute_forward` / `generic` contract types, `aeso_price` + `public_source` resolution, 1–30d horizons, AESO oracle module (`--aeso-oracle`, 503 + market stays OPEN on failure). | DONE 2026-11-10 | `python3 -m pytest tests/ -q` → **215 passed** (2026-11-10); `demo.py` clean, exact balances (Alice 1009.750104, Bob 985.124948, conservation 2000.0); `quickstart.py` first trade in 0.39s; `--aeso-oracle` HTTP smoke: 503 `AESO_NO_DATA`, market stays OPEN |
| M1 | **Verifiable Compute layer** — attestation envelope (`joulex-attest-0.1`, software kind, code-manifest measurement, REPORT_DATA binding), attestation policy on credentials, verifier step, HCS-schema audit log (append-only, hash-chained, consensus-timestamp format, `what_did_agent_know` / `who_told_whom` / `verify_log` queries), in-toto provenance links, `RealHCS` inert stub + `docs/HEDERA.md` with the exact owner steps. | DONE 2026-11-10 | `python3 -m pytest tests/ -q` → **256 passed** (215 M0 + 41 new M1); `demo.py` clean, exact balances (Alice 1009.750104, Bob 985.124948, conservation 2000.0), one attested order (measurement e969a7e2…); pinned measurement `e969a7e2b5355e2d…c7` in `data/measurements.json`, provenance link `data/provenance/0d517e86….link.json` verified |
| M2 | **Orchestrator simulation** — deterministic workload allocation against real AESO price histories (fetched via `joulex/aeso.py`, cached), scored vs fixed / power-price-only / optimisation baselines, with coordination cost as an explicit parameter and forecast-error sensitivity; V0 targets as acceptance criteria; labelled SYNTHETIC fallback only when explicitly requested. | DONE 2026-11-10 | `python3 -m pytest tests/ -q` → **285 passed** (256 M0+M1 + 29 new M2: test_aeso_synthetic 7, test_orchestrator 22); `demo.py` clean, exact balances (Alice 1009.750104, Bob 985.124948, conservation 2000.0); CLI `python3 -m joulex.orchestrator --synthetic --sensitivity` → **VERDICT GO**: improvement net vs greedy **7.48%** (≥5% target), SLA 100%, 100% auditable (24 alloc + 1 summary HCS msgs, `verify_log` ok), annualised saving vs fixed **$50,984/yr** ($46.38/MWh over 42.2 MWh — tens of thousands, consistent with Phase 1 §2.3); price history SYNTHETIC (live SMP covers only ~3h, PoolPriceReportServlet HTTP 403 — see BUILD_LOG); re-pinned measurement `cf591688…a57513`, provenance link `acb70fcb….link.json` verified |
| M3 | **Docs + demo** — quickstart (discovery → first paper trade ≤10 min, timed), API reference, one-page "how admission works", Hedera doc, demo.py with exact balances, README. | DONE 2026-11-10 | `python3 -m pytest tests/ -q` → **303 passed** (285 M0–M2 + 18 new M3: 7 HCS/attestation API routes in test_hcs_api.py, 11 docs checks in test_docs.py); `quickstart.py` 0.48–1.19s in-script (bar 600s), TRADE OK; `demo.py` clean, exact balances (Alice 1009.750104, Bob 985.124948, conservation 2000.0); live 48-endpoint probe verified every API.md response 2026-11-10; 5 missing DESIGN §10 routes implemented (GET /attestations, /hcs-log, /hcs-log/verify, /hcs-log/knowledge, /hcs-log/provenance — read-only over joulex/hcs.py); re-pinned measurement `ce69ac27…35aa`, provenance link `c7f02b9e….link.json` verified |
| M4 | **Settlement layer + auditability** — async anchor queue (durable, idempotent, retry + dead-letter; exchange lock never held by an HCS submit); fail-closed settlement (`--settle-anchor-policy` / `JOULEX_SETTLE_ANCHOR_POLICY`, default `fail_closed`: PENDING_ANCHOR until the receipt lands, idempotent re-settle, ANCHOR_FAILED + fresh supersede, oversize envelope refused 503 before appending); settlement receipts sidecar (19 fields incl. `ledger_entry_hash`, `inputs_hash`, `decision_basis`, `transfer_tx_id`); decision-basis capture (oracle label AESO-live/SYNTHETIC/OPERATOR_VALUE/PUBLIC_SOURCE + snapshot, CF-KYA credential id, market state hash); testnet-only HBAR value rail (`joulex/transfer.py` + `joulex/hcs_transfer.mjs`, record + link to receipt, NOT auto-wired); auditability (`joulex/audit.py`: `verify_settlement` VERIFIED/PARTIAL/NOT_FOUND/FAILED, `GET /audit/<id>`, `POST /audit/checkpoint`, log-head checkpointing, `scripts/audit_bundle.py` time-range bundles, `/health` anchor counts). DESIGN.md §7.7 + §10/§11/§12/§13 updated. **Live testnet settlement cycle NOT yet run — blocked on the owner supplying the operator key transiently; the demo below uses a fake runner and labels every simulated leg.** | DONE 2026-11-13 (live testnet cycle pending owner key) | `python3 -m pytest tests/ -q` → **426 passed** (425 at M4-close: 348 at M3-close + 11 anchor queue + 37 value rail + 9 settlement + 17 audit + 3 integration: oversize refusal + server flag/health; + 1 anchor-receipt race regression test 2026-11-14); `demo.py` clean, exact balances (Alice 1009.750104, Bob 985.124948, conservation 2000.0); `python3 scripts/m4_demo.py` → **M4 DEMO OK**: settle -> PENDING_ANCHOR -> drain -> finalized receipt -> `verify_settlement` VERIFIED (stub mirror) -> `GET /audit/<id>` 200 PARTIAL (real mirror honestly 404s the fake seq 42 — not on the real topic) -> bundle (4 msgs, chain verified) + checkpoint verified -> simulated value-rail transfer linked to receipt; queue probe 50 jobs in ~0.03s (local mechanics only; Hedera-side bound ~2–5s/submit observed on testnet 2026-11-10, i.e. ~17k–43k anchored actions/day per sequential submitter — sustained live throughput NOT yet measured); re-pinned measurement `f00e7849…c9526` (2026-11-14 anchor-receipt race fix), provenance link `data/provenance/74e00c69….link.json` verified |

## ALL MILESTONES DONE

M0–M4 complete 2026-11-13: exchange core (215 tests) + Verifiable
Compute layer (256) + orchestrator simulation, VERDICT GO 7.48% vs
greedy on SYNTHETIC (285) + docs/demo (303) + settlement layer &
auditability (426). Final suite **426 passed** (303 at M3 + 11 from
the owner-ordered testnet-Hedera / deploy-seed work ~12:32–13:16 CST
2026-11-10, see BUILD_LOG, + 9 from the settlement-auth hardening
~00:54–00:58 CST 2026-11-11, reconciled + re-pinned 02:01, + 24 from
the owner-ordered Hedera commercial audit + fixes ~08:01–10:01 CST
2026-11-12, reconciled + re-pinned at the 10:01 verification run,
+ 1 from the 2026-11-13 ~08:01 oversized-body drain fix — server now
drains a refused body before answering 413, fixing a BrokenPipeError
clients saw under full-suite load — reconciled + re-pinned at the
08:01 run, + 77 from M4 2026-11-13: 11 anchor queue, 37 value rail,
9 settlement, 17 audit, 3 integration — reconciled + re-pinned
at the M4 close);
demo exact; quickstart ≤1.2s vs the 600s bar. Paper only,
software attestation, local HCS log by default (RealHCS live on
testnet since 2026-11-10) — no live money, no mainnet Hedera,
no hardware TEE. **M4 caveat: the live testnet settlement cycle
has NOT been run — it needs the owner-supplied operator key; the
M4 demo proves the full cycle with a fake runner only.** What is
NOT built (owner-ordered later steps) is unchanged: x402/USDC live
settlement, mainnet Hedera transactions,
hardware TEE attestation.

## Explicitly NOT milestones (never build in this loop)

- **x402/USDC live settlement.** The adapter stays inert (raises on any
  call) until a separate, owner-ordered arming decision. No flag turns
  it live. Never a cron-loop task.
- **Mainnet Hedera transactions.** Testnet is live (owner-ordered
  2026-11-10) and the M4 value rail is a testnet-only prototype.
  Mainnet stays inert until the owner creates the mainnet account +
  topic and orders the connection. No flag turns it live. Never a
  cron-loop task.
- **Hardware TEE attestation.** No SEV-SNP/TDX/SGX build in this loop.
  The envelope is versioned for it; the hardware is a later,
  owner-decided step.
- **Real money, real keys, real orders.** Paper only, always, in this loop.

## Builder loop contract

Each run: pick the next NOT DONE milestone (in order), write the failing
test first, implement, run the full suite (must be green), log in
BUILD_LOG.md, update this table. If a run cannot finish its milestone,
it logs partial state and stops — never lands a half-tested change.
Routine runs stay silent; only a finished milestone, a blocked
milestone, or an unfixable suite is surfaced.

---

# Delivery Track — Phases 0–7 (mandate Nov 14 ~14:57)

End goal (unchanged): a stranger gets a URL, creates an account, the
system works end-to-end with persistent data, no Muse access. First
release = real functional app in **simulation/testnet mode**, not a claim
to operate an authorised live exchange. Paper-only, testnet-only; no
mainnet, no real HBAR, no operator key, no secrets in repos.
Statuses: PASS / FAIL / BLOCKED / NOT VERIFIED.

## Phase 0 — Audit (this run, 2026-11-14)

**Status: PASS with named gaps** — see `PHASE0_AUDIT.md` for the full
evidence table. Verified: 426-test suite green (51.03s); demo.py OK;
quickstart.py first trade 0.39s; local server boot + probe OK; sandbox
parity + exercise OK; smoke NOT VERIFIED (jsdom missing). Mirror Node
verified: topic `0.0.11399235` real on testnet, exactly 1 message
(seq 1, test wiring); no contracts, no tokens; RealHCS uses the real
`@hashgraph/sdk` via `hcs_submit.mjs`. Gaps G1–G11 logged in the audit.

## M1 — Repo + live deploy + account creation + core workflow (FIRST)

Dependency order: (1) GitHub repo, (2) push + CI, (3) Render deploy,
(4) human account creation, (5) server-served web UI, (6) core workflow
end-to-end (create account → view market → place/reserve → settlement
record → verify in ledger/audit trail) with persistence across requests.

- **M1.1 Repo + push — BLOCKED 2026-11-14.** `create-repo joulex` →
  403 "Resource not accessible by personal access token" (fine-grained
  PAT scoped to selected repos; cannot create repos). Owner step:
  create private `LunaTIKTOK/joulex` in the GitHub UI + widen PAT scope
  to it. Local git init + first commit DONE this run so the push is one
  command after that. Auth healthy (`whoami` 200 LunaTIKTOK).
- **M1.2 CI — DONE 2026-11-14.** `.github/workflows/ci.yml`
  (push + PR to `main`, Python 3.12, Node 24, `npm ci` +
  `npm install --no-save jsdom`): pinned-measurement check
  (`scripts/verify_pin.py`), full pytest suite, `demo.py`,
  `quickstart.py`, `scripts/m4_demo.py`, sandbox parity / exercise /
  smoke. `sandbox/tests/smoke.cjs` no longer hardcodes the builder's
  path (reads the HTML relative to `__dirname`; portability proven
  by a tmp-copy run + negative control). **First GitHub Actions run
  still pending M1.1** (no repo exists yet for Actions to run on);
  the workflow + every step it names were verified locally instead.
- **M1.3 Render deploy — BLOCKED 2026-11-14.** No Render connector or
  API key in this environment. Owner step: dashboard → New Web Service
  → connect repo → render.yaml detected → Free plan → Create. No
  secrets needed; no card. `render.yaml` already correct (local HCS
  only, no live key). **Known limitation to state on the live URL:**
  free-tier ephemeral disk wipes state on redeploy; sleep after 15 min
  idle (~30–60s wake).
- **M1.4 Human accounts — DONE 2026-11-14.** `joulex/accounts.py`
  (`AccountStore`): registration + login, scrypt passwords
  (n=2^14, r=8, p=1, 32-byte salt, 12–256 chars), opaque bearer
  sessions (SHA-256 hashed at rest, 24 h expiry, logout revokes),
  roles buyer/generator/provider/operator/admin, admin-only disable
  (one-way, revokes sessions). CF-KYA mapping: `POST
  /accounts/link-agent` takes a fresh challenge-bound presentation
  over `link_payload` (`admission.link_payload` +
  `link_payload_hash`, `ExchangeVerifier.verify_link`) — L3 only,
  credential must cover `joulex.place_order`, single-use 300 s
  challenge; one credential links to at most one account. Register /
  link / disable are HCS audit events (append before commit; 503 =
  nothing executes); logins/logouts are not. `joulex/errors.py` holds
  the shared `ExchangeError` (no circular import). Client helpers in
  `joulex/client.py`. Docs: `docs/API.md` routes + Accounts section +
  error table, DESIGN.md §14, README. Test-first: `python3 -m pytest
  tests/test_accounts.py -q` → **33 passed**; full suite **465
  passed** (426 + 33 new + 6 new docs-route probes); `demo.py`
  clean, exact balances (Alice 1009.750104, Bob 985.124948,
  conservation 2000.0); `quickstart.py` TRADE OK in 0.70s; re-pinned
  measurement `da4a5a71…48bb`, provenance link
  `5ff30e69….link.json` verified.
- **M1.5 Server-served web UI — DONE 2026-11-14.** `joulex/webui/`
  (`index.html` + `app.js` + `ed25519.js` + `sha256.js`, no build
  step), served by the server at `GET /app` (allowlisted static
  files only; `..`/unknown → 404; `GET /` stays JSON). The UI talks
  to the live API: health, AESO price, markets list, account
  register/login, demo-agent load + link, order form, positions,
  ledger, audit trail + verify. Orders are signed IN THE PAGE:
  `ed25519.js` is a dependency-free BigInt SHA-512 + Ed25519 signer
  (RFC 8032) and `sha256.js` is a verbatim copy of
  `sandbox/sha256.js` (JCS canonical JSON + SHA-256) — the server
  verifies the presentation exactly like a Python client's; no
  admission bypass anywhere. Demo seed held in page memory only,
  never stored/sent. Test-first: `tests/test_web_ui.py` (8 tests:
  serving, traversal, crypto vectors, E2E, jsdom smoke) +
  `tests/webui_crypto.cjs` (RFC 8032 TEST 1+2, canonical hashes,
  JS signature verified in Python) + `tests/webui_e2e.cjs`
  (JS-signed order → receipt → positions → ledger → hcs-log →
  verify) + `tests/webui_smoke.cjs` (jsdom DOM flow: render →
  demo agent → buy → positions update). Gap G11 closed: `npm
  install --no-save jsdom` (gitignored, not committed); sandbox
  `smoke.cjs` runs SMOKE OK. Docs: `docs/API.md` routes + Web UI
  section, DESIGN.md §10, README; `tests/test_docs.py` ALL_ROUTES
  + 4, `_raw` tolerates non-JSON bodies. Test-first: `python3 -m
  pytest tests/test_web_ui.py -q` → **8 passed**; full suite **477
  passed** (465 + 8 new web_ui + 4 new docs-route probes);
  `demo.py` clean, exact balances (Alice 1009.750104, Bob
  985.124948, conservation 2000.0); `quickstart.py` TRADE OK in
  0.48s; sandbox parity/exercise/smoke OK; re-pinned measurement
  `de43115e…e5e5e`, provenance link
  `2bc11204….link.json` verified; fresh envelope verifies against
  the pinned set.
- **M1.6 Core workflow E2E — DONE 2026-11-15 (LOCAL ONLY).** `scripts/m1_6_e2e.py`
  (29 checks, exit 0): register human account → login → view markets →
  create flex_capacity/aeso_price market (CF-KYA) → link agent (L3) → buy
  YES 10 (cost 5.124948) → settle operator value 101000 → outcome YES,
  payout 10 → ledger conserved (1000.0) → `GET /audit/<id>` VERIFIED →
  `/hcs-log/verify` ok (5 msgs) → full server restart: login, market
  RESOLVED, ledger, audit VERIFIED all persist. Live verification still
  needs M1.3 (deploy).

## Phase 1 — Functional market infrastructure

Participants + roles/permissions/authority limits/agent identities/
human approval rules; electricity markets (spot, forward, delivery
intervals/locations, offers/bids, capacity, grid constraints,
congestion/curtailment, reliability; Alberta simulation with market
rules clearly separated from actual Alberta operations); compute
markets (GPU-hour/CPU, reservations, spot, forward, pricing by
resource/location/window; PUE, utilisation, facility power
constraints); unified coordination (combined availability/cost,
flag unsupported compute reservations, choose among eligible
locations). Builds on M0–M2 (LMSR, orchestrator sim) but the
scheduling-with-constraints enforcement is new (gap G6).

## Phase 2 — Real matching engine

Order create/modify/cancel, bid/ask books, price-time priority,
partial fills, expiry, delivery windows, positions/obligations,
reservations/commitments, configurable fees, simulated balances /
margin / collateral. Append-only event ledger, deterministic
matching. **Never generate artificial trades to populate a
dashboard.** Simulated execution kept distinct from legally binding
trades. (Gap G5 — the current engine is LMSR only.)

## Phase 3 — Hedera integration

HCS timestamping/ordering of commitments, executions, settlements;
smart-contract escrow/collateral where suitable; HTS optional
settlement assets/entitlements; Mirror Node verification. Hybrid:
off-chain matching + on-chain commitments/settlements. Do NOT put
every order-book update on-chain by default — cost/latency/privacy/
throughput analysis first. Testnet only. Never label a DB record as
an on-chain settlement. (Gap G8; live settlement cycle still needs
the owner key, transiently.)

## Phase 4 — Agent APIs

Documented API for external autonomous agents: scoped credentials,
read products/order books/availability, submit/cancel authorised
orders, reserve capacity, evaluate constraints, retrieve
transaction/settlement records, verify Hedera commitments. Every
agent: distinct identity, explicit permissions, spending/exposure
limits, rate limits, immutable/tamper-evident audit trail,
emergency disablement, human escalation. Principle: an agent may
propose a transaction without automatically having authority to
execute it. (Gap G7.)

## Phase 5 — Scenario testing

Electricity shortage, compute shortage, congestion, curtailment,
demand spikes, GPU outages, infeasible delivery, insufficient
collateral, unavailable Hedera services, conflicting agent orders,
market manipulation/abuse. Verify matching engine, position ledger,
scheduling, settlement calculations agree. Distinguish simulated
results, observed external data, verified network transactions.
(Gap G9.)

## Phase 6 — Compliance boundaries

Research Alberta electricity market rules, AESO requirements, AUC
jurisdiction, Canadian commodity/derivatives/securities/privacy/
payment regulations. Classify each product/activity by applicable
regulatory questions. Do not assume Hedera makes trades/derivatives/
settlement legally permissible. Simulation/testnet until approvals
exist. (Gap G10 — no Phase 6 memo exists yet.)

## Phase 7 — Delivery (acceptance checklist)

1. Live usable web app
2. Functioning matching engine + market backend
3. Persistent market/customer records
4. Working agent APIs
5. Documented Hedera testnet integration with verifiable
   transaction IDs
6. Interactive spot + forward market UI
7. Unified power/compute scheduling + constraints engine
8. Automated matching/risk/security/settlement tests
9. GitHub repo + CI
10. Independent deployment + ops docs
11. Clear simulation/testnet/live-readiness distinction

Each item is marked PASS only on reproducible evidence; a feature is
not complete without its tests green and its evidence logged in
BUILD_LOG.md.
