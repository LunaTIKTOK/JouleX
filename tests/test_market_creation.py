"""Market-creation admission (M2): POST /markets requires CF-KYA.

Closes the documented v1 hole (DESIGN.md §11: open creation). Creation
uses the same admission as orders — L3 only, fresh single-use
challenge, presentation bound to the creation payload — or an operator
key. Every rejection the design promises is tested here, at the
verifier, Exchange, and HTTP levels.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from joulex.admission import CREATE_TOOL_NAME, creation_payload
from joulex.exchange import Exchange, ExchangeError
from joulex.server import make_server
from joulex.client import ExchangeClient
from tests.conftest import (
    T0,
    create_market_admitted,
    iso,
    make_market_payload,
    signed_creation,
)


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


def verify_creation_ok(verifier, bundle, challenge, fields, clock):
    pres = signed_creation(bundle, challenge, fields, iso(clock()))
    payload = creation_payload(agent_id=bundle["agent_id"], **fields)
    return verifier.verify_creation(pres, payload, agent_id=bundle["agent_id"])


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


# -- verifier level ------------------------------------------------------

def test_valid_l3_creation_verifies(verifier, agent_a, clock):
    challenge = verifier.issue_challenge()
    fields = make_market_payload()
    ok, reason, info = verify_creation_ok(verifier, agent_a, challenge, fields, clock)
    assert ok, reason
    assert reason == "OK"
    assert info["assurance_level"] == "L3"
    assert info["agent_id"] == "agent-alice-1"


def test_no_presentation_rejected(verifier, agent_a):
    payload = creation_payload(agent_id="agent-alice-1", **make_market_payload())
    ok, reason, _ = verifier.verify_creation(None, payload, agent_id="agent-alice-1")
    assert not ok
    assert reason == "KYA_PRESENTATION_MISSING"


def test_l1_credential_rejected_for_creation(issuer, verifier, clock):
    bundle = issuer.mint_agent(
        principal_id="principal-l1c-test",
        display_name="L1 Creation Test",
        agent_id="agent-l1c-1",
        assurance_level="L1",
    )
    ok, reason = verifier.refresh_revocation_list(
        issuer.revocation_list_for(
            issuer_id=bundle["principal_id"],
            issuer_seed=bundle["principal_seed"],
            issuer_public_key_hex=bundle["principal_public_key_hex"],
        ),
        issuer_public_key_hex=bundle["principal_public_key_hex"],
    )
    assert ok, reason
    challenge = verifier.issue_challenge()
    fields = make_market_payload()
    payload = creation_payload(agent_id="agent-l1c-1", **fields)
    pres = signed_creation(bundle, challenge, fields, iso(clock()))
    ok, reason, _ = verifier.verify_creation(pres, payload, agent_id="agent-l1c-1")
    assert not ok
    assert reason == "KYA_L1_REJECTED_CONSEQUENTIAL"


def test_l2_credential_rejected_for_creation(issuer, clock):
    from joulex.admission import ExchangeVerifier

    bundle = issuer.mint_agent(
        principal_id="principal-l2c-test",
        display_name="L2 Creation Test",
        agent_id="agent-l2c-1",
        assurance_level="L2",
        domain="l2ctest.example.invalid",
    )
    verifier = ExchangeVerifier(
        trusted_issuers=issuer.trust_store,
        domain_keys={"l2ctest.example.invalid": bundle["principal_public_key_hex"]},
        now_fn=clock,
    )
    ok, reason = verifier.refresh_revocation_list(issuer.revocation_list())
    assert ok, reason
    challenge = verifier.issue_challenge()
    fields = make_market_payload()
    ok, reason, _ = verify_creation_ok(verifier, bundle, challenge, fields, clock)
    assert not ok
    assert reason == "KYA_L2_INSUFFICIENT_CONSEQUENTIAL"


def test_creation_payload_mismatch_rejected(verifier, agent_a, clock):
    challenge = verifier.issue_challenge()
    signed_fields = make_market_payload(strike=999999)
    pres = signed_creation(agent_a, challenge, signed_fields, iso(clock()))
    payload = creation_payload(agent_id="agent-alice-1", **make_market_payload())
    ok, reason, _ = verifier.verify_creation(pres, payload, agent_id="agent-alice-1")
    assert not ok
    assert reason == "KYA_PAYLOAD_MISMATCH"


def test_replayed_creation_challenge_rejected(verifier, agent_a, clock):
    challenge = verifier.issue_challenge()
    fields = make_market_payload()
    ok, reason, _ = verify_creation_ok(verifier, agent_a, challenge, fields, clock)
    assert ok, reason
    ok, reason, _ = verify_creation_ok(verifier, agent_a, challenge, fields, clock)
    assert not ok
    assert reason == "KYA_CHALLENGE_REUSED"


def test_trade_only_credential_cannot_create(issuer, verifier, clock):
    bundle = issuer.mint_agent(
        principal_id="principal-scope-c-test",
        display_name="Scope Creation Test",
        agent_id="agent-scopec-1",
        tools=["joulex.place_order"],
        intent_classes=["TRADE"],
    )
    challenge = verifier.issue_challenge()
    fields = make_market_payload()
    ok, reason, _ = verify_creation_ok(verifier, bundle, challenge, fields, clock)
    assert not ok
    # The delegation chain itself does not cover joulex.create_market,
    # so the chain check fires first (delegation reason string).
    assert reason == "DELEGATION_SCOPE_EXCEEDED"


def test_credential_narrower_than_delegation_cannot_create(issuer, verifier, clock):
    # Delegation covers both tools, but the credential is scoped to
    # place_order only: the kya-level scope check fires.
    bundle = issuer.mint_agent(
        principal_id="principal-scope-c2-test",
        display_name="Scope Creation Test 2",
        agent_id="agent-scopec2-1",
        tools=["joulex.place_order", CREATE_TOOL_NAME],
        credential_tools=["joulex.place_order"],
        intent_classes=["TRADE"],
    )
    challenge = verifier.issue_challenge()
    fields = make_market_payload()
    ok, reason, _ = verify_creation_ok(verifier, bundle, challenge, fields, clock)
    assert not ok
    assert reason == "KYA_SCOPE_EXCEEDED"


def test_creation_challenge_not_issued_by_server_rejected(verifier, agent_a, clock):
    fields = make_market_payload()
    pres = signed_creation(agent_a, "deadbeef" * 4, fields, iso(clock()))
    payload = creation_payload(agent_id="agent-alice-1", **fields)
    ok, reason, _ = verifier.verify_creation(pres, payload, agent_id="agent-alice-1")
    assert not ok
    assert reason == "EXCHANGE_CHALLENGE_UNKNOWN"


def test_failed_creation_attempt_does_not_burn_challenge(verifier, agent_a, clock):
    # A failed verification (payload mismatch) must not consume the
    # challenge, so the agent can retry with a correct presentation.
    challenge = verifier.issue_challenge()
    wrong = make_market_payload(strike=999999)
    pres_bad = signed_creation(agent_a, challenge, wrong, iso(clock()))
    payload = creation_payload(agent_id="agent-alice-1", **make_market_payload())
    ok, _, _ = verifier.verify_creation(pres_bad, payload, agent_id="agent-alice-1")
    assert not ok
    ok, reason, _ = verify_creation_ok(
        verifier, agent_a, challenge, make_market_payload(), clock
    )
    assert ok, reason


# -- Exchange level ------------------------------------------------------

def test_create_market_without_presentation_is_401(exchange, agent_a):
    with pytest.raises(ExchangeError) as exc:
        exchange.create_market(agent_id="agent-alice-1", **make_market_payload())
    assert exc.value.status == 401
    assert exc.value.reason == "KYA_PRESENTATION_MISSING"
    assert exchange.list_markets() == []


def test_valid_l3_creation_records_creator(exchange, agent_a, clock):
    m = create_market_admitted(exchange, agent_a, make_market_payload(), clock)
    assert m["status"] == "OPEN"
    assert m["created_by"] == "agent-alice-1"
    assert m["price_yes"] == pytest.approx(0.5)
    listed = exchange.list_markets()
    assert len(listed) == 1
    assert listed[0]["created_by"] == "agent-alice-1"


def test_invalid_market_fields_rejected_before_admission(exchange, agent_a, clock):
    # resolves_at = now: invalid (must be >= 1 day out). The 400 fires
    # before verification, so the challenge is NOT consumed: the same
    # challenge then creates a valid market.
    bad = make_market_payload(resolves_at=iso(T0))
    challenge = exchange.issue_challenge()["challenge"]
    pres = signed_creation(agent_a, challenge, bad, iso(clock()))
    with pytest.raises(ExchangeError) as exc:
        exchange.create_market(pres, agent_id="agent-alice-1", **bad)
    assert exc.value.status == 400
    assert exc.value.reason.startswith("INVALID_MARKET")
    assert exchange.list_markets() == []

    good = make_market_payload()
    pres2 = signed_creation(agent_a, challenge, good, iso(clock()))
    m = exchange.create_market(pres2, agent_id="agent-alice-1", **good)
    assert m["status"] == "OPEN"


def test_operator_key_creates_market(tmp_path, issuer, clock):
    ex = Exchange(
        data_dir=str(tmp_path / "data-op"),
        now_fn=clock,
        trusted_issuers=issuer.trust_store,
        operator_key="op-secret-test",
    )
    m = ex.create_market(None, operator_key="op-secret-test", **make_market_payload())
    assert m["status"] == "OPEN"
    assert m["created_by"] == "operator"


def test_wrong_operator_key_is_401(tmp_path, issuer, clock):
    ex = Exchange(
        data_dir=str(tmp_path / "data-op2"),
        now_fn=clock,
        trusted_issuers=issuer.trust_store,
        operator_key="op-secret-test",
    )
    with pytest.raises(ExchangeError) as exc:
        ex.create_market(None, operator_key="wrong-key", **make_market_payload())
    assert exc.value.status == 401
    assert exc.value.reason == "OPERATOR_KEY_INVALID"
    assert ex.list_markets() == []


def test_operator_key_header_with_no_key_configured_is_401(exchange):
    with pytest.raises(ExchangeError) as exc:
        exchange.create_market(
            None, operator_key="any-key", **make_market_payload()
        )
    assert exc.value.status == 401
    assert exc.value.reason == "OPERATOR_KEY_INVALID"


# -- HTTP level ----------------------------------------------------------

def test_create_market_over_http_without_header_is_401(live):
    client, _, _ = live
    status, body = raw_request(
        client, "POST", "/markets", body=make_market_payload()
    )
    assert status == 401
    assert body["reason"] == "KYA_PRESENTATION_MISSING"


def test_create_market_over_http_with_credential(live, agent_a):
    client, _, _ = live
    m = client.create_market(
        agent_a["credential"], agent_a["agent_seed"], **make_market_payload()
    )
    assert m["status"] == "OPEN"
    assert m["created_by"] == "agent-alice-1"
    assert len(client.list_markets()) == 1


def test_replayed_creation_challenge_over_http_is_401(live, agent_a, clock):
    client, _, _ = live
    fields = make_market_payload()
    challenge = client.get_challenge()
    pres = client.sign_creation(
        agent_a["credential"], agent_a["agent_seed"],
        challenge=challenge["challenge"], fields=fields,
        agent_id="agent-alice-1", timestamp=challenge["server_time"],
    )
    from kya import presentation_to_header

    header = presentation_to_header(pres)
    body = dict(fields, agent_id="agent-alice-1")
    status, _ = raw_request(
        client, "POST", "/markets", body=body, headers={"X-CF-KYA": header}
    )
    assert status == 201
    status, body = raw_request(
        client, "POST", "/markets", body=body, headers={"X-CF-KYA": header}
    )
    assert status == 401
    assert body["reason"] == "KYA_CHALLENGE_REUSED"


def test_operator_key_wins_over_bad_kya_header(tmp_path, issuer, clock):
    # Both headers present: the operator key is authoritative — a valid
    # key creates the market even with a garbage KYA header, and a
    # wrong key is 401 with no KYA fallback.
    data_dir = str(tmp_path / "data-op-both")
    ex = Exchange(
        data_dir=data_dir,
        now_fn=clock,
        trusted_issuers=issuer.trust_store,
        operator_key="op-secret-both",
    )
    ex.verifier.refresh_revocation_list(issuer.revocation_list())
    server = make_server(ex, host="127.0.0.1", port=0, demo_clock=clock)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        client = ExchangeClient(f"http://127.0.0.1:{port}")
        status, body = raw_request(
            client, "POST", "/markets", body=make_market_payload(),
            headers={"X-Operator-Key": "op-secret-both", "X-CF-KYA": "garbage"},
        )
        assert status == 201
        assert body["created_by"] == "operator"
        status, body = raw_request(
            client, "POST", "/markets", body=make_market_payload(),
            headers={"X-Operator-Key": "wrong-key", "X-CF-KYA": "garbage"},
        )
        assert status == 401
        assert body["reason"] == "OPERATOR_KEY_INVALID"
    finally:
        server.shutdown()
        server.server_close()


def test_create_market_over_http_with_operator_key(tmp_path, issuer, clock):
    data_dir = str(tmp_path / "data-op-http")
    ex = Exchange(
        data_dir=data_dir,
        now_fn=clock,
        trusted_issuers=issuer.trust_store,
        operator_key="op-secret-http",
    )
    ex.verifier.refresh_revocation_list(issuer.revocation_list())
    server = make_server(ex, host="127.0.0.1", port=0, demo_clock=clock)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        client = ExchangeClient(f"http://127.0.0.1:{port}")
        status, body = raw_request(
            client, "POST", "/markets", body=make_market_payload(),
            headers={"X-Operator-Key": "op-secret-http"},
        )
        assert status == 201
        assert body["created_by"] == "operator"
        status, body = raw_request(
            client, "POST", "/markets", body=make_market_payload(),
            headers={"X-Operator-Key": "wrong-key"},
        )
        assert status == 401
        assert body["reason"] == "OPERATOR_KEY_INVALID"
    finally:
        server.shutdown()
        server.server_close()
