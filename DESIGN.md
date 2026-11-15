# JouleX — Design (v1)

**Status:** v1, paper-only. Local service, stdlib-only Python 3.12.
**Owner order:** 2026-11-09 23:52 — build JouleX as a real commercial
autonomous AI-agent exchange for energy and compute, with Verifiable
Compute and a Hedera-anchored audit trail.
**Phase 1 research:** `~/workspace/research_notes/joulex-phase1-20261109.md`
(commercial validation, landed 2026-11-09/10). Its verdict: conditional
GO for the simulated market; buyer side weak, permissions gated; AESO
data has NO commercial licence (written permission needed); the honest
framing is "flexibility as a grid-connection and reliability product",
not "arbitrage the spread". This build follows that verdict: the
simulator (M2) tests the falsifier; the exchange (M0) is the venue the
simulator's contracts would trade on; nothing here claims a buyer, a
licence, or a price that does not exist.

## 0. What this is — and what it is not

JouleX is an exchange where *only bound agents* trade energy–compute
flexibility contracts. Agents trade outcome shares in markets that
resolve in 1–30 days. Every order carries a CF-KYA presentation in
the `X-CF-KYA` header; the exchange verifies it before the order
touches the book. Winners are paid 1 paper point per share at
settlement; losers get 0. Markets are binary (YES/NO) or multi-outcome
(N named outcomes, `public_source` only).

