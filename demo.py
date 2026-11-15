#!/usr/bin/env python3
"""End-to-end demo: two bound agents trade two JouleX markets, they
settle, and the balances come out exactly right.

Runs the real HTTP service (subprocess, ``--demo-clock``) and drives it
over the real API with the client helper. All keys are generated fresh
for this run; the demo issuer's L3 attestation is asserted, not verified
(see DESIGN.md §4).

Flow:
  1. Mint demo L3 credentials for two distinct test principals/agents.
  2. Create one flex_capacity aeso_price market (pool price above
     $50/MWh in a 6h delivery window, resolves in 24h), admitted via
     Alice's CF-KYA creation presentation, plus one compute_forward
     public_source market (H100 spot above $4.00/GPU-hr), also
     admitted via Alice.
  3. Alice buys 10 YES, Bob buys 20 NO on the flex market — price
     moves 0.50 -> up -> down. On the compute market Alice buys 10
     YES, Bob buys 10 NO.
  4. Advance the demo clock past resolution, refresh the revocation list.
  5. Settle the flex market from a DEMO-STATED observed max pool
     price ($62.50/MWh — above the $50 strike, so YES wins). In
     production the AESO oracle fetches this from AESO (--aeso-oracle);
     the demo states it so the full cycle runs in seconds. A live AESO
     public-report spot check runs separately below as a connectivity
     proof. Settle the compute market with a settlement attestation
     envelope (YES: H100 spot $4.20/GPU-hr, evidence recorded) — since
     the 2026-11-11 settlement-auth fix, a public_source settlement
     REQUIRES a valid envelope bound to the settlement payload hash;
     Alice's credential mints it via the client helper.
  6. Assert exact final balances + ledger conservation, print everything.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from joulex.client import ClientError, ExchangeClient  # noqa: E402
from joulex.demo_issuer import DemoIssuer  # noqa: E402
from joulex.server import DemoClock, pick_port  # noqa: E402

PORT = pick_port(8472)
STRIKE = 50.0
OBSERVED_MAX_PRICE = 62.50  # DEMO-STATED max pool price (see module docstring)


def aeso_spot_check() -> None:
    """Live public-report connectivity proof (keyless). Best effort only:
    the demo does not depend on it."""
    try:
        from joulex.aeso import fetch_current_pool_price

        current = fetch_current_pool_price()
        print(f"  [live AESO check] latest hourly pool price "
              f"${current['price']:.2f}/MWh at {current['timestamp']} "
              f"(public ETS report reachable)")
    except Exception as exc:  # noqa: BLE001 - best effort by design
        print(f"  [live AESO check] unreachable ({exc}) — continuing with demo-stated value")


def wait_for_server(client: ExchangeClient, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            info = client.info()
            # Fail closed on identity, not just reachability: a
            # stranger on this port is not a JouleX server.
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
    clock = DemoClock(datetime.now(timezone.utc))
    issuer = DemoIssuer(now_fn=clock)
    tmp = tempfile.mkdtemp(prefix="joulex-demo-")
    data_dir = os.path.join(tmp, "data")
    trust_store_path = os.path.join(tmp, "trust_store.json")
    with open(trust_store_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "trusted_issuers": issuer.trust_store,
                "revocation_lists": {
                    issuer.issuer_id: issuer.revocation_list(),
                },
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
        print(f"joulex demo — service up at http://127.0.0.1:{PORT}")
        print(f"  server time: {client.server_time()} (demo clock)")
        print(f"  trust store: demo issuer {issuer.issuer_id} (DEMO attestation, not verified)")
        aeso_spot_check()

        # 1. Two distinct test principals/agents, real L3 credentials.
        alice = issuer.mint_agent(
            principal_id="principal-demo-alice",
            display_name="Alice Demo Principal",
            agent_id="agent-demo-alice",
        )
        bob = issuer.mint_agent(
            principal_id="principal-demo-bob",
            display_name="Bob Demo Principal",
            agent_id="agent-demo-bob",
        )
        print("  minted demo L3 credentials for agent-demo-alice and agent-demo-bob")

        # 2. One flex_capacity aeso_price market, resolving in 24h.
        #    Created by Alice through the admitted path: her credential
        #    covers create_market.
        server_now = datetime.fromisoformat(client.server_time())
        window_start = server_now + timedelta(days=1)
        window_end = window_start + timedelta(hours=6)
        market = client.create_market(
            alice["credential"], alice["agent_seed"],
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
        print(f"  market {mid}: {market['question']}")
        print(f"    created by: {market['created_by']}")
        print(f"    contract_type: {market['contract_type']}, "
              f"resolution: {market['resolution_type']} (strike ${STRIKE:.0f}/MWh)")
        print(f"    resolution source: {market['resolution_source_url']}")
        print(f"    price_yes at open: {market['price_yes']:.4f}")

        # 2b. One compute_forward public_source market, same admitted path.
        compute = client.create_market(
            alice["credential"], alice["agent_seed"],
            question="Will H100 spot be above $4.00/GPU-hr on "
                     f"{(server_now + timedelta(days=1)).date()}?",
            resolution_type="public_source",
            contract_type="compute_forward",
            source_url="https://example.com/gpu-prices",
            source_description="Published H100 spot price; YES iff above $4.00/GPU-hr.",
            resolves_at=(server_now + timedelta(days=1)).isoformat(),
            b=100.0,
        )
        mid_compute = compute["market_id"]
        print(f"  market {mid_compute}: {compute['question']}")
        print(f"    created by: {compute['created_by']}")
        print(f"    contract_type: {compute['contract_type']}, "
              f"resolution: {compute['resolution_type']}")
        print(f"    price_yes at open: {compute['price_yes']:.4f}")

        # 3. Orders from both agents (challenge -> presentation -> order).
        #    Alice's first order carries a software attestation envelope
        #    (M1, DESIGN.md §7.1): it proves which code tree minted the
        #    action and binds it to that code. It is NOT a hardware TEE
        #    attestation.
        r_alice = client.place_order(
            alice["credential"], alice["agent_seed"],
            {"agent_id": "agent-demo-alice", "market_id": mid,
             "side": "YES", "shares": 10, "action": "buy"},
            attested=True,
        )
        print(f"  Alice buys 10 YES for {r_alice['cost_points']:.6f} pts "
              f"-> price_yes {r_alice['price_yes_after']:.4f}")
        assert r_alice["attestation_status"] == "verified"
        assert r_alice["attestation"]["kind"] == "software"
        print(f"  [M1 attested] envelope joulex-attest-0.1/software, "
              f"measurement {r_alice['attestation']['measurement'][:16]}…, "
              f"status {r_alice['attestation_status']}")
        assert r_alice["price_yes_after"] > 0.5
        r_bob = client.place_order(
            bob["credential"], bob["agent_seed"],
            {"agent_id": "agent-demo-bob", "market_id": mid,
             "side": "NO", "shares": 20, "action": "buy"},
        )
        print(f"  Bob buys 20 NO for {r_bob['cost_points']:.6f} pts "
              f"-> price_yes {r_bob['price_yes_after']:.4f}")
        assert r_bob["price_yes_after"] < 0.5
        # 3b. Compute market trades: Alice 10 YES, Bob 10 NO.
        r_alice_c = client.place_order(
            alice["credential"], alice["agent_seed"],
            {"agent_id": "agent-demo-alice", "market_id": mid_compute,
             "side": "YES", "shares": 10, "action": "buy"},
        )
        print(f"  Alice buys 10 YES (compute) for {r_alice_c['cost_points']:.6f} pts "
              f"-> price_yes {r_alice_c['price_yes_after']:.4f}")
        assert r_alice_c["price_yes_after"] > 0.5
        r_bob_c = client.place_order(
            bob["credential"], bob["agent_seed"],
            {"agent_id": "agent-demo-bob", "market_id": mid_compute,
             "side": "NO", "shares": 10, "action": "buy"},
        )
        print(f"  Bob buys 10 NO (compute) for {r_bob_c['cost_points']:.6f} pts "
              f"-> price_yes {r_bob_c['price_yes_after']:.4f}")
        print(f"  balances after trading: Alice "
              f"{client.positions('agent-demo-alice')['balance_points']:.6f}, "
              f"Bob {client.positions('agent-demo-bob')['balance_points']:.6f}")

        # 4. Advance past resolution; refresh the revocation list at the
        #    new time (it would otherwise be stale: max age 3600s).
        new_time = server_now + timedelta(days=1, minutes=1)
        client.set_demo_time(new_time)
        clock.set(new_time)
        client.refresh_revocation_list(issuer.revocation_list())
        print(f"  clock advanced to {client.server_time()}; revocation list refreshed")

        # 5. Settle the flex market from the DEMO-STATED observed max
        #    pool price.
        receipt = client.settle(mid, observed_value=OBSERVED_MAX_PRICE)
        print(f"  settled flex market: outcome {receipt['outcome']} "
              f"(observed max pool price ${OBSERVED_MAX_PRICE:.2f}/MWh "
              f"vs strike ${STRIKE:.0f}/MWh)")
        for p in receipt["payouts"]:
            print(f"    payout: {p['agent_id']} {p['side']} x{p['shares']:.0f} "
                  f"-> {p['payout_points']:.6f} pts")

        # 5b. Settle the compute market, attested: public_source
        #     settlements require a valid attestation envelope bound
        #     to the settlement payload hash (2026-11-11 auth fix).
        #     Alice's credential mints it; the exchange verifies it
        #     before paying out.
        receipt_c = client.settle(
            mid_compute, outcome="YES",
            evidence="https://example.com/gpu-prices (H100 spot $4.20/GPU-hr)",
            credential=alice["credential"], attested=True)
        assert receipt_c["attestation_status"] == "verified"
        print(f"  settled compute market: outcome {receipt_c['outcome']} "
              f"(attested settlement, status "
              f"{receipt_c['attestation_status']}, evidence recorded)")
        for p in receipt_c["payouts"]:
            print(f"    payout: {p['agent_id']} {p['side']} x{p['shares']:.0f} "
                  f"-> {p['payout_points']:.6f} pts")

        # 6. Exact assertions.
        alice_final = client.positions("agent-demo-alice")["balance_points"]
        bob_final = client.positions("agent-demo-bob")["balance_points"]
        expected_alice = (1000.0 - r_alice["cost_points"] + 10.0  # 10 YES shares won (flex)
                          - r_alice_c["cost_points"] + 10.0)  # 10 YES shares won (compute)
        expected_bob = (1000.0 - r_bob["cost_points"]  # 20 NO shares lost (flex): 0
                        - r_bob_c["cost_points"])  # 10 NO shares lost (compute): 0
        assert abs(alice_final - expected_alice) < 1e-9, (alice_final, expected_alice)
        assert abs(bob_final - expected_bob) < 1e-9, (bob_final, expected_bob)
        ledger = client.ledger()
        assert ledger["conservation"]["ok"], ledger["conservation"]
        assert abs(ledger["conservation"]["total"] - 2000.0) < 1e-9
        print(f"  final balances: Alice {alice_final:.6f} (expected {expected_alice:.6f}), "
              f"Bob {bob_final:.6f} (expected {expected_bob:.6f})")
        print(f"  ledger conservation: total {ledger['conservation']['total']:.6f} "
              f"== expected 2000.000000; pool {ledger['pool']:.6f} (LMSR subsidy)")
        print("DEMO OK — balances exact, ledger conserved.")
        print(f"  (demo state kept at {data_dir}; server stopped)")
        return 0
    except ClientError as exc:
        print(f"DEMO FAILED: HTTP {exc.status}: {exc.payload}", file=sys.stderr)
        return 1
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    raise SystemExit(main())
