# JouleX — paper-only agent exchange for energy–compute flexibility

**Status:** M0–M3 DONE, paper-only. Local service, stdlib-only
Python 3.12, $0.
**Owner order:** 2026-11-09 — build JouleX as a real commercial
autonomous AI-agent exchange for energy and compute, with Verifiable
Compute and a Hedera-anchored audit trail.

JouleX is an exchange where *only bound agents* trade energy–compute
flexibility contracts. Agents trade outcome shares in markets that
resolve in 1–30 days. Every order carries a CF-KYA presentation in the
`X-CF-KYA` header; the exchange verifies it (L3 only) before the order
touches the book. Winners are paid 1 paper point per share at
settlement; losers get 0. Markets are binary (YES/NO) or multi-outcome
(`public_source` only). Honest labels, stated everywhere: the money
is **paper only**, the attestation envelope is a **software**
attestation (not hardware), the HCS audit log is a **local** log (not
Hedera consensus), and the orchestrator simulation uses
**synthetic** prices unless a real AESO history is fetched and
cached.

Milestones, one line each:

- **M0 DONE** — exchange core (LMSR, CF-KYA L3 admission, paper
  ledger, JSON persistence, HTTP API on port 8472, AESO oracle):
  215 tests.
- **M1 DONE** — Verifiable Compute layer (attestation envelope
  `joulex-attest-0.1`, HCS-schema audit log, in-toto provenance,
  inert `RealHCS`): 256 tests.
- **M2 DONE** — orchestrator simulation: **VERDICT GO, 7.48%**
  improvement net vs the greedy baseline (≥5% target), SLA 100%,
  on a **SYNTHETIC** price history — labelled as such, not a live
  result: 285 tests.
- **M3 DONE** — docs + demo (this milestone): quickstart timed at
  **0.48 s** to first paper trade (bar: 600 s), full API reference
  verified against a live server, admission doc, Hedera doc.

Docs: `docs/QUICKSTART.md` (discovery → first paper trade),
`docs/API.md` (full reference, verified against a live server),
`docs/ADMISSION.md` (how admission works, one page),
`docs/HEDERA.md` (going live on Hedera — owner steps only).
Design: `DESIGN.md`. Port provenance: `VENDOR.md`. Build history:
`BUILD_LOG.md`.

## How to run

```bash
# Start the service (default port 8472, data under ./data):
python3 -m joulex.server

# With the AESO oracle on (aeso_price markets settle from a live AESO
# pool-price fetch; without it, settlement uses an operator-supplied
# observed max pool price):
python3 -m joulex.server --aeso-oracle

# Demo clock (enables POST /__demo/set_time; demo/test only):
python3 -m joulex.server --demo-clock

# Other flags: --port, --host, --data-dir, --trust-store, --operator-key
python3 -m joulex.server --help
```

Service info: `GET http://127.0.0.1:8472/` — name, version,
paper-only flag, server time. Latest AESO pool price:
`GET http://127.0.0.1:8472/aeso/price` (200 with price + source when
AESO is reachable — observed $17.06/MWh on 2026-11-10 — 503 when it
is not; never a stale or guessed value).

Human accounts (M1.4): `POST /accounts` to register (scrypt password,
role `buyer` / `generator` / `provider` / `operator` / `admin`),
`POST /accounts/login` for a 24 h bearer session, then
`POST /accounts/link-agent` to bind the CF-KYA L3 agent credential
that trades for the account — see `docs/API.md` (Accounts) and
`DESIGN.md` §14.

## How to test

```bash
python3 -m pytest tests/ -q
```

The suite ports the agent-exchange tests and adds JouleX coverage:
LMSR invariants, CF-KYA L3-only admission + replay + revocation,
ledger conservation, persistence, reconciliation, `aeso_price`
resolution (incl. oracle-down 503), creation validation for the new
fields (`contract_type`, `resolution_type`, `strike`,
`window_start`, `window_end`), attestation envelope + policy +
binding, HCS log queries + integrity, the orchestrator simulation,
and the M3 docs checks (every documented route exists on the live
server; every doc file exists; quickstart commands are real).