**Three layers (owner's structure):**

- **Layer 1 — Intelligence:** forecast electricity and compute demand.
  In v1 this is the AESO price feed + a price-history dataset the
  orchestrator consumes (M2). Forecasting models are future work.
- **Layer 2 — Orchestrator:** autonomously allocate workloads and
  optimise costs — shift/pause/shape AI jobs against power signals.
  In v1 this is the deterministic simulator (M2): given deferrable
  jobs and a price history, allocate and score against strong
  baselines. It is a *simulation*, labelled as such everywhere.
- **Layer 3 — Exchange:** match buyers and sellers of compute,
  electricity-linked capacity and flexibility contracts — multi-party
  bidding, verifiable clearing, standardised contracts, transparent
  settlement. In v1 this is the paper exchange (M0) + the Verifiable
  Compute audit layer (M1). It is the long-term ambition; the paper
  venue exists now so the mechanism can be proven before any
  participant, volume, or permission exists.

**The Verifiable Compute + Hedera layer (M1)** is not a side feature.
Every agent action carries an attestation envelope, and every action
is recorded in an ordered, timestamped, hash-chained audit log built
to the Hedera Consensus Service (HCS) schema. The log answers the
owner's question verbatim: *when did what agent know what, and who
told whom*. The local log is a faithful HCS-schema implementation;
the live Hedera connection is a separate, later, owner-ordered step
(no account is created by this build).

**Non-goals for v1:** real money (no USDC, no x402 settlement),
order-book matching, leverage, shorting, live Hedera transactions,
hardware TEE attestation (see §7 — software attestation envelope
only), any claim of a buyer, a licence, or AESO commercial use.
AESO data is used under its published terms: the v1 prototype is
non-commercial research; commercial use needs AESO's written
permission (Phase 1 §3.3 — verified from the AESO site terms).

## 1. Architecture: reuse, not reinvention

JouleX M0 is a port of the proven agent-exchange core
(`~/workspace/agent-exchange/`, M0–M6 done, suite 190/190 green
2026-11-09). The port rule: **copy the code, adapt the market
vocabulary, keep the verified mechanics identical.** The copy is
recorded with source, date, and SHA-256 in `joulex/VENDOR.md`
(same vendoring rule as the agent exchange's CF-KYA snapshot).
What changes in the port:

| Agent-exchange (source) | JouleX (this build) |
|---|---|
| Prediction markets, 1–7d horizon | Flexibility / compute contracts, 1–30d horizon |
| `kraken_price` resolution (Kraken daily close) | `aeso_price` resolution (AESO Alberta pool price vs strike over a delivery window) + `public_source` |
| `public_source` operator attestation | Same, unchanged |
| Port 8471 | Port **8472** (no clash) |
| LMSR, CF-KYA L3 admission, paper ledger, JSON persistence, reconciliation | Identical mechanics, new market vocabulary |
| SettlementOracle (Kraken) | **AESOOracle** (AESO public pool-price endpoints; `--aeso-oracle` flag) |
| No attestation layer | M1 attestation envelope + HCS-schema audit log |

What does NOT change: LMSR math, admission flow, ledger
conservation, persistence format, reconciliation algorithm, error
shape, challenge store. A JouleX test that replays the
agent-exchange invariants must pass unchanged.

## 2. Markets and contracts

**Contract types** (`contract_type`, stated at creation):

- `flex_capacity` — a flexibility / curtailment contract. Example:
  "Will 1 MW of flexible compute load curtail during delivery window
  W?" Resolution is normally `aeso_price` (pool price exceeding a
  strike is the curtailment trigger — the mechanism Phase 1 §2.7
  documents: load is dispatched when price is high).
- `compute_forward` — a forward price contract on GPU-hours.
  Example: "Will H100 spot be above $4.00/GPU-hr on <date>?"
  Resolution is `public_source` in v1 (operator-attested with
  evidence; an automated GPU-price oracle is future work).
- `generic` — any other event, `public_source` only.

**Resolution types, stated at creation, per market:**

- `aeso_price`: "Will the Alberta pool price exceed `<strike>`
  $/MWh at any point during window `[window_start, window_end]`?"
  (max-pool-price rule — v1 uses MAX, not mean: it matches the
  "avoid the cap" story and is the simplest deterministic rule.)
  Source: AESO public pool-price / SMP endpoints (see §5). The
  observed series (or its max) and the source URL are recorded on
  the market at settlement, so any third party can re-derive the
  outcome. Ties (max == strike) resolve NO.
- `public_source`: any other event, binary or multi-outcome.
  Creation must name a public source URL and a one-line resolution
  rule. Settlement is operator-attested: the settler supplies
  the outcome plus evidence text/URL, recorded on the market —
  and since the 2026-11-11 settlement-auth fix the settler MUST
  also supply a valid attestation envelope bound to the settlement
  payload hash (absent is 401 `JOULEX_VC_REQUIRED`); an
  unauthenticated caller can no longer settle a public_source
  market on an evidence string alone.
  Multi-outcome markets are `public_source` only — an `aeso_price`
  market with outcomes other than `["YES", "NO"]` is rejected at
  creation (400). Same rule as the agent exchange's `kraken_price`.

**Shape:** binary (YES/NO) or multi-outcome (2–10 outcomes, labels
≤64 chars, no `|`). **Horizon:** `resolves_at` must be 1–30 days
after creation. Trading stops at `resolves_at`; settlement only
at/after it. **No trading after resolution** (409). **Settlement
is operator-invoked and authenticated** (`POST /markets/<id>/settle`):
when an operator key is configured, a matching `X-Operator-Key`
header is required (401 `OPERATOR_KEY_INVALID` otherwise — the
same semantics as market creation), and `public_source`
settlements additionally require the attestation envelope above.
Before 2026-11-11 this route performed no authentication at all;
the fix is fail-closed and changes no other path. The AESO
oracle (M0, `--aeso-oracle`) fetches the pool-price series itself
for `aeso_price` markets — the fetched values win over any
operator-supplied value, and if AESO is unreachable the settlement
is refused (503) and the market stays OPEN. Without the flag, the
operator-supplied value path remains (demo + tests).

## 3. Matching: LMSR automated market maker (unchanged)

Identical to the agent-exchange DESIGN.md §2: generalized LMSR over
N outcomes, `C(q) = b · ln( Σ_i exp(q_i/b) )`, prices start at 1/N,
buying Δ shares costs `C(q + Δ·e_i) − C(q)`, no shorting (sell only
what you hold), worst-case maker loss `b·ln N` per market. A CLOB
is a later-version decision (needs an owner decision, replaces LMSR,
never runs alongside it).

## 4. Admission: CF-KYA at order time (unchanged)

Identical to the agent-exchange DESIGN.md §3: challenge (TTL 300s,
single-use, persistent store), `kya.verify_presentation(...,
require_l3=True)`, L1/L2 rejected for TRADE, revocation list fresh
(≤3600s) or reject, payload binding (order payload is exactly
`{"agent_id", "market_id", "shares", "side", "action"}`), challenge
marked seen only on success. Market creation is admitted the same
way (CF-KYA bound to the creation payload, or the operator key;
creation payload = market-defining fields + `agent_id`, including
`contract_type`, `resolution_type`, `strike`, `window_start`,
`window_end` alongside the shared fields). The trust store pins the
demo issuer for test principals; swapping it for a production
issuer's key is the only change needed for production admission.
The CF-KYA code is vendored unmodified from the agent-exchange
snapshot (`joulex/vendor/`, hashes in `joulex/VENDOR.md`).

## 5. AESO oracle

`joulex/aeso.py` — the AESO price feed. Public endpoints only
(no key for the ETS public reports; the Azure APIM gateway needs a
free self-serve key — the oracle uses the **keyless public
reports** first and documents the APIM path as the upgrade).

- `fetch_pool_price_series(window_start, window_end)` returns the
  hourly pool prices over the window, with source URL, from the
  AESO public pool-price report. On fetch failure: raise
  `AESOUnavailable` — the caller refuses settlement (503), never
  guesses.
- `fetch_current_pool_price()` returns the latest pool price +
  timestamp (used by the orchestrator and the demo).
- Every fetch records `source_url` + `fetched_at`; settlement
  receipts carry both.
- **Licence honesty:** the module docstring states the AESO terms
  (non-commercial use without written permission) and the oracle
  is labelled research-only. The oracle never republishes AESO data
  as a commercial feed.
- The M2 simulator caches fetched history under `data/` so a run
  is reproducible offline; a cache miss with no network falls back
  to the labelled synthetic series in `joulex/aeso_synthetic.py`
  (built from the Phase 1 verified price statistics — 2025 avg
  $43.68, 929 zero-price hours, 8h at cap — and labelled
  `SYNTHETIC` in every output that uses it). Synthetic data is
  never presented as observed data.

## 6. Money: paper points ledger (unchanged)

Identical to the agent-exchange DESIGN.md §6: 1,000 paper points
per agent (lazy credit on first verified order), LMSR cost debits
balance / credits pool, settlement pays 1 point per winning share,
conservation invariant `Σ agent balances + pool == Σ initial
endowments`, insufficient points → 403 with no state change,
reconciliation replays `trades.jsonl` + `settlements.jsonl` and
fails loudly on mismatch (`GET /reconcile`, CLI
`python3 -m joulex.reconcile`). No fees in v1.

## 7. Verifiable Compute layer (M1) — the attestation envelope + HCS audit log

Implements Phase 0 of `~/workspace/cf-kya-dynamic/VERIFIABLE_COMPUTE.md`
in software, adapted to JouleX. **Honest labelling, stated in the
code and the docs: the v1 envelope is a SOFTWARE attestation. It
proves which code version minted the action and binds the action to
that code; it is NOT a hardware TEE attestation (no SEV-SNP/TDX/SGX
hardware is involved) and must never be described as one.** The
hardware path (Phase 1 of that document — SEV-SNP CVM) is a later,
owner-decided step; the envelope is versioned so a hardware kind
can be added without changing the wire format.

### 7.1 The attestation envelope

Version `"joulex-attest-0.1"`. Carried as an optional field on every
action record (order, market creation, settlement attestation) and
on the trade/settlement receipts. Fields:

```json
{
  "version": "joulex-attest-0.1",
  "kind": "software",
  "measurement": "sha256 hex (code manifest hash — see below)",
  "report_data": "sha256 hex (binding — see below)",
  "evidence": "base64 (in-toto link metadata for the build, or the signed envelope)",
  "issued_at": "2026-11-09T15:00:00Z",
  "expires_at": "2026-11-09T15:05:00Z"
}
```

- `kind` ∈ `software` (v1). Hardware kinds (`sev-snp`, `tdx`,
  `sgx-dcap`, `nitro`, `zkvm-risc0`, `zkvm-sp1`) are reserved and
  require a version bump + a hardware verification path. A verifier
  MUST reject an envelope whose kind it does not implement.
- `measurement`: SHA-256 of the canonical code manifest — the
  sorted list of `(relative path, sha256)` for every `.py` file in
  the `joulex/` package at mint time, hashed as canonical JSON.
  This is what "which code ran" means in software: the exact code
  tree that minted the action. `joulex/attestation.py` computes it
  (`compute_measurement()`), and the pinned measurement set lives
  in `data/measurements.json` (operator-maintained; a measurement
  not in the pinned set is rejected when a policy requires it).
- **Binding rule (normative).** `report_data` MUST equal SHA-256
  over the JCS canonical bytes of
  `{credential_id, challenge_nonce, payload_hash}` for THIS action
  (same rule as VERIFIABLE_COMPUTE.md §5.1, with JouleX field
  names). A report captured from an earlier action cannot be
  stapled to a later one: the challenge nonce and payload hash
  differ. A verifier MUST recompute `report_data` from the live
  request and compare; a mismatch is a binding failure
  (`JOULEX_VC_BINDING`), rejected like any other binding failure.
- `expires_at − issued_at` MUST be ≤ 300s (same bound as the
  token TTL — an attestation outlives nothing it attests to).
- `evidence`: in-toto link metadata for the build that produced
  the code tree (see §7.4), base64-encoded; or, for a
  software-minted envelope with no link file, the base64 of the
  canonical envelope bytes signed by the minting agent key. The
  verifier re-verifies the evidence; it never trusts the agent's
  assertion that it verified.

### 7.2 Attestation policy

Optional field on the identity credential (inside the signed
content, so the quorum attests to it) — same shape as
VERIFIABLE_COMPUTE.md §5.2, with JouleX kinds:

```json
"attestation_policy": {
  "required": false,
  "kinds": ["software"],
  "measurements": ["<sha256 hex, pinned code-manifest hash>"],
  "max_age_seconds": 300
}
```

`required: true` → the verifier MUST see a valid envelope for
every action under this credential; absence is `JOULEX_VC_REQUIRED`.
`required: false` (default) → advisory: present and valid →
recorded on the receipt; present and invalid → rejected (an invalid
attestation is evidence of attack, never ignored); absent → plain
CF-KYA verification. Default policy for v1 is advisory — required
is per-credential where a counterparty demands it.

**Settlement authentication (amendment, 2026-11-11).** Settlement
is not a credential action, so the per-credential policy above
never governed it — and until this amendment the settle route
checked nothing at all. Two gates now apply, both fail closed:
(1) when an operator key is configured, settlement requires a
matching `X-Operator-Key` header (the create_market semantics:
presented key authoritative, constant-time compare, 401
`OPERATOR_KEY_INVALID` for missing/wrong/unconfigured); (2) a
`public_source` settlement MUST carry a valid envelope bound to
the settlement payload hash — absence is `JOULEX_VC_REQUIRED`
regardless of the operator key, because the settler is asserting
an off-exchange fact. `aeso_price` settlements keep the advisory
rule above: their outcome derives from the fetched oracle series
or the key-gated operator value, and a present envelope is still
always verified.

### 7.3 The HCS-schema audit log — "when did what agent know what, and who told whom"

`joulex/hcs.py` — the audit log. Every agent action (order,
market creation, settlement, attestation verification result) is
appended as one ordered message to `data/hcs_log.jsonl`. The
schema mirrors Hedera Consensus Service semantics:

```json
{
  "topic": "joulex-actions",
  "sequence": 42,
  "consensus_timestamp": "2026-11-09T23:52:00.000000000Z",
  "sender": "<agent_id>",
  "action": "place_order",
  "payload_hash": "sha256 hex",
  "payload": {"market_id": "...", "side": "YES", "shares": 10},
  "attestation": {"version": "joulex-attest-0.1", "kind": "software", "measurement": "...", "report_data": "..."},
  "provenance": {"told_by": "<agent_id or 'exchange' or 'aeso-oracle'>", "known_facts": ["sha256 ...", "..."]},
  "prev_hash": "sha256 hex of previous message",
  "message_hash": "sha256 hex of this message (canonical bytes, excluding message_hash)"
}
```

- `sequence` is strictly increasing, assigned by the log (the
  local stand-in for HCS sequence numbers). `consensus_timestamp`
  is the server-assigned timestamp at append time, in HCS
  nanosecond format — **labelled `LOCAL` in the log metadata:
  it is a local simulation of a consensus timestamp, not a Hedera
  consensus timestamp.** The log file header records
  `{"mode": "local", "hedera_topic": null}` until a real topic is
  configured (owner step, §7.5).
- `prev_hash` chains every message to its predecessor (hash chain
  — tampering with any message breaks every later hash).
  `message_hash` covers the canonical bytes of the message.
- `provenance.told_by` records who supplied the information the
  action relied on (the challenge issuer for orders, the oracle
  for settlements, another agent for a relayed fact). `known_facts`
  lists the payload hashes the acting agent had seen at mint time.
  Together they answer "who told whom" and, by replay, "what did
  agent X know at time T".
- **Query functions** (`joulex/hcs.py`, tested):
  - `what_did_agent_know(agent_id, t)` — replay the log up to
    timestamp `t`; return the set of facts (payload hashes +
    messages) that agent had received (as sender or as
    `told_by` target) by then.
  - `who_told_whom(agent_a, agent_b, t0=None, t1=None)` — all
    messages where A told B (A is sender or `told_by`, B is the
    acting agent), in order, with timestamps.
  - `verify_log()` — re-walk the chain: sequence continuity,
    hash-chain integrity, timestamp monotonicity. Returns a
    report; any break is a hard failure (the log is the audit
    trail — a broken log is reported, never silently repaired).
- The log is append-only. No edit, no delete, no rewrite API.
  A corrupt log refuses startup (fail closed), like the
  challenge store.

### 7.4 Provenance — in-toto link metadata

Every release of the `joulex/` code produces signed in-toto link
metadata (`joulex/provenance.py`): the build step records the
materials (source files + hashes) and products (the code manifest
hash = the measurement). The link file is stored under
`data/provenance/` and its hash is referenced by the attestation
envelope's `evidence`. This is the Phase 0 provenance leg from
VERIFIABLE_COMPUTE.md §8 step 4. SLSA Level 2 is the bar (signed
provenance, not yet hermetic builders). in-toto is a pip
dependency — if it is unavailable in the build environment, the
provenance module falls back to a stdlib-only link format with
the identical schema, labelled as such; the measurement does not
depend on in-toto being installed.

### 7.5 The live Hedera step (owner-only, NOT built here)

`joulex/hcs.py` exposes an `HCSClient` interface with two
implementations:

- `LocalHCS` — LIVE in v1: writes the local `data/hcs_log.jsonl`.
- `RealHCS` — LIVE on testnet since 2026-11-10 (was "designed
  but inert" in the v1 draft of this section; implemented and
  verified end to end that day — see `docs/HEDERA.md`). With
  `JOULEX_HCS_MODE=hedera` and a `hedera` config it submits each
  log message to the topic via `ConsensusSubmitMessage` (through
  `joulex/hcs_submit.mjs`) and records the returned sequence +
  consensus timestamp alongside the local copy. The local log
  remains the fallback and the replay source either way.

Submission semantics (audit 2026-11-12,
`AUDIT_HEDERA_COMMERCIAL_20261112.md`): local-first, then submit
with bounded retries (two retries, 1s/2s backoff; a timeout or a
missing key is not retried — the submit helper caps at 60s so a
hung submit cannot hold the exchange lock for three minutes).
Every outcome is recorded durably: successes to
`data/hcs_receipts.jsonl`, final failures to
`data/hcs_dead_letter.jsonl`, keyed by local sequence
(`receipt_for()` / `hcs_stats()`). Ordinary log messages stay
fail-RETAIN — a failed submit never loses the local record and
never blocks the action — but the failure is now observable
instead of living only in a return value no caller reads.
**Settlement has its own policy flag (M4, §7.7):**
`--settle-anchor-policy` / `JOULEX_SETTLE_ANCHOR_POLICY`,
default `fail_closed` — a settlement is never marked SETTLED
without an anchor receipt; `fail_retain` restores the old
inline-finalize behaviour for the settlement path. The final
anchoring policy is the owner's call; the flag implements it and
recommends fail-closed for settlement. Messages
larger than the SDK default (~20 KiB: 20 chunks x 1024 bytes)
fail fast with a clear error and are dead-lettered, never
submitted. `scripts/hcs_mirror_replay.py` verifies the local log
against the topic from the public mirror node (read-only, no key).

The owner steps to go live are in `docs/HEDERA.md`: (1) create a
Hedera account (testnet first, via the Hedera portal — owner, with
their own keys); (2) create an HCS topic (testnet, then mainnet)
and record the topic ID in `data/hcs_config.json`; (3) fund the
account with HBAR for message fees ($0.0008/message as of Jan
2026 — verified against the live receipt, 717,339 tinybars for a
~900-byte message on 2026-11-10); (4) set
`JOULEX_HCS_MODE=hedera`. A hedera request whose config disagrees
(env says hedera, config says local) fails closed at startup —
it never silently runs local and loses anchoring.

### 7.6 What the M1 layer does NOT prove

Stated in the code and docs, once: the software envelope proves
which code tree minted the action and binds the action to that
code; it does NOT prove the code ran on genuine hardware, that
the host was uncompromised, or that the agent's judgement was
sound. A compromised host can mint a valid software envelope for
bad code — the measurement would differ from the pinned set, which
is the detection, not prevention. The hardware leg (SEV-SNP CVM)
is the answer to that residual and is a later, owner-decided step.

### 7.7 Settlement layer + auditability (M4, 2026-11-13)

**Async anchor queue (`joulex/anchor_queue.py`).** `RealHCS.append`
writes the local record, then enqueues an anchor job and returns
immediately with `hedera.status == "queued"` and a `job_id`. A
worker thread (`start_anchor_worker()` / `stop_anchor_worker()`,
started by `joulex.server main()` under RealHCS) drains the queue
with retry + dead-letter, reusing the
`hcs_receipts.jsonl` / `hcs_dead_letter.jsonl` patterns. The
exchange lock is never held by an HCS submit again. The queue is
durable (`data/anchor_queue.json`), idempotent on `message_hash`,
and survives restarts; pending settlements are persisted
separately (`data/pending_settlements.json`) and resume on
startup. Queue mechanics measured locally: 50 jobs enqueued +
drained in ~0.03s. The Hedera-side bound is receipt latency
(~2–5s per submit observed on testnet 2026-11-10), which caps a
single sequential submitter at roughly 17k–43k anchored actions
per day; sustained live throughput is NOT yet measured — it needs
the owner-supplied operator key (see §7.7, open decisions).

**Fail-closed settlement (`Exchange.settle_anchor_policy`,
default `fail_closed`).** Under RealHCS + fail-closed,
`settle_market` appends the settle message and returns
`{"status": "PENDING_ANCHOR", "settlement_id", "anchor_job_id"}`,
with the market frozen as `SETTLEMENT_PENDING` — no payout, no
`settlements.jsonl` line, no receipt until the anchor receipt
lands and `on_anchor` finalizes it. A re-settle while pending is
idempotent: it returns the same `settlement_id` and enqueues no
second message. If the anchor job dies (retries exhausted) the
pending record becomes `ANCHOR_FAILED` and a fresh settle
supersedes it with a new `settlement_id`. Under LocalHCS, or
under `fail_retain`, settlement finalizes inline exactly as
before, plus the receipt sidecar. **Oversize refusal:** an
envelope estimated above the ~20 KiB HCS limit is refused BEFORE
appending with 503 `HCS_ANCHOR_FAILED` — the market stays OPEN
and no strandable PENDING_ANCHOR record is created (an append
that fails outright is refused the same way as belt-and-braces).

**Settlement receipts (`data/settlement_receipts.jsonl`,
`joulex/settlement_receipts.py`).** One line per settlement:
`{settlement_id, market_id, outcome, observed_value, evidence,
settled_at, ledger_entry_hash, hcs_topic, topic_sequence,
consensus_timestamp, tx_id, attestation, attestation_status,
attestation_binding, inputs_hash, decision_basis, anchor_job_id,
anchor_status, transfer_tx_id}`. `ledger_entry_hash` is the
SHA-256 of the canonical `settlements.jsonl` record; the
settlement itself now also carries `settlement_id` + status
`SETTLED` so that hash is well-defined. `transfer_tx_id` is
`None` until a value-rail transfer is recorded for the
settlement.

**Decision-basis capture (`joulex/decision_basis.py`).** The
settlement envelope carries the inputs the agent saw when it
decided: oracle label (`AESO-live` / `SYNTHETIC` / `OPERATOR_VALUE`
/ `PUBLIC_SOURCE` — a synthetic marker on the oracle is never
labelled live), the oracle snapshot (or evidence hash for
`public_source`), the settling agent's CF-KYA credential id (or
`operator`), and a `market_state_hash` over the market before
settlement, all folded into `inputs_hash`. This is
non-repudiation of the decision basis: the anchored record is
self-describing, and a later auditor can recompute `inputs_hash`
from the stored basis.

