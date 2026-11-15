"""Commercial observability — Exchange.info() reports the live HCS
mode (audit 2026-11-12: it hardcoded "local"), /health + /metrics
endpoints, request-body cap, JSON request logging.

No real key material anywhere; the hedera-mode test builds an
Exchange against a RealHCS whose runner is never invoked at
construction (no action is submitted until an order/market/settlement
happens).
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from joulex.client import ExchangeClient
from joulex.exchange import Exchange
from joulex.server import make_server
from tests.conftest import T0, create_market_admitted, make_market_payload, make_order_payload


@pytest.fixture()
def live(tmp_path, issuer, clock):
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


def _raw_get(client, path):
    req = urllib.request.Request(client.base_url + path, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _raw_post(client, path, body: bytes, content_type="application/json"):
    req = urllib.request.Request(
        client.base_url + path, data=body, method="POST",
        headers={"Content-Type": content_type},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_info_reports_local_hcs_mode(exchange):
    vc = exchange.info()["verifiable_compute"]
    assert vc["hcs_mode"] == "local"
    assert vc["hcs_topic"] is None
    assert vc["hcs_network"] is None
    assert vc["kind"] == "software"


def test_info_reports_hedera_mode(tmp_path, issuer, clock, monkeypatch):
    import os

    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "hcs_config.json"), "w") as f:
        json.dump({"mode": "hedera", "hedera_topic": "0.0.11399235", "network": "testnet"}, f)
    monkeypatch.setenv("JOULEX_HCS_MODE", "hedera")
    monkeypatch.setenv("JOULEX_HCS_OPERATOR", "0.0.11389089")
    monkeypatch.setenv("JOULEX_HCS_PRIVATE_KEY", "0x" + "ab" * 32)
    ex = Exchange(data_dir=data_dir, now_fn=clock, trusted_issuers=issuer.trust_store)
    vc = ex.info()["verifiable_compute"]
    assert vc["hcs_mode"] == "hedera"
    assert vc["hcs_topic"] == "0.0.11399235"
    assert vc["hcs_network"] == "testnet"


def test_metrics_counters(live, agent_a):
    client, ex, clock = live
    m0 = ex.metrics()
    assert m0["orders"] == 0 and m0["markets_created"] == 0 and m0["settlements"] == 0
    assert m0["hcs"]["messages"] == 0
    market = create_market_admitted(ex, agent_a, make_market_payload(), clock)
    receipt = client.place_order(
        agent_a["credential"], agent_a["agent_seed"],
        make_order_payload(agent_a["agent_id"], market["market_id"]),
    )
    assert receipt["trade_id"].startswith("trd_")
    m1 = ex.metrics()
    assert m1["markets_created"] == 1
    assert m1["orders"] == 1
    assert m1["hcs"]["messages"] == 2
    assert m1["uptime_seconds"] >= 0


def test_health_endpoint(live):
    client, ex, _ = live
    status, body = _raw_get(client, "/health")
    assert status == 200
    assert body["status"] == "ok"
    assert body["hcs_mode"] == "local"
    assert body["markets"] == 0
    assert body["hcs_messages"] == 0
    assert body["hcs_failed_submits"] == 0
    assert body["uptime_seconds"] >= 0


def test_metrics_endpoint(live, agent_a):
    client, ex, clock = live
    status, body = _raw_get(client, "/metrics")
    assert status == 200
    assert body["orders"] == 0
    market = create_market_admitted(ex, agent_a, make_market_payload(), clock)
    client.place_order(
        agent_a["credential"], agent_a["agent_seed"],
        make_order_payload(agent_a["agent_id"], market["market_id"]),
    )
    _, body = _raw_get(client, "/metrics")
    assert body["orders"] == 1
    assert body["markets_created"] == 1
    assert body["hcs"]["messages"] == 2


def test_body_too_large_rejected(live):
    client, _, _ = live
    status, body = _raw_post(client, "/orders", b"x" * (1_000_001))
    assert status == 413
    assert "too_large" in body["reason"].lower()


def test_body_too_large_rejected_other_route(live):
    # Regression (2026-11-13): the 413 was raised without draining
    # the body, so the client saw a broken pipe instead of the 413
    # under full-suite load. _read_body is shared by all POST routes;
    # /revocation-lists reads the body before any auth, so it covers
    # the same path on a second route.
    client, _, _ = live
    status, body = _raw_post(client, "/revocation-lists", b"x" * (1_000_001))
    assert status == 413
    assert "too_large" in body["reason"].lower()
    # The server stays usable after the refusal (no poisoned state).
    status, body = _raw_get(client, "/health")
    assert status == 200
    assert body["status"] == "ok"
