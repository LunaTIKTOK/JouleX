#!/usr/bin/env python3
"""Quickstart: discovery -> first paper trade in under ten minutes.

This is the script README.md tells a stranger to run. It does the
whole first-trade flow against a local demo server and prints the time
each step took:

  1. Start the service (demo clock + demo issuer trust store).
  2. Discover it: GET / (service info) and GET /time.
  3. Mint a demo L3 credential for one test agent (demo issuer only —
     a real agent gets its credential from a real issuer).
  4. Create one flex_capacity aeso_price market through the admitted
     path (CF-KYA presentation bound to the creation payload).
  5. Check the agent's paper balance (1,000 points) and place the
     first order: buy 10 YES. Print the trade receipt.

Paper only, stdlib only. Run from the repo root:

    python3 quickstart.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from joulex.client import ExchangeClient  # noqa: E402
from joulex.demo_issuer import DemoIssuer  # noqa: E402
from joulex.server import DemoClock, pick_port  # noqa: E402

PORT = pick_port(8472)
STRIKE = 50.0


def step(t0: float, label: str) -> None:
    print(f"  [{time.monotonic() - t0:6.2f}s] {label}")


def wait_for_server(client: ExchangeClient, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            info = client.info()
            # Fail closed on identity, not just reachability: a
            # stranger on this port (another service, a stale server)
            # is not a JouleX server, and trading against it would
            # be a silent wrong-server trade.
            if info.get("name") != "joulex":
                raise RuntimeError(
                    f"server on port {PORT} answered but is not JouleX "
                    f"(name={info.get('name')!r}) — refusing to trade against it"
                )
            return
        except RuntimeError:
            raise
        except Exception:
            time.sleep(0.25)
    raise RuntimeError("server did not come up")


def main() -> int:
    t0 = time.monotonic()
    clock = DemoClock(datetime.now(timezone.utc))
    issuer = DemoIssuer(now_fn=clock)
    tmp = tempfile.mkdtemp(prefix="joulex-quickstart-")
    data_dir = os.path.join(tmp, "data")
    trust_store_path = os.path.join(tmp, "trust_store.json")
    with open(trust_store_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "trusted_issuers": issuer.trust_store,
                "revocation_lists": {issuer.issuer_id: issuer.revocation_list()},
            },
            f,
        )

    server = subprocess.Popen(
        [
            sys.executable, "-m", "joulex.server",
            "--port", str(PORT),
            "--data-dir", data_dir,
            "--trust-store", trust_store_path,
            "--demo-clock",
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        client = ExchangeClient(f"http://127.0.0.1:{PORT}")
        wait_for_server(client)
        print("quickstart — discovery -> first paper trade")
        print(f"  server: http://127.0.0.1:{PORT} (paper-only, demo clock)")

        # 1. Discovery: service info + server time.
        info = client.info()
        step(t0, f"GET / -> {info['name']} v{info['version']}, "
                 f"paper_only={info['paper_only']}, demo_clock={info['demo_clock']}")
        step(t0, f"GET /time -> {client.server_time()}")

        # 2. One test agent, real L3 demo credential. In production the
        #    credential comes from a real issuer; the demo issuer's
        #    attestation is asserted, not verified.
        agent = issuer.mint_agent(
            principal_id="principal-quickstart",
            display_name="Quickstart Test Principal",
            agent_id="agent-quickstart-1",
        )
        agent_id = agent["agent_id"]
        step(t0, f"minted demo L3 credential for {agent_id} "
                 f"(tool joulex.place_order + joulex.create_market)")

        # 3. Create one flex_capacity aeso_price market through the
        #    admitted path.
        server_now = datetime.fromisoformat(client.server_time())
        window_start = server_now + timedelta(days=1)
        window_end = window_start + timedelta(hours=6)
        market = client.create_market(
            agent["credential"], agent["agent_seed"],
            question=f"Will the Alberta pool price exceed ${STRIKE:.0f}/MWh "
                     f"during the delivery window {window_start.date()}?",
            resolution_type="aeso_price",
            contract_type="flex_capacity",
            strike=STRIKE,
            window_start=window_start.isoformat(),
            window_end=window_end.isoformat(),
            resolves_at=(server_now + timedelta(days=1)).isoformat(),
            b=100.0,
        )
        mid = market["market_id"]
        step(t0, f"created market {mid} (created_by={market['created_by']}, "
                 f"contract_type={market['contract_type']}, "
                 f"price_yes={market['price_yes']:.4f})")

        # 4. Balance check, then the first paper trade. The 1,000-point
        #    endowment is credited lazily on the first verified order
        #    (DESIGN.md §6), so a fresh agent reads 0.00 here — that is
        #    expected, not an error.
        before = client.positions(agent_id)
        step(t0, f"GET /positions -> balance {before['balance_points']:.2f} pts "
                 f"(fresh agent: the 1,000-pt endowment lands on the first order)")
        receipt = client.place_order(
            agent["credential"], agent["agent_seed"],
            {"agent_id": agent_id, "market_id": mid,
             "side": "YES", "shares": 10, "action": "buy"},
        )
        after = client.positions(agent_id)
        held = next(
            (p["shares"] for p in after["positions"]
             if p["market_id"] == mid and p["side"] == "YES"),
            0.0,
        )
        step(t0, f"POST /orders -> bought 10 YES for {receipt['cost_points']:.6f} pts, "
                 f"price_yes now {receipt['price_yes_after']:.4f}")
        step(t0, f"balance after trade: {after['balance_points']:.6f} "
                 f"(holds {held:.0f} YES shares)")

        elapsed = time.monotonic() - t0
        print(f"TRADE OK — first paper trade complete in {elapsed:.2f}s "
              f"(bar: 600s). Receipt: {json.dumps(receipt, sort_keys=True)}")
        print(f"  (quickstart state kept at {data_dir}; server stopped)")
        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    raise SystemExit(main())
