# JouleX — Build Log

## 2026-11-09 23:52 — Order + coordinator start

Owner order (23:52 CST): build JouleX as a real commercial autonomous
AI-agent exchange with Verifiable Compute + Hedera-anchored audit trail.
Coordinator session started. Root `~/workspace/joulex/`.

Verified facts carried in (checked by the parent agent, cited here,
not re-verified): Verifiable Compute is real (EQTY Lab + NVIDIA +
Intel + Dell; white paper Dec 2025; Verifiable Runtime at NVIDIA GTC
2026; the white paper itself documents HCS anchoring + HTS
tokenisation; Hedera EQTY case study confirms HCS-anchored
attestations). EQTY's Governance Appliance R760 is a PAID enterprise
hardware product — JouleX does NOT use it; JouleX implements the
software pattern at $0. Hedera HCS: hashgraph aBFT, deterministic
finality in seconds, consensus timestamps, fair ordering, append-only
log, $0.0008/ConsensusSubmitMessage (Jan 2026). HCS is NOT a database.

Read before building: `~/workspace/agent-exchange/` (DESIGN.md,
MILESTONES.md, BUILD_LOG.md, exchange/ code, tests/ — M0–M6 done,
suite 190/190 green 2026-11-09); `~/workspace/cf-kya-dynamic/
VERIFIABLE_COMPUTE.md` (Phase 0 design — attestation envelope,
REPORT_DATA binding, in-toto provenance); `~/workspace/research_notes/
joulex-phase1-20261109.md` (Phase 1 commercial research — LANDED
2026-11-09/10: conditional GO for the simulator; AESO data has NO
commercial licence; buyers weak; eStruxture CAL-3/CoreWeave is the
best Alberta lead; REM coming mid-2027 with LMP + negative pricing).

DESIGN.md written (v1: exchange core port + AESO oracle + M1
attestation/HCS layer + M2 orchestrator sim + API on port 8472).
MILESTONES.md written (M0–M3, quality bar incl. honest-labels gate).
M0 builder spawned.

## 2026-11-10 ~00:00–00:30 CST — M0 DONE (exchange core)

Builder: M0 port + AESO oracle, test-first. What was ported/adapted
(full provenance + SHA-256 in `VENDOR.md`):

- Ported the agent-exchange core (`exchange/` → `joulex/`,
  `tests/` → `tests/`, `demo.py`, `quickstart.py`); CF-KYA snapshot
  in `joulex/vendor/` copied UNMODIFIED — `sha256sum -c
  VENDOR_HASHES.txt` verifies all six files OK.
- Adapted vocabulary: package `joulex`; port 8472; `contract_type` ∈
  {flex_capacity, compute_forward, generic}; `resolution_type` ∈
  {aeso_price, public_source}; horizon 1–30d; `aeso_price` carries
  strike + window_start + window_end, resolves YES iff max hourly pool
  price over the window > strike (ties NO), binary-only (400
  otherwise); creation-payload binding includes the new fields;
  tool names `joulex.place_order` / `joulex.create_market`;
  operator-key env `JOULEX_OPERATOR_KEY`; demo issuer id
  `demo-issuer.joulex.local`. Kraken oracle NOT ported (deleted).
- NEW `joulex/aeso.py`: AESO pool-price feed from the public ETS
  System Marginal Price CSV
  (`http://ets.aeso.ca/ets_web/ip/Market/Reports/CSMPriceReportServlet?contentType=csv`,
  keyless), minute SMP averaged to hourly pool prices (AESO's own
  definition). `fetch_pool_price_series` / `fetch_current_pool_price`
  / `AESOPriceOracle.resolve_market`; any failure raises
  `AESOUnavailable` → settlement 503, market stays OPEN.
  `GET /aeso/price` route; `--aeso-oracle` server flag (fetched values
  win; operator path remains when off). Module docstring states the
  AESO terms: free to access, commercial use needs AESO's written
  permission; prototype is non-commercial research only.
- NEW tests (written first, failed on import before the code landed):
  `tests/test_aeso.py` (18 tests: CSV parse, hourly aggregation,
  max-price rule, tie NO, NO_DATA/UNAVAILABLE, licence docstring)
  and `tests/test_joulex_markets.py` (27 tests: vocabulary,
  validation, 1–30d horizon, payload binding, oracle settlement incl.
  503 + stays OPEN, reconcile). Ported tests adapted (payloads,
  horizon, strike values, `test_docs.py` rewritten for the M0 README;
  full docs are M3).

