# Vendored / ported code — provenance

JouleX M0 is a port of the agent-exchange core. The rule (DESIGN.md
§1): **copy the code, adapt the market vocabulary, keep the verified
mechanics identical.**

- **Source:** `~/workspace/agent-exchange/` — DESIGN.md v1, M0–M6
  done, suite 190/190 green 2026-11-09.
- **Copied:** 2026-11-10 (JouleX M0 build).
- **Destination:** `joulex/` (package, renamed `exchange` → `joulex`),
  `tests/`, `demo.py`, `quickstart.py` at this root.

## What was copied, and what changed in the port

| Source file | JouleX file | Port change |
|---|---|---|
| `exchange/__init__.py` | `joulex/__init__.py` | Package renamed; vendor dir path updated |
| `exchange/lmsr.py` | `joulex/lmsr.py` | Unchanged (verified mechanics identical) |
| `exchange/ledger.py` | `joulex/ledger.py` | Unchanged |
| `exchange/store.py` | `joulex/store.py` | Unchanged |
| `exchange/challenges.py` | `joulex/challenges.py` | Unchanged |
| `exchange/settlement.py` | `joulex/settlement.py` | Docstring § ref only; X402 hook still inert |
| `exchange/reconcile.py` | `joulex/reconcile.py` | Docstring `kraken_price` → `aeso_price` only |
| `exchange/markets.py` | `joulex/markets.py` | **Adapted:** `kraken_price` → `aeso_price`; `contract_type` ∈ {flex_capacity, compute_forward, generic}; horizon 1–30d (was 1–7d); `strike` + `window_start` + `window_end`, max-pool-price rule, ties NO; `aeso_price` binary-only; `symbol`/`direction`/`resolution_date` removed |
| `exchange/admission.py` | `joulex/admission.py` | **Adapted:** creation-payload binding gains `contract_type`, `strike`, `window_start`, `window_end`; drops `symbol`/`direction`/`resolution_date`; tool names `joulex.place_order` / `joulex.create_market` |
| `exchange/exchange.py` | `joulex/exchange.py` | **Adapted:** oracle path for `aeso_price` (catches `AESOUnavailable` → 503); service name `joulex`, info key `aeso_oracle` |
| `exchange/server.py` | `joulex/server.py` | **Adapted:** default port 8472; `--aeso-oracle` (was `--kraken-oracle`); `GET /aeso/price` route; operator-key env `JOULEX_OPERATOR_KEY` |
| `exchange/client.py` | `joulex/client.py` | Tool-name/docstring renames only |
| `exchange/demo_issuer.py` | `joulex/demo_issuer.py` | Demo issuer id `demo-issuer.joulex.local`; tool names renamed |
| `exchange/oracle.py` | *(deleted — replaced by `joulex/aeso.py`)* | Kraken OHLC oracle NOT ported. `joulex/aeso.py` is new code (AESO SMP report → hourly pool prices; see its docstring) |
| `exchange/vendor/*` | `joulex/vendor/*` | **Copied UNMODIFIED** — CF-KYA snapshot; hashes below |
| `tests/*.py` (except `test_oracle.py`) | `tests/*.py` | **Adapted:** `exchange` → `joulex` imports; market payloads use the JouleX `aeso_price` vocabulary; horizon checks 30d; `test_docs.py` rewritten for the M0 README (full docs are M3) |
| `tests/test_oracle.py` | *(deleted — replaced by `tests/test_aeso.py`)* | New AESO oracle tests, written test-first |
| `demo.py` | `demo.py` | **Adapted:** two agents, one `flex_capacity` `aeso_price` market + one `compute_forward` `public_source` market; live AESO spot check |
| `quickstart.py` | `quickstart.py` | **Adapted:** first trade on a `flex_capacity` `aeso_price` market, port 8472 |

New files in M0 (not ports): `joulex/aeso.py`, `tests/test_aeso.py`,
`tests/test_joulex_markets.py`, `README.md`, this file.

## SHA-256 of the source files at copy time

Verify with: `cd ~/workspace/agent-exchange && sha256sum -c <this file's table>`
(manual — the table below is the record, not a checksum file).