**Value rail — testnet prototype only (`joulex/transfer.py`,
`joulex/hcs_transfer.mjs`).** A testnet HBAR transfer leg links a
transfer transaction to a settlement id: `record_transfer`
performs the transfer (Node `TransferTransaction`, testnet-only,
fail-closed — a mainnet argument refuses before any key is read)
and appends one line to `data/settlement_transfers.jsonl`;
`update_receipt_transfer` writes the `transfer_tx_id` back onto
the receipt. Idempotent per settlement id. Worthless test tokens
only; mainnet is NOT part of this build. The rail is NOT
auto-wired into settlement finalization — who gets paid, and how
much, is an owner decision (see below).

**Auditability (`joulex/audit.py`).** `verify_settlement(id)`
returns `{settlement_id, found, receipt, ledger_entry,
ledger_entry_hash_ok, local_message, chain, mirror, attestation,
verdict}` with verdict `VERIFIED` / `PARTIAL` / `NOT_FOUND` /
`FAILED`: receipt found, ledger hash recomputes, log chain
intact, attestation absent-or-verified, and the anchor confirmed
(mirror message hash matches, or local-only with the local
message present). A mirror hash mismatch, a payload that
disagrees with the receipt, or an invalid attestation is FAILED;
an unverifiable attestation or an unreachable mirror is PARTIAL,
never claimed as verified. `GET /audit/<settlement_id>` exposes
the same check over HTTP (404 `{"error": "AUDIT_NOT_FOUND"}` for
an unknown id), so a third party with only the settlement id +
topic id can verify without trusting the server: fetch the
message from the public mirror node, decode the base64 payload,
check its hash against the local `message_hash` and its payload
against the receipt, and recompute the ledger hash from
`settlements.jsonl`. `POST /audit/checkpoint` (operator key when
configured) appends a `log_checkpoint` message recording the log
head (`head_sequence`, `head_message_hash`, `log_hash`);
`verify_checkpoint` recomputes the head from `hcs_log.jsonl`.
`scripts/audit_bundle.py` generates a time-range bundle
(`bundle.json` + `manifest.json`, hash-chained) and verifies it;
`scripts/audit_checkpoint.py` is the CLI for checkpoints.
`GET /health` now also reports `hcs_anchor_queued` /
`hcs_anchor_anchored` / `hcs_anchor_dead` and the active
`settle_anchor_policy`.