Test evidence (builder's own runs, 2026-11-10 ~00:21 CST):
`python3 -m pytest tests/ -q` → **215 passed, 0 failed** (24.9s).
`python3 demo.py` → clean, DEMO OK: Alice 1009.750104, Bob
985.124948, conservation total 2000.0, pool 5.124948.
`python3 quickstart.py` → TRADE OK in 0.39s (bar 600s).
`--aeso-oracle` HTTP smoke: `GET /aeso/price` live ($24.29/MWh);
settle of a 10-day-old window refused 503 `AESO_NO_DATA`, market
stays OPEN.

AESO fetch result — LIVE (not fixture): the SMP CSV endpoint returns
HTTP 200 from this VM (verified 2026-11-10 00:02–00:20 CST; latest
hourly pool price $24.29–$24.68/MWh across checks; CSD report also
HTTP 200). The historical Pool Price report servlet
(`PoolPriceReportServlet`) returns **HTTP 403 from this VM in every
form tried** (no params, startDate/endDate, beginDate/endDate) —
documented in `joulex/aeso.py` as the upgrade path for multi-day
windows, along with the keyed Azure APIM gateway
(`https://apimgw.aeso.ca/public`, needs a free self-serve key; no key
created). Consequence, recorded honestly: the live SMP report only
covers the recent ~3 hours, so any settlement window outside that
range is a NO_DATA refusal (503) until the pool-price report or APIM
path is reachable. Tests use a stub fetcher + labelled fixture CSV
(real report shape, captured live 2026-11-10), never presented as
live data.

Two bugs found and fixed during the build (test-first caught both):
1. Circular import `aeso.py` ↔ `markets.py` (markets needed the SMP
   URL) — fixed with a function-level import in `new_market`.
2. Container tzdata quirk: this VM's `America/Edmonton` zone file
   reports UTC-6 in November (real MST is UTC-7; verified via zdump).
   The oracle matches hours on AESO-local wall-clock fields
   (`_local_naive`) instead of instants across tzinfo objects, and
   tests build windows from the AESO timezone — stable on any system
   where ZoneInfo is self-consistent. Also fixed the 24:00 row parse:
   AESO writes midnight as 24:00 = 00:00 at the START of the date
   (hour-ending 01), not the next day.

Constraints honored: stdlib-only Python 3.12, $0, no keys/secrets in
code or files, paper only (X402 hook still inert, raises on any call),
no Hedera account/topic/messages, no M1/M2 modules (M0 is
self-contained; no attestation/HCS/orchestrator imports), nothing
touched outside `~/workspace/joulex/`, no goal timeline entries.
No Groq calls used (0/5 — not needed; the port was mechanical plus
the two fixes above, both verified by tests).

## 2026-11-10 ~00:30–01:00 CST — M1 DONE (Verifiable Compute layer)

Builder: M1 per DESIGN.md §7, test-first (tests written before the
modules existed; collection failed on the missing imports, then 41
new tests were built green). What was built:

- `joulex/attestation.py`: envelope `joulex-attest-0.1`, kind
  `software` only (hardware kinds sev-snp/tdx/sgx-dcap/nitro/
  zkvm-risc0/zkvm-sp1 reserved, verifier MUST reject any kind it
  does not implement — `JOULEX_VC_KIND`). `compute_measurement()` =
  SHA-256 of canonical JSON of sorted (relative path, sha256) for
  every `.py` in `joulex/`. Binding rule (normative): report_data =
  SHA-256 over JCS canonical bytes of {credential_id,
  challenge_nonce, payload_hash}; verifier recomputes from the live
  request, mismatch = `JOULEX_VC_BINDING`. TTL ≤ 300s, expired =
  `JOULEX_VC_EXPIRED`. Policy (inside signed credential content):
  required=true + absent = `JOULEX_VC_REQUIRED`; advisory default —
  present+valid recorded, present+invalid REJECTED, absent = plain
  CF-KYA. Measurement pinning: policy.measurements wins, else the
  verifier's pinned set; unpinned = `JOULEX_VC_MEASUREMENT`.
  Evidence re-verified from scratch (base64 canonical JSON naming
  the measurement; link-backed evidence must name an existing
  provenance link whose product hash matches — else
  `JOULEX_VC_EVIDENCE`). DESIGN.md §7.6 "what M1 does NOT prove"
  stated in the module docstring and in docs/HEDERA.md.
- `joulex/hcs.py`: HCS-schema audit log, `data/hcs_log.jsonl`,
  header `{"mode": "local", "hedera_topic": null}`, topic
  `joulex-actions`, strictly increasing sequence, HCS nanosecond
  consensus-timestamp format (labelled LOCAL — a local simulation,
  never called a Hedera consensus timestamp), payload_hash,
  provenance {told_by, known_facts}, prev_hash chain, message_hash
  over canonical bytes excluding message_hash. Queries:
  `what_did_agent_know`, `who_told_whom`, `verify_log` (sequence,
  hash chain, payload hashes, timestamp monotonicity — any break is
  a hard failure, reported never repaired). Append-only, no
  edit/delete/rewrite API. Corrupt log refuses startup (fail
  closed). `HCSClient` interface; `LocalHCS` live; `RealHCS`
  DESIGNED BUT INERT — every method raises RuntimeError; no flag
  turns it live.
- `joulex/provenance.py`: in-toto link metadata. in-toto is NOT
  installed here (checked: `import in_toto` fails; no pip install
  per the $0/no-install bar), so the stdlib-only link format with
  the identical schema, labelled as such. Link records materials
  (source files + hashes) and products (code-manifest hash =
  measurement), stored under `data/provenance/` named by its own
  content hash; `verify_link` re-checks materials + product.
  The measurement never depends on in-toto.
- Wiring: envelope as optional field on presentations → order /
  market-creation / settlement records and trade/settlement
  receipts (`attestation` + `attestation_status`); verifier step in
  `ExchangeVerifier._verify` after the kya checks, before any
  irreversible consumption — a failed attestation check un-burns
  the challenge (the "failed verification never burns a challenge"
  invariant is preserved). HCS append on every action (order,
  market creation, settlement) plus attestation-verification
  results on the rejection path; appends happen after all refusal
  checks and before execution, so a log failure (503
  `HCS_LOG_FAILURE`) executes nothing. Settlement attestation is
  advisory: the settler supplies credential_id + challenge_nonce
  alongside the envelope; the verifier recomputes the settlement
  payload hash. `DemoIssuer.mint_agent(attestation_policy=...)`
  places the policy inside the signed credential content and
  re-signs with the issuer seed (verified: tampering breaks the
  signature). `ExchangeClient.place_order/create_market(attested=…)`
  mints and attaches envelopes. `Exchange.info()` reports the
  verifiable_compute block.
- `docs/HEDERA.md`: exact owner steps (portal account, testnet
  first, own keys; HCS topic testnet then mainnet, topic ID in
  `data/hcs_config.json`; HBAR funding, $0.0008/message Jan 2026;
  `JOULEX_HCS_MODE=hedera`; `RealHCS.submit` is a code change +
  owner steps, never a runtime toggle; local log stays fallback).
  Also documents the re-pinning procedure (any `joulex/*.py`
  change changes the measurement).
- Pinned post-M1 measurement: `data/measurements.json` =
  `e969a7e2b5355e2dbf199db09f6caf22811e8c655d9b727db273ffb863e736c7`;
  provenance link
  `data/provenance/0d517e866fc92f44400712f8fa5ddd078378bb4642c3cdeb87370ac3c499e34d.link.json`,
  `verify_link` → (True, []).

Tests: `python3 -m pytest tests/ -q` → **256 passed** (215 M0 +
41 new: test_attestation 16, test_hcs 11, test_provenance 4,
test_attestation_wiring 10). Two test-side fixes during the run
(missing import; required-policy test creating its market with the
policy credential — creation enforces the policy too, which is
correct behaviour). No M0 test changed or broken.

Demo: `python3 demo.py` clean — exact balances (Alice 1009.750104,
Bob 985.124948, conservation 2000.0); Alice's first order attested
(`joulex-attest-0.1`/software, measurement e969a7e2…, status
verified); demo HCS log: 8 messages (2 create, 4 orders, 2
settlements), `verify_log` ok.

Constraints honored: stdlib-only Python 3.12, $0, no keys/secrets
in code or files, paper only, no live Hedera (no account/topic/
messages), no hardware TEE, nothing touched outside
`~/workspace/joulex/`, no goal timeline entries. No Groq calls used
(0/5 — not needed). M2/M3 not built.

## 2026-11-10 ~00:50–01:30 CST — M2 DONE (orchestrator simulation)

Builder: M2 per DESIGN.md §8, test-first (tests written before the
modules existed; collection failed on the missing imports, then the
suite was built green). What was built:

- NEW `joulex/aeso_synthetic.py` — the labelled synthetic series
  DESIGN.md §5 references but M0 never built. Deterministic, seeded:
  hourly shape (evening peak HE 21, midday trough HE 13) x lognormal
  noise, clipped to [$0, $999.99]; lowest 10.6% of hours set to
  exactly $0, one hour set to the $999.99 cap (a deliberate stress
  tail — 1 in 336h is 0.3%, above the 2025 annual 0.09% rate, labelled
  as such), remaining prices rescaled so the mean is the Phase 1
  verified 2025 average $43.68. Every record carries label/source/
  warning: SYNTHETIC, never presented as observed AESO data.
- NEW `joulex/orchestrator.py` — deterministic simulation. Jobs are
  {job_id, hours, power_mw, earliest_start, deadline} (hour-slot
  indices, splittable across non-contiguous hours — checkpointed
  batch work). Cost = pool price x MW x hours. Schedulers, all on the
  SAME history and job set: fixed (run at arrival, ignores price/
  deadlines/capacity), price-only (globally cheapest hours, ignores
  deadlines/SLA/capacity — a cost lower bound that cheats on SLA,
  reported, NOT the bar), greedy (earliest-deadline-first, cheapest
  feasible hours under the cap — the strong baseline), orchestrator
  (greedy AND most-constrained-first allocations, each improved by
  deterministic local search: single-hour moves + pairwise hour
  swaps, strictly cost-reducing; keeps the cheapest — never worse
  than greedy on energy by construction). The per-hour power cap
  (default 1.0 MW) is an explicit modelling choice: without a shared
  cap every job would independently take its own cheapest hours and
  no scheduler could beat another. Coordination cost is an explicit
  parameter, default $0.01/decision, one decision per job allocated.
  Forecast-error sensitivity plans on perturbed prices (bias +/-10%,
  noise +/-10%/+/-25%, seeded) and scores on TRUE prices. Every
  allocation decision is appended to the HCS log (action
  `orchestrator_allocate`, payload carries job/hours/prices/power/
  cost/source label/series hash; provenance told_by `synthetic-series`
  or `aeso-oracle`) plus a summary message; the result carries the
  `verify_log` report. CLI `python3 -m joulex.orchestrator
  --synthetic [--sensitivity]` prints the comparison table + verdict;
  without `--synthetic` and without a multi-day REAL cache it refuses
  (exit 1, no guess). Verdict GO iff net improvement vs greedy >=5%
  AND SLA 100% AND auditable 100%; improvement vs price-only is
  reported but is not the bar (it is cheaper whenever it violates
  deadlines). 0 unauthorised transactions recorded as "N/A -
  simulation" (there is no money here).
- Price history — LIVE FETCH ATTEMPTED at build time, then synthetic
  (exactly the outcome the task anticipated): SMP report live
  (2026-11-10 00:00 local $21.78/MWh; 4 recent hours fetched and
  cached to `data/aeso_prices.json` as REAL with source + note);
  PoolPriceReportServlet returns HTTP 403 from this VM in every form
  tried (no params, startDate/endDate) — multi-day history is
  therefore unavailable, and no AESO APIM key was created (owner
  step). The simulator ran on the labelled SYNTHETIC 14-day series
  (336h). Every output carries the SYNTHETIC label.

Test evidence (builder's own runs, 2026-11-10):
`python3 -m pytest tests/ -q` → **285 passed, 0 failed** (256 M0+M1
+ 29 new: test_aeso_synthetic 7, test_orchestrator 22 — deadlines/
earliest start, cost arithmetic, same-input baselines, coordination
cost deducted, SLA %, falsifier NO-GO path, sensitivity, SYNTHETIC
labelling, HCS audit per decision, duplicate-hours regression).
`python3 demo.py` → clean, exact balances (Alice 1009.750104, Bob
985.124948, conservation 2000.0). No M0/M1 test changed or broken.

Simulation verdict — GO (numbers from the final post-fix CLI run,
`--synthetic --sensitivity`, 336h / 24 jobs / 1.0 MW cap):
fixed $2,222.36 (SLA 100%), price-only $0.00 (SLA 4.2% — it piles
every job onto the 36 zero-price hours, ignoring deadlines),
greedy $288.38 (SLA 100%), orchestrator $266.56 energy + $0.24
coordination (24 decisions @ $0.01) = $266.80 net (SLA 100%).
Improvement net vs fixed 87.99%, vs greedy **7.48%** (>= 5% target),
vs price-only 0.00% (not the bar — see above). Strategy:
slack-order-improved. Forecast sensitivity: +/-10% bias +0.00%,
+/-10% noise +1.06%, +/-25% noise +3.37% vs unperturbed planning.
Annualised saving vs fixed for this job mix: **$50,984/yr**
($46.38/MWh over 42.2 MWh) — tens of thousands per year, CONSISTENT
with Phase 1 §2.3 (pure energy arbitrage on 1 MW is tens of
thousands, not hundreds of thousands); per-MWh saving $46.38 is
below Phase 1's $99–112/MWh top-decile-avoidance figure because the
sim shifts a small job mix, not a flat 1 MW. The 5% bar is the honest
margin here: 7.48% clears it, but not by much — a less favourable job
mix or a higher coordination cost flips it to NO-GO, which is what a
falsifier should do.

Bug found and fixed during the build (HCS-log inspection caught it,
test-first regression added): the local-search swap pass appended an
hour a job already held, duplicating hours (e.g. job-01
[98,98,98,98,...]) and double-counting cost — usually on $0 hours,
so the first CLI run UNDERSTATED orchestrator cost ($202.62) and
overstated improvement vs greedy (29.65%). Fix: swap skips when the
target hour is already held. The first run's 25 HCS messages (seq
1–25, 18 with duplicate hours) remain in the append-only log as
history — NOT rewritten; the corrected run is seq 26–50, 0
duplicates, `verify_log` ok. Lesson: never trust a sim number until
the audit log's payloads have been eyeballed, not just its hashes.

Re-pin (mandatory, DESIGN.md §7 / docs/HEDERA.md): adding
orchestrator.py + aeso_synthetic.py changed the code manifest, so
the M1 pinned measurement e969a7e2… went stale. Recomputed via
compute_measurement(), updated `data/measurements.json` to
**cf59168813e8f69a1a4c94cc99ebddb0b26ac41aa1001dccf198c8c102a57513**,
rebuilt the provenance link
`data/provenance/acb70fcb44c2d2d4dde4ba701529a43f4f570ab235cbc2c91e22f4c6e46982a9.link.json`
(`verify_link` → (True, [])), independently re-verified the pinned
value with a fresh compute_measurement() call (matches), then
re-ran the FULL suite: **285 passed** with demo.py exact — the
attestation tests pass against the new pinned measurement.

Constraints honored: stdlib-only Python 3.12, $0, no keys/secrets in
code or files, paper only, no live Hedera, no hardware TEE, nothing
touched outside `~/workspace/joulex/`, no goal timeline entries, no
M3 built. No Groq calls used (0/5 — not needed). M3 not built.

## 2026-11-10 ~07:30 — M3 DONE (docs + demo)

Baselines first (pre-change): full suite 285 passed; demo.py exact
(Alice 1009.750104, Bob 985.124948, conservation 2000.0);
quickstart.py TRADE OK in 0.41s in-script; pinned measurement
cf591688… verified unchanged by an independent compute_measurement()
call.

API GAP FOUND (the M3 catch): DESIGN.md §10 lists five routes the
server never implemented — GET /attestations, GET /hcs-log,
GET /hcs-log/verify, GET /hcs-log/knowledge, GET /hcs-log/provenance.
Decision: implement them (not paper over them). Test-first:
tests/test_hcs_api.py (7 tests, written failing — all 7 failed 404 —
then green). Read-only views over the existing joulex/hcs.py query
functions (messages / what_did_agent_know / who_told_whom /
verify_log) plus the envelopes already carried on log messages; new
Exchange methods list_attestations / hcs_log / hcs_verify /
hcs_knowledge / hcs_provenance; server routes in joulex/server.py
do_GET. Pagination limit (default 50, clamped 1–200) / offset;
?agent_id=, ?from=/?to= filters on /hcs-log; knowledge requires
agent_id+at (400 otherwise); provenance takes from_agent/to_agent
(DESIGN §10 writes ?from=&to= — renamed for clarity, deviation
recorded in docs/API.md). One real finding while testing: a bare "+"
in a query timestamp is decoded as a space (parse_qs), so "at" must
be percent-encoded (%2B) — documented in API.md and covered by a
test comment. Nothing in DESIGN §10 is left unimplemented; API.md
says so explicitly.

Live verification: probe_live.py (scratch, deleted after the run)
drove a real server (demo clock, demo-issuer trust store, pinned
measurements loaded, throwaway operator key) through 48 calls and
recorded the real status + body of every endpoint. docs/API.md is
written from those observed responses, including: error shape is
{"error": "exchange_error", "reason": "<CODE>..."} (code in reason,
NOT {"error": "<code>"} as DESIGN §10 says — deviation recorded);
JOULEX_VC_KIND exists as a distinct code (DESIGN §11 maps kind-not-
implemented to JOULEX_VC_INVALID); HCS failure is 503
HCS_LOG_FAILURE (DESIGN §11 says HCS_LOG_UNAVAILABLE); L1 over HTTP
surfaces as KYA_REVOCATION_LIST_MISSING (the L1 issuer is the
principal itself and its list cannot be registered over HTTP — the
KYA_L1_REJECTED_CONSEQUENTIAL reason is only reached once the list
is registered in-process); POST /revocation-lists and
GET /__demo/agents are implemented but absent from the DESIGN §10
table. All VC codes observed live: REQUIRED / BINDING / EXPIRED /
MEASUREMENT / KIND (sev-snp) / replay KYA_CHALLENGE_REUSED /
INSUFFICIENT_SHARES (400) / INSUFFICIENT_POINTS (403) /
MARKET_NOT_YET_RESOLVABLE (409) / MARKET_RESOLVED (409). Bonus
observation: GET /aeso/price returned HTTP 200, $17.055/MWh, on
2026-11-10 — the live SMP CSV is reachable from this VM now (M0
noted the Pool Price servlet 403; the SMP path works).

Docs built: docs/QUICKSTART.md (timed, stranger-usable; §4 by-hand
snippet extracted from the doc and run verbatim — works, exact
receipt values match quickstart.py), docs/API.md (20 routes, full
error table, attestation section, deviations section), docs/ADMISSION.md
(one page: L3-only, challenge+presentation binding, replay defence,
revocation, failure cheat sheet), README.md polished to final state
(M0–M2 one-liners, M2 GO 7.48% labelled SYNTHETIC, links to all four
docs, honest-labels section). docs/HEDERA.md verified still accurate
(re-pin commands used verbatim; JOULEX_HCS_MODE correctly stated as
inert in v1 — the code never reads it); no fix needed. demo.py +
quickstart.py verified, not redesigned (demo exact, quickstart
0.48–1.19s in-script across cold runs on 2026-11-10, bar 600s).

Docs tested too: tests/test_docs.py extended (18 → 29 tests) —
ALL_ROUTES incl. the 5 new routes exist on the live server; every
route in the API.md Routes table exists on the live server AND the
table covers the full implemented surface; all four DESIGN §13 doc
files exist; QUICKSTART.md's commands name real scripts/modules
and its §4 snippet runs end to end; README carries the honest labels
(paper-only, software attestation, local HCS, synthetic, "no hardware
TEE", "no live Hedera"). Two test bugs found and fixed during the run
(API.md table paths carry query strings — strip at "?"; snippet test
needs PYTHONPATH when the script lives in a temp dir).

Re-pin (mandatory — joulex/server.py + joulex/exchange.py changed):
compute_measurement() → **ce69ac27567b8b7706463b3c3bacd3f22d2c1e048b201b8f698c2e14199f35aa**,
updated data/measurements.json, rebuilt provenance link
data/provenance/c7f02b9e5148ca0b8eb83bf6d81f52c986591addd3a57ed86fdd5413e42ffe78.link.json
(verify_link → (True, [])), independently re-verified pinned ==
live compute. Final: full suite **303 passed** (285 M0–M2 + 18 new
M3: 7 test_hcs_api + 11 test_docs), demo.py exact, quickstart TRADE
OK. MILESTONES.md: M3 → DONE + ALL-MILESTONES-DONE summary.

Constraints honored: stdlib-only Python 3.12, $0, no keys/secrets in
code or files (probe used a throwaway operator key via env, server
only, never persisted), paper only, no live Hedera, no hardware TEE,
nothing touched outside ~/workspace/joulex/, no goal timeline
entries, Lenny untouched. No Groq calls used (0/5 — not needed).

## 2026-11-10 ~06:01 — Builder loop: flaky-test fix, no milestone pending

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). Verification
run caught a real flake: first full-suite run showed **1 failed, 302
passed** — `tests/test_admission.py::
test_tampered_presentation_signature_rejected` failed at `assert not
ok` (line 108), i.e. the "tampered" presentation verified as valid.

Root cause (test bug, not production): the tamper line flipped the
FIRST hex char based on the LAST char —
`("0" if sig[-1] != "0" else "1") + sig[1:]`. When the signature
already starts with "0" and does not end with "0" (~5.9% of random
signatures), the replacement is identical to the original — the
"tamper" is a no-op and verification correctly passes. The test
passed in isolation and on the immediate full-suite re-run (303
passed), which is consistent with a ~1-in-17 flake, not a regression.

Fix (test-only, one line): flip based on `sig[0]` so the replaced
char is guaranteed to differ —
`("0" if sig[0] != "0" else "1") + sig[1:]`. No other test uses this
pattern (grep confirms the single occurrence). Verification: the
fixed test run 30x → 30 passed; full suite **303 passed**; `demo.py`
exact (Alice 1009.750104, Bob 985.124948, conservation 2000.0).
No production code touched, so the pinned measurement
ce69ac27… is unaffected (manifest covers `joulex/*.py` only, no
re-pin needed). No Groq calls used (0/5). Loop idle until the owner
orders the next phase; live Hedera / x402 / hardware TEE remain
owner-ordered later steps, never built in this loop.

## 2026-11-10 ~08:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **303
passed**; `python3 demo.py` → clean, exact balances (Alice
1009.750104, Bob 985.124948, conservation 2000.0). No code changed, no
milestone progressed, no Groq calls used. Loop idle until the owner
orders the next phase (live Hedera / x402 / hardware TEE are all
owner-ordered later steps, never built in this loop).

## 2026-11-10 ~04:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **303
passed**; `python3 demo.py` → clean, exact balances (Alice
1009.750104, Bob 985.124948, conservation 2000.0). No code changed, no
milestone progressed, no Groq calls used. Loop idle until the owner
orders the next phase (live Hedera / x402 / hardware TEE are all
owner-ordered later steps, never built in this loop).

## 2026-11-10 ~12:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **303
passed**; `python3 demo.py` → clean, exact balances (Alice
1009.750104, Bob 985.124948, conservation 2000.0). No code changed, no
milestone progressed, no Groq calls used. Loop idle until the owner
orders the next phase (live Hedera / x402 / hardware TEE are all
owner-ordered later steps, never built in this loop).

## 2026-11-10 ~10:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **303
passed**; `python3 demo.py` → clean, exact balances (Alice
1009.750104, Bob 985.124948, conservation 2000.0). No code changed, no
milestone progressed, no Groq calls used. Loop idle until the owner
orders the next phase (live Hedera / x402 / hardware TEE are all
owner-ordered later steps, never built in this loop).

## 2026-11-10 ~14:01 — Builder loop: verification-only run + unlogged change found and reconciled

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). Verification:
`python3 -m pytest tests/ -q` → **314 passed** (was 303 at the last
logged run); `python3 demo.py` → clean, exact balances (Alice
1009.750104, Bob 985.124948, conservation 2000.0).

