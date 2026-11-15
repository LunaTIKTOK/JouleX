"""Shared fixtures for joulex tests.

All tests use a fake clock so credential expiry, presentation freshness,
revocation freshness and market resolution are deterministic. All keys
are generated fresh per test run — no real key material anywhere.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from joulex.admission import ExchangeVerifier  # noqa: E402
from joulex.admission import creation_payload, creation_payload_hash  # noqa: E402
from joulex.demo_issuer import DemoIssuer  # noqa: E402
from joulex.exchange import Exchange  # noqa: E402

import kya as kya_module  # noqa: E402

T0 = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat()


class FakeClock:
    """Mutable clock. Tests advance it; the exchange and issuer read it."""

    def __init__(self, start: datetime = T0):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now = self.now + timedelta(**kwargs)

    def set(self, when: datetime) -> None:
        self.now = when


@pytest.fixture()
def clock():
    return FakeClock()


@pytest.fixture()
def issuer(clock):
    return DemoIssuer(now_fn=clock)


@pytest.fixture()
def agent_a(issuer):
    return issuer.mint_agent(
        principal_id="principal-alice-test",
        display_name="Alice Test Principal",
        agent_id="agent-alice-1",
    )


@pytest.fixture()
def agent_b(issuer):
    return issuer.mint_agent(
        principal_id="principal-bob-test",
        display_name="Bob Test Principal",
        agent_id="agent-bob-1",
    )


@pytest.fixture()
def verifier(issuer, clock):
    """Verifier with the demo trust store and a fresh revocation list."""
    v = ExchangeVerifier(trusted_issuers=issuer.trust_store, now_fn=clock)
    ok, reason = v.refresh_revocation_list(issuer.revocation_list())
    assert ok, reason
    return v


@pytest.fixture()
def exchange(tmp_path, issuer, clock):
    """Full Exchange wired to the demo issuer, fake clock, temp data dir."""
    ex = Exchange(
        data_dir=str(tmp_path / "data"),
        now_fn=clock,
        trusted_issuers=issuer.trust_store,
    )
    ok, reason = ex.verifier.refresh_revocation_list(issuer.revocation_list())
    assert ok, reason
    return ex


def make_market_payload(**overrides):
    payload = {
        "question": "Will the Alberta pool price exceed $50/MWh during the delivery window?",
        "resolution_type": "aeso_price",
        "contract_type": "flex_capacity",
        "strike": 50.0,
        "window_start": iso(T0 + timedelta(days=1)),
        "window_end": iso(T0 + timedelta(days=1, hours=6)),
        "resolves_at": iso(T0 + timedelta(days=1)),
        "b": 100.0,
    }
    payload.update(overrides)
    return payload


def make_public_market_payload(**overrides):
    payload = {
        "question": "Will H100 spot be above $4.00/GPU-hr on 2026-11-07?",
        "resolution_type": "public_source",
        "contract_type": "compute_forward",
        "source_url": "https://example.com/gpu-prices",
        "source_description": "Published H100 spot price; YES iff above $4.00/GPU-hr.",
        "resolves_at": iso(T0 + timedelta(days=1)),
        "b": 100.0,
    }
    payload.update(overrides)
    return payload


def make_order_payload(agent_id, market_id, side="YES", shares=10, action="buy"):
    return {
        "agent_id": agent_id,
        "market_id": market_id,
        "side": side,
        "shares": shares,
        "action": action,
    }


def signed_creation(bundle, challenge, fields, timestamp):
    """Agent-side: sign (challenge + creation-payload-hash + timestamp)."""
    payload = creation_payload(agent_id=bundle["agent_id"], **fields)
    return kya_module.create_presentation(
        bundle["credential"],
        agent_seed=bundle["agent_seed"],
        challenge=challenge,
        payload_hash=creation_payload_hash(payload),
        timestamp=timestamp,
    )


def create_market_admitted(exchange, bundle, fields, clock):
    """Full Exchange-level creation path: challenge -> presentation ->
    create_market. The bundle's credential must cover
    joulex.create_market (the default demo mint does)."""
    challenge = exchange.issue_challenge()["challenge"]
    pres = signed_creation(bundle, challenge, fields, iso(clock()))
    return exchange.create_market(pres, agent_id=bundle["agent_id"], **fields)
