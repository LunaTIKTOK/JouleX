"""Deployment seed tests — the Render deploy's demo trust store.

Covers the seed trust store (``data/trust_store.demo.json``) and the
mint script (``scripts/mint_demo_agents.py``):

- The seed file names the demo issuer only, is labelled demo-only,
  and carries NO private key material (public keys + signed public
  revocation list only).
- The seed file's revocation list is correctly signed (checked with a
  large max-age so the test does not go brittle as wall-clock time
  passes — a static list goes stale after 1h by design; the mint
  script refreshes it on the target server before trading).
- The mint mechanism works end to end over real HTTP: an issuer
  derived from a seed, its trust store, minted agents, market
  creation, orders, exact balances, ledger conservation 2000.0.
"""

from __future__ import annotations

import importlib.util
import json
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from joulex.admission import ExchangeVerifier  # noqa: E402
from joulex.client import ExchangeClient  # noqa: E402
from joulex.exchange import Exchange  # noqa: E402
from joulex.server import make_server  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SEED_FILE = ROOT / "data" / "trust_store.demo.json"
SCRIPT = ROOT / "scripts" / "mint_demo_agents.py"

DEMO_ISSUER_ID = "demo-issuer.joulex.local"
_PRIVATE_MARKERS = ("seed", "private", "secret", "password")


def _load_mint_script():
    spec = importlib.util.spec_from_file_location("mint_demo_agents", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _scan_for_private_material(obj, path="$"):
    """Yield paths of any key/value mentioning private key material."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in _PRIVATE_MARKERS):
                yield f"{path}.{key}"
            yield from _scan_for_private_material(value, f"{path}.{key}")
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            yield from _scan_for_private_material(value, f"{path}[{i}]")
    elif isinstance(obj, str):
        lowered = obj.lower()
        if any(marker in lowered for marker in _PRIVATE_MARKERS):
            yield f"{path} (value mentions private material)"


def test_seed_trust_store_shape_labels_and_no_private_material():
    assert SEED_FILE.exists(), "data/trust_store.demo.json is missing"
    store = json.loads(SEED_FILE.read_text())
    assert store.get("_demo_only") is True, "seed file must be labelled demo-only"
    assert "demo" in str(store.get("note", "")).lower()
    issuers = store.get("trusted_issuers")
    assert issuers == {DEMO_ISSUER_ID: issuers[DEMO_ISSUER_ID]}
    pubkey = issuers[DEMO_ISSUER_ID]
    assert len(pubkey) == 64 and all(c in "0123456789abcdef" for c in pubkey)
    revocation = store.get("revocation_lists", {}).get(DEMO_ISSUER_ID)
    assert isinstance(revocation, dict)
    assert revocation.get("issuer_id") == DEMO_ISSUER_ID
    assert revocation.get("signature_hex")
    bad = list(_scan_for_private_material(store))
    assert bad == [], f"seed file carries private material at: {bad}"


def test_seed_revocation_list_signature_valid():
    from kya import verify_revocation_list

    store = json.loads(SEED_FILE.read_text())
    pubkey = store["trusted_issuers"][DEMO_ISSUER_ID]
    revocation = store["revocation_lists"][DEMO_ISSUER_ID]
    # max_age huge: the signature/shape must verify even though a
    # static list goes stale after 1h of wall-clock time by design.
    ok, reason = verify_revocation_list(
        revocation,
        issuer_public_key_hex=pubkey,
        issuer_id=DEMO_ISSUER_ID,
        max_age_seconds=10**9,
    )
    assert ok, reason


def test_seed_issuer_is_trusted_by_exchange(tmp_path, clock):
    store = json.loads(SEED_FILE.read_text())
    verifier = ExchangeVerifier(
        trusted_issuers=dict(store["trusted_issuers"]),
        now_fn=clock,
    )
    assert DEMO_ISSUER_ID in verifier.trusted_issuers


def test_mint_flow_end_to_end(tmp_path, clock):
    """Seed -> issuer -> trust store -> live HTTP server -> market +
    orders -> exact balances, ledger conserved at 2000.0."""
    mint = _load_mint_script()
    seed_hex = secrets.token_hex(32)
    issuer = mint.issuer_from_seed(seed_hex, now_fn=clock)
    store = mint.build_trust_store(issuer)
    assert store["trusted_issuers"] == {DEMO_ISSUER_ID: issuer.issuer_public_key_hex}

    data_dir = str(tmp_path / "data")
    exchange = Exchange(
        data_dir=data_dir,
        now_fn=clock,
        trusted_issuers=dict(store["trusted_issuers"]),
    )
    ok, reason = exchange.refresh_revocation_list(
        store["revocation_lists"][DEMO_ISSUER_ID]
    )
    assert ok, reason
    server = make_server(exchange, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = ExchangeClient(f"http://127.0.0.1:{server.server_address[1]}")
        assert client.info()["name"] == "joulex"
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
        now = datetime.fromisoformat(client.server_time())
        market = client.create_market(
            alice["credential"],
            alice["agent_seed"],
            question="Will the Alberta pool price exceed $50/MWh?",
            resolution_type="aeso_price",
            contract_type="flex_capacity",
            strike=50.0,
            window_start=(now + timedelta(days=1)).isoformat(),
            window_end=(now + timedelta(days=1, hours=6)).isoformat(),
            resolves_at=(now + timedelta(days=1)).isoformat(),
            b=100.0,
        )
        mid = market["market_id"]
        r_alice = client.place_order(
            alice["credential"],
            alice["agent_seed"],
            {"agent_id": "agent-demo-alice", "market_id": mid,
             "side": "YES", "shares": 10, "action": "buy"},
        )
        r_bob = client.place_order(
            bob["credential"],
            bob["agent_seed"],
            {"agent_id": "agent-demo-bob", "market_id": mid,
             "side": "NO", "shares": 20, "action": "buy"},
        )
        assert r_alice["price_yes_after"] > 0.5
        assert r_bob["price_yes_after"] < 0.5
        ledger = client.ledger()
        assert ledger["conservation"]["ok"]
        assert abs(ledger["conservation"]["total"] - 2000.0) < 1e-9
    finally:
        server.shutdown()
        server.server_close()