Suite count moved 303 → 314. Cause found (unlogged work between the
12:01 run and this one, ~12:32–13:16 CST, no BUILD_LOG entry — the
gap is in the record, not the code): `joulex/exchange.py` and
`joulex/hcs.py` changed, `tests/test_hcs.py` gained 7 tests and the
new `tests/test_deploy_seed.py` added 4 — the +11 exactly. The new
tests are RealHCS submit-path coverage (runner-based submit, missing
key, append-then-submit, submit failure keeps the local record, reads
delegate to local, config load, factory) and the Render deploy seed
trust store + mint flow (`data/trust_store.demo.json`,
`scripts/mint_demo_agents.py`). This is the owner-ordered testnet
Hedera activation (memory: first live testnet message ~12:29 CST),
not builder-loop work — this loop built no live Hedera and submitted
no live message in this run. RealHCS stays fail-closed: it raises
without operator account + topic, and the key is read from
`JOULEX_HCS_PRIVATE_KEY` at submit time only, never stored.

Attestation chain re-verified after the change (the re-pin WAS done
by that work, 12:33 CST): live `compute_measurement()` =
`c7db11237b90168e2d5d24301100672562fb96b8a9e5cdf931e9efdce8a82304`
== pinned in `data/measurements.json`; newest provenance link
`data/provenance/275e7a87….link.json` (created 12:33:30 CST) →
`verify_link` (True, []). Note: links sort alphabetically, NOT by
time — the M3 link `c7f02b9e…` sorts later but is the OLD link and
correctly fails against the current tree; always pick the link by
mtime, never by name order. (An earlier check in this run picked the
wrong link by name and briefly looked like a stale-link failure —
operator note for future runs.)

No code changed in this run, no milestone progressed, no Groq calls
used (0/5). MILESTONES.md summary suite count updated 303 → 314.
Loop idle until the owner orders the next phase; live Hedera / x402 /
hardware TEE remain owner-ordered later steps, never built in this
loop.

## 2026-11-10 ~20:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **314
passed**; `python3 demo.py` → clean, exact balances (Alice
1009.750104, Bob 985.124948, conservation 2000.0). No code changed, no
milestone progressed, no Groq calls used. Loop idle until the owner
orders the next phase; live Hedera / x402 / hardware TEE remain
owner-ordered later steps, never built in this loop.

## 2026-11-10 ~18:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **314
passed**; `python3 demo.py` → clean, exact balances (Alice
1009.750104, Bob 985.124948, conservation 2000.0). No code changed, no
milestone progressed, no Groq calls used. Loop idle until the owner
orders the next phase; live Hedera / x402 / hardware TEE remain
owner-ordered later steps, never built in this loop.

## 2026-11-10 ~16:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **314
passed**; `python3 demo.py` → clean, exact balances (Alice
1009.750104, Bob 985.124948, conservation 2000.0). No code changed, no
milestone progressed, no Groq calls used. Loop idle until the owner
orders the next phase (live Hedera / x402 / hardware TEE are all
owner-ordered later steps, never built in this loop).

## 2026-11-10 ~22:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **314
passed**; `python3 demo.py` → clean, exact balances (Alice
1009.750104, Bob 985.124948, conservation 2000.0). No code changed, no
milestone progressed, no Groq calls used. Loop idle until the owner
orders the next phase; live Hedera / x402 / hardware TEE remain
owner-ordered later steps, never built in this loop.

## 2026-11-11 ~00:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **314
passed**; `python3 demo.py` → clean, exact balances (Alice
1009.750104, Bob 985.124948, conservation 2000.0). No code changed, no
milestone progressed, no Groq calls used. Loop idle until the owner
orders the next phase; live Hedera / x402 / hardware TEE remain
owner-ordered later steps, never built in this loop.

## 2026-11-11 ~02:01 — Builder loop: verification-only run + unlogged change reconciled, stale pin re-pinned

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). Verification:
`python3 -m pytest tests/ -q` → **323 passed** (was 314 at the 00:01
run); `python3 demo.py` → clean, exact balances (Alice 1009.750104,
Bob 985.124948, conservation 2000.0).

Suite count moved 314 → 323. Cause found (unlogged work ~00:54–00:58
CST, no BUILD_LOG entry — the gap is in the record, not the code):
a settlement-authentication hardening, backups
`*.bak_20261111_settleauth` left in place. `joulex/exchange.py` now
gates `settle_market` on the operator key (presented key must match
the startup key, constant-time compare; wrong key — or any key when
none is configured — is 401 OPERATOR_KEY_INVALID; when a key IS
configured a missing key is also 401, fail closed; no KYA fallback
on settlement; check runs before any market lookup or oracle fetch).
For `public_source` markets the attestation envelope is now
MANDATORY (absent = 401 JOULEX_VC_REQUIRED, market stays OPEN);
`aeso_price` stays advisory. `joulex/server.py` forwards the
`X-Operator-Key` header; `joulex/client.py` `settle()` takes
`operator_key` / `credential` / `attested`. New
`tests/test_settle_auth.py`, `tests/test_multi_outcome.py` extended
(58 tests across the two files; +9 net). `docs/API.md` was updated
by that work (OPERATOR_KEY_INVALID in the error table + settlement
section). The change closes a real hole the code itself documents:
"this path previously had NO credential/policy at all: any caller
could settle any market." This loop built no part of it and submitted
no live message.

STALE PIN FOUND AND FIXED: that work changed `joulex/*.py` but did
NOT re-pin the measurement, and the suite/demo stayed green anyway
(both run with empty pinned sets — demo uses a temp data dir, tests
use tmp paths — so nothing enforced the pin). Reproduced the gap
first: an envelope minted from the current code verified
`False JOULEX_VC_MEASUREMENT` against the pinned set in
`data/measurements.json` (pinned c7db1123…, live 68d4c572…) — any
verifier started on the real data dir would have rejected correctly
attested current-code orders. Applied the documented re-pin
procedure (docs/HEDERA.md): `data/measurements.json` →
**68d4c57283a8e9c13673f363e189b30ab113bca1ec7c5dc2b40ecffb8d1979ac**,
rebuilt provenance link
`data/provenance/58e0c4dc….link.json` (`verify_link` → (True, [])),
independently re-verified pinned == live compute, re-ran the
reproduction → verifies OK against the pinned set. Final: full suite
**323 passed**, demo.py exact. MILESTONES.md summary suite count
updated 314 → 323.

No milestone progressed, no Groq calls used (0/5). Loop idle until
the owner orders the next phase; live Hedera / x402 / hardware TEE
remain owner-ordered later steps, never built in this loop.

## 2026-11-11 ~04:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **323
passed**; `python3 demo.py` → clean, exact balances (Alice
1009.750104, Bob 985.124948, conservation 2000.0). Pinned
measurement verified: live `compute_measurement()` =
68d4c57283… == `data/measurements.json` (measurements[0]); newest
provenance link `data/provenance/58e0c4dc….link.json` → `verify_link`
(True, []). No code changed, no milestone progressed, no Groq calls
used. Loop idle until the owner orders the next phase; live Hedera /
x402 / hardware TEE remain owner-ordered later steps, never built in
this loop.

## 2026-11-11 ~06:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **323
passed** (29.28s); `python3 demo.py` → clean, exact balances (Alice
1009.750104, Bob 985.124948, conservation 2000.0). Pinned
measurement verified: live `compute_measurement()` =
68d4c57283… == `data/measurements.json` (measurements[0]); newest
provenance link `data/provenance/58e0c4dc….link.json` → `verify_link`
(True, []). No code changed, no milestone progressed, no Groq calls
used. Loop idle until the owner orders the next phase; live Hedera /
x402 / hardware TEE remain owner-ordered later steps, never built in
this loop.

## 2026-11-11 ~08:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **323
passed** (33.71s, no drift from the 06:01 run); `python3 demo.py` →
clean, exact balances (Alice 1009.750104, Bob 985.124948, conservation
2000.0). Pinned measurement verified: live `compute_measurement()` =
68d4c57283… == `data/measurements.json` (measurements[0]); newest
provenance link `data/provenance/58e0c4dc….link.json` → `verify_link`
(True, []). No code changed, no milestone progressed, no Groq calls
used. Loop idle until the owner orders the next phase; live Hedera /
x402 / hardware TEE remain owner-ordered later steps, never built in
this loop.

## 2026-11-11 ~10:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **323
passed** (30.88s, no drift from the 08:01 run); `python3 demo.py` →
clean, exact balances (Alice 1009.750104, Bob 985.124948, conservation
2000.0). Pinned measurement verified: live `compute_measurement()` =
68d4c57283… == `data/measurements.json` (measurements[0]); newest
provenance link `data/provenance/58e0c4dc….link.json` → `verify_link`
(True, []). No code changed, no milestone progressed, no Groq calls
used. Loop idle until the owner orders the next phase; live Hedera /
x402 / hardware TEE remain owner-ordered later steps, never built in
this loop.

## 2026-11-11 ~12:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **323
passed** (34.53s, no drift from the 10:01 run); `python3 demo.py` →
clean, exact balances (Alice 1009.750104, Bob 985.124948, conservation
2000.0). Pinned measurement verified: live `compute_measurement()` =
68d4c57283… == `data/measurements.json` (measurements[0]); newest
provenance link `data/provenance/58e0c4dc….link.json` → `verify_link`
(True, []). No code changed, no milestone progressed, no Groq calls
used. Loop idle until the owner orders the next phase; live Hedera /
x402 / hardware TEE remain owner-ordered later steps, never built in
this loop.

