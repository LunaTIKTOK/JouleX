#!/usr/bin/env python3
"""Build sandbox/data.json from the REAL JouleX modules — no hand-typed data.

- Synthetic 14-day hourly series: joulex.aeso_synthetic.synthetic_record
  (seed 20261110, start 2026-10-27 UTC) — labelled SYNTHETIC.
- Live AESO snapshot: the SMP report fetched from ets.aeso.ca on
  2026-11-12 ~08:47 MT, parsed with joulex.aeso.parse_smp_csv and
  averaged to hourly pool prices with joulex.aeso._hourly_pool_prices
  (the same code the oracle uses). Timestamped; covers ~3h only.
- Default orchestrator job set: joulex.orchestrator.default_job_set
  (24 jobs, seed 20261110) — embedded so the JS scheduler port runs
  on the exact same jobs as the Python original.
- Seed markets: windows inside the synthetic series.

Re-run to refresh the live snapshot: re-fetch /tmp/smp.csv first with
curl (see README), then run this script. Never hand-edit data.json.
"""
import json
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/home/hatch/workspace/joulex")

from joulex.aeso import _hourly_pool_prices, parse_smp_csv  # noqa: E402
from joulex.aeso_synthetic import synthetic_record  # noqa: E402
from joulex.orchestrator import default_job_set  # noqa: E402

SMP_CSV = "/tmp/smp.csv"
OUT = "/home/hatch/workspace/joulex/sandbox/data.json"
LIVE_FETCHED_AT = "2026-11-12T14:47:21Z"  # Date header of the SMP fetch
LIVE_SOURCE_URL = (
    "http://ets.aeso.ca/ets_web/ip/Market/Reports/"
    "CSMPriceReportServlet?contentType=csv"
)


def main() -> int:
    synth = synthetic_record(days=14)
    # Live snapshot: parse the fetched SMP CSV with the real oracle code.
    with open(SMP_CSV, encoding="utf-8") as f:
        text = f.read()
    rows = parse_smp_csv(text)
    hourly = _hourly_pool_prices(rows)
    live = [
        {"timestamp": ts.astimezone(timezone.utc).isoformat(), "price": round(p, 2)}
        for ts, p in hourly
    ]
    jobs = default_job_set(n_jobs=24, n_hours=336)

    # Settlement previews (computed here so the README/verification can
    # cite them; the sandbox derives them live from the same series).
    series = [(p["timestamp"], p["price"]) for p in synth["prices"]]
    by_ts = {ts: p for ts, p in series}

    def window_max(start_iso, end_iso):
        start = datetime.fromisoformat(start_iso)
        end = datetime.fromisoformat(end_iso)
        vals = [p for ts, p in series if start <= datetime.fromisoformat(ts) < end]
        return max(vals) if vals else None

    flex_max = window_max("2026-11-03T00:00:00+00:00", "2026-11-03T06:00:00+00:00")
    peak_max = window_max("2026-11-04T00:00:00+00:00", "2026-11-05T00:00:00+00:00")

    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "synthetic": synth,
        "live_snapshot": {
            "label": "LIVE SNAPSHOT",
            "source_url": LIVE_SOURCE_URL,
            "fetched_at": LIVE_FETCHED_AT,
            "fetched_by": "joulex.aeso.parse_smp_csv + _hourly_pool_prices (same code as the oracle)",
            "coverage_note": (
                "The live SMP report covers only the recent ~3 hours. The "
                "historical PoolPriceReportServlet returns HTTP 403 from "
                "this VM, so no multi-day live history exists. A browser "
                "cannot fetch this endpoint directly (no CORS headers, "
                "plain HTTP) — hence the embedded snapshot."
            ),
            "hours": live,
        },
        "jobs": jobs,
        "seed_markets": [
            {
                "market_id": "mkt_sandbox_flex1",
                "question": "Will the Alberta pool price exceed $50/MWh during the delivery window Nov 2, 18:00–24:00 MT?",
                "resolution_type": "aeso_price",
                "contract_type": "flex_capacity",
                "strike": 50.0,
                "window_start": "2026-11-03T00:00:00+00:00",
                "window_end": "2026-11-03T06:00:00+00:00",
                "resolution_source_url": LIVE_SOURCE_URL,
                "resolves_at": "2026-11-04T00:00:00+00:00",
                "b": 100.0,
                "outcomes": ["YES", "NO"],
            },
            {
                "market_id": "mkt_sandbox_compute1",
                "question": "Will H100 spot be above $4.00/GPU-hr on Nov 8, 2026?",
                "resolution_type": "public_source",
                "contract_type": "compute_forward",
                "source_url": "https://example.com/gpu-prices",
                "source_description": "Published H100 spot price; YES iff above $4.00/GPU-hr. PLACEHOLDER SOURCE — sandbox only.",
                "resolves_at": "2026-11-08T00:00:00+00:00",
                "b": 100.0,
                "outcomes": ["YES", "NO"],
            },
            {
                "market_id": "mkt_sandbox_peak1",
                "question": "In which band will the max Alberta pool price fall during Nov 4, 2026 (MT)?",
                "resolution_type": "public_source",
                "contract_type": "flex_capacity",
                "source_url": "SYNTHETIC series (joulex.aeso_synthetic, seed 20261110)",
                "source_description": "Max hourly pool price over Nov 4, 2026 from the labelled SYNTHETIC series; the sandbox derives the band and shows the derivation.",
                "resolves_at": "2026-11-06T00:00:00+00:00",
                "b": 100.0,
                "outcomes": ["$0–25", "$25–50", "$50–100", "$100+"],
            },
        ],
        "seed_trades": [
            {"agent_id": "alice", "market_id": "mkt_sandbox_flex1", "side": "YES", "shares": 10},
            {"agent_id": "bob", "market_id": "mkt_sandbox_flex1", "side": "NO", "shares": 20},
            {"agent_id": "alice", "market_id": "mkt_sandbox_compute1", "side": "YES", "shares": 10},
            {"agent_id": "bob", "market_id": "mkt_sandbox_compute1", "side": "NO", "shares": 10},
        ],
        "sandbox_clock_start": "2026-10-27T00:00:00+00:00",
        "verification": {
            "flex_window_max_usd_per_mwh": flex_max,
            "flex_outcome_vs_strike_50": "YES" if flex_max is not None and flex_max > 50 else "NO",
            "peak_window_max_usd_per_mwh": peak_max,
        },
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    print(f"wrote {OUT}")
    print("synthetic stats:", synth["stats"])
    print("live hours:", [(h["timestamp"], h["price"]) for h in live])
    print("flex window max:", flex_max, "-> outcome:",
          "YES" if flex_max and flex_max > 50 else "NO")
    print("peak window max:", peak_max)
    print("jobs:", len(jobs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