**Live demo:** `python3 scripts/m4_demo.py` runs the full cycle
against a fake runner (no network, dummy key): settle ->
PENDING_ANCHOR -> drain -> finalized receipt -> VERIFIED (stub
mirror) -> `GET /audit/<id>` 200 -> bundle + checkpoint ->
simulated value-rail transfer. The live testnet cycle (same
steps, real runner) is blocked on the owner supplying the
operator key transiently — no receipt or verification has been
faked; the demo labels every simulated leg as such.

**Open decisions (owner's, not the builder's):** mainnet topic /
keys (never reuse the testnet topic); final anchoring policy
(flag implemented, fail-closed recommended for settlement);
AESO written permission; durable storage for `data/`; value-rail
wiring (recipient account + amount per settlement — currently a
manual, operator-invoked step); sustained live anchor
throughput measurement once the key is supplied.

## 8. Orchestrator simulation (M2)

`joulex/orchestrator.py` — deterministic, labelled simulation.
Inputs: (a) a price history (AESO pool prices — fetched via
`joulex/aeso.py` and cached, or the labelled synthetic series);
(b) a job set: deferrable GPU jobs, each with GPU-hours, deadline,
and earliest start. Allocation: assign each job to the cheapest
feasible hours before its deadline (cost = pool price × MW × hours,
with a per-job power draw). Baselines, all computed on the same
history and job set: **fixed scheduling** (run at arrival),
**power-price-only scheduling** (run at the cheapest hours,
ignoring deadlines/SLA), **optimisation baseline** (a simple
greedy that respects deadlines — the bar the orchestrator must
beat). Outputs: net operating-cost improvement vs each baseline,
SLA completion %, coordination cost (modelled: inference + clearing
overhead per decision, stated as an explicit parameter, default
$0.01/decision — the falsifier is sensitive to it, so it is a
parameter, never hidden), and sensitivity to forecast error
(re-run with perturbed prices). The V0 targets (§0) are the
acceptance criteria: ≥5% vs strong baselines, 100% SLA, 0
unauthorised transactions (N/A in simulation — recorded as such),
100% auditable decisions (every allocation decision is written to
the HCS log with its inputs). If the orchestrator cannot beat the
strong baselines net of costs, the report says so — the simulator
is the falsifier, not a marketing demo. Phase 1 §2.3 arithmetic
is the calibration: pure energy arbitrage on 1 MW is worth tens
of thousands per year, not hundreds of thousands; the simulator
must not produce a number that contradicts it without explaining
why.