## 2026-11-11 ~14:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **323
passed** (40.92s, no drift from the 12:01 run); `python3 demo.py` →
clean, exact balances (Alice 1009.750104, Bob 985.124948, conservation
2000.0). Pinned measurement verified: live `compute_measurement()` =
68d4c57283… == `data/measurements.json` (measurements[0]); newest
provenance link `data/provenance/58e0c4dc….link.json` → `verify_link`
(True, []). No code changed, no milestone progressed, no Groq calls
used. Loop idle until the owner orders the next phase; live Hedera /
x402 / hardware TEE remain owner-ordered later steps, never built in
this loop.

## 2026-11-11 ~16:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **323
passed** (28.64s, no drift from the 14:01 run); `python3 demo.py` →
clean, exact balances (Alice 1009.750104, Bob 985.124948, conservation
2000.0). Pinned measurement verified: live `compute_measurement()` =
68d4c57283… == `data/measurements.json` (measurements[0]); newest
provenance link `data/provenance/58e0c4dc….link.json` → `verify_link`
(True, []). No code changed, no milestone progressed, no Groq calls
used. Loop idle until the owner orders the next phase; live Hedera /
x402 / hardware TEE remain owner-ordered later steps, never built in
this loop.

## 2026-11-11 ~18:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **323
passed** (36.72s, no drift from the 16:01 run); `python3 demo.py` →
clean, exact balances (Alice 1009.750104, Bob 985.124948, conservation
2000.0). Pinned measurement verified: live `compute_measurement()` =
68d4c57283… == `data/measurements.json` (measurements[0]); newest
provenance link `data/provenance/58e0c4dc….link.json` → `verify_link`
(True, []). No code changed, no milestone progressed, no Groq calls
used. Loop idle until the owner orders the next phase; live Hedera /
x402 / hardware TEE remain owner-ordered later steps, never built in
this loop.

## 2026-11-11 ~20:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **323
passed** (35.83s, no drift from the 18:01 run); `python3 demo.py` →
clean, exact balances (Alice 1009.750104, Bob 985.124948, conservation
2000.0). Pinned measurement verified: live `compute_measurement()` =
68d4c57283… == `data/measurements.json` (measurements[0]); newest
provenance link `data/provenance/58e0c4dc….link.json` → `verify_link`
(True, []). No code changed, no milestone progressed, no Groq calls
used. Loop idle until the owner orders the next phase; live Hedera /
x402 / hardware TEE remain owner-ordered later steps, never built in
this loop.

## 2026-11-11 ~22:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **323
passed** (40.83s, no drift from the 20:01 run); `python3 demo.py` →
clean, exact balances (Alice 1009.750104, Bob 985.124948, conservation
2000.0). Pinned measurement verified: live `compute_measurement()` =
68d4c57283… == `data/measurements.json` (measurements[0]); newest
provenance link `data/provenance/58e0c4dc….link.json` → `verify_link`
(True, []). No code changed, no milestone progressed, no Groq calls
used. Loop idle until the owner orders the next phase; live Hedera /
x402 / hardware TEE remain owner-ordered later steps, never built in
this loop.

## 2026-11-12 ~00:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **323
passed** (31.15s, no drift from the 2026-11-11 ~22:01 run);
`python3 demo.py` → clean, exact balances (Alice 1009.750104, Bob
985.124948, conservation 2000.0). Pinned measurement verified: live
`compute_measurement()` = 68d4c57283… == `data/measurements.json`
(measurements[0]); newest provenance link
`data/provenance/58e0c4dc….link.json` → `verify_link` (True, []). No
code changed, no milestone progressed, no Groq calls used. Loop idle
until the owner orders the next phase; live Hedera / x402 / hardware
TEE remain owner-ordered later steps, never built in this loop.

## 2026-11-12 ~02:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **323
passed** (29.25s, no drift from the 2026-11-12 ~00:01 run);
`python3 demo.py` → clean, exact balances (Alice 1009.750104, Bob
985.124948, conservation 2000.0). Pinned measurement verified: live
`compute_measurement()` = 68d4c57283… == `data/measurements.json`
(measurements[0]); newest provenance link
`data/provenance/58e0c4dc….link.json` → `verify_link` (True, []). No
code changed, no milestone progressed, no Groq calls used. Loop idle
until the owner orders the next phase; live Hedera / x402 / hardware
TEE remain owner-ordered later steps, never built in this loop.


## 2026-11-12 ~06:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **323
passed** (33.37s, no drift from the 2026-11-12 ~04:01 run);
`python3 demo.py` → clean, exact balances (Alice 1009.750104, Bob
985.124948, conservation 2000.0). Pinned measurement verified: live
`compute_measurement()` = 68d4c57283… == `data/measurements.json`
(measurements[0]); newest provenance link
`data/provenance/58e0c4dc….link.json` → `verify_link` (True, []). No
code changed, no milestone progressed, no Groq calls used. Loop idle
until the owner orders the next phase; live Hedera / x402 / hardware
TEE remain owner-ordered later steps, never built in this loop.

## 2026-11-12 ~08:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **323
passed** (40.05s, no drift from the 2026-11-12 ~06:01 run);
`python3 demo.py` → clean, exact balances (Alice 1009.750104, Bob
985.124948, conservation 2000.0). Pinned measurement verified: live
`compute_measurement()` = 68d4c57283… == `data/measurements.json`
(measurements[0]); newest provenance link
`data/provenance/58e0c4dc….link.json` → `verify_link` (True, []). No
code changed, no milestone progressed, no Groq calls used. Loop idle
until the owner orders the next phase; live Hedera / x402 / hardware
TEE remain owner-ordered later steps, never built in this loop.

## 2026-11-12 ~10:01 — Builder loop: verification-only run + unlogged change reconciled

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). Verification:
`python3 -m pytest tests/ -q` → **347 passed** (was 323 at the 08:01
run); `python3 demo.py` → clean, exact balances (Alice 1009.750104,
Bob 985.124948, conservation 2000.0).

Suite count moved 323 → 347. Cause found (unlogged work between the
08:01 run and this one, ~08:01–10:01 CST, no BUILD_LOG entry — the gap
is in the record, not the code): an owner-ordered Hedera commercial
audit + fixes + a working paper-trading sandbox website, done by the
coordinator + verification subagent (not this loop).

- Audit `AUDIT_HEDERA_COMMERCIAL_20261112.md`: verdict CONDITIONAL GO
  — money path sound (settle-auth fix holds, attestation verifier
  correct, hash chain intact, no key material stored); the operational
  layer around the anchor was fixed (failures invisible, no retry,
  no observability, docs stale). Live-verified: testnet topic
  `0.0.11399235` exists, exactly 1 message (the unattested wiring
  probe — no real attested JouleX action anchored on-chain yet), fee
  717,339 tinybars ≈ $0.0008/message; topic has NO admin/submit key
  (fully open, undeletable — acceptable for testnet, not commercial).
- Fixes: `joulex/hcs.py` (212 diff lines vs `.bak_20261112` — retry /
  observability / mirror replay), `joulex/exchange.py` (33),
  `joulex/server.py` (53); new `scripts/backup_data.py`,
  `scripts/hcs_mirror_replay.py`; new tests `test_backup_data` (1),
  `test_commercial_ops` (6), `test_hcs_commercial` (11),
  `test_hcs_mirror_replay` (6) = +24 exactly.
  `docs/MAINNET_READINESS.md`: NOT READY for mainnet (owner steps
  only: mainnet topic + keys, fail-closed policy, AESO permission,
  durable storage, async anchor queue for volume).
  `DESIGN.md`, `docs/API.md`, `docs/HEDERA.md` updated (`.bak_20261112`
  kept).
- Sandbox `sandbox/` (owner-ordered, "a working sandbox website until
  real Alberta power suppliers and data centres are onboarded"):
  single-file `joulex-sandbox.html` (123 KB, built by `build_html.py`,
  never hand-edit), LMSR port 1:1 from `joulex/lmsr.py`,
  `sha256.js` byte-matching `joulex/attestation.py`, real flexibility
  scheduler, HCS-schema audit log, PAPER ONLY banner.
  JS tests verified this run: `tests/parity.cjs` → PARITY OK (LMSR
  300 cases exact, canonical/SHA-256 exact, orchestrator
  costs/allocations match Python); `tests/exercise.cjs` → EXERCISE OK
  (end-to-end, scheduler verdict GO 7.48% vs greedy, $50,984/yr
  annualised); `tests/smoke.cjs` → SMOKE OK in a headless browser
  (jsdom installed to /tmp per the test's own header — scratch only,
  not committed; canvas getContext notices are jsdom limits, not
  failures).

Attestation chain re-verified after the change — the re-pin WAS done
by that work (unlike the 02:01 incident): live `compute_measurement()`
= `1a00c3ef63788b018d9d55d8ab27f5c7cd48bf4ab2b77296ab0e65a21821fa16`
== pinned in `data/measurements.json`; newest provenance link (by
mtime, not name) `data/provenance/a68aca7947….link.json` →
`verify_link` (True, []). `data/hcs_config.json` unchanged
(mode hedera, testnet topic 0.0.11399235); no live Hedera submit by
this loop, no new on-chain message (audit confirmed still 1).

No code changed in this run, no milestone progressed, no Groq calls
used (0/5). MILESTONES.md summary suite count updated 323 → 347.
Loop idle until the owner orders the next phase; live Hedera / x402 /
hardware TEE remain owner-ordered later steps, never built in this
loop.

## 2026-11-12 ~12:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **347
passed** (35.35s, no drift from the 2026-11-12 ~10:01 run);
`python3 demo.py` → clean, exact balances (Alice 1009.750104, Bob
985.124948, conservation 2000.0). Pinned measurement verified: live
`compute_measurement()` = 1a00c3ef63… == `data/measurements.json`
(measurements[0]); newest provenance link
`data/provenance/a68aca7947….link.json` → `verify_link` (True, []).
(Note: an in-run comparison script initially reported match False —
a bug in my check, comparing against the dict instead of its
measurements[0] list; corrected re-check confirms True.) No code
changed, no milestone progressed, no Groq calls used. Loop idle until
the owner orders the next phase; live Hedera / x402 / hardware TEE
remain owner-ordered later steps, never built in this loop.

## 2026-11-12 ~14:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **347
passed** (41.18s, no drift from the 2026-11-12 ~12:01 run);
`python3 demo.py` → clean, exact balances (Alice 1009.750104, Bob
985.124948, conservation 2000.0). Pinned measurement verified: live
`compute_measurement()` = 1a00c3ef63… == `data/measurements.json`
(measurements[0]); newest provenance link (by mtime)
`data/provenance/a68aca7947….link.json` → `verify_link` (True, []).
No code changed, no milestone progressed, no Groq calls used. Loop
idle until the owner orders the next phase; live Hedera / x402 /
hardware TEE remain owner-ordered later steps, never built in this
loop.

## 2026-11-12 ~16:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **347
passed** (45.88s, no drift from the 2026-11-12 ~14:01 run);
`python3 demo.py` → clean, exact balances (Alice 1009.750104, Bob
985.124948, conservation 2000.0). Pinned measurement verified: live
`compute_measurement()` = 1a00c3ef63… == `data/measurements.json`
(measurements[0]); newest provenance link (by mtime)
`data/provenance/a68aca7947….link.json` → `verify_link` (True, []).
No code changed, no milestone progressed, no Groq calls used. Loop
idle until the owner orders the next phase; live Hedera / x402 /
hardware TEE remain owner-ordered later steps, never built in this
loop.

## 2026-11-12 ~18:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **347
passed** (35.70s, no drift from the 2026-11-12 ~16:01 run);
`python3 demo.py` → clean, exact balances (Alice 1009.750104, Bob
985.124948, conservation 2000.0). Pinned measurement verified: live
`compute_measurement()` = 1a00c3ef63… == `data/measurements.json`
(measurements[0]); newest provenance link (by mtime)
`data/provenance/a68aca7947….link.json` → `verify_link` (True, []).
No code changed, no milestone progressed, no Groq calls used. Loop
idle until the owner orders the next phase; live Hedera / x402 /
hardware TEE remain owner-ordered later steps, never built in this
loop.

## 2026-11-12 ~20:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **347
passed** (44.29s, no drift from the 2026-11-12 ~18:01 run);
`python3 demo.py` → clean, exact balances (Alice 1009.750104, Bob
985.124948, conservation 2000.0). Pinned measurement verified: live
`compute_measurement()` = 1a00c3ef63… == `data/measurements.json`
(measurements[0]); newest provenance link (by mtime)
`data/provenance/a68aca7947….link.json` → `verify_link` (True, []).
No code changed, no milestone progressed, no Groq calls used. Loop
idle until the owner orders the next phase; live Hedera / x402 /
hardware TEE remain owner-ordered later steps, never built in this
loop.

## 2026-11-12 ~22:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **347
passed** (35.19s, no drift from the 2026-11-12 ~12:01 run);
`python3 demo.py` → clean, exact balances (Alice 1009.750104, Bob
985.124948, conservation 2000.0). Pinned measurement verified: live
`compute_measurement()` = 1a00c3ef63… == `data/measurements.json`
(measurements[0]); newest provenance link
`data/provenance/a68aca7947….link.json` → `verify_link` (True, []).
No code changed, no milestone progressed, no Groq calls used. Loop idle
until the owner orders the next phase; live Hedera / x402 / hardware
TEE remain owner-ordered later steps, never built in this loop.

## 2026-11-13 ~00:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **347
passed** (35.37s, no drift from the 2026-11-12 ~22:01 run);
`python3 demo.py` → clean, exact balances (Alice 1009.750104, Bob
985.124948, conservation 2000.0). Pinned measurement verified: live
`compute_measurement()` = 1a00c3ef63… == `data/measurements.json`
(measurements[0]); newest provenance link (by mtime)
`data/provenance/a68aca7947….link.json` → `verify_link` (True, []).
No code changed, no milestone progressed, no Groq calls used. Loop
idle until the owner orders the next phase; live Hedera / x402 /
hardware TEE remain owner-ordered later steps, never built in this
loop.

## 2026-11-13 ~02:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **347
passed** (35.49s, no drift from the 2026-11-13 ~00:01 run);
`python3 demo.py` → clean, exact balances (Alice 1009.750104, Bob
985.124948, conservation 2000.0). Pinned measurement verified: live
`compute_measurement()` = 1a00c3ef63… == `data/measurements.json`
(measurements[0]); newest provenance link (by mtime)
`data/provenance/a68aca7947….link.json` → `verify_link` (True, []).
No code changed, no milestone progressed, no Groq calls used. Loop
idle until the owner orders the next phase; live Hedera / x402 /
hardware TEE remain owner-ordered later steps, never built in this
loop.

## 2026-11-13 ~04:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **347
passed** (35.80s, no drift from the 2026-11-13 ~02:01 run);
`python3 demo.py` → clean, exact balances (Alice 1009.750104, Bob
985.124948, conservation 2000.0). Pinned measurement verified: live
`compute_measurement()` = 1a00c3ef63… == `data/measurements.json`
(measurements[0]); newest provenance link (by mtime)
`data/provenance/a68aca7947….link.json` → `verify_link` (True, []). No
code changed, no milestone progressed, no Groq calls used. Loop idle
until the owner orders the next phase; live Hedera / x402 / hardware
TEE remain owner-ordered later steps, never built in this loop.

## 2026-11-13 ~06:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **347
passed** (34.48s, no drift from the 2026-11-13 ~04:01 run);
`python3 demo.py` → clean, exact balances (Alice 1009.750104, Bob
985.124948, conservation 2000.0). Pinned measurement verified: live
`compute_measurement()` = 1a00c3ef63… == `data/measurements.json`
(measurements[0]); newest provenance link (by mtime)
`data/provenance/a68aca7947….link.json` → `verify_link` (True, []). No
code changed, no milestone progressed, no Groq calls used. Loop idle
until the owner orders the next phase; live Hedera / x402 / hardware
TEE remain owner-ordered later steps, never built in this loop.

## 2026-11-13 ~08:01 — Builder loop: real flake found and fixed, suite green

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). First
full-suite run of this run: **1 failed, 346 passed** —
`tests/test_commercial_ops.py::test_body_too_large_rejected`
(`urllib.error.URLError: BrokenPipeError`). The same test passes in
isolation (verified) and failed 3x in full-suite runs — a real
server bug, not a test flake.