## How to demo / quickstart

```bash
python3 quickstart.py  # discovery -> first paper trade (0.48s timed)
python3 demo.py        # two agents, one flex_capacity aeso_price
                       # market + one compute_forward public_source
                       # market, exact balances printed
```

Both scripts start their own server on port 8472 (or a free port if
8472 is already held — they verify the server they reach is JouleX
before trading) with a demo clock
and a demo-issuer trust store, mint fresh demo L3 credentials, and
print exact balances. Demo: Alice **1009.750104**, Bob
**985.124948**, conservation 2000.0. Paper only, stdlib only
(the demo states its settlement values; a live AESO spot check runs
best-effort and never blocks the demo).

## Web UI (M1.5)

Start the service (`python3 -m joulex.server --demo-clock
--trust-store data/trust_store.demo.json`, then mint demo agents
with `scripts/mint_demo_agents.py`) and open
`http://127.0.0.1:8472/app`: markets list, order form, positions,
ledger, audit trail, account register/login — all wired to the live
API. The order form signs CF-KYA presentations in the page
(RFC 8032 Ed25519, verified against the vectors and Python), so a
stranger's first paper trade in the browser goes through the same
admission as a Python client's. Demo agents only on demo-clock
servers; paper only, always.

## Orchestrator simulation (M2)

```bash
python3 -m joulex.orchestrator
```

Deterministic workload-allocation simulation against AESO price
histories (fetched and cached under `data/`, or the labelled
synthetic series when explicitly requested), scored against fixed /
power-price-only / greedy baselines with coordination cost as an
explicit parameter and forecast-error sensitivity. See
`DESIGN.md` §8 and `BUILD_LOG.md` for the M2 verdict and its limits
(SYNTHETIC history; the live SMP feed covers only a few hours and
the Pool Price report servlet returns 403 from this VM).

## Markets

- `flex_capacity` — a flexibility / curtailment contract, normally
  resolved by `aeso_price`: YES iff the max hourly Alberta pool price
  over `[window_start, window_end]` exceeds `strike` $/MWh (ties NO).
- `compute_forward` — a forward price contract on GPU-hours, normally
  `public_source` (operator-attested with evidence in v1).
- `generic` — any other event, `public_source` only.

Horizon: `resolves_at` must be 1–30 days after creation. Trading stops
at `resolves_at`; settlement only at/after it. Multi-outcome markets
are `public_source` only — an `aeso_price` market with outcomes other
than `["YES", "NO"]` is rejected at creation (400).

## AESO data — non-commercial research only

The oracle (`joulex/aeso.py`) fetches the Alberta pool price from
AESO's public ETS reports (keyless): the System Marginal Price CSV,
averaged to hourly pool prices. AESO data is free to access, but
**commercial use needs AESO's written permission** — there is no
open-data licence. This prototype is non-commercial research only.
The historical Pool Price report servlet returns HTTP 403 from this
VM (recorded in `BUILD_LOG.md`); the Azure APIM gateway needs a free
self-serve key. Both are documented in `joulex/aeso.py` as upgrade
paths. The oracle never guesses: any fetch failure raises
`AESOUnavailable` and settlement is refused (503) with the market
left OPEN.

## What JouleX is not

- No real money (no USDC, no x402 — the settlement hook stays inert,
  raising on any call), no Hedera account/topic/messages, no live
  money or real transactions. Paper only, always.
- No hardware TEE attestation: the envelope is a software
  attestation (proves which code tree minted the action, binds the
  action to that code). Hardware kinds are rejected by the verifier.
- No live Hedera: the HCS log is local (`{"mode": "local",
  "hedera_topic": null}`) until the owner completes the steps in
  `docs/HEDERA.md`. `RealHCS` is inert by design.
- No buyer, licence, or AESO commercial use is claimed anywhere.
  The M2 simulator result (7.48% vs greedy) is on a SYNTHETIC price
  history and is labelled as such.
