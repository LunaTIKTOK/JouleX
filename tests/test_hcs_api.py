"""M3 — HCS audit-log + attestation HTTP routes (DESIGN.md §10).

DESIGN.md §10 lists five routes the M0/M1 server never implemented:
``GET /attestations``, ``GET /hcs-log``, ``GET /hcs-log/verify``,
``GET /hcs-log/knowledge`` and ``GET /hcs-log/provenance``. These
tests are the failing-first contract for them: read-only views over
the existing ``joulex/hcs.py`` query functions (``messages``,
``what_did_agent_know``, ``who_told_whom``, ``verify_log``) plus the
attestation envelopes already carried on the log messages. No new
state, no new writes — the log stays append-only.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from joulex.client import ExchangeClient
from joulex.exchange import Exchange
from joulex.server import make_server
from tests.conftest import T0, iso, make_market_payload, make_order_payload


@pytest.fixture()
def live(tmp_path, issuer, clock):
    """(client, exchange, clock) against a running server with demo clock."""
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


def raw_get(client, path):
    req = urllib.request.Request(client.base_url + path, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except ValueError:
            return exc.code, {"reason": raw.decode("utf-8", "replace")}


def seed_activity(client, agent_a, agent_b):
    """One market, one attested order (Alice), one plain order (Bob).
    Returns the market id."""
    m = client.create_market(
        agent_a["credential"], agent_a["agent_seed"], **make_market_payload()
    )
    mid = m["market_id"]
    client.place_order(
        agent_a["credential"], agent_a["agent_seed"],
        make_order_payload("agent-alice-1", mid, "YES", 10),
        attested=True,
    )
    client.place_order(
        agent_b["credential"], agent_b["agent_seed"],
        make_order_payload("agent-bob-1", mid, "NO", 20),
    )
    return mid


def test_hcs_log_empty(live):
    client, _, _ = live
    status, body = raw_get(client, "/hcs-log")
    assert status == 200, body
    assert body["messages"] == []
    assert body["total"] == 0
    status, body = raw_get(client, "/hcs-log/verify")
    assert status == 200, body
    assert body["ok"] is True
    assert body["messages"] == 0
    status, body = raw_get(client, "/attestations")
    assert status == 200, body
    assert body["attestations"] == []
    assert body["total"] == 0


def test_attestations_lists_envelopes(live, agent_a, agent_b):
    client, _, _ = live
    seed_activity(client, agent_a, agent_b)
    status, body = raw_get(client, "/attestations")
    assert status == 200, body
    # Only the attested order carries an envelope; the market creation
    # and the plain order do not.
    assert body["total"] == 1, body
    entry = body["attestations"][0]
    assert entry["action"] == "place_order"
    assert entry["sender"] == "agent-alice-1"
    assert entry["attestation"]["kind"] == "software"
    assert entry["attestation"]["version"] == "joulex-attest-0.1"
    assert entry["sequence"] >= 1
    assert "consensus_timestamp" in entry


def test_hcs_log_lists_messages_latest_first(live, agent_a, agent_b):
    client, _, _ = live
    seed_activity(client, agent_a, agent_b)
    status, body = raw_get(client, "/hcs-log")
    assert status == 200, body
    # create_market + 2 orders = 3 messages.
    assert body["total"] == 3, body
    seqs = [m["sequence"] for m in body["messages"]]
    assert seqs == sorted(seqs, reverse=True), "latest first"
    assert {m["action"] for m in body["messages"]} == {
        "create_market", "place_order",
    }
    for m in body["messages"]:
        assert m["topic"] == "joulex-actions"
        assert "message_hash" in m and "prev_hash" in m


def test_hcs_log_filter_by_agent_and_pagination(live, agent_a, agent_b):
    client, _, _ = live
    seed_activity(client, agent_a, agent_b)
    status, body = raw_get(client, "/hcs-log?agent_id=agent-alice-1")
    assert status == 200, body
    assert body["total"] == 2, body  # create_market + her order
    assert all(m["sender"] == "agent-alice-1" for m in body["messages"])
    status, body = raw_get(client, "/hcs-log?limit=1&offset=0")
    assert status == 200, body
    assert len(body["messages"]) == 1 and body["total"] == 3
    assert body["limit"] == 1 and body["offset"] == 0
    status, body = raw_get(client, "/hcs-log?limit=1&offset=2")
    assert status == 200, body
    assert len(body["messages"]) == 1
    assert body["messages"][0]["sequence"] == 1  # oldest, latest-first page 3
    status, body = raw_get(client, "/hcs-log?limit=abc")
    assert status == 400, body
    assert body["reason"].startswith("INVALID_REQUEST")


def test_hcs_log_verify_after_activity(live, agent_a, agent_b):
    client, _, _ = live
    seed_activity(client, agent_a, agent_b)
    status, body = raw_get(client, "/hcs-log/verify")
    assert status == 200, body
    assert body["ok"] is True
    assert body["messages"] == 3
    assert body["errors"] == []


def test_hcs_log_knowledge(live, agent_a, agent_b, clock):
    client, _, _ = live
    seed_activity(client, agent_a, agent_b)
    # NOTE: the "+" in "+00:00" must be percent-encoded in a query
    # string (parse_qs reads a bare "+" as a space) — quote it.
    at = urllib.parse.quote(iso(clock()), safe="")
    status, body = raw_get(
        client, f"/hcs-log/knowledge?agent_id=agent-alice-1&at={at}"
    )
    assert status == 200, body
    assert body["agent_id"] == "agent-alice-1"
    assert len(body["known_facts"]) >= 2, body  # her creation + her order
    assert len(body["messages"]) == 2
    # Missing params are 400, not a silent empty answer.
    status, body = raw_get(client, "/hcs-log/knowledge?agent_id=agent-alice-1")
    assert status == 400, body
    status, body = raw_get(client, f"/hcs-log/knowledge?at={at}")
    assert status == 400, body
    status, body = raw_get(
        client, "/hcs-log/knowledge?agent_id=agent-alice-1&at=not-a-time"
    )
    assert status == 400, body


def test_hcs_log_provenance(live, agent_a, agent_b):
    client, _, _ = live
    seed_activity(client, agent_a, agent_b)
    # Orders name told_by="exchange": the exchange told Alice (her
    # order) and Bob (his order). The creation names told_by="exchange"
    # too, so Alice has two exchange-told messages.
    status, body = raw_get(
        client, "/hcs-log/provenance?from_agent=exchange&to_agent=agent-alice-1"
    )
    assert status == 200, body
    assert len(body) == 2, body
    assert all(
        m["sender"] == "agent-alice-1"
        and m["provenance"]["told_by"] == "exchange"
        for m in body
    )
    assert [m["sequence"] for m in body] == sorted(m["sequence"] for m in body)
    # Nobody told Bob's agent anything under a name that never appears.
    status, body = raw_get(
        client, "/hcs-log/provenance?from_agent=nobody&to_agent=agent-bob-1"
    )
    assert status == 200, body
    assert body == []
    status, body = raw_get(client, "/hcs-log/provenance?from_agent=exchange")
    assert status == 400, body
    status, body = raw_get(client, "/hcs-log/provenance?to_agent=agent-alice-1")
    assert status == 400, body
