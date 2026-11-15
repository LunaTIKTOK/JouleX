# JouleX on Hedera — audit + commercial scalability

Date: 2026-11-12. Auditor: Thiel (coordinator) + verification subagent.
Scope: the Hedera surface of JouleX (`joulex/hcs.py`,
`joulex/hcs_submit.mjs`, `joulex/attestation.py`,
`joulex/server.py` settle path, `joulex/exchange.py`,
`joulex/admission.py` (skim), `joulex/aeso.py`, `data/hcs_config.json`,
`render.yaml`, `DESIGN.md` §7, `docs/API.md`, `docs/HEDERA.md`,
and the Hedera integration package
`~/workspace/your_files/joulex-hedera-integration/`).
Test suite run by the auditor this day: **323 passed** before changes
(verified by the verification subagent, `python3 -m pytest tests/ -q`,
34.41s); see §6 for the final count after the fixes.

## Headline verdict: CONDITIONAL GO

JouleX on Hedera is **sound as an audit anchor at pilot scale and
NOT ready for commercial scale as built** — the conditions are the
fixes in §4 (done in this pass) plus the decisions in §5 (owner
only). Nothing in the audit found a flaw in the money path: the
settle-auth fix holds, the attestation verifier is correct, the
local log's hash chain is intact, and no key material is stored
anywhere. What was broken was the *operational* layer around the
anchor: failures were invisible, nothing retried, nothing could be
observed, and the docs described a system that no longer exists.
Those are fixed below. The remaining blockers are all owner
decisions (mainnet topic with keys, fail-closed policy, AESO
permission, durable storage) or a known throughput ceiling that
needs an async anchor queue before high volume.

## 1. What was verified live (not taken on trust)

| Claim | Live check | Result |
|---|---|---|
| Suite 323/323 | `python3 -m pytest tests/ -q` run 2026-11-12 | **323 passed, 0 failed** |
| Topic `0.0.11399235` exists, 1 message | testnet mirror node `GET /api/v1/topics/0.0.11399235/messages` | 1 message, seq 1, consensus `1794335407.470921104`, tx `0.0.11389089-1794335331-345021439`, result `SUCCESS`, payer `0.0.11389089` |
| First message content matches the local format | base64-decoded payload | Valid JouleX envelope (`action: test_message`, `message_hash` present, hash chain fields present) — but it is the **unattested wiring probe** (`attestation: null`); **no real attested JouleX action has been anchored on-chain yet** |
| Fee per message | Same mirror-node receipt | **717,339 tinybars** = 0.00717339 HBAR ≈ **$0.0008** at ~$0.11/HBAR — matches the $0.0008/message figure in DESIGN.md §7.5 and docs/HEDERA.md |
| Topic key state | mirror node `GET /api/v1/topics/0.0.11399235` | `deleted: false`, **`admin_key: null`, `submit_key: null`**, memo `""`, `auto_renew_account: 0.0.11389089`, `auto_renew_period: 7776000` — **fully open: anyone can post, it can never be deleted or updated** |
| No stored key material | grep over `~/workspace/joulex` excl. node_modules/.git | Clean: no `*key*`/`*secret*`/`*.pem` files, no `BEGIN * PRIVATE KEY`, no key assigned a value (only env reads: `joulex/hcs.py:45`, `joulex/hcs_submit.mjs:17,31`); test-only dummy `tests/test_hcs.py:187` (`"0x"+"ab"*32` via monkeypatch); 64-char hex strings found are SHA-256 file hashes, not keys |
| HCS message size | Hedera docs ("Submit a message", updated 2026-07-10) | Default max chunk 1024 bytes, max chunks 20 → **~20 KiB/message** with the SDK defaults (`hcs_submit.mjs` sets neither) |
| Local message sizes | `data/hcs_log.jsonl` (75 msgs) | Max **1,230 bytes**; all 75 are `orchestrator_allocate`/`orchestrator_summary`, **0 carry an attestation** — an attested message is ~2 KiB by construction, ~10x headroom under the 20 KiB cap |
| Mirror replay tool | `python3 scripts/hcs_mirror_replay.py --data-dir data` (new, this pass) | Ran live: `matched: 0, mirror_count: 1, mirror_only: [1], local_only: [1..75], ok: false` — correct: the one on-chain message is the Nov 10 test probe, which was never written to the local log, and the 75 local messages were never submitted (local-only mode, no receipts) |

## 2. What was verified in code