Root cause (production, `joulex/server.py::_read_body`): on an
oversized body the handler raised 413 WITHOUT consuming the request
body. With HTTP/1.1 keep-alive the server answered while the client
was still writing its 1,000,001 bytes, so the client saw a broken
pipe instead of the 413 response — and the unread bytes would have
been parsed as the next request line. In isolation the client's
write usually finished into the socket buffer first, which is why
the test passed alone. The server log confirms the 413 WAS sent
(`POST /orders 413`), so the refusal itself was correct; only the
delivery to the client was broken.

Fix (test-first: the existing test is the reproduction — it failed
in the full suite before the fix and passes after):
`_read_body` now drains the body (bounded, `MAX_DRAIN_BYTES` =
16,000,000, 64 KB chunks) before raising 413; bodies above the
drain cap are still refused without draining, and the 413 is still
logged. New regression test
`test_body_too_large_rejected_other_route` posts an oversized body
to `/revocation-lists` (same shared `_read_body` path, no auth
before the read) and asserts a clean 413 plus a working `/health`
afterwards — no poisoned state.

Verification: full suite **348 passed** twice consecutively
(37.95s, 36.04s), then again after the re-pin (38.93s);
`python3 demo.py` → clean, exact balances (Alice 1009.750104, Bob
985.124948, conservation 2000.0).

Re-pin (mandatory — `joulex/server.py` changed): live
`compute_measurement()` = **f568c3ed99b117f738cfe859ddd604c3dca67fc769035a31bd29310f58f4474d**
== `data/measurements.json` (measurements[0]); newest provenance
link (by mtime)
`data/provenance/cbb2d8df67d3532f0cc4e0489c4f1cbe94607900b102fdf637917574a2107770.link.json`
→ `verify_link` (True, []), independently re-verified after the
re-pin. MILESTONES.md summary suite count updated 347 → 348.

No milestone progressed, no Groq calls used (0/5 — the fix is a
bounded drain with a clear root cause; no draft/review needed).
Loop idle until the owner orders the next phase; live Hedera / x402 /
hardware TEE remain owner-ordered later steps, never built in this
loop.

## 2026-11-13 ~10:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **348
passed** (35.72s, no drift from the 2026-11-13 ~08:01 run);
`python3 demo.py` → clean, exact balances (Alice 1009.750104, Bob
985.124948, conservation 2000.0). Pinned measurement verified: live
`compute_measurement()` = f568c3ed99… == `data/measurements.json`
(measurements[0]); newest provenance link (by mtime)
`data/provenance/cbb2d8df67….link.json` → `verify_link` (True, []).
No code changed, no milestone progressed, no Groq calls used. Loop
idle until the owner orders the next phase; live Hedera / x402 /
hardware TEE remain owner-ordered later steps, never built in this
loop.

## 2026-11-13 ~12:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M3 all DONE 2026-11-10). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **348
passed** (37.60s, no drift from the 2026-11-13 ~08:01 run);
`python3 demo.py` → clean, exact balances (Alice 1009.750104, Bob
985.124948, conservation 2000.0). Pinned measurement verified: live
`compute_measurement()` = f568c3ed99… == `data/measurements.json`
(measurements[0]); newest provenance link (by mtime)
`data/provenance/cbb2d8df67….link.json` → `verify_link` (True, []).
No code changed, no milestone progressed, no Groq calls used. Loop idle
until the owner orders the next phase; live Hedera / x402 / hardware
TEE remain owner-ordered later steps, never built in this loop.

## 2026-11-13 ~13:30 — M4 settlement layer + auditability (owner order 12:46 CST: "Build a settlement layer and auditability. This needs to be a functioning commercial and scaling prototype")

Coordinator: M4 builder coordinator (parent session
a4ed2079-cce9-491f-8ebc-f009721fd4c6). Three implementation chunks
fanned out to subagents, all test-first, all backed up
(`.bak_20261113`); coordinator owned integration, the oversize
edge-case fix, server wiring, the end-to-end demo, docs, and the
re-pin. DESIGN.md §7.7 + §10/§11/§12/§13 and MILESTONES.md M4 row
written at close.

What landed:

- **Async anchor queue** (`joulex/anchor_queue.py`, 11 tests):
  durable `data/anchor_queue.json`, idempotent on `message_hash`,
  backoff + dead-letter, worker thread. `RealHCS.append` writes
  locally, enqueues, returns `hedera.status == "queued"` + `job_id`
  — the exchange lock is never held by an HCS submit again.
- **Fail-closed settlement + receipts + decision basis**
  (`joulex/decision_basis.py`, `joulex/settlement_receipts.py`,
  `joulex/exchange.py`, 9 tests): `settle_anchor_policy` (env
  `JOULEX_SETTLE_ANCHOR_POLICY`, flag `--settle-anchor-policy`,
  default `fail_closed`); PENDING_ANCHOR two-phase with market
  `SETTLEMENT_PENDING`; idempotent re-settle; ANCHOR_FAILED +
  fresh supersede; pending settlements persist across restarts
  (`data/pending_settlements.json`); 19-field receipt sidecar;
  decision basis with oracle label (AESO-live/SYNTHETIC/
  OPERATOR_VALUE/PUBLIC_SOURCE), snapshot, CF-KYA credential id,
  market state hash, `inputs_hash`.
- **Value rail, testnet only** (`joulex/transfer.py`,
  `joulex/hcs_transfer.mjs`, 37 tests): testnet HBAR transfer
  leg, record + `transfer_tx_id` written back onto the receipt.
  Mainnet arg fails closed before any key is read. NOT auto-wired
  into settlement finalization (recipient + amount are owner
  decisions).
- **Auditability** (`joulex/audit.py`, `scripts/audit_bundle.py`,
  `scripts/audit_checkpoint.py`, 17 tests): `verify_settlement`
  (VERIFIED/PARTIAL/NOT_FOUND/FAILED), `GET /audit/<id>` (404
  `{"error": "AUDIT_NOT_FOUND"}`), `POST /audit/checkpoint`,
  `Exchange.audit_settlement()` / `checkpoint_audit_log()`,
  time-range audit bundles (create + verify), log-head checkpoint
  verify.

Integration fixes by the coordinator (test-first, in
`tests/test_settlement_m4.py`, +3 tests): (1) **oversize
settlement envelope** — a >20 KiB message fails in
`RealHCS.append` with no queue job, so a fail-closed settlement
would have sat PENDING_ANCHOR forever; now refused BEFORE
appending with 503 `HCS_ANCHOR_FAILED` (market stays OPEN, no
pending record), plus a belt-and-braces refusal if an append
fails outright. (2) **Server wiring** — `--settle-anchor-policy`
flag through `build_exchange` to the Exchange, anchor worker
started/stopped in `server.main()`, `/health` gains
`hcs_anchor_*` counts + `settle_anchor_policy` (subprocess
smoke test + env-var test).

Verification: full suite **425 passed** (348 + 77 M4: 11 + 37 +
9 + 17 + 3 integration); `python3 demo.py` clean, exact balances
(Alice 1009.750104, Bob 985.124948, conservation 2000.0);
`python3 scripts/m4_demo.py` → **M4 DEMO OK** (settle ->
PENDING_ANCHOR -> drain -> finalized receipt -> VERIFIED via
stub mirror -> `GET /audit/<id>` 200 PARTIAL — the real mirror
honestly 404s the fake seq 42, which is not on the real topic —
-> bundle 4 messages chain-verified + checkpoint verified ->
simulated value-rail transfer linked; queue probe 50 jobs in
~0.03s, local mechanics only).

Re-pin (mandatory — `joulex/exchange.py`, `joulex/server.py`,
new modules): live `compute_measurement()` =
**f739ae7712f9ec9f9012990542cbc86725c72f4b9004d1e7778c0bf656c6cc52**
== `data/measurements.json` (measurements[0]); newest provenance
link `data/provenance/649212d340….link.json` → `verify_link`
(True, []). MILESTONES.md summary suite count updated 348 → 425.

