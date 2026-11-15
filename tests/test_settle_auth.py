"""Settlement authentication — owner-ordered fix (2026-11-11).

Before this fix, ``POST /markets/<id>/settle`` performed NO
authentication at all: no ``X-Operator-Key`` check, no CF-KYA parsing,
and the settlement attestation was advisory — any caller could settle
any ``public_source`` market with any outcome plus an evidence string,
and any ``aeso_price`` market (oracle off) with a self-supplied
``observed_value``.

Policy now (fail closed, mirroring ``Exchange.create_market``'s
operator-key semantics):

- Operator key configured on the exchange -> settle REQUIRES a
  matching ``X-Operator-Key`` header. Missing or wrong key is 401
  ``OPERATOR_KEY_INVALID``; the market stays OPEN and nothing changes.
  A key presented when none is configured is likewise 401.
- ``public_source`` settlements REQUIRE a present AND valid
  attestation envelope bound to the settlement payload hash (absent
  is 401 ``JOULEX_VC_REQUIRED``), regardless of operator key.
- ``aeso_price`` with an oracle: unchanged — the fetched series wins,
  caller values ignored; the operator-key gate still applies first.
"""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from joulex.attestation import mint_envelope, payload_hash_for_settlement
from joulex.client import ClientError, ExchangeClient
from joulex.exchange import Exchange, ExchangeError
from joulex.server import make_server
from tests.conftest import (
    T0,
    create_market_admitted,
    iso,
    make_market_payload,
    make_public_market_payload,
)
from tests.test_api import raw_request

OP_KEY = "op-secret-settle"
EVIDENCE = "https://example.com/gpu-prices (H100 spot $4.20/GPU-hr)"


def _live(tmp_path, issuer, clock, *, operator_key=None):
    """(client, exchange) against a running server with demo clock,
    optionally configured with an operator key."""
    ex = Exchange(
        data_dir=str(tmp_path / "data"),
        now_fn=clock,
        trusted_issuers=issuer.trust_store,
        operator_key=operator_key,
    )
    ex.verifier.refresh_revocation_list(issuer.revocation_list())
    server = make_server(ex, host="127.0.0.1", port=0, demo_clock=clock)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = ExchangeClient(f"http://127.0.0.1:{server.server_address[1]}")
    return client, ex, server


@pytest.fixture()
def live_keyed(tmp_path, issuer, clock):
    client, ex, server = _live(tmp_path, issuer, clock, operator_key=OP_KEY)
    yield client, ex, clock
    server.shutdown()
    server.server_close()


@pytest.fixture()
def live_unkeyed(tmp_path, issuer, clock):
    client, ex, server = _live(tmp_path, issuer, clock)
    yield client, ex, clock
    server.shutdown()
    server.server_close()


def _aeso_market_ready(client, agent_a):
    """Create an aeso_price market over HTTP and advance the demo
    clock past resolution, so only the settle auth stands in the way."""
    m = client.create_market(
        agent_a["credential"], agent_a["agent_seed"], **make_market_payload()
    )
    client.set_demo_time(iso(T0 + timedelta(days=1, minutes=1)))
    return m["market_id"]


def _settle_envelope(market_id, *, outcome, observed_value, evidence, clock):
    """A valid settlement envelope + binding, minted the same way
    tests/test_attestation_wiring.py does (settler-side inputs)."""
    ph = payload_hash_for_settlement(
        market_id=market_id, outcome=outcome,
        observed_value=observed_value, evidence=evidence,
    )
    env = mint_envelope(
        credential_id="joulex-settlement",
        challenge_nonce=f"settle-{market_id}",
        payload_hash=ph,
        now=clock(),
    )
    binding = {
        "credential_id": "joulex-settlement",
        "challenge_nonce": f"settle-{market_id}",
    }
    return env, binding


# -- operator key on /settle (HTTP) --------------------------------------


def test_settle_without_operator_key_is_401_and_market_stays_open(
    live_keyed, agent_a
):
    client, ex, _ = live_keyed
    mid = _aeso_market_ready(client, agent_a)
    status, body = raw_request(
        client, "POST", f"/markets/{mid}/settle", body={"observed_value": 62.5}
    )
    assert status == 401
    assert body["reason"] == "OPERATOR_KEY_INVALID"
    assert client.get_market(mid)["status"] == "OPEN"
    # No state change: the same settle with the key still succeeds.
    receipt = client.settle(mid, observed_value=62.5, operator_key=OP_KEY)
    assert receipt["outcome"] == "YES"


def test_settle_with_wrong_operator_key_is_401_and_market_stays_open(
    live_keyed, agent_a
):
    client, ex, _ = live_keyed
    mid = _aeso_market_ready(client, agent_a)
    status, body = raw_request(
        client, "POST", f"/markets/{mid}/settle",
        body={"observed_value": 62.5},
        headers={"X-Operator-Key": "wrong-key"},
    )
    assert status == 401
    assert body["reason"] == "OPERATOR_KEY_INVALID"
    assert client.get_market(mid)["status"] == "OPEN"


def test_settle_with_correct_operator_key_settles(live_keyed, agent_a):
    client, ex, _ = live_keyed
    mid = _aeso_market_ready(client, agent_a)
    receipt = client.settle(mid, observed_value=62.5, operator_key=OP_KEY)
    assert receipt["outcome"] == "YES"
    assert client.get_market(mid)["status"] == "RESOLVED"


