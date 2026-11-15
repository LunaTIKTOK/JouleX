"""M1.6 core workflow E2E — LOCAL ONLY (no Hedera, no live money).

Drives the real HTTP server end to end, exactly as MILESTONES.md scopes
M1.6: create account -> view market -> place/reserve -> settlement
record -> verify in ledger/audit trail, with persistence across
requests AND across a full server restart on the same data dir.

Run: python3 scripts/m1_6_e2e.py
Exit 0 + "M1.6 E2E OK" only if every step passes. Uses a fresh temp
data dir and fresh demo keys; nothing touches the repo's data/ dir.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from joulex.client import ExchangeClient  # noqa: E402
from joulex.demo_issuer import DemoIssuer  # noqa: E402
from joulex.exchange import Exchange  # noqa: E402
from joulex.server import make_server  # noqa: E402
from tests.conftest import FakeClock, T0, iso, make_market_payload, make_order_payload  # noqa: E402


def raw(client, method, path, body=None, headers=None):
    req = urllib.request.Request(
        client.base_url + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read() or b"{}"
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, {"_raw": body.decode("utf-8", "replace")[:200]}
    except urllib.error.HTTPError as e:
        body = e.read() or b"{}"
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"_raw": body.decode("utf-8", "replace")[:200]}


def check(name, cond, detail=""):
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        raise AssertionError(name)


def boot(data_dir, clock, issuer):
    ex = Exchange(data_dir=data_dir, now_fn=clock, trusted_issuers=issuer.trust_store)
    ok, reason = ex.verifier.refresh_revocation_list(issuer.revocation_list())
    assert ok, reason
    server = make_server(ex, host="127.0.0.1", port=0, demo_clock=clock)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return ex, server, ExchangeClient(f"http://127.0.0.1:{server.server_address[1]}")


def main():
    clock = FakeClock()
    issuer = DemoIssuer(now_fn=clock)
    alice = issuer.mint_agent(
        principal_id="principal-alice-e2e", display_name="Alice E2E",
        agent_id="agent-alice-e2e",
    )
    data_dir = tempfile.mkdtemp(prefix="joulex-m16-")
    print(f"M1.6 E2E — data dir {data_dir} (fresh temp dir)")

    ex, server, client = boot(data_dir, clock, issuer)
    try:
        print("1. health + web UI")
        info = client.info()
        check("paper_only", info["paper_only"] is True)
        check("GET /app serves UI", raw(client, "GET", "/app")[0] == 200)

        print("2. human account: register -> login -> me")
        acct = client.register_account(
            email="alice@example.com", password="correct-horse-battery-staple-1",
            display_name="Alice E2E", role="buyer",
        )
        check("register returns account_id", bool(acct.get("account_id")), acct.get("account_id", ""))
        sess = client.login(email="alice@example.com", password="correct-horse-battery-staple-1")
        token = sess["session_token"]
        check("login returns session", bool(token))
        me = client.me(token)
        check("me() round-trips", me["email"] == "alice@example.com", me["email"])

        print("3. view markets (empty) -> create market (CF-KYA agent)")
        check("markets start empty", client.list_markets() == [])
        m = client.create_market(alice["credential"], alice["agent_seed"], **make_market_payload())
        mid = m["market_id"]
        check("market OPEN", m["status"] == "OPEN", mid)
        check("flex_capacity / aeso_price", m["contract_type"] == "flex_capacity")
        markets = client.list_markets()
        check("market visible in list", len(markets) == 1 and markets[0]["market_id"] == mid)
        check("market detail fetch", client.get_market(mid)["market_id"] == mid)

        print("4. link agent to human account (CF-KYA, L3)")
        linked = client.link_agent(token, alice["credential"], alice["agent_seed"],
                                   account_id=acct["account_id"])
        check("link ok", linked.get("linked") is True or linked.get("account_id") == acct["account_id"], str(linked)[:120])

        print("5. place order (reserve) via linked agent")
        r = client.place_order(
            alice["credential"], alice["agent_seed"],
            make_order_payload("agent-alice-e2e", mid, "YES", 10),
        )
        check("order filled", r["side"] == "YES" and r["shares"] == 10, f"cost {r['cost_points']}")
        pos = client.positions("agent-alice-e2e")
        check("balance deducted", abs(pos["balance_points"] - (1000.0 - r["cost_points"])) < 1e-6,
              f"balance {pos['balance_points']}")
        check("position held", pos["positions"][0]["shares"] == 10)

        print("6. settle (operator observed value, demo clock past resolution)")
        client.set_demo_time(iso(T0 + timedelta(days=1, minutes=1)))
        receipt = client.settle(mid, observed_value=101000)
        check("outcome YES", receipt["outcome"] == "YES")
        payouts = {p["agent_id"]: p["payout_points"] for p in receipt["payouts"]}
        check("alice payout 10", payouts["agent-alice-e2e"] == 10.0, str(payouts))
        sid = receipt["settlement_id"]
        print(f"    settlement_id {sid}")

        print("7. verify in ledger + audit trail")
        ledger = client.ledger()
        check("ledger conservation ok", ledger["conservation"]["ok"])
        check("ledger total 1000.0 (single funded agent)",
              abs(ledger["conservation"]["total"] - 1000.0) < 1e-6,
              f"total {ledger['conservation']['total']}")
        status, audit = raw(client, "GET", f"/audit/{sid}")
        check("GET /audit/<id> 200", status == 200, f"status {status}")
        print("    audit body:", json.dumps(audit)[:600])
        check("audit VERIFIED", audit.get("verdict") == "VERIFIED", str(audit.get("verdict")))
        status, hcs = raw(client, "GET", "/hcs-log/verify")
        check("HCS log verifies", status == 200 and hcs.get("ok") is True, str(hcs)[:120])
        status, log = raw(client, "GET", "/hcs-log")
        kinds = {msg.get("action") for msg in log.get("messages", [])}
        check("register+link+settle audited", {"account_register", "account_link_agent", "settle_market"} <= kinds, str(sorted(kinds)))

        print("8. persistence across a full server restart (same data dir)")
    finally:
        server.shutdown()
        server.server_close()

    ex2, server2, client2 = boot(data_dir, clock, issuer)
    try:
        sess2 = client2.login(email="alice@example.com", password="correct-horse-battery-staple-1")
        check("login still works after restart", bool(sess2.get("session_token")))
        check("market survives restart", len(client2.list_markets()) == 1)
        m2 = client2.get_market(mid)
        check("market still RESOLVED", m2["status"] == "RESOLVED", m2["status"])
        check("outcome persists", m2.get("outcome") == "YES", str(m2.get("outcome")))
        ledger2 = client2.ledger()
        check("ledger still conserved", ledger2["conservation"]["ok"]
              and abs(ledger2["conservation"]["total"] - 1000.0) < 1e-6)
        status, audit2 = raw(client2, "GET", f"/audit/{sid}")
        check("audit still VERIFIED after restart", status == 200 and audit2.get("verdict") == "VERIFIED",
              str(audit2.get("verdict")))
        status, hcs2 = raw(client2, "GET", "/hcs-log/verify")
        check("HCS log still verifies after restart", status == 200 and hcs2.get("ok") is True)
    finally:
        server2.shutdown()
        server2.server_close()

    print("M1.6 E2E OK")


if __name__ == "__main__":
    main()