**BLOCKED / NOT done: the live testnet settlement cycle.** No
Hedera operator key exists in this environment
(`JOULEX_HCS_PRIVATE_KEY` / `JOULEX_HCS_OPERATOR` unset; the
owner's key was used transiently 2026-11-10 and never stored).
No key was searched for, created, or invented; no receipt or
verification was faked — the M4 demo uses a fake runner and a
dummy key and labels every simulated leg. Sustained anchored
settlements/day on testnet is therefore NOT measured live: the
model bound from the 2026-11-10 observed ~2–5s receipt latency
is ~17k–43k anchored actions/day per sequential submitter; the
queue itself drains 50 jobs in ~0.03s locally. The live cycle
(settlement id, receipt, third-party verification walkthrough
against the real mirror) needs the owner to supply the operator
key transiently; the exact command is in
`joulex/hcs_transfer.mjs` / `joulex/transfer.py` docstrings and
in `scripts/m4_demo.py`'s header.

**Owner decisions still open (recorded, not decided):** mainnet
topic/keys (never reuse the testnet topic); final anchoring
policy (flag implemented, fail-closed recommended for
settlement); AESO written permission; durable storage for
`data/`; value-rail wiring (recipient account + amount per
settlement). No Groq calls used by the coordinator for the
integration work.

## 2026-11-13 ~14:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M4 all DONE 2026-11-13). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **425
passed** (38.73s, no drift from the M4 close); `python3 demo.py` →
clean, exact balances (Alice 1009.750104, Bob 985.124948, conservation
2000.0). Pinned measurement verified: live `compute_measurement()` =
f739ae7712f… == `data/measurements.json` (measurements[0]); newest
provenance link `data/provenance/649212d340….link.json` →
`verify_link` (True, []). No code changed, no milestone progressed,
no Groq calls used. Loop idle until the owner orders the next phase;
the live testnet settlement cycle stays blocked on the owner-supplied
operator key; live Hedera / x402 / hardware TEE remain owner-ordered
later steps, never built in this loop.

## 2026-11-13 ~16:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M4 all DONE 2026-11-13). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **425
passed** (41.70s, no drift from the M4 close); `python3 demo.py` →
clean, exact balances (Alice 1009.750104, Bob 985.124948, conservation
2000.0). Pinned measurement verified: live `compute_measurement()` =
f739ae7712… == `data/measurements.json` (measurements[0]); newest
provenance link (by mtime) `data/provenance/649212d3….link.json` →
`verify_link` (True, []). `data/hcs_config.json` unchanged (mode
hedera, testnet topic 0.0.11399235); no live Hedera submit by this
loop. No code changed, no milestone progressed, no Groq calls used
(0/5). Loop idle until the owner orders the next phase; live testnet
settlement cycle still pending the owner-supplied operator key; x402 /
mainnet / hardware TEE remain owner-ordered later steps, never built
in this loop.

## 2026-11-13 ~18:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M4 all DONE 2026-11-13). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **425
passed** (40.35s, no drift from the M4 close); `python3 demo.py` →
clean, exact balances (Alice 1009.750104, Bob 985.124948, conservation
2000.0). Pinned measurement verified: live `compute_measurement()` =
f739ae7712… == `data/measurements.json` (measurements[0]); newest
provenance link (by mtime) `data/provenance/649212d3….link.json` →
`verify_link` (True, []). No code changed, no milestone progressed,
no Groq calls used. Loop idle until the owner orders the next phase;
live Hedera / x402 / hardware TEE remain owner-ordered later steps,
never built in this loop. M4 caveat stands: the live testnet
settlement cycle has NOT been run — it needs the owner-supplied
operator key.

## 2026-11-13 ~20:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M4 all DONE 2026-11-13). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **425
passed** (43.70s, no drift from the 18:01 run); `python3 demo.py` →
clean, exact balances (Alice 1009.750104, Bob 985.124948, conservation
2000.0). Pinned measurement verified: live `compute_measurement()` =
f739ae7712… == `data/measurements.json` (measurements[0]); newest
provenance link (by mtime) `data/provenance/649212d3….link.json` →
`verify_link` (True, []). No code changed, no milestone progressed,
no Groq calls used. Loop idle until the owner orders the next phase;
live Hedera / x402 / hardware TEE remain owner-ordered later steps,
never built in this loop. M4 caveat stands: the live testnet
settlement cycle has NOT been run — it needs the owner-supplied
operator key.

## 2026-11-13 ~22:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M4 all DONE 2026-11-13). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **425
passed** (40.72s, no drift from the 20:01 run); `python3 demo.py` →
clean, exact balances (Alice 1009.750104, Bob 985.124948, conservation
2000.0). Pinned measurement verified: live `compute_measurement()` =
f739ae7712… == `data/measurements.json` (measurements[0]); newest
provenance link (by mtime) `data/provenance/649212d3….link.json` →
`verify_link` (True, []). No code changed, no milestone progressed,
no Groq calls used. Loop idle until the owner orders the next phase;
live Hedera / x402 / hardware TEE remain owner-ordered later steps,
never built in this loop. M4 caveat stands: the live testnet
settlement cycle has NOT been run — it needs the owner-supplied
operator key.

## 2026-11-14 ~00:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M4 all DONE 2026-11-13). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **425
passed** (43.35s, no drift from the M4 close); `python3 demo.py` →
clean, exact balances (Alice 1009.750104, Bob 985.124948, conservation
2000.0). Pinned measurement verified: live `compute_measurement()` =
f739ae7712f… == `data/measurements.json` (measurements[0]); newest
provenance link (by mtime) `data/provenance/649212d3….link.json` →
`verify_link` (True, []). No code changed, no milestone progressed,
no Groq calls used. Loop idle until the owner orders the next phase;
live testnet settlement cycle still pending the owner-supplied
operator key; x402 / mainnet / hardware TEE remain owner-ordered
later steps, never built in this loop.

## 2026-11-14 ~02:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M4 all DONE 2026-11-13). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **425
passed** (41.84s, no drift from the M4 close); `python3 demo.py` →
clean, exact balances (Alice 1009.750104, Bob 985.124948, conservation
2000.0). Pinned measurement verified: live `compute_measurement()` =
f739ae7712f9ec9f… == `data/measurements.json` (measurements[0]); newest
provenance link `data/provenance/649212d340d6bde2….link.json` →
`verify_link` (True, []). No code changed, no milestone progressed,
no Groq calls used. Loop idle until the owner orders the next phase;
live Hedera / x402 / hardware TEE remain owner-ordered later steps,
never built in this loop.

## 2026-11-14 ~04:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M4 all DONE 2026-11-13). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **425
passed** (44.40s, no drift from the M4 close); `python3 demo.py` →
clean, exact balances (Alice 1009.750104, Bob 985.124948, conservation
2000.0). Pinned measurement verified: live `compute_measurement()` =
f739ae7712… == `data/measurements.json` (measurements[0]); newest
provenance link (by mtime) `data/provenance/649212d3….link.json` →
`verify_link` (True, []). No code changed, no milestone progressed, no
Groq calls used. Loop idle until the owner orders the next phase; live
testnet settlement cycle still pending owner key; x402/USDC / mainnet /
hardware TEE remain owner-ordered later steps, never built in this loop.

## 2026-11-14 ~06:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M4 all DONE 2026-11-13). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **425
passed** (55.20s, no drift from the M4 close); `python3 demo.py` →
clean, exact balances (Alice 1009.750104, Bob 985.124948, conservation
2000.0). Pinned measurement verified: live `compute_measurement()` =
f739ae7712f9ec9f… == `data/measurements.json` (measurements[0]); newest
provenance link (by mtime) `data/provenance/649212d3….link.json` →
`verify_link` (True, []). No code changed, no milestone progressed, no
Groq calls used. Loop idle until the owner orders the next phase; live
testnet settlement cycle still pending owner key; x402/USDC / mainnet /
hardware TEE remain owner-ordered later steps, never built in this loop.

## 2026-11-14 ~08:01 — Builder loop: anchor-receipt race fixed, no milestone pending

No NOT DONE milestone remains (M0–M4 all DONE 2026-11-13). First
full-suite run of this run: **1 failed, 424 passed** —
`tests/test_anchor_queue.py::test_worker_thread_drains_in_background`
failed with TypeError at `receipt_for(1)["status"]` (line 206): stats
said anchored == 1 while `receipt_for(1)` returned None. Passed in
isolation and on the immediate full-suite re-run (425 passed) — a
real race, not a stale state.

Root cause (verified, not assumed): `AnchorQueue.drain_once` marks
the job anchored and persists `anchor_queue.json` BEFORE invoking
`on_complete`; the receipts file is only written inside that callback
(`RealHCS._anchor_completed` → `_record_outcome`). A consumer polling
`anchor_stats()` and then reading the receipt can observe the window
between the two — under full-suite load on a shared 1.5-CPU cell the
worker thread is descheduled in exactly that window. The settlement
path itself was never affected (finalization runs on the callback,
after the receipt is recorded); only the stats→receipt observable
contract was racy.

Fix (test-first, standing rule): new deterministic regression test
`test_receipt_visible_once_stats_say_anchored` blocks
`_record_outcome` on an event, drains in a thread, and asserts the
receipt is visible once stats say anchored — failed before the fix
(`assert None is not None`), green after. Fix: `RealHCS.receipt_for`
now falls back to the queue job's stored receipt (new
`AnchorQueue.job_for_sequence`) when the file row has not landed
yet, synthesized in the same record shape `_record_outcome` writes;
the file row wins when it exists. No change to submit/retry/
dead-letter ordering or crash semantics — the queue still persists
the terminal status before the callback runs.

Verification: new test green; `test_anchor_queue.py` 12 passed; the
two race-sensitive tests pass 5x in a row; full suite **426 passed**
(425 + 1 new); `demo.py` exact (Alice 1009.750104, Bob 985.124948,
conservation 2000.0). Re-pin (mandatory — `joulex/hcs.py` +
`joulex/anchor_queue.py` changed): measurement
**f00e78496c052411327d172c7bb4ed6245ba106cf52137b38b75f6c4363c9526**,
provenance link
`data/provenance/74e00c69aef80e1ab076ae004c58f9cde8983a3212d0691576bc23a6566c2628.link.json`
(`verify_link` → (True, [])), pinned == live re-verified; final
full suite re-run post re-pin: **426 passed**, demo exact.

No milestone progressed, no Groq calls used (0/5 — mechanical fix).
Loop idle until the owner orders the next phase; live testnet
settlement cycle still pending owner key; x402/USDC / mainnet /
hardware TEE remain owner-ordered later steps, never built in this
loop.

## 2026-11-14 ~10:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M4 all DONE 2026-11-13). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **426
passed** (45.04s, no drift from the 2026-11-14 ~08:01 run);
`python3 demo.py` → clean, exact balances (Alice 1009.750104, Bob
985.124948, conservation 2000.0). Pinned measurement verified: live
`compute_measurement()` = f00e78496c… == `data/measurements.json`
(measurements[0]); newest provenance link (by mtime)
`data/provenance/74e00c69….link.json` → `verify_link` (True, []). No
code changed, no milestone progressed, no Groq calls used. Loop idle
until the owner orders the next phase; live Hedera / x402 / hardware
TEE remain owner-ordered later steps, never built in this loop.

## 2026-11-14 ~12:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M4 all DONE 2026-11-13). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **426
passed** (41.32s, no drift from the 2026-11-14 ~10:01 run);
`python3 demo.py` → clean, exact balances (Alice 1009.750104, Bob
985.124948, conservation 2000.0). Pinned measurement verified: live
`compute_measurement()` = f00e78496c… == `data/measurements.json`
(measurements[0]); newest provenance link (by mtime)
`data/provenance/74e00c69….link.json` → `verify_link` (True, []).
Record fix: MILESTONES.md M4 row still cited the pre-race-fix pin
f739ae77…/649212d3… — updated to the current f00e7849…/74e00c69…
(the 2026-11-14 race fix had re-pinned measurements.json and the
BUILD_LOG but not the M4 table row). No code changed, no milestone
progressed, no Groq calls used. Loop idle until the owner orders the
next phase; live testnet settlement cycle still pending owner key;
x402/USDC / mainnet / hardware TEE remain owner-ordered later steps,
never built in this loop.

## 2026-11-14 ~14:01 — Builder loop: verification-only run

No NOT DONE milestone remains (M0–M4 all DONE 2026-11-13). Re-verified
state rather than building: `python3 -m pytest tests/ -q` → **426
passed** (43.29s, no drift from the 2026-11-14 ~12:01 run);
`python3 demo.py` → clean, exact balances (Alice 1009.750104, Bob
985.124948, conservation 2000.0). Pinned measurement verified: live
`compute_measurement()` = f00e78496c… == `data/measurements.json`
(measurements[0]); newest provenance link (by mtime)
`data/provenance/74e00c69….link.json` → `verify_link` (True, []). No
code changed, no milestone progressed, no Groq calls used. Loop idle
until the owner orders the next phase; live testnet settlement cycle
still pending owner key; x402/USDC / mainnet / hardware TEE remain
owner-ordered later steps, never built in this loop.

## 2026-11-14 ~15:00 CST — Delivery track: Phase 0 audit + M1 attempt (subagent)
- Phase 0 audit written: `PHASE0_AUDIT.md`. Mirror Node verified live:
  topic 0.0.11399235 exists on testnet (created ~2026-11-10, operator
  0.0.11389089), exactly 1 message (seq 1, test wiring). No contracts,
  no tokens. RealHCS uses real @hashgraph/sdk via hcs_submit.mjs.
  Gaps G1–G11 logged (no human accounts, no server UI, no repo/CI/
  deploy, no durable storage, LMSR-only engine, no live settlement
  cycle, no scenario suite, no compliance memo, smoke needs jsdom).