- **Settle auth HOLDS** (`joulex/exchange.py` `settle_market`):
  the operator-key check runs before any market lookup or oracle
  fetch (`hmac.compare_digest`, 401 `OPERATOR_KEY_INVALID` on a
  wrong key, on any key when none is configured, or on a missing
  key when one is configured); `public_source` settlements require
  a present + valid attestation envelope (401 `JOULEX_VC_REQUIRED`
  / `JOULEX_VC_INVALID`); a present envelope is always verified
  on every market type; oracle failure refuses settlement (503)
  and the market stays OPEN. No change made.
- **Attestation verification is correct**
  (`joulex/attestation.py` `verify_envelope`): shape/version →
  kind whitelist (software only; hardware kinds rejected) →
  TTL/expiry (≤300s) → `report_data` binding hash → measurement
  pinning → evidence re-verification including link-backed
  provenance. No change made. Minor note: an envelope issued
  even slightly in the future is rejected (`current < issued`) —
  clock skew between minter and verifier can cause false
  rejections; keep clocks synced (NTP), no code change.
- **Local log integrity HOLDS** (`joulex/hcs.py`
  `verify_log_file`): header, sequence continuity, hash chain,
  payload-hash integrity, timestamp monotonicity; corrupt log
  refuses startup (fail closed). `data/hcs_log.jsonl` verifies
  `ok: true` (75 messages). No change made.
- **Key handling HOLDS** (`joulex/hcs_submit.mjs:17,26,31`): the
  operator key comes from `JOULEX_HCS_PRIVATE_KEY` in env only,
  never argv, never logged, never written to disk. Caveat:
  `PrivateKey.fromStringECDSA` — an ED25519 operator key fails
  here; the operator is ECDSA, so this is a documented
  constraint, not a bug.
