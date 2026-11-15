"""HTTP API tests: the full flow over the real stdlib server.

The server runs in-process on an ephemeral port with the demo clock
enabled, so the whole create -> challenge -> order -> settle cycle runs
over HTTP exactly as demo.py and demo.html do it.
"""

from __future__ import annotations

import json
import threading
import urllib.request
import urllib.error
from datetime import timedelta

import pytest

from joulex.client import ClientError, ExchangeClient
from joulex.exchange import Exchange
from joulex.server import make_server
from tests.conftest import T0, iso, make_market_payload, make_order_payload


@pytest.fixture()
def live(tmp_path, issuer, clock):
    """(client, exchange, clock) against a running server with demo clock.

    The server's demo clock IS the fake clock, so POST /__demo/set_time
    moves exchange time too (same object as the Exchange's now_fn).
    """
    data_dir = str(tmp_path / "data")
    ex = Exchange(data_dir=data_dir, now_fn=clock, trusted_issuers=issuer.trust_store)
    ex.verifier.refresh_revocation_list(issuer.revocation_list())
    server = make_server(ex, host="127.0.0.1", port=0, demo_clock=clock)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    client = ExchangeClient(f"http://127.0.0.1:{port}")
    yield client, ex, clock
    server.shutdown()
    server.server_close()


def raw_request(client, method, path, body=None, headers=None):
    req = urllib.request.Request(
        client.base_url + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_service_info(live):
    client, _, _ = live
    info = client.info()
    assert info["paper_only"] is True
    assert info["demo_clock"] is True
    assert info["name"] == "joulex"


def test_create_and_list_markets_over_http(live, agent_a):
    client, _, _ = live
    assert client.list_markets() == []
    m = client.create_market(
        agent_a["credential"], agent_a["agent_seed"], **make_market_payload()
    )
    assert m["status"] == "OPEN"
    assert m["price_yes"] == pytest.approx(0.5)
    markets = client.list_markets()
    assert len(markets) == 1
    assert markets[0]["market_id"] == m["market_id"]


def test_full_http_flow_create_trade_settle(live, issuer, agent_a, agent_b, clock):
    client, ex, _ = live
    m = client.create_market(
        agent_a["credential"], agent_a["agent_seed"], **make_market_payload()
    )
    mid = m["market_id"]

    r1 = client.place_order(
        agent_a["credential"], agent_a["agent_seed"],
        make_order_payload("agent-alice-1", mid, "YES", 10),
    )
    assert r1["side"] == "YES" and r1["shares"] == 10
    r2 = client.place_order(
        agent_b["credential"], agent_b["agent_seed"],
        make_order_payload("agent-bob-1", mid, "NO", 20),
    )
    assert r2["side"] == "NO"

    pos = client.positions("agent-alice-1")
    assert pos["balance_points"] == pytest.approx(1000.0 - r1["cost_points"])
    assert pos["positions"][0]["shares"] == pytest.approx(10.0)

    # Advance the demo clock past resolution, then settle from the
    # stated max pool price over the window (operator path — no oracle
    # on this server).
    client.set_demo_time(iso(T0 + timedelta(days=1, minutes=1)))
    receipt = client.settle(mid, observed_value=101000)
    assert receipt["outcome"] == "YES"
    payouts = {p["agent_id"]: p["payout_points"] for p in receipt["payouts"]}
    assert payouts["agent-alice-1"] == pytest.approx(10.0)
    assert payouts["agent-bob-1"] == pytest.approx(0.0)

    ledger = client.ledger()
    assert ledger["conservation"]["ok"]
    assert ledger["conservation"]["total"] == pytest.approx(2000.0)


def test_order_without_header_is_401(live, agent_a):
    client, ex, _ = live
    m = client.create_market(
        agent_a["credential"], agent_a["agent_seed"], **make_market_payload()
    )
    payload = make_order_payload("agent-alice-1", m["market_id"])
    status, body = raw_request(client, "POST", "/orders", body=payload)
    assert status == 401
    assert body["reason"] == "KYA_PRESENTATION_MISSING"


def test_replayed_challenge_over_http_is_401(live, agent_a, clock):
    client, _, _ = live
    m = client.create_market(
        agent_a["credential"], agent_a["agent_seed"], **make_market_payload()
    )
    payload = make_order_payload("agent-alice-1", m["market_id"])
    challenge = client.get_challenge()
    pres = client.sign_order(
        agent_a["credential"], agent_a["agent_seed"],
        challenge=challenge["challenge"], payload=payload,
        timestamp=challenge["server_time"],
    )
    from kya import presentation_to_header

    header = presentation_to_header(pres)
    status, _ = raw_request(
        client, "POST", "/orders", body=payload,
        headers={"X-CF-KYA": header},
    )
    assert status == 200
    status, body = raw_request(
        client, "POST", "/orders", body=payload,
        headers={"X-CF-KYA": header},
    )
    assert status == 401
    assert body["reason"] == "KYA_CHALLENGE_REUSED"


def test_demo_clock_endpoint_absent_without_flag(tmp_path, issuer, clock):
    data_dir = str(tmp_path / "data2")
    ex = Exchange(data_dir=data_dir, now_fn=clock, trusted_issuers=issuer.trust_store)
    server = make_server(ex, host="127.0.0.1", port=0, demo_clock=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        client = ExchangeClient(f"http://127.0.0.1:{port}")
        status, _ = raw_request(client, "POST", "/__demo/set_time", body={"time": iso(T0)})
        assert status == 404
        assert client.info()["demo_clock"] is False
    finally:
        server.shutdown()
        server.server_close()


def test_unknown_market_404(live):
    client, _, _ = live
    status, _ = raw_request(client, "GET", "/markets/nope")
    assert status == 404
