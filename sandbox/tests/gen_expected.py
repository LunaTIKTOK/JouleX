#!/usr/bin/env python3
"""Generate ground-truth expectations from the REAL JouleX modules for
the JS parity test (tests/parity.mjs).

Covers:
  1. LMSR: random multi-outcome cost/price/trade cases (joulex.lmsr).
  2. Canonical JSON + SHA-256: joulex.attestation.canonical_bytes and
     hashlib.sha256 over tricky floats/unicode/nesting.
  3. Orchestrator: joulex.orchestrator.run_simulation on the synthetic
     14-day series + default 24-job set (same inputs the sandbox uses).
"""
import hashlib
import json
import random
import sys

sys.path.insert(0, "/home/hatch/workspace/joulex")

from joulex.attestation import canonical_bytes  # noqa: E402
from joulex.aeso_synthetic import synthetic_price_series  # noqa: E402
from joulex.lmsr import (  # noqa: E402
    cost_function_multi,
    prices_multi,
    trade_cost_multi,
)
from joulex.orchestrator import (  # noqa: E402
    default_job_set,
    run_simulation,
)

OUT = "/home/hatch/workspace/joulex/sandbox/tests/expected.json"


def main() -> int:
    rng = random.Random(20261112)
    lmsr_cases = []
    for _ in range(300):
        n = rng.choice([2, 2, 3, 4, 5])
        q = [round(rng.uniform(0, 400), 4) for _ in range(n)]
        b = rng.choice([10.0, 50.0, 100.0, 250.0])
        idx = rng.randrange(n)
        shares = round(rng.uniform(0.5, 60), 4) * rng.choice([1, 1, -1])
        lmsr_cases.append({
            "q": q, "b": b, "index": idx, "shares": shares,
            "cost_before": cost_function_multi(q, b),
            "prices_before": prices_multi(q, b),
            "trade_cost": trade_cost_multi(q, b, idx, shares),
        })

    hash_cases = []
    # NOTE: every number here is a float literal. Real JouleX payloads
    # carry floats (shares 10.0, b 100.0, strike 50.0 — see
    # joulex/admission.py order_payload / creation_payload), and JS has
    # no int/float distinction, so the canonical form under test is
    # the float form ("10.0", never "10"). Python ints are untestable
    # from JS and never occur in a payload hash.
    payloads = [
        {"a": 1.0, "b": "x"},
        {"z": [3.0, 2.0, 1.0], "a": {"y": 2.0, "x": 1.0}},
        {"price": 62.5, "tiny": 1e-06, "third": 1 / 3, "neg": -0.000001},
        {"unicode": "héllo wörld — AESO", "n": 0.0, "f": False, "nil": None},
        {"shares": 10.0, "side": "YES", "cost": 5.000001, "big": 1e16, "small": 5e-05},
        {"nested": [{"b": 2.0, "a": [1.5, 2.5]}, {"d": 4.0}]},
        {"market_id": "mkt_x", "q": [0.0, 0.0], "b": 100.0},
        {"empty": {}, "list": [], "f": 123456789.123456789},
    ]
    for p in payloads:
        canon = canonical_bytes(p).decode("utf-8")
        hash_cases.append({
            "payload": p,
            "canonical": canon,
            "sha256": hashlib.sha256(canonical_bytes(p)).hexdigest(),
        })

    prices = synthetic_price_series(days=14)
    jobs = default_job_set(n_jobs=24, n_hours=len(prices))
    sim = run_simulation(prices, jobs, source_label="SYNTHETIC")
    orch = {
        "costs": sim["costs"],
        "improvement_pct": sim["improvement_pct"],
        "sla_pct": sim["sla_pct"],
        "verdict": sim["verdict"],
        "strategy": sim["strategy"],
        "annualised": sim["annualised"],
        "allocations": sim["allocations"],
    }

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"lmsr": lmsr_cases, "hash": hash_cases, "orchestrator": orch},
                  f, indent=1)
    print(f"wrote {OUT}: {len(lmsr_cases)} LMSR cases, "
          f"{len(hash_cases)} hash cases, orchestrator verdict {sim['verdict']}")
    print("orchestrator costs:", sim["costs"])
    print("improvement:", sim["improvement_pct"], "SLA:", sim["sla_pct"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