## 9. Persistence

JSON files under `data/` (created on first run):

- `markets.json`, `ledger.json`, `trades.jsonl`, `settlements.jsonl`
  — same shapes as the agent exchange.
- `challenges.json` — persistent challenge store (same as M3).
- `hcs_log.jsonl` — append-only HCS-schema audit log (M1).
- `measurements.json` — pinned code-manifest measurements (M1).
- `aeso_prices.json` — cached AESO price history (M2).
- `provenance/` — in-toto link files (M1).

Writes are atomic-ish (temp file, fsync, os.replace). Corrupt
state refuses startup. The reconciler replays trades +
settlements; a second verifier (`joulex/hcs.py verify_log`)
replays the audit log.

## 10. API surface

Base: `http://127.0.0.1:8472` (default; `--port` overrides). JSON
everywhere. Reference: `docs/API.md` (M3). Same surface as the
agent exchange, plus JouleX rows:

| Method & path | Auth | Purpose |
|---|---|---|
| `GET /` | — | Service info: name, version, paper-only flag, server time |
| `GET /time` | — | Server time |
| `GET /markets` | — | List markets (all statuses) |
| `POST /markets` | `X-CF-KYA` or `X-Operator-Key` | Create market, admitted (CF-KYA L3 or operator key) |
| `GET /markets/<id>` | — | One market |
| `GET /challenge` | — | Fresh challenge + server time + TTL |
| `POST /orders` | `X-CF-KYA` | Place order (admission, then LMSR + balance) |
| `GET /positions?agent_id=` | — | Agent balance + positions |
| `GET /ledger` | — | All balances, pool, conservation check |
| `GET /reconcile` | — | Reconciliation report |
| `POST /markets/<id>/settle` | `X-Operator-Key` (when configured) | Settle — authenticated (`aeso_price`: oracle or operator value; `public_source`: outcome + evidence + attestation). Under RealHCS + fail-closed returns 202 `PENDING_ANCHOR` until the anchor receipt lands; idempotent re-settle returns the same `settlement_id` |
| `GET /audit/<settlement_id>` | — | Third-party settlement verification: receipt, ledger entry + hash check, local message, hash-chain proof, mirror verification, attestation verification, verdict `VERIFIED` / `PARTIAL` / `NOT_FOUND` / `FAILED` |
| `POST /audit/checkpoint` | `X-Operator-Key` (when configured) | Append a `log_checkpoint` message recording the audit-log head |
| `GET /attestations` | — | List attestation envelopes (paginated, latest first) |
| `GET /hcs-log` | — | HCS audit log entries (paginated, `?agent_id=&from=&to=`) |
| `GET /hcs-log/verify` | — | Log integrity report (hash chain, sequence, monotonicity) |
| `GET /hcs-log/knowledge?agent_id=&at=` | — | What agent X knew at time T (replay) |
| `GET /hcs-log/provenance?from=&to=` | — | Who told whom (ordered, with timestamps) |
| `GET /aeso/price` | — | Latest AESO pool price + source + fetched_at |
| `POST /accounts` | — | Register a human account (M1.4) |
| `POST /accounts/login` | — | Log in: session token + expires_at + account (M1.4) |
| `GET /accounts/me` | `Authorization: Bearer <token>` | The calling account's public view (M1.4) |
| `POST /accounts/logout` | `Authorization: Bearer <token>` | Revoke the session, idempotent (M1.4) |
| `POST /accounts/link-agent` | `Authorization: Bearer <token>` | Bind a CF-KYA L3 agent (M1.4) |
| `POST /accounts/<id>/disable` | `Authorization: Bearer <token>` (admin) | Disable an account, revoke sessions (M1.4) |
| `GET /app` | — | Server-served web UI shell (M1.5) |
| `GET /app/app.js` | — | Web UI client: talks to this API, signs CF-KYA presentations in the page (M1.5) |
| `GET /app/ed25519.js` | — | Web UI crypto: SHA-512 + Ed25519 signing, RFC 8032 (M1.5) |
| `GET /app/sha256.js` | — | Web UI crypto: JCS canonical JSON + SHA-256 (M1.5) |
| `POST /__demo/set_time` | — | Demo-clock servers only |