def test_settle_key_presented_when_none_configured_is_401(
    live_unkeyed, agent_a
):
    """Mirror of create_market: a presented key is authoritative —
    when no key is configured, any presented key is invalid."""
    client, ex, _ = live_unkeyed
    mid = _aeso_market_ready(client, agent_a)
    status, body = raw_request(
        client, "POST", f"/markets/{mid}/settle",
        body={"observed_value": 62.5},
        headers={"X-Operator-Key": "any-key"},
    )
    assert status == 401
    assert body["reason"] == "OPERATOR_KEY_INVALID"
    assert client.get_market(mid)["status"] == "OPEN"


# -- public_source attestation requirement (exchange level) --------------


def _public_market_ready(exchange, agent_a, clock):
    market = create_market_admitted(
        exchange, agent_a, make_public_market_payload(), clock
    )
    clock.advance(days=1, minutes=1)
    return market["market_id"]


def test_public_source_settle_without_attestation_is_401(
    exchange, agent_a, clock
):
    mid = _public_market_ready(exchange, agent_a, clock)
    with pytest.raises(ExchangeError) as exc:
        exchange.settle_market(mid, outcome="YES", evidence=EVIDENCE)
    assert exc.value.status == 401
    assert exc.value.reason == "JOULEX_VC_REQUIRED"
    assert exchange.get_market(mid)["status"] == "OPEN"


def test_public_source_settle_with_valid_attestation_settles(
    exchange, agent_a, clock
):
    mid = _public_market_ready(exchange, agent_a, clock)
    env, binding = _settle_envelope(
        mid, outcome="YES", observed_value=None, evidence=EVIDENCE, clock=clock
    )
    receipt = exchange.settle_market(
        mid, outcome="YES", evidence=EVIDENCE,
        attestation=env, attestation_binding=binding,
    )
    assert receipt["outcome"] == "YES"
    assert receipt["attestation_status"] == "verified"
    assert exchange.get_market(mid)["status"] == "RESOLVED"


def test_public_source_settle_with_tampered_attestation_is_401(
    exchange, agent_a, clock
):
    mid = _public_market_ready(exchange, agent_a, clock)
    # Envelope bound to a DIFFERENT settlement (outcome NO): stapling
    # it to this YES settlement is a binding failure.
    env, binding = _settle_envelope(
        mid, outcome="NO", observed_value=None, evidence=EVIDENCE, clock=clock
    )
    with pytest.raises(ExchangeError) as exc:
        exchange.settle_market(
            mid, outcome="YES", evidence=EVIDENCE,
            attestation=env, attestation_binding=binding,
        )
    assert exc.value.status == 401
    assert exc.value.reason == "JOULEX_VC_BINDING"
    assert exchange.get_market(mid)["status"] == "OPEN"


# -- public_source attestation over HTTP, via the client helper ----------


def test_public_source_settle_over_http_requires_attestation(
    live_unkeyed, agent_a
):
    client, ex, _ = live_unkeyed
    m = client.create_market(
        agent_a["credential"], agent_a["agent_seed"],
        **make_public_market_payload(),
    )
    mid = m["market_id"]
    client.set_demo_time(iso(T0 + timedelta(days=1, minutes=1)))
    # Bare settle (evidence only) is refused now.
    with pytest.raises(ClientError) as exc:
        client.settle(mid, outcome="YES", evidence=EVIDENCE)
    assert exc.value.status == 401
    assert client.get_market(mid)["status"] == "OPEN"
    # Attested settle (the demo's path) settles and records verified.
    receipt = client.settle(
        mid, outcome="YES", evidence=EVIDENCE,
        credential=agent_a["credential"], attested=True,
    )
    assert receipt["outcome"] == "YES"
    assert receipt["attestation_status"] == "verified"
    assert client.get_market(mid)["status"] == "RESOLVED"


# -- operator key still gates the oracle path ----------------------------


def test_keyed_oracle_settle_requires_key_and_uses_fetched_values(
    tmp_path, issuer, clock, agent_a, agent_b
):
    from joulex.aeso import AESOPriceOracle
    from tests.test_joulex_markets import (
        W_END,
        W_START,
        _stub_text_fetcher,
        _trade_and_advance,
    )

    ex = Exchange(
        data_dir=str(tmp_path / "kdata"),
        now_fn=clock,
        trusted_issuers=issuer.trust_store,
        operator_key=OP_KEY,
    )
    ok, reason = ex.verifier.refresh_revocation_list(issuer.revocation_list())
    assert ok, reason
    mid = _trade_and_advance(
        ex, agent_a, agent_b, clock,
        window_start=W_START, window_end=W_END, strike=20.0,
    )
    oracle = AESOPriceOracle(fetcher=_stub_text_fetcher())
    # No key: refused even though the oracle would settle it.
    with pytest.raises(ExchangeError) as exc:
        ex.settle_market(mid, oracle=oracle)
    assert exc.value.status == 401
    assert exc.value.reason == "OPERATOR_KEY_INVALID"
    assert ex.get_market(mid)["status"] == "OPEN"
    # With the key: the fetched series wins; the caller value (1.0,
    # which would resolve NO) is ignored, exactly as before.
    receipt = ex.settle_market(
        mid, observed_value=1.0, oracle=oracle, operator_key=OP_KEY
    )
    assert receipt["outcome"] == "YES"
    assert receipt["observed_value"] == pytest.approx(20.38)
