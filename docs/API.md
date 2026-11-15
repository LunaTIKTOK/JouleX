# JouleX API reference (v1)

Base URL: `http://127.0.0.1:8472` by default (`--port` overrides).
JSON everywhere. Every response below was verified against a live
server on 2026-11-10 (probe: real server, demo clock, demo-issuer
trust store, pinned measurements loaded) — nothing in this file is
documented from memory alone. Values shown (ids, hashes, prices) are
examples from that probe run, not live data.

Paper only. No route moves real money, touches Hedera, or needs a
hardware TEE. The attestation envelope is a **software** attestation;
the HCS log is a **local** log (see `HEDERA.md`).

## Conventions

- **Error shape (real).** Every refusal is
  `{"error": "exchange_error", "reason": "<CODE>..."}` with the HTTP
  status on the wire. The `reason` carries the machine-readable code
  (and sometimes a detail after `": "`); `error` is always the literal
  string `"exchange_error"` except for an unexpected 500, which is
  `{"error": "internal", "reason": "<message>"}`. (DESIGN.md §10
  describes the shape as `{"error": "<code>", ...}` — the code as
  implemented puts the code in `reason`. See "Deviations" below.)
- **Timestamps.** ISO-8601 with timezone everywhere
  (`2026-11-10T07:19:03.526599+00:00`). In **query strings** the `+`
  in `+00:00` must be percent-encoded (`%2B`): a bare `+` is decoded
  as a space and the timestamp is rejected as invalid.
- **IDs.** Markets are `mkt_<12 hex>`; trades are `trd_<12 hex>`.
- **Auth headers.** `X-CF-KYA`: a CF-KYA presentation (see
  `ADMISSION.md`). `X-Operator-Key`: the operator key configured at
  startup (`--operator-key` / `JOULEX_OPERATOR_KEY`); when present it
  is authoritative for market creation and the KYA header is not even
  parsed. No other route takes an operator key.

## Routes

| Method & path | Auth | Purpose |
|---|---|---|
| `GET /` | — | Service info: name, version, paper-only flag, server time |
| `GET /` | — | Service info (incl. live `verifiable_compute.hcs_mode`) |
| `GET /health` | — | Liveness + cheap readiness: HCS mode/topic, markets, HCS message/submitted/failed counts, anchor-queue counts (`hcs_anchor_queued` / `hcs_anchor_anchored` / `hcs_anchor_dead`) + active `settle_anchor_policy`, uptime (audit 2026-11-12; anchor fields M4) |
| `GET /metrics` | — | Process-start counters (orders, markets created, settlements) + HCS stats (audit 2026-11-12) |
| `GET /time` | — | Server time |
| `GET /markets` | — | List markets (all statuses) |
| `POST /markets` | `X-CF-KYA` or `X-Operator-Key` | Create market, admitted (CF-KYA L3 or operator key) |
| `GET /markets/<id>` | — | One market |
| `GET /challenge` | — | Fresh challenge + server time + TTL |
| `POST /orders` | `X-CF-KYA` | Place order (admission, then LMSR + balance) |
| `GET /positions?agent_id=` | — | Agent balance + positions |
| `GET /ledger` | — | All balances, pool, conservation check |
| `GET /reconcile` | — | Reconciliation report |
| `POST /markets/<id>/settle` | `X-Operator-Key` (when configured) | Settle — authenticated (`aeso_price`: oracle or operator value; `public_source`: outcome + evidence + attestation). Under RealHCS + fail-closed returns 202 `PENDING_ANCHOR` until the anchor receipt lands; re-settle while pending returns the same `settlement_id` (M4) |
| `GET /audit/<settlement_id>` | — | Third-party settlement verification (M4): receipt, ledger entry + hash check, local message, hash-chain proof, mirror verification, attestation verification, verdict `VERIFIED` / `PARTIAL` / `NOT_FOUND` / `FAILED`; unknown id is 404 `AUDIT_NOT_FOUND` |
| `POST /audit/checkpoint` | `X-Operator-Key` (when configured) | Append a `log_checkpoint` message recording the audit-log head (M4) |
| `GET /attestations` | — | Attestation envelopes (paginated, latest first) |
| `GET /hcs-log` | — | HCS audit-log entries (paginated, latest first, `?agent_id=&from=&to=`) |
| `GET /hcs-log/verify` | — | Log integrity report (hash chain, sequence, monotonicity) |
| `GET /hcs-log/knowledge?agent_id=&at=` | — | What agent X knew at time T (replay) |
| `GET /hcs-log/provenance?from_agent=&to_agent=` | — | Who told whom (ordered, with timestamps) |
| `GET /aeso/price` | — | Latest AESO pool price + source + fetched_at |
| `POST /revocation-lists` | — | Register a signed revocation list (operator) |
| `POST /__demo/set_time` | — | Demo-clock servers only |
| `GET /__demo/agents` | — | Demo fixtures only (demo-clock servers only) |
| `POST /accounts` | — | Register a human account (M1.4): 201 + public view |
| `POST /accounts/login` | — | Log in (M1.4): 200 + session token + expires_at + account |
| `GET /accounts/me` | `Authorization: Bearer <token>` | The calling account's public view (M1.4) |
| `POST /accounts/logout` | `Authorization: Bearer <token>` | Revoke the session (M1.4, idempotent 200) |
| `POST /accounts/link-agent` | `Authorization: Bearer <token>` | Bind a CF-KYA L3 agent to the calling account (M1.4) |
| `POST /accounts/<id>/disable` | `Authorization: Bearer <token>` (admin) | Disable an account + revoke its sessions (M1.4, one-way) |
| `GET /app` | — | Server-served web UI shell (M1.5): HTML, `text/html` |
| `GET /app/app.js` | — | Web UI client (M1.5): talks to this API, signs CF-KYA presentations in the page |
| `GET /app/ed25519.js` | — | Web UI crypto (M1.5): SHA-512 + Ed25519 signing, RFC 8032, no dependencies |
| `GET /app/sha256.js` | — | Web UI crypto (M1.5): JCS canonical JSON + SHA-256 (copy of `sandbox/sha256.js`) |