Error shape: `{"error": "<code>", "reason": "<detail>"}`.
Admission failures 401 with the kya reason verbatim.
Attestation failures: `JOULEX_VC_REQUIRED` / `JOULEX_VC_INVALID` /
`JOULEX_VC_MEASUREMENT` / `JOULEX_VC_BINDING` / `JOULEX_VC_EXPIRED`
(401 when the policy requires attestation and it fails; advisory
failures are also 401 — an invalid attestation is never ignored).
HCS log failures: 503 with `HCS_LOG_UNAVAILABLE` if the log cannot
be appended (fail closed — an action that cannot be audited is not
executed).

## 11. Failure modes and fail-closed rules

Same table as the agent-exchange DESIGN.md §11, plus JouleX rows:

| Failure | Behaviour |
|---|---|
| AESO unreachable at settlement (`aeso_price`, oracle on) | 503, market stays OPEN, no outcome guessed |
| Settle with missing/wrong operator key (key configured), or any key when none is configured | 401 `OPERATOR_KEY_INVALID`, market stays OPEN, no state change |
| `public_source` settle without an attestation envelope | 401 `JOULEX_VC_REQUIRED`, market stays OPEN |
| AESO returns a series that does not cover the window | 503, market stays OPEN |
| Attestation required by policy, envelope absent | 401 `JOULEX_VC_REQUIRED` |
| Envelope present, kind not implemented | 401 `JOULEX_VC_INVALID` |
| Envelope measurement not in pinned set | 401 `JOULEX_VC_MEASUREMENT` |
| `report_data` binding mismatch (replay across actions) | 401 `JOULEX_VC_BINDING` |
| Envelope expired (>300s) | 401 `JOULEX_VC_EXPIRED` |
| HCS log append fails (disk / corrupt log) | 503 `HCS_LOG_UNAVAILABLE`, action NOT executed |
| Settlement envelope exceeds HCS message limit (~20 KiB) under RealHCS + fail-closed | 503 `HCS_ANCHOR_FAILED`, market stays OPEN, nothing logged, no pending record |
| Anchor job dies (retries exhausted) under fail-closed | Pending record `ANCHOR_FAILED`, market stays `SETTLEMENT_PENDING`; fresh settle supersedes with a new `settlement_id` |
| Re-settle while a settlement is pending | Same `settlement_id` returned, no second HCS message, no second anchor job |
| RealHCS submit fails transiently (network) | Retried (2 retries, 1s/2s backoff); queue retries + dead-letter after exhaustion; local record never lost |
| HCS log fails integrity check at startup | Server refuses to start |
| Orchestrator: no price history available (no cache, no network) | Simulation refuses to run; synthetic fallback only when explicitly requested with `--synthetic`, and every output labelled SYNTHETIC |