| Source file | SHA-256 |
|---|---|
| `exchange/__init__.py` | `5d8609002ef19353168ee3a0a26780d676e4beb2bb09d8ad1adc34c0525fc0e0` |
| `exchange/admission.py` | `d22dd49d0b44f9ef37be4c1ea733dadf3be6891c23f67ed629095c5afa7c2055` |
| `exchange/challenges.py` | `c15bde8e73254ca5002022e5ff249b337a6c4ab8bf8e6f0966f172a2860af6c5` |
| `exchange/client.py` | `6ce6e862dfd778c2461149ee3727e32602d0b2f898ed3588b27d1620cfcc999b` |
| `exchange/demo_issuer.py` | `15c60da6d496facbbd4f16869acda5dc1b7a9b4919310887996b4c07a6ac906b` |
| `exchange/exchange.py` | `a1c21a205905a0f8cc5a56825b17522e869f5580c6dc6a4ba4c662f272b9b6b9` |
| `exchange/ledger.py` | `01582f11ed9ec9d88ebd5dff9f1b190798232ad6c4e76f7c0d1d25557080edbf` |
| `exchange/lmsr.py` | `fb9e06343bc86de8808b76fac88b609220e66aebc79c9c925001d818515052df` |
| `exchange/markets.py` | `1bbc71000219817f85702102ae240122916230332bc4a8a70e1f9d81470a18f0` |
| `exchange/oracle.py` | `eb208adcab7cf199ef5968cce53c53964631934c5c0fd5ded1c20c8736ba3e36` |
| `exchange/reconcile.py` | `294c898e44d1046ee0b380a9766cd759b0cc37aab179a45f18762de0b179112a` |
| `exchange/server.py` | `ba1ec4ddfcadca9195e08315b9f3fae1021190c6984ec0342b8e9e1062c4ecac` |
| `exchange/settlement.py` | `d25e14ea41c4039d969a8accc9da6c2a2426b691b79b0f62f2a6c225e06dc5df` |
| `exchange/store.py` | `6231c7cbccb50cf10ca0e8063178ceb32fc5dc056b2da5a5424f295649c082da` |
| `exchange/vendor/delegation.py` | `821247e53c42e008652053679da7624fcf086eda0da5dded30ed281ecb99e212` |
| `exchange/vendor/ed25519.py` | `6b238d3e2f0c16b911b86a2a66471b0603e8e8e0415dc20d02b36c63de158f09` |
| `exchange/vendor/ed25519_pure.py` | `ba78c476afc96900d7cfd628ff34ce79eebff0ca1fc86785b81a7f87f2b9bfae` |
| `exchange/vendor/intent_classification.py` | `bcdf97f3732680caff188cd08ad90e09ff3d194077cb9f099425f65f9e827e1e` |
| `exchange/vendor/kya.py` | `42150ef755be1268798b75278230e55e9ea72e3f7096040fc5eb6a132e3819b5` |
| `exchange/vendor/principal.py` | `67b924fac15541c7e3c6edeabff01d1894d10deef46298920d5d7c8a3b37150e` |
| `tests/conftest.py` | `4d602edc4a3f3528960196f1c34dc364c34b8457daa675e9fce366fb95c1f9e7` |
| `tests/test_admission.py` | `14ed379b8eff40d1d3322e884ce5003c7299550a005230fc92950fd2ec877e46` |
| `tests/test_api.py` | `0ee770bd5afd1174399c8fd28361fd4cd49ba5a7cb6d1d326123750cdd600d16` |
| `tests/test_challenge_store.py` | `8f364fdd2f769927927453f0424daa9899049efb89241afbbe1e046900733f77` |
| `tests/test_docs.py` | `995628f13a5fe2950758bd00dec7cbd96fe0c6244284b4555a330fe9d64114ab` |
| `tests/test_ledger.py` | `f9944fb30bf1444abef5abf7993bfdce376df6a3da40369efa149e465cafd354` |
| `tests/test_lmsr.py` | `ea573962457ace8f8d094d190199019bf9d5c14404ae9ec213054ee0207543ad` |
| `tests/test_market_creation.py` | `49878cde7e2c826659c01a6122d3918df4962cf87dc9fc2c556b5a98e36bc14e` |
| `tests/test_multi_outcome.py` | `ee4377f299e98be4545ed9e281a4003bca5396befd7cefaccfc75e49144958b3` |
| `tests/test_oracle.py` | `0f258201b4f768c4311d77c8d2a1243c80e589460fe27334e37a23ee6f31102d` |
| `tests/test_reconcile.py` | `8b1e3716b491492ac76b28c6cbe290df7286beecce63d69fa626f285d46dd87f` |
| `demo.py` | `a04ccf07b68e95f71f543e7161a3bfe7cdce2c1cc8add40e565d962dd712aaf2` |
| `quickstart.py` | `c2a296ea287342bfed844464fc94d987a7888d3b36751796db8c933762c98d21` |

## CF-KYA snapshot (vendored, unmodified)

The CF-KYA snapshot inside `joulex/vendor/` is copied UNMODIFIED from
the agent-exchange snapshot (which itself is a snapshot of
`~/workspace/cognitive-firewall-build/`, CF-KYA v0.1,
`KYA_VERSION = "cf-kya-0.1"`, copied 2026-11-05). The six files and
their hashes are identical to the source — verified at M0 build time
with `sha256sum -c VENDOR_HASHES.txt` from `joulex/vendor/` (all six
OK). Do not edit them here; fixes go upstream in the firewall repo,
and this snapshot is refreshed only by re-copying from a tagged
version and re-recording the hashes. See `joulex/vendor/VENDOR.md`.
