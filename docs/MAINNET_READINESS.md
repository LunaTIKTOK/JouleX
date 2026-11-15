# Mainnet readiness checklist — JouleX on Hedera

Status: **NOT READY for mainnet** (audit 2026-11-12,
`AUDIT_HEDERA_COMMERCIAL_20261112.md`). Testnet is verified live;
every item below is an owner step, a policy call, or a build step
that must land before mainnet traffic. Do not flip the network flag
until every box is checked.

## 1. Mainnet topic + account (owner steps)

- [ ] New **mainnet HCS topic**, separate from testnet `0.0.11399235`
      (never reuse the testnet topic for mainnet traffic).
- [ ] Topic carries a **submit key** (only the operator account can
      post) AND an **admin key** (so it can be updated/deleted).
      The live testnet topic has NEITHER — verified 2026-11-12 via
      the mirror node (`admin_key: null`, `submit_key: null`, empty
      memo). Anyone can post to it; it can never be deleted. That
      is acceptable for a testnet wiring probe and unacceptable
      for commercial traffic.
- [ ] Funded **mainnet operator account** (owner's keys, owner's
      exchange purchase). Never reuse testnet keys.
- [ ] `data/hcs_config.json` on the deploy: `{"mode": "hedera",
      "hedera_topic": "<mainnet topic>", "network": "mainnet"}`,
      `JOULEX_HCS_MODE=hedera`, `JOULEX_HCS_OPERATOR=<mainnet
      account>`, `JOULEX_HCS_PRIVATE_KEY` in env only, never in a
      file, never in Render's dashboard as a plain value unless
      the owner accepts that exposure (prefer a secrets manager).

## 2. Cost + funding (verified figures)

Observed live receipt (2026-11-10, ~900-byte message): 717,339
tinybars ≈ **$0.0008/message**. Cost is one message per action
(order / market creation / settlement / attestation-verification
result), so the burn rate is exactly the action rate.

| Messages/day | $/day | $/year | HBAR/day (~$0.11) |
|---:|---:|---:|---:|
| 100 | 0.08 | 29 | 0.7 |
| 1,000 | 0.80 | 292 | 7.3 |
| 10,000 | 8.00 | 2,920 | 73 |
| 100,000 | 80.00 | 29,200 | 727 |

- [ ] HBAR budget funded for the target rate + 30 days of headroom.
- [ ] Alert when the operator balance falls below 7 days of burn
      (no such alert exists yet — wire `GET /health` +
      `hcs_failed_submits` into whatever monitoring the deploy has).
- [ ] Fee-schedule watch: the $0.0008 figure is a network fee
      (Jan 2026 pricing, re-check before funding). If the topic
      ever enables custom fees, the cost jumps to ~$0.05/message
      (Hedera docs) — with a submit key the operator controls
      that; with no key, nobody can change it.

## 3. Anchoring policy (owner decision — the one real policy call)

- [ ] Decide: **fail-retain (current)** or **fail-closed**.
      Fail-retain: a failed anchor never blocks the trade; the
      failure lands in `data/hcs_dead_letter.jsonl` and
      `GET /health` (`hcs_failed_submits`). Fail-closed: an
      action that cannot be anchored does not execute (the local
      log failure already behaves this way — 503 `HCS_LOG_FAILURE`).
      The machinery (retry, dead-letter, receipts, metrics) is
      built for either policy; the default stays fail-retain until
      the owner chooses. Commercial settlement of real value
      argues for fail-closed on settlements at minimum.
- [ ] Decide the dead-letter drill cadence: a failed anchor must
      be re-submitted by hand from `hcs_dead_letter.jsonl` (no
      automatic re-drive exists — by design, re-anchoring is an
      operator action, not a silent retry loop).

## 4. Throughput (build step before high volume)

- [ ] Synchronous-submit ceiling understood: each action submits
      while holding the exchange lock; the helper caps at 60s and
      a normal submit takes ~2–5s (receipt wait). That bounds
      throughput at roughly 12–30 anchored actions/minute.
      Fine at 1,000 msgs/day (~0.7/min average); marginal at
      10,000/day if bursty; **an async anchor queue (submit off
      the trade path, ordered, with the same retry/dead-letter
      machinery) is required before 100,000 msgs/day** — it is a
      design change to the ordering guarantee, not a config flip.
- [ ] Load test at the target message rate against testnet before
      mainnet (measure p95 submit latency + lock hold time).

## 5. Durability + recovery (owner/ops steps)

- [ ] Persistent storage for `data/` (Render paid disk, or a
      volume) — the free tier wipes `data/` on redeploy. Until
      then, run `python3 scripts/backup_data.py` on a cadence the
      owner sets and copy the archive off the box.
- [ ] Recovery drill done once: restore `data/` from a backup,
      start the server, run `GET /hcs-log/verify` (must be ok)
      and `python3 scripts/hcs_mirror_replay.py` (must report
      `ok: true` for receipted messages).
- [ ] `data/measurements.json` re-pinned after the last code
      change (any `joulex/*.py` change alters the measurement;
      see `docs/HEDERA.md` "Re-pinning the measurement").

## 6. AESO (owner step — blocks commercial use of the oracle)

- [ ] **AESO's written permission obtained** for commercial use of
      the pool-price data. The code is fail-closed and honest
      about this (see `joulex/aeso.py`); the prototype is
      NON-COMMERCIAL RESEARCH ONLY until permission exists.
- [ ] Keyed AESO APIM access (free self-serve key from
      https://developer-apim.aeso.ca/) — the keyless ETS report
      covers only ~3 hours, so multi-day settlement windows
      cannot resolve (`AESO_NO_DATA`) without it.

## 7. Hardening (deploy decisions)

- [ ] CORS restricted from `*` to the sim site's origin(s) (the
      wildcard exists for the `file://` demo page).
- [ ] Rate limiting per agent (none exists yet — the challenge
      store bounds challenge reuse, not request rate).
- [ ] `GET /health` wired into the deploy's health check and
      alerting (it already reports `hcs_failed_submits`).
- [ ] Operator key rotation plan (the key is env-only; rotation
      means re-supplying it on restart).

## 8. Verification (run on mainnet, in order)

1. Submit one test action; read the topic back (HashScan mainnet
   topic page + mainnet mirror node) and confirm message,
   sequence, consensus timestamp, `message_hash`.
2. `python3 scripts/hcs_mirror_replay.py --network mainnet` —
   must report `ok: true`.
3. `GET /hcs-log/verify` — must report `ok: true`.
4. Dead-letter drill: stop the network path, confirm the action
   still executes locally, the failure lands in
   `hcs_dead_letter.jsonl`, and `hcs_failed_submits` increments.
5. Only then call the integration live.