## 12. What is simulated vs real

**Real:** Ed25519 signatures and verification (RFC 8032); CF-KYA
credential issuance, delegation, revocation, presentation binding,
challenge replay defence; LMSR pricing math; ledger conservation;
JSON persistence; the HTTP API; the attestation envelope binding
(SHA-256 over canonical bytes); the HCS hash chain and query
functions (replay is real logic over a real log); fail-closed
settlement (two-phase PENDING_ANCHOR, receipt sidecar,
decision-basis inputs hash) and third-party settlement
verification (`verify_settlement`, `GET /audit/<id>`) — all real
logic, exercised end to end with a fake Hedera runner and
verified against the live testnet mirror node for the one real
anchored message.

**Simulated / paper:** the money (paper points); the demo
issuer's L3 attestation (asserted, not verified); the demo clock;
the operator-supplied settlement values (when the oracle is off);
the **software attestation envelope** (proves code-tree identity,
not hardware execution); the **local HCS log** when running in
local mode (HCS-schema, local timestamps — real Hedera consensus
timestamps only in hedera mode, on the testnet topic since
2026-11-10); the orchestrator (a simulation on price
histories, labelled as such); the **M4 demo's Hedera leg** (fake
runner + dummy key — the live testnet settlement cycle is still
owed and needs the owner-supplied operator key).

**Not built:** x402/USDC settlement (inert stub only), CLOB
matching, hardware TEE attestation,
GPU-price oracle, forecasting models, mainnet Hedera (the testnet
value-rail leg exists as a prototype in `joulex/transfer.py` +
`joulex/hcs_transfer.mjs`; mainnet is owner-decided and not part
of any build).

## 13. Files