- Tests run this session: `pytest tests/ -q` → **426 passed in 51.03s**;
  `demo.py` DEMO OK (Alice 1009.750104, Bob 985.124948, conservation
  2000.0); `quickstart.py` TRADE OK in 0.39s; local server boot + probe
  OK (GET / 200, /health ok, /markets [], /hcs-log serves); sandbox
  `parity.cjs` PARITY OK, `exercise.cjs` EXERCISE OK; `smoke.cjs` NOT
  RUN (jsdom missing — `Cannot find module 'jsdom'`).
- M1.1 BLOCKED: `github_api.py create-repo joulex` → 403 "Resource not
  accessible by personal access token" (fine-grained PAT cannot create
  repos). Owner step: create private LunaTIKTOK/joulex in GitHub UI +
  widen PAT scope. Auth healthy (whoami 200 LunaTIKTOK).
- M1.3 BLOCKED: no Render connector / API key in this environment;
  owner step is dashboard New Web Service from the pushed repo.
- Local git init + first commit DONE: `dc33d973b195840098ed401fe7addc0d48dbce88`
  (138 files; node_modules/__pycache__/.pytest_cache excluded via
  .gitignore; secrets scan clean). Push is one command once the repo exists.
- MILESTONES.md extended with the Delivery Track (Phases 0–7 + M1
  breakdown). No live URL yet; no money spent; paper/testnet only.

## 2026-11-14 ~16:01 — Builder loop: M1.4 DONE (human accounts) + port-squatter fix

Next NOT DONE milestone in order was M1.4 (M1.1 repo + push and M1.3
Render deploy remain BLOCKED on owner steps; M1.2 CI blocked on
M1.1). Built test-first, per the loop contract.

### Baseline incident first: port 8472 squatted, 2 tests + demo failing

The run opened with `pytest` at **424 passed, 2 failed** — both
quickstart tests (`test_quickstart_hand_snippet_runs`,
`test_quickstart_runs_under_ten_minutes`) — and `demo.py` failing
with `INVALID_MARKET: new_market() got an unexpected keyword
argument 'contract_type'`. Root cause (verified, not assumed): PID
18524, `python3 -m exchange.server --port 8472 --host 127.0.0.1
--kraken-oracle --data-dir .../agent-exchange-live-data` — the
PARENT agent-exchange live server, running since ~15:20 CST (after
the 15:00 audit run, which was 426 green) — occupies 8472.
quickstart.py, demo.py and the QUICKSTART §4 snippet spawn their own
server on 8472 with stdout/stderr DEVNULL, then poll `client.info()`
and happily talk to whichever server answers — the squatter, whose
older `new_market` has no `contract_type`.

Fix (JouleX-side, no touch of the foreign process — it is outside
`~/workspace/joulex/` and belongs to another operation; killing it
was never an option): new `joulex/server.py::pick_port(preferred)`
— 8472 when nothing listens there, else an ephemeral port — wired
into quickstart.py, demo.py and the QUICKSTART §4 snippet, plus an
identity check in each wait loop (`info()["name"] == "joulex"`,
else refuse to trade against it). `m4_demo.py` already uses port 0
and was unaffected. Both tests + demo.py green again after the fix.
Docs updated (QUICKSTART §3/§4, README). The squatter is still
running; the scripts now route around it.

### M1.4 — what was built (test-first)

Tests written first (`tests/test_accounts.py`, collection failed on
the missing `link_payload` import, then built green):

- NEW `joulex/accounts.py` (`AccountStore`): registration + login,
  scrypt passwords (n=2^14, r=8, p=1, dklen=32, fresh 32-byte salt,
  12–256 chars), opaque `token_urlsafe(32)` bearer sessions stored
  only as SHA-256 hashes, 24 h fixed expiry, logout revokes,
  roles buyer/generator/provider/operator/admin, admin-only disable
  (one-way, revokes sessions). Persistence via atomic `JsonStore`
  (`accounts.json` / `sessions.json`); corrupt state refuses
  startup. No password material in any response, stored file, or
  log line (tested).
- CF-KYA mapping: `admission.link_payload` /
  `link_payload_hash` + `ExchangeVerifier.verify_link` — the link
  presentation binds `{type: "joulex.account_link", account_id,
  agent_id}` and is verified exactly like an order (L3 only,
  trusted issuer, not revoked/expired, credential covering
  `joulex.place_order`, single-use 300 s challenge). A bare
  credential never links. One credential → at most one account.
- Exchange methods `register_account` / `login_account` /
  `logout_account` / `account_for_token` / `link_agent_to_account` /
  `disable_account`; register/link/disable are HCS audit events
  appended BEFORE the state change (503 = nothing executes);
  login/logout are not log events.
- NEW `joulex/errors.py`: `ExchangeError` moved here so the store
  can raise it without a circular import (`exchange.py` re-exports
  by import; all existing imports unchanged).
- Server routes: `POST /accounts` (201), `POST /accounts/login`,
  `GET /accounts/me`, `POST /accounts/logout` (idempotent),
  `POST /accounts/link-agent`, `POST /accounts/<id>/disable`
  (admin); `Authorization` added to CORS allow-headers.
  `joulex/client.py`: `register_account` / `login` / `me` /
  `logout` / `sign_link` / `link_agent` / `disable_account`.
- Docs: `docs/API.md` Routes table + Accounts section + error
  table; DESIGN.md §10 table + §14 (new); README; `tests/
  test_docs.py` ALL_ROUTES + 6 (and the GET probe status list now
  admits 401/403 — the docstring already said a real route answers
  400/401/403/409/503 on an empty call; the assert list had omitted
  401/403 because no GET route returned them before).

Groq (local-groq, gpt-oss-120b, 4 calls this run incl. the models
check; budget 5): 1 draft call + 2 review calls. Applied from the
reviews: password upper bound 256 chars (unbounded input burns
scrypt CPU per login); dummy-hash timing equalisation on
unknown-email login (missing account now costs the same ~50 ms
scrypt); 2 regression tests proving the reviewer's link concerns
are already handled (same presentation replays → 401, challenge
burned; presentation minted for account A cannot link account B —
the server recomputes the hash from the target account).
Declined with reasons recorded: no pepper (this loop holds no
server secrets; salt + scrypt is the bar for paper-only demo);
no IP/UA token binding or sliding expiry (standard bearer
semantics, 24 h bound); SESSION_EXPIRED vs ACCOUNT_UNAUTHORIZED
kept distinct (a random 256-bit token's expiry state is not
actionable information); disabled-account 403 after password check
kept (standard locked-account behaviour); challenge TTL is already
300 s (single-use by the kya replay defence).

Test evidence: `python3 -m pytest tests/test_accounts.py -q` →
**33 passed**; full suite **465 passed** (426 + 33 new + 6 new
docs-route probes); `python3 demo.py` → DEMO OK, exact balances
(Alice 1009.750104, Bob 985.124948, conservation 2000.0);
`python3 quickstart.py` → TRADE OK in 0.70s (bar 600s).

Re-pin (mandatory — `joulex/*.py` changed): measurement
**da4a5a71ddd958e5ba9cbf886aeb6ef72195280b8781457a502945ec4d0f48bb**,
provenance link
`data/provenance/5ff30e69eaf82c4af68e1bed47c284f8e5666514098b94cfdc1c16636d26617a.link.json`
(`verify_link` → (True, [])); pinned == live re-verified; a fresh
envelope verifies against the pinned set.

No Groq 429 (4/5 calls used). No live Hedera, no x402/USDC, no
hardware TEE, nothing touched outside `~/workspace/joulex/`, no
goal timeline entries. Blockers unchanged: M1.1 (repo 403) and
M1.3 (no Render key) still need owner steps; M1.2 CI blocked on
M1.1; live testnet settlement cycle still pending the owner key.
Next buildable: M1.5 server-served web UI.

## 2026-11-14 ~18:01 — Builder loop: M1.5 DONE (server-served web UI)

Next NOT DONE milestone in order was M1.5 (M1.1 repo + push and M1.3
Render deploy remain BLOCKED on owner steps; M1.2 CI blocked on
M1.1; M1.4 done 16:01). Built test-first, per the loop contract.

### Baseline first: jsdom (gap G11) + suite

`npm install --no-save jsdom` in the repo root (gitignored,
not committed) — sandbox `tests/smoke.cjs` now runs SMOKE OK
(19 checks), closing audit gap G11. Baseline suite 465 passed;
demo.py exact; quickstart TRADE OK 0.38s.

### M1.5 — what was built (test-first)

Tests written first (`tests/test_web_ui.py`, 5 of 8 failed on the
first run — serving 404s + missing webui files — then built green):

- NEW `joulex/webui/` (no build step): `index.html` (PAPER ONLY
  banner; account / demo-agent / markets / order / positions /
  ledger / audit sections), `app.js` (fetch-based client over the
  live API — health, AESO price, markets, register/login/logout,
  demo-agent load + link-agent, order form, positions, ledger,
  audit log + verify), `ed25519.js` (dependency-free BigInt
  SHA-512 + Ed25519 signing, RFC 8032), `sha256.js` (verbatim copy
  of `sandbox/sha256.js` — JCS canonical JSON + SHA-256).
- The order form signs CF-KYA presentations IN THE PAGE (challenge
  → canonical payload hash → Ed25519 sign → base64url header) and
  the server verifies them exactly like a Python client's — no
  admission bypass anywhere. The demo seed is held in page memory
  only, never localStorage, never sent anywhere. Demo agents exist
  only on demo-clock servers (`GET /__demo/agents` 404s elsewhere).
- Server (`joulex/server.py`): `GET /app` → shell, `GET /app/<file>`
  for the allowlisted four files with correct content types; `..`,
  backslashes, subpaths and unknown names are 404, never a read
  outside `joulex/webui/`; `GET /` stays the JSON service info.
  `_send` refactored over a new `_send_bytes` (same structured
  request log).
- Node checks load the EXACT browser files: `tests/webui_crypto.cjs`
  (RFC 8032 §7.1 TEST 1+2 — vectors verified against Python's
  ed25519 before hardcoding — + canonical-hash parity for 3 fixed
  objects + one JS signature the pytest wrapper re-verifies with
  Python ed25519), `tests/webui_e2e.cjs` (demo agents → challenge
  → JS-signed order → receipt → positions → ledger conservation →
  hcs-log → verify), `tests/webui_smoke.cjs` (jsdom + Node fetch
  injected: shell renders, market renders from the live API, demo
  agent loads, DOM order form buys, positions/ledger/audit update).

Groq (local-groq, gpt-oss-120b, 4 calls this run; budget 5): draft
call truncated at 2000 tokens (SHA-512 K constants cut off);
K-constants call returned empty text; the constants were derived
independently in Python (Decimal cube-root method, 60-digit
precision) and PROVEN by testing sha512() against Node's crypto
on 18 inputs incl. block boundaries (55/56/111/112/127/128) —
the derivation source does not matter once the tests pin the
bytes. Targeted review pass (call 4): 4 of 6 points confirmed
correct; the 2 "WRONG" findings were both reviewer-side errors —
(1) the scalar-mult loop starting at bit 254 is harmless (bits
253/254 are zero for scalars < L; identity stays identity),
(4) my question's "56-byte message needs 2 blocks" claim was
wrong, the code is right (boundary test proves 56 bytes → 1
block). No code change from the review. Random-seed cross-check:
5 fresh seeds, JS pubkeys + signatures all verify in Python.

Bugs found and fixed during the build (all test-caught, none
production):
1. Uint8Array `.toString("hex")` is comma-decimal, not hex — the
   e2e script's signature + pubkey checks used it (server answered
   `KYA_PRESENTATION_SIGNATURE_INVALID`); fixed with
   `Buffer.from(...).toString("hex")` in the Node test scripts
   (app.js builds hex with a manual loop and was correct).
2. jsdom does not load `<script src>` without `resources:
   "usable"`, and relative script paths resolve against `/app`
   as a file — fixed with `resources: "usable"` in the smoke
   script and absolute `/app/*.js` paths in index.html.
3. jsdom has no fetch — the smoke script injects Node's fetch
   with relative URLs resolved against the page URL.
4. `/hcs-log` returns `{"messages": [...]}` (not a bare array) and
   the place_order log payload carries the bound order fields,
   not `trade_id` — fixed the e2e check and the same shape bug in
   app.js `refreshAudit` (it silently swallowed the TypeError).
5. Ledger total on a fresh exchange is `1000 × funded agents`,
   not 2000.0 — the e2e/smoke checks now assert conservation
   (`total == expected`, `ok`) instead of a hardcoded total.
6. `tests/test_docs.py::_raw` assumed JSON bodies — the HTML/JS
   responses broke `json.loads`; it now returns the raw text as
   the reason for non-JSON bodies (a 200 still proves the route
   exists).