- **AESO oracle is honest and fail-closed** (`joulex/aeso.py:27-31`,
  `fetch_pool_price_series`): any fetch failure or uncovered
  window raises `AESOUnavailable` → 503, market stays OPEN, no
  outcome ever guessed. The module docstring states the licence
  position plainly: AESO data is free to access but commercial
  use needs AESO's written permission; the prototype is
  NON-COMMERCIAL RESEARCH ONLY. No change made — but see §5:
  the permission is a hard blocker for commercial use, and the
  keyless ETS report covers only ~3 hours, so multi-day
  settlement windows cannot resolve (`AESO_NO_DATA`) without the
  keyed APIM path (free self-serve key, owner's step).
- **Store writes are atomic per file, not per state**
  (`joulex/store.py` `save_json`: temp file + fsync +
  `os.replace`; `joulex/exchange.py` `save()` writes
  `markets.json` then `ledger.json` sequentially). A crash
  between the two diverges state. Mitigation exists and is real:
  `GET /reconcile` re-derives all money state from the
  append-only logs and reports divergence loudly. A single
  combined snapshot write is a later milestone, not this pass.

## 3. Findings (graded)

### HIGH

**H1 — Failed HCS submits left no durable trace.**
`RealHCS.append` (pre-fix `joulex/hcs.py`) caught every submit
exception and returned `{"status": "failed"}` inside the returned
dict — which no caller reads (`Exchange._hcs_append`,
`joulex/exchange.py:147`, ignores the return). The local log line
never carried the receipt. A failed anchor was invisible the
moment the request returned: no file, no counter, no alert.
**FIXED this pass** (§4): successes → `data/hcs_receipts.jsonl`,
final failures → `data/hcs_dead_letter.jsonl`, both fsync'd per
line, keyed by local sequence; `receipt_for()` /
`hcs_stats()` expose them; `GET /health` surfaces
`hcs_failed_submits`.

**H2 — No retry.** A single submit attempt; a transient network
blip or a slow node lost the anchor permanently (silently, per
H1). **FIXED this pass** (§4): two retries, 1s/2s backoff;
timeouts and a missing key are not retried (waiting helps
nothing); after exhaustion the outcome is still fail-retain, now
dead-lettered.

**H3 — Synchronous submit holds the exchange lock.**
`place_order` / `create_market` / `settle_market` run under the
exchange `RLock` (`joulex/exchange.py`), and the HCS submit
happens inside it. A hung or unreachable Hedera blocked ALL
trading for up to the runner timeout (was 180s). **PARTLY FIXED
this pass**: runner timeout 180s → 60s (`joulex/hcs.py:64`); the
structural fix — an async anchor queue that submits off the trade
path — is a design change to the ordering guarantee and is NOT
done here (see §4.5 / §5).

**H4 — Testnet topic is fully open.** Verified live (§1):
`admin_key: null`, `submit_key: null`. Anyone can post to
`0.0.11399235`; it can never be deleted or updated. Acceptable
for a testnet wiring probe; unacceptable for commercial traffic.
**DECISION OWED** (§5): the mainnet topic (and any further
testnet topic used for real anchoring) MUST carry a submit key
and an admin key.

**H5 — `Exchange.info()` lied about HCS mode.**
`joulex/exchange.py` `info()` hardcoded `"hcs_mode": "local",
"hcs_topic": null` even when anchoring live — operators could
not tell from `GET /` whether anchoring was on. **FIXED this
pass**: `verifiable_compute` now reports the live mode/topic/
network from `hcs.hcs_info()` (`joulex/hcs.py:208,581`).

**H6 — Mixed mode/config silently degraded to local.**
`make_hcs` returned `LocalHCS` whenever the env flag and the
config disagreed — a deploy that sets `JOULEX_HCS_MODE=hedera`
with a stale or missing config silently lost anchoring.
**FIXED this pass** (`joulex/hcs.py:445-452`): env=hedera +
config≠hedera now raises at startup. (Config=hedera + flag
unset still stays local by design — the Render default.)

### MEDIUM

**M1 — No health, metrics, or structured logging.** The server
had only `GET /` (info); Render's health check pointed at it;
there was no way to observe anchor failures, throughput, or
uptime, and request logs were raw stdlib stderr lines.
**FIXED this pass** (§4): `GET /health`, `GET /metrics`,
one-JSON-line-per-request stderr logging.

**M2 — No mirror-node replay.** If `data/` is wiped (Render
free tier wipes it on redeploy), the HCS topic still holds the
messages but nothing could read them back or verify the local log
against the topic. **FIXED this pass**: `scripts/hcs_mirror_replay.py`
(read-only, no key) + `scripts/backup_data.py` (one-command
tar + SHA-256 manifest) + recovery drill in
`docs/MAINNET_READINESS.md` §5.

**M3 — Stale docs.** `DESIGN.md` §7.5 said `RealHCS` was
"designed but inert" (false since 2026-11-10); `docs/HEDERA.md`
step 4 said the mode flag "changes nothing" (false);
`docs/API.md` said `hcs_mode` is always `"local"` (false after
the H5 fix). **FIXED this pass**: all three updated, plus the
relay's `INTEGRATION_SPEC.md` §7–§8.

**M4 — Relay verify could never verify.** In the integration
package, `relay.py` `verify_seq` read the `hedera` block off
the local log message (never persisted — H1) so it always
answered `not_anchored`, and it fetched the mirror message by
the LOCAL sequence while the mirror's `sequence_number` is the
TOPIC sequence. **FIXED this pass**: verify now reads the
receipt from the durable sidecar and looks the mirror message up
by `topic_sequence`; smoke-tested in local mode (submit →
`anchored_local_only`, verify → `not_anchored`, correct).

**M5 — AESO commercial licence + ~3h window.** See §2. Hard
blocker for commercial oracle use; **DECISION OWED** (§5).

**M6 — No rate limiting; CORS `*`; unbounded bodies.**
`joulex/server.py` had no per-agent rate limit, allowed any
origin (needed for the `file://` demo page), and read request
bodies with no cap. **PARTLY FIXED**: body cap 1 MB → 413
`REQUEST_TOO_LARGE` (`joulex/server.py:37,145`). Rate limiting
and CORS restriction are deployment-policy decisions —
**DECISION OWED** (§5).

### LOW

**L1 — Message-size bound was undocumented and unguarded.**
SDK defaults cap a message at ~20 KiB (20 chunks × 1024 B);
current messages are ~1.2 KiB, attested ~2 KiB. **FIXED this
pass**: `MAX_MESSAGE_BYTES = 20 * 1024` (`joulex/hcs.py:60`)
with a fail-fast guard in `append` (oversize → failed +
dead-lettered, never submitted, runner never called).

**L2 — Two-file state save is not atomic as a pair.** See §2.
Mitigated by `/reconcile`; no change this pass.

**L3 — `hcs_submit.mjs` is ECDSA-only** (see §2). Documented
constraint, no change.

## 4. What changed in this pass

Backups: every modified file has a `.bak_20261112` copy next to
it (same convention as the tree). No key material in any artifact
(grep-verified again after the changes — §6).

| File | Change |
|---|---|
| `joulex/hcs.py` | Receipts sidecar (`hcs_receipts.jsonl`) + dead-letter file (`hcs_dead_letter.jsonl`), `receipt_for()`, `hcs_stats()`, `hcs_info()`; bounded retry (2 retries, 1s/2s, no retry on timeout/missing key); runner timeout 180s → 60s; message-size guard (20 KiB); `make_hcs` fails closed on env/config mismatch |
| `joulex/exchange.py` | `info()` reports live HCS mode/topic/network; `metrics()` (process-start counters + HCS stats); counters incremented on market creation / order / settlement |
| `joulex/server.py` | `GET /health`, `GET /metrics`; JSON request logging (one line per request, no bodies/secrets); request-body cap 1 MB → 413 |
| `scripts/hcs_mirror_replay.py` (new) | Mirror-node replay check: fetch topic messages, decode, match to local log by `message_hash`; exit 0/1 with a JSON report |
| `scripts/backup_data.py` (new) | One-command `data/` backup: timestamped tar.gz + SHA-256 manifest, temp files excluded |
| `tests/test_hcs_commercial.py` (new, 11 tests) | Receipts, dead-letter, retry/backoff, no-retry-on-timeout, size guard, `hcs_info`/`hcs_stats`, config fail-closed (both directions) |
| `tests/test_commercial_ops.py` (new, 6 tests) | `info()` live mode, metrics counters, `/health`, `/metrics`, body cap |
| `tests/test_hcs_mirror_replay.py` (new, 6 tests) | Mirror decode, all-matched, local-only (ok), receipted-but-missing (fail), mirror-only (fail), undecodable (fail) |
| `tests/test_backup_data.py` (new, 1 test) | Archive contents + manifest hashes + temp exclusion |
| `DESIGN.md` §7.5 | RealHCS live (was "inert"); submission semantics, receipts/retry/dead-letter, mirror tool, fail-closed config |
| `docs/HEDERA.md` | Step 4 fixed (flag is live, not inert); receipts/retry/dead-letter + mirror replay documented |
| `docs/API.md` | `/health`, `/metrics`, live `hcs_mode` in `GET /`, 413 error row |
| `docs/MAINNET_READINESS.md` (new) | Full mainnet checklist: topic/account, cost table, anchoring-policy decision, throughput, durability/recovery, AESO, hardening, verification steps |
| Integration package | `hcs.py` re-copied (hash-identical to `joulex/hcs.py`, verified); `relay.py` `verify_seq` fixed (receipt sidecar + topic-sequence lookup); `INTEGRATION_SPEC.md` §7–§8 updated (incl. open-topic warning) |
| `data/measurements.json` | Re-pinned after the code changes (measurement is a hash of every `joulex/*.py`); provenance link rebuilt |

### 4.5 What was deliberately NOT changed

- **Fail-retain anchoring policy**: the default outcome after
  retries exhaust is still fail-retain (trade executes, failure
  dead-lettered). Switching to fail-closed is the owner's policy
  call (§5), not an engineer's silent default change.
- **Settle auth**: untouched (verified, holds).
- **Synchronous submit**: still on the trade path. The async
  anchor queue is the next milestone's design work, not a
  drive-by refactor — it changes the ordering guarantee
  (audit-before-execution) that the current 503 path relies on.
- **CORS `*`**: left as-is (the `file://` demo page needs it);
  restriction is a deploy decision (§5).
- **No mainnet anything**: no accounts, no topics, no HBAR moved,
  no deploy, no contact with AESO/Hedera.

## 5. Decisions owed (owner only)

1. **Mainnet topic + account**: create a mainnet HCS topic WITH
   a submit key and an admin key, and a funded mainnet operator
   account. Never reuse the testnet topic. (H4; checklist
   `docs/MAINNET_READINESS.md` §1.)
2. **Anchoring policy**: fail-retain (current) or fail-closed —
   and whether settlements of real value require fail-closed at
   minimum. (H3-adjacent; checklist §3.)
3. **AESO written permission** for commercial use of pool-price
   data, plus the free keyed APIM account for multi-day windows.
   Without it the oracle stays research-only. (M5; checklist §6.)
4. **Durable storage**: Render paid disk (or a volume), or a
   `backup_data.py` cadence with off-box copies. Until then every
   redeploy wipes `data/`. (M2; checklist §5.)
5. **Async anchor queue** before high volume: approve the design
   (submit off the trade path, ordered, same retry/dead-letter
   machinery) once the target message rate is known. (H3; §6
   model below.)
6. **CORS + rate limits** at deploy: restrict origins to the sim
   site; set a per-agent request rate. (M6; checklist §7.)
7. **Render deploy itself**: GitHub repo + Render service creation
   (prepped `render.yaml`; still not deployed). Note: do NOT set
   `JOULEX_HCS_MODE=hedera` or the operator key in Render until
   decisions 1–2 are made — the render.yaml comments already say
   this.
8. **Live relay submit end-to-end**: needs the operator key in
   env (owner step); the relay code is fixed and smoke-tested in
   local mode, but no live submit has gone through it yet.

## 6. Scalability model

**Cost** — one HCS message per action (order / market creation /
settlement / attestation-verification result). Observed fee
717,339 tinybars ≈ **$0.0008/message**. Mirror-node reads are
free.

| Messages/day | $/day | $/year | HBAR/day |
|---:|---:|---:|---:|
| 100 | 0.08 | 29 | 0.7 |
| 1,000 | 0.80 | 292 | 7.3 |
| 10,000 | 8.00 | 2,920 | 73 |
| 100,000 | 80.00 | 29,200 | 727 |

The fee is USD-denominated (converted at the network exchange
rate), so the USD cost is stable while the tinybar amount moves
with HBAR. Watch the fee schedule: if the topic ever enables
custom fees the cost jumps to ~$0.05/message (Hedera docs) — the
submit key (decision 1) is what keeps that under operator
control.

**Throughput** — the binding constraint is the synchronous
submit: each action waits for the Hedera receipt (~2–5s typical)
while holding the exchange lock, so the ceiling is roughly
**12–30 anchored actions/minute**. At 1,000 msgs/day (~0.7/min
average) there is ample headroom; at 10,000/day (~7/min average,
bursty) it is marginal; at 100,000/day (~70/min average) it is
impossible without the async anchor queue (decision 5). Message
size is not a constraint (2 KiB vs the 20 KiB SDK default).

**Failure modes** (and what now catches each):

| Failure | Before this pass | Now |
|---|---|---|
| Transient network blip on submit | Silent loss | Retried (2×), then dead-lettered + counted |
| Hedera unreachable for minutes | Trade path blocked up to 180s per action | Blocked up to 60s per action; failures dead-lettered + counted; trades still execute (fail-retain) |
| Oversize message | Paid for a doomed tx | Fail-fast guard, dead-lettered, runner never called |
| Config/flag mismatch | Silently local, anchoring lost | Startup refusal (fail closed) |
| `data/` wiped on redeploy | State + log gone, no recovery path | `backup_data.py` + mirror replay verifies what the topic still holds; recovery drill documented |
| Operator balance runs out | Submits fail one by one, silently | Dead-letter + `hcs_failed_submits` on `/health`; needs an alert wired (decision 7) |
| Unexpected poster on the open testnet topic | Nothing detected it | `hcs_mirror_replay.py` reports `mirror_only` |
| Local log tampered | Startup refusal (pre-existing) | Unchanged — still fail-closed |

**What HCS does not do (unchanged, stated for the record):**
HCS is an ordered, timestamped log, not a database — state is
reconstructed by replaying the LOCAL logs (`hcs_log.jsonl` +
`trades.jsonl` / `settlements.jsonl`), not by reading the topic.
The topic cannot rebuild `markets.json`/`ledger.json` on its own;
that is what the backup + recovery drill are for. HCS also proves
nothing about physical delivery (see `INTEGRATION_SPEC.md` §9),
and the attestation remains software signatures, not TEE hardware
(DESIGN.md §7.6).

## 7. Market context (not a claim about JouleX)

Apex Group ($3.5T under administration) joined the Hedera
Council on 2026-11-11 to explore tokenization and settlement
(The Block, 2026-11-11). That is tailwind for Hedera-based
settlement infrastructure in general — it does not change any
finding above, and JouleX claims no relationship with Apex.

## Appendix — test suite

Final run after all changes (this pass): **347 passed, 0 failed**
(`python3 -m pytest tests/ -q`, 44.06s). New tests:
`tests/test_hcs_commercial.py` (11),
`tests/test_commercial_ops.py` (6),
`tests/test_hcs_mirror_replay.py` (6),
`tests/test_backup_data.py` (1). Pre-existing suite was 323/323
before this pass. Backups: `.bak_20261112` next to every modified
file. Key-material grep re-run after the changes: clean (no key
material in any artifact produced). Integration zip rebuilt:
`~/workspace/your_files/joulex-review/joulex-hedera-integration.zip`
(29,891 bytes, 8 files; `hcs.py`/`attestation.py` hash-identical
to the live code).