### `GET /app` — the web UI (M1.5)

A stranger opens `http://127.0.0.1:8472/app` and gets a working
app, not JSON. The shell (`index.html`, served from
`joulex/webui/`) plus `app.js` talk to this API only: health,
AESO price, markets list, account register/login, positions,
ledger, HCS audit log + verify.

First paper trade in the browser, no hand-holding:

1. Register an account (or log in) — `POST /accounts`,
   `POST /accounts/login`.
2. Load a demo agent — `GET /__demo/agents` (demo-clock servers
   only; the seed is held in page memory only, never stored, never
   sent anywhere — it is only the input to the in-page Ed25519
   signer). Optionally link it to the account
   (`POST /accounts/link-agent`, presentation signed in the page
   over the link payload).
3. Pick a market, side, shares — the page fetches a fresh
   challenge, signs the order presentation in JS (`ed25519.js`:
   SHA-512 + Ed25519, RFC 8032, verified against the RFC vectors
   and against Python's `ed25519`; `sha256.js`: JCS canonical JSON
   + SHA-256, byte-identical to the server's hashing), and posts
   it with `X-CF-KYA`. The server verifies it exactly like a
   Python client's order — **no admission bypass in the UI**.

Static serving is allowlisted to the four files above; anything
else under `/app/` (including `..` segments) is a 404, never a
read outside `joulex/webui/`. `GET /` stays the JSON service
info — the UI does not shadow the API contract. The UI is
paper-only throughout (the banner says so); it cannot settle,
cannot touch Hedera, cannot spend real money.

### `GET /`

```json
{
  "aeso_oracle": false, "demo_clock": true, "markets": 0,
  "name": "joulex", "operator_key_configured": true,
  "paper_only": true,
  "server_time": "2026-11-10T07:19:03.526599+00:00",
  "verifiable_compute": {
    "envelope_version": "joulex-attest-0.1", "hcs_mode": "local",
    "hcs_topic": null, "hcs_network": null, "kind": "software"
  },
  "version": "0.1.0"
}
```

`paper_only` is always `true` in v1. `hcs_mode` is the LIVE mode
(audit 2026-11-12: it used to hardcode `"local"` even when
anchoring to a real topic): `"local"` with `hcs_topic`/`hcs_network`
`null` until the owner completes the Hedera steps in `HEDERA.md`,
`"hedera"` with the real topic + network once live.

### `GET /time`

Server time (ISO-8601).

```json
{"server_time": "2026-11-10T07:19:03.526599+00:00"}
```

### `GET /health` (audit 2026-11-12)

Liveness + cheap readiness. Always 200 while the process can
answer; the deep log-integrity check stays on
`GET /hcs-log/verify` (it re-walks the whole log).

```json
{
  "status": "ok", "server_time": "2026-11-12T14:00:00+00:00",
  "hcs_mode": "local", "hcs_topic": null, "hcs_network": null,
  "markets": 0, "hcs_messages": 0, "hcs_submitted": 0,
  "hcs_failed_submits": 0,
  "hcs_anchor_queued": 0, "hcs_anchor_anchored": 0, "hcs_anchor_dead": 0,
  "settle_anchor_policy": "fail_closed",
  "uptime_seconds": 123.456
}
```

`hcs_failed_submits` > 0 means anchoring is failing — read
`data/hcs_dead_letter.jsonl` and `GET /metrics`.

### `GET /audit/<settlement_id>` (M4)

Third-party settlement verification — no server trust needed. The
response carries the receipt, the `settlements.jsonl` entry it
hashes, whether the ledger hash recomputes, the local HCS
message, the hash-chain check, the mirror-node check, the
attestation check, and a verdict: `VERIFIED` / `PARTIAL` /
`NOT_FOUND` / `FAILED`. Unknown id is 404
`{"error": "AUDIT_NOT_FOUND"}`. A stranger with only the
settlement id + topic id repeats the check by fetching the
message from the public mirror node
(`GET https://testnet.mirrornode.hedera.com/api/v1/topics/<topic>/messages/<topic_sequence>`),
decoding the base64 payload, comparing its hash to the local
`message_hash` and its payload to the receipt, and recomputing
the ledger hash from `settlements.jsonl`. `scripts/m4_demo.py`
runs the full cycle end to end (fake runner; the live testnet
cycle needs the owner-supplied operator key).

### `POST /audit/checkpoint` (M4)

Operator key when configured. Appends one `log_checkpoint`
message recording the audit-log head (`head_sequence`,
`head_message_hash`, `log_hash`) so a later auditor can prove
the log has not been rewritten after the checkpoint.
`verify_checkpoint` in `joulex/audit.py` recomputes the head
from `hcs_log.jsonl`.

### `GET /metrics` (audit 2026-11-12)

Process-start counters (in-memory; the durable record is the HCS
log + `trades.jsonl` / `settlements.jsonl`).

```json
{
  "orders": 0, "markets_created": 0, "settlements": 0,
  "uptime_seconds": 123.456,
  "hcs": {"messages": 0, "submitted": 0, "failed": 0}
}
```

### `GET /markets` / `GET /markets/<id>`

`GET /markets` returns a JSON array of market records (all statuses,
creation order). `GET /markets/<id>` returns one record; unknown id
is 404 `MARKET_NOT_FOUND`. A market record (binary `aeso_price`
example, as created):

```json
{
  "market_id": "mkt_6fc2449f91d0",
  "question": "Will the Alberta pool price exceed $50/MWh during the delivery window?",
  "resolution_type": "aeso_price", "contract_type": "flex_capacity",
  "strike": 50.0,
  "window_start": "2026-11-11T07:19:03.526599+00:00",
  "window_end": "2026-11-11T13:19:03.526599+00:00",
  "resolution_source_url": "http://ets.aeso.ca/ets_web/ip/Market/Reports/CSMPriceReportServlet?contentType=csv",
  "resolution_source_description": "AESO System Marginal Price (minute), averaged to hourly pool price over [...); YES iff max hourly pool price > 50 $/MWh",
  "resolves_at": "2026-11-11T07:19:03.526599+00:00",
  "created_at": "2026-11-10T07:19:03.526599+00:00",
  "b": 100.0, "outcomes": ["YES", "NO"], "q": [0.0, 0.0],
  "prices": {"YES": 0.5, "NO": 0.5},
  "q_yes": 0.0, "q_no": 0.0, "price_yes": 0.5,
  "status": "OPEN", "outcome": null, "observed_value": null,
  "evidence": null, "settled_at": null,
  "created_by": "agent-probe-alice", "attestation": null
}
```

Binary markets also carry the legacy mirrors `q_yes` / `q_no` /
`price_yes`. After settlement, `status` is `"RESOLVED"` and
`outcome`, `observed_value`, `evidence`, `settled_at` are filled.
`attestation` is the envelope carried on the creation presentation,
or `null`.

### `GET /challenge`

```json
{
  "challenge": "1f6779950f55a92744bfebdc01d179e9",
  "server_time": "2026-11-10T07:19:03.526599+00:00",
  "ttl_seconds": 300
}
```

One challenge, one use, 300 s TTL. A failed verification never burns
a challenge (only a successful verification marks it seen), so an
agent can retry with a corrected presentation. Challenges persist
across restarts (`data/challenges.json`).

### `POST /markets`

Creates a market. Two auth paths (see `ADMISSION.md` for the full
flow):

- **Operator key:** header `X-Operator-Key` matching the key
  configured at startup. Authoritative; the KYA header is ignored.
  A wrong key — or any key when none is configured — is 401
  `OPERATOR_KEY_INVALID`. The market is recorded as
  `"created_by": "operator"`.
- **CF-KYA:** header `X-CF-KYA` with a presentation bound to the
  creation payload. No header at all is 401
  `KYA_PRESENTATION_MISSING`.

Request body — common fields:

```json
{
  "question": "Will the Alberta pool price exceed $50/MWh during the delivery window?",
  "resolution_type": "aeso_price",
  "contract_type": "flex_capacity",
  "resolves_at": "2026-11-11T07:19:03.526599+00:00",
  "b": 100.0
}
```

| Field | Required | Notes |
|---|---|---|
| `question` | yes | Non-empty. |
| `resolution_type` | yes | `aeso_price` or `public_source`. |
| `contract_type` | no | `flex_capacity` / `compute_forward` / `generic` (default `generic`). Descriptive in v1; bound by the creation presentation. |
| `resolves_at` | yes | ISO-8601 with timezone, 1–30 days after creation. |
| `b` | no | LMSR liquidity parameter, > 0 (default 100). |
| `outcomes` | no | 2–10 labels (≤64 chars, no `\|`, distinct); default `["YES", "NO"]`. |
| `strike`, `window_start`, `window_end` | for `aeso_price` | `strike` in $/MWh; window is `[start, end)`, end after start. `aeso_price` is binary only. |
| `source_url`, `source_description` | for `public_source` | Public source + one-line resolution rule. |

On success: **201** with the full market record. On a bad field:
**400** `INVALID_MARKET: <detail>` (e.g. `INVALID_MARKET: strike
must be a number`). Ordering: operator-key check first, then missing
presentation (401), then field validation (400), then KYA
verification (401) — a 400 never burns a challenge and a 401 never
creates a market. The body must also carry `agent_id` on the KYA
path (400 `INVALID_MARKET: agent_id required` if missing).

### `POST /orders`

Body (the bound payload — exactly these fields, no more, no less):

```json
{
  "agent_id": "agent-probe-alice", "market_id": "mkt_6fc2449f91d0",
  "side": "YES", "shares": 10, "action": "buy"
}
```

`action` is `buy` (default if omitted) or `sell`; `shares` > 0;
`side` must be one of the market's outcomes. The `X-CF-KYA` header
carries the presentation bound to this payload (challenge +
payload hash + timestamp). An optional `attestation` field on the
presentation carries a software attestation envelope — see
"Attestation" below.

Checks run in order; the first failure wins and nothing changes:
payload shape (400 `INVALID_ORDER`) → market exists (404
`MARKET_NOT_FOUND`) → side is an outcome (400 `INVALID_ORDER`) →
market open and before `resolves_at` (409 `MARKET_RESOLVED` /
`MARKET_CLOSED`) → CF-KYA admission (401, kya reason verbatim) →
sell position (400 `INSUFFICIENT_SHARES`) → balance (403
`INSUFFICIENT_POINTS`). A brand-new agent's effective balance is
the 1,000-point endowment; the account is only created at execution,
so a rejected order leaves no trace.

Success: **200** with the trade receipt:

```json
{
  "trade_id": "trd_731da67b9354",
  "timestamp": "2026-11-10T07:19:03.526599+00:00",
  "market_id": "mkt_6fc2449f91d0", "agent_id": "agent-probe-alice",
  "principal_id": "principal-probe-alice",
  "credential_id": "cred-agent-probe-alice-349c95c6",
  "side": "YES", "action": "buy", "shares": 10.0,
  "cost_points": 5.124948,
  "prices_after": {"YES": 0.52497918747894, "NO": 0.47502081252106},
  "price_yes_after": 0.52497918747894,
  "balance_after": 994.875052,
  "payload_hash": "94449ec371d3b9a86cd2ea8e31f572aa8999e8a297c78d9cd831e32e4c25d4de",
  "attestation": { "...envelope..." },
  "attestation_status": "verified"
}
```

`price_yes_after` is present on binary markets only.
`attestation_status` is `"verified"` when an envelope was presented
and verified, `"absent"` otherwise.

### `GET /positions?agent_id=<id>`

```json
{
  "agent_id": "agent-probe-alice", "balance_points": 989.500831,
  "positions": [
    {"market_id": "mkt_6fc2449f91d0", "shares": 20.0, "side": "YES"}
  ]
}
```

A fresh agent (never traded) reads `balance_points: 0.0` with no
positions — the 1,000-point endowment lands on the first verified
order, not before. Missing `agent_id` is 400
`INVALID_REQUEST: agent_id required`.

### `GET /ledger`

```json
{
  "balances": {"agent-probe-alice": 989.500831},
  "pool": 10.499169,
  "conservation": {
    "agents": 1, "expected": 1000.0, "ok": true,
    "pool": 10.499169, "total": 1000.0
  }
}
```

Conservation invariant: Σ agent balances + pool == Σ initial
endowments (1,000 per agent that has traded). `ok: false` means the
ledger does not reconcile — stop and investigate, do not trade on.

### `GET /reconcile`

Re-derives every balance from `trades.jsonl` + `settlements.jsonl`
and compares with the stored ledger/markets. Always 200; the
report's `ok` flag is the verdict:

```json
{
  "checked_at": "2026-11-10T07:19:04.737499+00:00",
  "conservation": {"agents": 1, "expected": 1000.0, "ok": true,
                   "pool": 10.499169, "total": 1000.0},
  "counts": {"agents": 1, "markets": 2, "settlements": 0, "trades": 2},
  "mismatches": [], "ok": true
}
```

### `POST /markets/<id>/settle`

Authenticated (settlement auth added 2026-11-11; before that this
route checked nothing). Two gates, both fail closed — a refusal
settles nothing and the market stays OPEN:

- **Operator key.** When the server is configured with an operator
  key (`--operator-key` / `JOULEX_OPERATOR_KEY`), the
  `X-Operator-Key` header is REQUIRED and must match (constant-time
  compare, same semantics as market creation). Missing or wrong key
  is **401** `OPERATOR_KEY_INVALID`; so is any key presented when
  none is configured. When no key is configured and none is
  presented, this gate is open — the attestation gate below still
  applies.
- **Attestation (public_source).** A `public_source` settlement
  asserts an off-exchange fact, so it REQUIRES a valid attestation
  envelope (see below): absent is **401** `JOULEX_VC_REQUIRED`,
  regardless of the operator key.

Body depends on resolution type:

- `aeso_price`, oracle **off** (default): `{"observed_value": 62.5}`
  — the observed MAX hourly pool price over the window. YES iff max
  > strike; a tie resolves NO.
- `aeso_price`, oracle **on** (`--aeso-oracle`): the server fetches
  the series itself; a supplied `observed_value` is ignored. If AESO
  is unreachable or the series does not cover the window: **503**
  (`AESO_NO_DATA` / `AESO_UNAVAILABLE` / `AESO_TZ_NAME` — see
  "Errors"), market stays OPEN, no outcome guessed.
- `public_source`: `{"outcome": "YES", "evidence": "<url or text>"}`.
  `outcome` must be one of the market's outcomes; `evidence` is
  required (400 `INVALID_SETTLEMENT` without it).

Body fields `attestation` + `attestation_binding`
(`{"credential_id": ..., "challenge_nonce": ...}`): a settlement
attestation envelope, verified against the settlement payload hash.
**Required for `public_source`** (absent is **401**
`JOULEX_VC_REQUIRED`); optional for `aeso_price` (absent means
plain settlement — the outcome there comes from the oracle series
or the key-gated operator value). Present+valid is recorded on the
receipt (`"attestation_status": "verified"`); present+invalid is
**401** (`JOULEX_VC_*`) on every type, and the market stays OPEN.

Settling before `resolves_at` is 409 `MARKET_NOT_YET_RESOLVABLE`;
settling twice is 409 `MARKET_RESOLVED`; unknown market is 404
`MARKET_NOT_FOUND`. Success: **200** with the settlement receipt:

```json
{
  "market_id": "mkt_6fc2449f91d0",
  "question": "Will the Alberta pool price exceed $50/MWh during the delivery window?",
  "resolution_type": "aeso_price",
  "resolution_source_url": "http://ets.aeso.ca/ets_web/ip/Market/Reports/CSMPriceReportServlet?contentType=csv",
  "outcome": "YES", "observed_value": 62.5, "evidence": null,
  "settled_at": "2026-11-11T07:20:03.526599+00:00",
  "payouts": [
    {"agent_id": "agent-probe-alice", "market_id": "mkt_6fc2449f91d0",
     "payout_points": 20.0, "shares": 20.0, "side": "YES"}
  ],
  "attestation": null, "attestation_status": "absent"
}
```

Winners are paid 1 paper point per winning share; losers get 0.
When the oracle settles, the receipt also carries
`oracle_source_url`.

### `GET /attestations`

Attestation envelopes carried on HCS log messages, latest first.
Paginated: `?limit=` (default 50, clamped to 1–200) and `?offset=`
(default 0). Only actions that carried an envelope appear — and
that includes **rejected** envelopes: a failed attestation check is
logged as an `attestation_verification` message, so the envelope is
visible here as evidence of attack. Entry shape:

```json
{
  "attestations": [
    {
      "sequence": 8,
      "consensus_timestamp": "2026-11-10T07:19:03.526599000Z",
      "sender": "agent-probe-alice", "action": "attestation_verification",
      "attestation": { "version": "joulex-attest-0.1", "kind": "sev-snp", "...": "..." }
    }
  ],
  "total": 5, "limit": 50, "offset": 0
}
```

### `GET /hcs-log`

HCS-schema audit-log messages, latest first. Same pagination
(`?limit=`, `?offset=`). Filters: `?agent_id=` (messages the agent
sent), `?from=` / `?to=` (consensus-timestamp bounds, inclusive,
ISO-8601 — remember to percent-encode the `+`). Message shape is
the DESIGN.md §7.3 schema: `topic`, `sequence`,
`consensus_timestamp` (HCS nanosecond format, **LOCAL** — see
`HEDERA.md`), `sender`, `action`, `payload_hash`, `payload`,
`attestation`, `provenance` (`told_by`, `known_facts`), `prev_hash`,
`message_hash`. Response: `{"messages": [...], "total": N,
"limit": 50, "offset": 0}`. The log is append-only: there is no
edit, delete, or rewrite route.

### `GET /hcs-log/verify`

Re-walks the log from disk (header, sequence continuity, hash-chain
integrity, payload-hash integrity, timestamp monotonicity):

```json
{"errors": [], "messages": 9, "ok": true}
```

Any break is a hard failure (`ok: false` with the errors listed) —
reported, never silently repaired. A corrupt log refuses server
startup.

### `GET /hcs-log/knowledge?agent_id=<id>&at=<ISO-8601>`

Replay: what agent X knew at time T — the facts (payload hashes +
messages) where the agent was the sender or the `told_by` target, up
to `at` (inclusive). Both params required (400 `INVALID_REQUEST`
otherwise); an unparseable `at` is 400. Response:

```json
{
  "agent_id": "agent-probe-alice",
  "as_of": "2026-11-10T07:19:03.526599000Z",
  "known_facts": ["48f0e29a...", "94449ec3...", "..."],
  "messages": [ "...log messages..." ]
}
```

### `GET /hcs-log/provenance?from_agent=<A>&to_agent=<B>`

All messages where A told B — B is the acting agent (sender) and
the message's provenance names A as `told_by`. In order, with
timestamps. Optional `?t0=` / `?t1=` time window (inclusive).
Both `from_agent` and `to_agent` are required (400 otherwise).
Response is a JSON array of log messages (empty array when nothing
matches). (DESIGN.md §10 writes these params as `?from=&to=`; the
implemented names are `from_agent`/`to_agent` — see "Deviations".)

### `GET /aeso/price`

Latest AESO hourly pool price, live-fetched from the public ETS
report. Observed 2026-11-10:

```json
{
  "fetched_at": "2026-11-10T07:19:04.275997+00:00",
  "price": 17.055,
  "source_url": "http://ets.aeso.ca/ets_web/ip/Market/Reports/CSMPriceReportServlet?contentType=csv",
  "timestamp": "2026-11-10T01:00:00-06:00"
}
```

When AESO is unreachable: **503** with reason
`AESO_NO_DATA: ...` / `AESO_UNAVAILABLE: ...` / `AESO_TZ_NAME: ...`
— never a stale or guessed value. AESO data is non-commercial
research only (no commercial licence); see `README.md`.

### `POST /revocation-lists`

Register a signed revocation list (operator). Body is the list
itself (`{"issuer_id": ..., "revoked": [...], ...}` as signed by the
issuer). Success: 200 `{"ok": true, "reason": "OK"}`. A bad or stale
list is 400 with the kya reason (`KYA_REVOCATION_LIST_INVALID` /
`KYA_REVOCATION_LIST_STALE`); an issuer with no pinned key is 400
`EXCHANGE_ISSUER_UNKNOWN`. Only issuers already in the trust store
(key pinned at startup) can be registered this way — an L1
self-issuer's list cannot be registered over HTTP (see "Admission
failures" below).

### `POST /__demo/set_time`

Demo-clock servers only (`--demo-clock`). Sets server time; without
the flag the route does not exist (404 `NOT_FOUND`). Body:
`{"time": "<ISO-8601>"}`. Success: 200
`{"server_time": "2026-11-11T07:20:03.526599+00:00"}`. Bad/missing
time is 400 `INVALID_REQUEST: time required (ISO-8601)`.

### `GET /__demo/agents`

Demo-clock servers only. Returns the demo fixture credentials
written by `demo.py` (throwaway keys, labelled in the page) — or
404 `DEMO_AGENTS_NOT_FOUND` when none were written. Demo/test only;
never enable the demo clock on a shared server.

### Accounts (M1.4)

Human accounts are how a stranger becomes a user: register, log in,
then link the CF-KYA L3 agent credential that trades for them. The
account never trades by itself — every order still goes through
CF-KYA L3 admission; the account is the human identity the agent's
orders attribute to.

- **Roles:** `buyer`, `generator`, `provider`, `operator`, `admin`.
  The role is a plain field; the only enforcement in v1 is that
  `POST /accounts/<id>/disable` requires the caller to be `admin`.
  The first account on a fresh deployment may be `admin` (bootstrap);
  there is no separate admin-creation gate.
- **Passwords:** scrypt (n=2^14, r=8, p=1, dklen=32), fresh 32-byte
  salt per account, 12–256 characters. Only salt + hash are stored
  (`data/accounts.json`); no password ever appears in a response, a
  log line, or the HCS audit log. Unknown email costs the same scrypt
  as a real one (dummy hash), so login timing does not enumerate
  users; wrong password and unknown email return the identical
  401 `ACCOUNT_INVALID_CREDENTIALS`.
- **Sessions:** opaque `secrets.token_urlsafe(32)` bearer tokens,
  stored only as SHA-256 hashes (`data/sessions.json`), 24 h expiry.
  Send `Authorization: Bearer <token>`. Logout revokes the session
  (idempotent 200). An expired session is deleted on first use and
  answers 401 `SESSION_EXPIRED`.
- **Linking an agent:** `POST /accounts/link-agent` with the calling
  account's session plus a **fresh challenge-bound presentation**
  over the link payload `{type: "joulex.account_link", account_id,
  agent_id}` (`joulex/client.py: link_agent()` builds it). The
  verifier checks it exactly like an order — L3 only, trusted
  issuer, not revoked/expired, credential covering
  `joulex.place_order`, single-use challenge (300 s TTL, replay is a
  401). A bare credential never links: it proves nothing about key
  control. One credential links to at most one account, so an
  agent's orders attribute to exactly one human; a second link of
  the same credential is 409 `ACCOUNT_AGENT_ALREADY_LINKED`.
- **Disable:** admin-only, one-way in v1 (no enable endpoint — an
  operator edits `data/accounts.json` to re-enable). Disabling
  revokes all the account's sessions immediately.
- **Audit:** registration, agent links and disables are appended to
  the HCS log (`account_register`, `account_link_agent`,
  `account_disable`) BEFORE the state change — a log failure is a
  503 and nothing executes. Logins and logouts are not log events.

```bash
curl -X POST http://127.0.0.1:8472/accounts \
  -H 'Content-Type: application/json' \
  -d '{"email": "alice@example.com", "password": "correct-horse-battery-staple", "display_name": "Alice", "role": "buyer"}'
# 201 {"account_id": "acct_...", "email": "alice@example.com", "display_name": "Alice", "role": "buyer", "status": "active", "linked_agents": [], "created_at": "..."}

curl -X POST http://127.0.0.1:8472/accounts/login \
  -H 'Content-Type: application/json' \
  -d '{"email": "alice@example.com", "password": "correct-horse-battery-staple"}'
# 200 {"session_token": "...", "expires_at": "...", "account": {...}}

curl http://127.0.0.1:8472/accounts/me -H "Authorization: Bearer <token>"
# 200 {"account_id": "acct_...", ...}
```

## Attestation

The envelope (`joulex-attest-0.1`, DESIGN.md §7.1) rides on the
presentation as `presentation["attestation"]` for orders and market
creations, and in the settle body as `attestation` +
`attestation_binding` for settlements. `joulex/client.py` mints it
for you: `place_order(..., attested=True)`,
`create_market(..., attested=True)` and
`settle(..., attested=True)`.

```json
{
  "version": "joulex-attest-0.1", "kind": "software",
  "measurement": "ce69ac27567b8b7706463b3c3bacd3f22d2c1e048b201b8f698c2e14199f35aa",
  "report_data": "c237f95a05f78deb1687d46d50bfa506d205942908a016ba01934b89f7d56da2",
  "evidence": "eyJsaW5rX3NoYTI1NiI6bnVsbCwibWVhc3VyZW1lbnQiOiJjZTY5YWMyNyJ9",
  "issued_at": "2026-11-10T07:19:03.526599+00:00",
  "expires_at": "2026-11-10T07:24:03.526599+00:00"
}
```

- `measurement`: SHA-256 of the canonical code manifest (sorted
  `(path, sha256)` for every `.py` in `joulex/`). The pinned set
  lives in `data/measurements.json`; the current pinned measurement
  is `ce69ac27…35aa` (re-pinned 2026-11-10 after the M3 routes
  landed — any code change changes it).
- `report_data`: SHA-256 over the canonical bytes of
  `{credential_id, challenge_nonce, payload_hash}` for **this**
  action. A report captured from an earlier action cannot be
  stapled to a later one — the challenge nonce and payload hash
  differ.
- `expires_at − issued_at` ≤ 300 s.
- `kind` is `software` in v1. Hardware kinds (`sev-snp`, `tdx`,
  …) are reserved and rejected (`JOULEX_VC_KIND`) — the verifier
  implements software only.
- `evidence`: base64 in-toto link metadata for the build, or the
  base64 of the canonical envelope bytes (software-minted default).

**Policy.** A credential may carry `attestation_policy` inside its
signed content: `{"required": bool, "kinds": [...],
"measurements": [...], "max_age_seconds": 300}`. `required: true`
means every action under that credential must carry a valid
envelope; absence is `JOULEX_VC_REQUIRED`. Default policy is
advisory (`required: false`): absent → plain CF-KYA verification;
present+valid → recorded on the receipt; present+invalid → rejected
— an invalid attestation is evidence of attack, never ignored.

## Errors

| HTTP | `reason` | Meaning |
|---|---|---|
| 400 | `INVALID_JSON` / `INVALID_JSON: body must be an object` | Malformed JSON body. |
| 400 | `INVALID_REQUEST: ...` | Missing/invalid parameter (e.g. `agent_id` on `/positions`; `agent_id`+`at` on `/hcs-log/knowledge`; `from_agent`+`to_agent` on `/hcs-log/provenance`; `limit`/`offset` not integers; bad timestamp). |
| 400 | `INVALID_MARKET: ...` | Market creation validation failed. |
| 400 | `INVALID_ORDER: ...` | Order payload shape / side / shares invalid. |
| 400 | `INSUFFICIENT_SHARES` | Selling shares the agent does not hold. |
| 400 | `INVALID_SETTLEMENT: ...` | Settlement value/outcome/evidence invalid. |
| 400 | `KYA_REVOCATION_LIST_INVALID` / `KYA_REVOCATION_LIST_STALE` | A posted revocation list fails verification / is stale. |
| 400 | `EXCHANGE_ISSUER_UNKNOWN` | Revocation list for an issuer with no pinned key. |
| 401 | `KYA_PRESENTATION_MISSING` | No `X-CF-KYA` header (orders, or market creation). |
| 401 | `KYA_PRESENTATION_INVALID` | Header unparsable, or presentation/credential malformed. |
| 401 | `KYA_PRESENTATION_SIGNATURE_INVALID` | Presentation signature does not verify. |
| 401 | `KYA_PRESENTATION_STALE` / `KYA_PRESENTATION_NOT_YET_VALID` | Presentation timestamp outside its validity window. |
| 401 | `KYA_CHALLENGE_MISMATCH` / `KYA_CHALLENGE_REUSED` | Challenge does not match the presentation / already used. Replays are 401, always. |
| 401 | `KYA_PAYLOAD_MISMATCH` | Presentation bound to a different payload hash. |
| 401 | `KYA_AGENT_MISMATCH` | Credential agent ≠ payload agent. |
| 401 | `KYA_CREDENTIAL_EXPIRED` / `KYA_CREDENTIAL_NOT_YET_VALID` / `KYA_CREDENTIAL_VALIDITY_TOO_LONG` / `KYA_CREDENTIAL_SIGNATURE_INVALID` / `KYA_INVALID_CREDENTIAL` / `KYA_UNKNOWN_VERSION` | Credential itself fails. |
| 401 | `KYA_L1_REJECTED_CONSEQUENTIAL` / `KYA_L2_INSUFFICIENT_CONSEQUENTIAL` | L1/L2 credential on a consequential (trade/create) action — L3 only. |
| 401 | `KYA_SCOPE_EXCEEDED` / `KYA_CREDENTIAL_SCOPE_EXCEEDS_DELEGATION` / `KYA_CHAIN_PRINCIPAL_MISMATCH` / `KYA_TERMS_HASH_REQUIRED` / `KYA_L3_ISSUER_UNTRUSTED` / `KYA_L3_NOT_INDEPENDENT` / `KYA_L3_METHOD_INVALID` / `KYA_L1_NOT_SELF_SIGNED` / `KYA_L2_DOMAIN_MISMATCH` | Scope / chain / delegation failures. |
| 401 | `KYA_CREDENTIAL_REVOKED` | Credential is on the revocation list. |
| 401 | `KYA_REVOCATION_LIST_MISSING` / `KYA_REVOCATION_LIST_STALE` | No (fresh) revocation list for the issuer — fail closed. **Note:** over HTTP, an L1 credential surfaces as `KYA_REVOCATION_LIST_MISSING`, because the L1 issuer is the principal itself and its list cannot be registered over HTTP (see `POST /revocation-lists`); the `KYA_L1_REJECTED_CONSEQUENTIAL` reason is only reached once that list is registered in-process. |
| 401 | `EXCHANGE_CHALLENGE_UNKNOWN` / `EXCHANGE_CHALLENGE_EXPIRED` | Challenge was never issued by this server / past its 300 s TTL. |
| 401 | `OPERATOR_KEY_INVALID` | Wrong operator key, any operator key when none is configured, or no key on a route that requires one (settlement, when a key is configured). |
| 401 | `JOULEX_VC_REQUIRED` | Policy requires an envelope; none was presented. |
| 401 | `JOULEX_VC_INVALID` | Envelope malformed, wrong version, missing field, TTL > 300 s, or malformed policy. |
| 401 | `JOULEX_VC_KIND` | Envelope kind the verifier does not implement (any hardware kind in v1). |
| 401 | `JOULEX_VC_BINDING` | `report_data` does not match this action (replay across actions). |
| 401 | `JOULEX_VC_EXPIRED` | Envelope expired, or older than the policy `max_age_seconds`. |
| 401 | `JOULEX_VC_MEASUREMENT` | Measurement not in the pinned set (policy list, else `data/measurements.json`). |
| 401 | `JOULEX_VC_EVIDENCE` | Envelope evidence fails verification. |
| 401 | `ACCOUNT_UNAUTHORIZED` | No / malformed `Authorization: Bearer` header, or unknown session token. |
| 401 | `SESSION_EXPIRED` | Session past its 24 h expiry (the session is deleted). |
| 401 | `ACCOUNT_INVALID_CREDENTIALS` | Wrong password or unknown email — identical for both (no user enumeration). |
| 403 | `ACCOUNT_DISABLED` | Correct password, disabled account. |
| 403 | `ACCOUNT_FORBIDDEN` | Non-admin caller on an admin-only route (`POST /accounts/<id>/disable`). |
| 404 | `ACCOUNT_NOT_FOUND` | Unknown account id (link / disable). |
| 409 | `ACCOUNT_EXISTS` | Email already registered (case-insensitive). |
| 409 | `ACCOUNT_AGENT_ALREADY_LINKED` | Credential already linked to an account (this one or another). |
| 400 | `ACCOUNT_VALIDATION: ...` | Bad email / password outside 12–256 chars / missing display_name. |
| 400 | `ACCOUNT_ROLE_INVALID: ...` | Role not one of `buyer`, `generator`, `provider`, `operator`, `admin`. |
| 403 | `INSUFFICIENT_POINTS` | Order cost exceeds the agent's balance. |
| 404 | `NOT_FOUND` | Unknown route (or demo route without `--demo-clock`). |
| 413 | `REQUEST_TOO_LARGE: ...` | Request body exceeds 1,000,000 bytes (audit 2026-11-12). |
| 404 | `MARKET_NOT_FOUND` | Unknown market id. |
| 404 | `DEMO_AGENTS_NOT_FOUND` | `GET /__demo/agents` with no demo fixtures written. |
| 409 | `MARKET_CLOSED` | Trading after `resolves_at`. |
| 409 | `MARKET_RESOLVED` | Trading or settling an already-resolved market. |
| 409 | `MARKET_NOT_YET_RESOLVABLE` | Settling before `resolves_at`. |
| 500 | `internal` | Unexpected server error (never a stack trace in the body). |
| 503 | `AESO_NO_DATA: ...` / `AESO_UNAVAILABLE: ...` / `AESO_TZ_NAME: ...` | AESO unreachable / no data covering the window / bad timezone name. Market stays OPEN. |
| 503 | `HCS_LOG_FAILURE: ...` | The HCS audit log could not be appended — the action is **not executed** (fail closed). No action that cannot be audited runs. |

Admission failures return the kya reason verbatim in `reason`
(401). A 401 never creates a market, places an order, or settles
one; a failed verification never burns a challenge.

## Deviations from DESIGN.md §10 / §11 (recorded, not hidden)

1. **Error shape.** DESIGN.md §10 says `{"error": "<code>", ...}`;
   the implementation returns `{"error": "exchange_error",
   "reason": "<CODE>..."}` (code in `reason`). This file documents
   the real shape.
2. **`JOULEX_VC_KIND` exists.** DESIGN.md §11 maps "kind not
   implemented" to `JOULEX_VC_INVALID`; the code returns the
   distinct `JOULEX_VC_KIND`. `JOULEX_VC_EVIDENCE` also exists
   (evidence verification failure) and is not in the DESIGN table.
3. **HCS failure code.** DESIGN.md §11 says 503
   `HCS_LOG_UNAVAILABLE`; the code raises 503 `HCS_LOG_FAILURE:
   ...`. Same behaviour (action not executed), different code.
4. **Provenance params.** DESIGN.md §10 writes
   `/hcs-log/provenance?from=&to=`; the implemented params are
   `from_agent` / `to_agent` (plus optional `t0` / `t1` time window).
5. **Two implemented routes are not in the DESIGN §10 table:**
   `POST /revocation-lists` and `GET /__demo/agents` (both
   documented above). The six M1.4 account routes are in DESIGN
   §14, not §10.
6. **No route is missing.** The five M1 rows DESIGN.md §10 lists
   (`/attestations`, `/hcs-log`, `/hcs-log/verify`,
   `/hcs-log/knowledge`, `/hcs-log/provenance`) were unimplemented
   in M0/M1 and were implemented in M3 (read-only views over
   `joulex/hcs.py`); nothing in this file is marked "not
   implemented in v1".

## Client helper

`joulex/client.py` (`ExchangeClient`, stdlib urllib only) wraps the
flow: `info()`, `server_time()`, `list_markets()`,
`get_market(id)`, `positions(agent_id)`, `ledger()`,
`get_challenge()`, `sign_order(...)`, `sign_creation(...)`,
`create_market(credential, seed, attested=False, operator_key=None,
**fields)`, `place_order(credential, seed, payload,
attested=False)`, `place_order_raw(payload, header)`,
`settle(market_id, operator_key=None, credential=None,
attested=False, **fields)`, `refresh_revocation_list(list)`,
`set_demo_time(when)`. `demo.py` and `quickstart.py` use it; a
stranger agent can copy either script as a starting point. The
presentation timestamp comes from the server (`GET /challenge`
returns `server_time`), so client and server agree on "now" even on
a demo clock.