Test evidence: `python3 -m pytest tests/test_web_ui.py -q` →
**8 passed**; full suite **477 passed** (465 + 8 new web_ui + 4
new docs-route probes); `python3 demo.py` → DEMO OK, exact
balances (Alice 1009.750104, Bob 985.124948, conservation
2000.0); `python3 quickstart.py` → TRADE OK in 0.48s (bar 600s);
sandbox `parity.cjs` PARITY OK, `exercise.cjs` EXERCISE OK,
`smoke.cjs` SMOKE OK.

Re-pin (mandatory — `joulex/server.py` changed): measurement
**de43115eacccdf159127e424121e4deafc3736eae883e856bb39de5932dd5e5e**,
provenance link
`data/provenance/2bc112043c0463cddf73ac723412837f867aee1078f1d00130d322a4f8a642dc.link.json`
(`verify_link` → (True, [])); pinned == live re-verified; a fresh
attested envelope verifies against the pinned set (with a real
issued challenge — a fake challenge correctly fails
`EXCHANGE_CHALLENGE_UNKNOWN`, which is the challenge check
firing before attestation, not a measurement problem).

Docs: `docs/API.md` Routes table + `GET /app` Web UI section,
DESIGN.md §10, README Web UI section; `tests/test_docs.py`
ALL_ROUTES + 4 (the docs tests now cover the UI surface both
directions).

No live Hedera, no x402/USDC, no hardware TEE, nothing touched
outside `~/workspace/joulex/`, no goal timeline entries.
Blockers unchanged: M1.1 (repo 403) and M1.3 (no Render key)
still need owner steps; M1.2 CI blocked on M1.1; live testnet
settlement cycle still pending the owner key. Next buildable:
M1.6 core workflow E2E — verified locally only; live
verification needs M1.3 (deploy) + M1.5 (now done).

## 2026-11-14 ~20:01 — Builder loop: M1.2 DONE (CI workflow)

Next NOT DONE milestone in order was M1.2 (M1.1 repo + push and M1.3
Render deploy remain BLOCKED on owner steps; M1.4/M1.5 done). Built
test-first, per the loop contract. The workflow file itself is the
deliverable: GitHub Actions cannot execute until the repo exists
(M1.1, owner step), so every step the workflow names was verified
locally instead — the same "DONE with the live leg pending an owner
step" shape as M4.

### Baseline first

Full suite **477 passed** (68.99s); `demo.py` DEMO OK, exact balances
(Alice 1009.750104, Bob 985.124948, conservation 2000.0);
`quickstart.py` TRADE OK in 0.64s. Pinned measurement de43115e…
verified against `data/measurements.json` before any change.

### M1.2 — what was built (test-first)

Tests written first (`tests/test_ci.py`, 13 of 14 failed on the first
run — no workflow file, no pin script, hardcoded path in smoke.cjs —
then built green):

- NEW `.github/workflows/ci.yml`: push + PR to `main`, `permissions:
  contents: read`, concurrency cancel-in-progress per ref, job `ci`
  on ubuntu-latest (15 min timeout), actions/checkout@v4,
  setup-python@v5 (3.12), setup-node@v4 (24), `pip install pyyaml`
  (test-only dep — the exchange itself stays stdlib-only; test_ci.py
  is the one test importing yaml), `npm ci`, `npm install --no-save
  jsdom`, `python3 scripts/verify_pin.py`, `python3 -m pytest tests/
  -q`, `python3 demo.py`, `python3 quickstart.py`, `python3 scripts/
  m4_demo.py`, then sandbox parity / exercise / smoke as three
  separate steps. No secrets, no live Hedera, no mainnet, no push.
- NEW `scripts/verify_pin.py` (stdlib-only, `--root` flag):
  recomputes `compute_measurement()` from the live `joulex/` tree,
  compares with `data/measurements.json`, verifies the newest
  provenance link (by mtime) via `verify_link`. Exit 0 / 1 with a
  plain-language line. CI runs it before pytest so a stale pin fails
  fast — the exact class the 2026-11-11 02:01 incident caught late
  (suite green, pin stale).
- FIX `sandbox/tests/smoke.cjs`: it read the built HTML via the
  hardcoded absolute path `/home/hatch/workspace/joulex/sandbox/
  joulex-sandbox.html`, which does not exist on a runner — CI would
  have failed on its first run. Now `path.join(__dirname, "..",
  "joulex-sandbox.html")`. Portability is proven, not asserted: the
  test copies `sandbox/` to a tmp dir and runs smoke there with
  `NODE_PATH` at the repo's node_modules (SMOKE OK), then breaks the
  COPY's banner and asserts the run fails — the negative control
  proving it reads the copy, not the builder's path. (The first
  version of that test passed vacuously through the hardcoded path;
  the negative control is what makes it a real proof.)
- The workflow tests assert the file parses (PyYAML, incl. the
  `on:`-parses-as-True gotcha), triggers on push + PR to main, pins
  Python 3.12 / Node 24, and contains every command above; the pin
  script is tested on the real tree (exit 0, "PIN OK") and on a
  tampered tmp tree (exit 1, "mismatch").

Groq (local-groq, gpt-oss-120b, 3 calls this run incl. the models
check; budget 5): 1 draft call (workflow YAML — used as the base,
with workflow-level concurrency + `permissions: contents: read` +
clearer step names applied) + 1 review call. Review findings:
5 of 6 were reviewer-side misreads — it reviewed my compressed
prompt paraphrase, not the file (`on:` mapping, concurrency mapping,
`jobs:` key, `timeout-minutes`, explicit `python-version:` /
`node-version:` are all already exactly as it prescribed; verified
by the parsing tests). The 6th (Node 24 "does not exist on
ubuntu-latest") is wrong for setup-node, which downloads the
requested version — Node 24 is real (local toolchain v24.22.1, LTS
since Oct 2025). No code change from the review.

### Verification: full local CI simulation, exact workflow order

`npm ci` (lock consistent with package.json, 315 packages — this is
the check that would have caught a stale lock on first push) +
`npm install --no-save jsdom` → `verify_pin.py` PIN OK (de43115e…,
link 2bc11204… verifies) → full suite **491 passed** (477 + 14 new
test_ci) → `demo.py` DEMO OK, exact balances → `quickstart.py`
TRADE OK in 0.58s → `scripts/m4_demo.py` M4 DEMO OK (fake runner
only) → sandbox `parity.cjs` PARITY OK, `exercise.cjs` EXERCISE OK,
`smoke.cjs` SMOKE OK. Chain exit 0 throughout
(`CI_SIMULATION_ALL_GREEN`).

No re-pin needed: no `joulex/*.py` changed (workflow YAML +
`scripts/verify_pin.py` + one JS test file + tests only), so the
measurement is unchanged at de43115e…; `verify_pin.py` confirms it
independently. `package-lock.json` untouched by `npm ci`.
Committed locally (M1.1 push still waits on the owner-created repo).

No live Hedera, no x402/USDC, no hardware TEE, nothing touched
outside `~/workspace/joulex/`, no goal timeline entries. Blockers
unchanged: M1.1 (repo 403) and M1.3 (no Render key) still need owner
steps; the first real GitHub Actions run waits on M1.1; live testnet
settlement cycle still pending the owner key. Next buildable: M1.6
core workflow E2E — verified locally only; live verification needs
M1.3 (deploy).

## 2026-11-14 ~22:01 — Builder loop: verification-only run

No NOT DONE milestone buildable in this run (M0–M4 all DONE 2026-11-13;
M1.4/M1.5/M1.2 done today; M1.1 repo + push and M1.3 Render deploy
remain BLOCKED on owner steps; M1.6 live verification needs M1.3;
Phase 1 follows M1 in dependency order and has no scoped milestone
spec for this loop). Re-verified state rather than building:
`python3 -m pytest tests/ -q` → **491 passed** (60.79s, no drift from
the 2026-11-14 ~20:01 run); `python3 scripts/verify_pin.py` → PIN OK
(measurement de43115e…, newest provenance link 2bc11204… verifies);
`python3 demo.py` → DEMO OK, exact balances (Alice 1009.750104, Bob
985.124948, conservation 2000.0); `python3 quickstart.py` → TRADE OK
in 0.43s (bar 600s). Working tree clean, HEAD still the M1.2 commit
(0a9aa9e); no unlogged changes since the 20:01 run. Port 8472:
nothing answering at check time (curl returned empty; the 16:01
agent-exchange squatter is not answering now, and quickstart/demo
route via pick_port regardless). No code changed, no milestone
progressed, no Groq calls used. Loop idle until the owner orders the
next phase or clears M1.1/M1.3; live testnet settlement cycle still
pending the owner key; x402/USDC / mainnet / hardware TEE remain
owner-ordered later steps, never built in this loop.

## 2026-11-15 ~00:01 — Builder loop: verification-only run

No NOT DONE milestone buildable in this run (M0–M4 all DONE 2026-11-13;
M1.4/M1.5/M1.2 done 2026-11-14; M1.1 repo + push and M1.3 Render deploy
remain BLOCKED on owner steps; M1.6 live verification needs M1.3;
Phase 1 follows M1 in dependency order and has no scoped milestone
spec for this loop). Re-verified state rather than building:
`python3 -m pytest tests/ -q` → **491 passed** (65.86s, no drift from
the 2026-11-14 ~22:01 run); `python3 scripts/verify_pin.py` → PIN OK
(measurement de43115e…, newest provenance link 2bc11204… verifies);
`python3 demo.py` → DEMO OK, exact balances (Alice 1009.750104, Bob
985.124948, conservation 2000.0); `python3 quickstart.py` → TRADE OK
in 0.39s (bar 600s). Working tree clean except BUILD_LOG.md itself
(uncommitted log entries only); HEAD still the M1.2 commit (0a9aa9e);
no unlogged changes since the 22:01 run. No code changed, no milestone
progressed, no Groq calls used. Loop idle until the owner orders the
next phase or clears M1.1/M1.3; live testnet settlement cycle still
pending the owner key; x402/USDC / mainnet / hardware TEE remain
owner-ordered later steps, never built in this loop.

## 2026-11-15 ~00:40 — Track 3: M1.6 core workflow E2E, LOCAL ONLY

M1.6 DONE (local). Live verification still needs M1.3 (Render deploy).
New `scripts/m1_6_e2e.py` (stdlib-only, fresh temp data dir, fresh demo
keys, real HTTP server via `make_server`): register human account
(scrypt, buyer role) -> login -> me() -> markets empty -> create
flex_capacity/aeso_price market via CF-KYA demo agent -> list/detail ->
link agent to account (challenge-bound L3 presentation) -> buy YES 10
(cost 5.124948, balance 994.875052) -> advance demo clock past
resolution -> settle operator value 101000 -> outcome YES, payout 10 ->
ledger conservation ok (total 1000.0, single funded agent) ->
GET /audit/<id> verdict VERIFIED (anchor_status local, decision_basis
OPERATOR_VALUE, inputs_hash + market_state_hash recorded) ->
/hcs-log/verify ok (5 msgs, chain intact; actions account_register,
account_link_agent, create_market, place_order, settle_market) ->
FULL SERVER RESTART on same data dir: login works, market still
RESOLVED outcome YES, ledger still conserved, audit still VERIFIED,
HCS log still verifies. 29 checks, exit 0, "M1.6 E2E OK" (~1.2s).
Three script bugs found and fixed during the run were all in the new
script, not the product: raw() choked on HTML (/app), register takes
`role` not `roles`, link takes `account_id`; audit field is `verdict`
(market status RESOLVED, receipt status SETTLED; HCS action
`settle_market`).

Re-verified this run: full suite **491 passed** (72.67s, no failures);
`verify_pin.py` PIN OK (de43115e…, link 2bc11204… verifies); `demo.py`
DEMO OK, exact balances (Alice 1009.750104, Bob 985.124948,
conservation 2000.0); `quickstart.py` TRADE OK in 0.39s;
`scripts/m4_demo.py` M4 DEMO OK (fake runner only); sandbox parity /
exercise / smoke all OK (jsdom present). Mirror Node re-checked:
topic 0.0.11399235 EXISTS on testnet, not deleted, admin/submit keys
null (fully open), exactly 1 message (seq 1, test payload, payer
0.0.11389089); operator 0.0.11389089 balance 999.383 HBAR, holds no
tokens. No keys in repo (JOULEX_HCS_PRIVATE_KEY appears only as an
env-var reference; test fake is "ab"*32), no mainnet in render.yaml or
hcs_config.json (testnet topic only), deploy stays local-HCS unless the
owner sets JOULEX_HCS_MODE=hedera. No re-pin needed: no `joulex/*.py`
changed (new script under scripts/ only). No live Hedera, no keys used,
no goal timeline entries. Blockers unchanged: M1.1 (repo 403, owner
creates LunaTIKTOK/joulex) + M1.3 (no Render key, owner dashboard step);
live testnet settlement cycle still pending the owner key (transient);
AESO written permission still owed; Phase 1 follows M1.