```
joulex/
├── DESIGN.md            this file
├── README.md            how to run / test / demo
├── MILESTONES.md        milestone queue + quality bar + loop contract
├── BUILD_LOG.md         dated build entries
├── VENDOR.md            provenance of copied code (agent-exchange core,
│                        CF-KYA snapshot) — source, date, SHA-256
├── docs/                QUICKSTART.md, API.md, ADMISSION.md, HEDERA.md
├── quickstart.py        runnable first-trade script
├── demo.py              scripted end-to-end demo (two agents, one market,
│                        attestation + HCS log shown)
├── joulex/
│   ├── __init__.py
│   ├── vendor/          CF-KYA snapshot (unmodified, hashes in VENDOR.md)
│   ├── lmsr.py          LMSR cost / price math (ported)
│   ├── markets.py       market record + creation validation + resolution
│   ├── ledger.py        balances, positions, pool, conservation (ported)
│   ├── admission.py     challenge issuance + CF-KYA verify wrapper (ported)
│   ├── demo_issuer.py   demo L3 issuer (ported, test principals only)
│   ├── settlement.py    settlement + SettlementHook interface (ported)
│   ├── reconcile.py     reconciliation (ported)
│   ├── store.py         JSON persistence (ported)
│   ├── exchange.py      Exchange: service core (ported + JouleX types)
│   ├── client.py        agent-side helper (ported)
│   ├── server.py        stdlib http.server JSON API (ported + new routes)
│   ├── aeso.py          AESO pool-price feed (NEW)
│   ├── aeso_synthetic.py labelled synthetic price series (NEW)
│   ├── attestation.py   envelope schema, measurement, binding, verify (NEW, M1)
│   ├── hcs.py           HCS-schema audit log + queries + verify (NEW, M1)
│   ├── hcs_submit.mjs   Node submit helper (RealHCS transport, M1)
│   ├── hcs_transfer.mjs Node HBAR transfer helper, testnet-only (NEW, M4)
│   ├── provenance.py    in-toto link generation (NEW, M1)
│   ├── orchestrator.py  workload-allocation simulator (NEW, M2)
│   ├── anchor_queue.py  durable async anchor queue + worker (NEW, M4)
│   ├── decision_basis.py decision-basis capture + inputs hash (NEW, M4)
│   ├── settlement_receipts.py settlement receipt sidecar (NEW, M4)
│   ├── audit.py         third-party settlement verification, checkpoints (NEW, M4)
│   ├── transfer.py      testnet HBAR value rail, record + link (NEW, M4)
│   ├── accounts.py      human accounts: scrypt passwords, sessions, roles,
│   │                    CF-KYA link (NEW, M1.4)
│   └── errors.py        shared ExchangeError (NEW, M1.4 — avoids a
│                        accounts ↔ exchange circular import)
├── scripts/
│   ├── audit_bundle.py  time-range audit bundles (NEW, M4)
│   ├── audit_checkpoint.py checkpoint CLI (NEW, M4)
│   └── m4_demo.py       M4 full-cycle demo, fake runner (NEW, M4)
├── tests/               pytest, stdlib-only
└── data/                runtime state (created on first run)
```

## 14. Human accounts (M1.4)

A stranger becomes a user by registering a human account, logging in,
and linking the CF-KYA L3 agent credential that trades for them. The
account itself never trades — every order still passes CF-KYA L3
admission (§4); the account is the human identity an agent's orders
attribute to.

- **Module:** `joulex/accounts.py` (`AccountStore` — persistence +
  password/session logic) with the Exchange owning the HCS audit
  appends and the verifier calls. `ExchangeError` lives in
  `joulex/errors.py` so the store can raise it without a circular
  import.
- **Passwords:** scrypt (n=2^14, r=8, p=1, dklen=32), fresh 32-byte
  salt per account, 12–256 characters (upper bound added on Groq
  review 2026-11-14: unbounded input burns scrypt CPU per login).
  Only salt + hash are stored; verification is `hmac.compare_digest`.
  Unknown-email logins run a dummy hash first, so login timing does
  not enumerate users; wrong password and unknown email return the
  identical 401. No pepper: this loop holds no server secrets, and
  per-account salt + scrypt is the bar for a paper-only demo.
- **Sessions:** opaque `token_urlsafe(32)` bearer tokens, stored only
  as SHA-256 hashes, 24 h fixed expiry (no sliding renewal), logout
  revokes, expiry enforced on every authentication. Bearer tokens are
  replayable if stolen until expiry — accepted as standard bearer
  semantics for v1; no IP/User-Agent binding (clients change
  addresses; the API is not a browser app).
- **Roles:** buyer / generator / provider / operator / admin. Only
  enforcement in v1: disable requires an admin caller. The first
  account on a fresh deployment may be `admin` (bootstrap).
- **Link:** `POST /accounts/link-agent` takes the session plus a
  fresh challenge-bound presentation over
  `link_payload {type: "joulex.account_link", account_id, agent_id}`,
  verified by `ExchangeVerifier.verify_link` exactly like an order
  (L3 only, trusted issuer, not revoked/expired, credential covering
  `joulex.place_order`, single-use 300 s challenge — replay is a
  401). A bare credential never links. One credential links to at
  most one account (409 otherwise), so attribution is unambiguous.
- **Disable:** admin-only, one-way in v1 (no enable endpoint;
  re-enable is an operator edit of `data/accounts.json`). Disabling
  revokes all the account's sessions immediately.
- **Audit:** register / link / disable are HCS events
  (`account_register`, `account_link_agent`, `account_disable`)
  appended before the state change — a log failure is a 503 and
  nothing executes. Logins/logouts are not log events (session
  noise, no money moves).
- **Error semantics** (see `docs/API.md` Errors table):
  `ACCOUNT_EXISTS` 409, `ACCOUNT_VALIDATION` / `ACCOUNT_ROLE_INVALID`
  400, `ACCOUNT_INVALID_CREDENTIALS` 401, `ACCOUNT_UNAUTHORIZED` /
  `SESSION_EXPIRED` 401, `ACCOUNT_DISABLED` / `ACCOUNT_FORBIDDEN`
  403, `ACCOUNT_NOT_FOUND` 404, `ACCOUNT_AGENT_ALREADY_LINKED` 409.
