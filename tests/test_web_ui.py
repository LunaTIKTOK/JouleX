"""Server-served web UI tests (M1.5).

The server serves a thin browser UI at ``GET /app`` (HTML shell +
JS that talks to the live API — markets list, order form, positions,
ledger, audit trail, account register/login, demo-agent order flow).
The browser signs CF-KYA presentations in JS (``webui/ed25519.js``)
— no admission bypass: the order the UI places is verified by the
same server-side verifier as a Python client's.

Serving tests hit the real stdlib server over HTTP (same ``live``
fixture pattern as test_api.py). The crypto + end-to-end flows run
as Node scripts (``tests/webui_*.cjs``) so the exact files the
browser loads are what gets exercised; they are skipped when Node
is unavailable.
"""

from __future__ import annotations

import http.client
import json
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from joulex.client import ExchangeClient
from joulex.exchange import Exchange
from joulex.server import make_server

ROOT = Path(__file__).resolve().parent.parent
WEBUI_DIR = ROOT / "joulex" / "webui"


@pytest.fixture()
def live(tmp_path, issuer, clock):
    """(client, exchange, clock, base_url) against a running server
    with demo clock + a demo-agents file, so GET /__demo/agents works
    exactly as on a deployed demo server."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    ex = Exchange(data_dir=str(data_dir), now_fn=clock, trusted_issuers=issuer.trust_store)
    ex.verifier.refresh_revocation_list(issuer.revocation_list())
    # Demo agents file, same shape as scripts/mint_demo_agents.py
    # writes: {"alice": {credential, agent_seed_hex, agent_id,
    # principal_id}, "bob": {...}}.
    agents = {}
    for key, principal_id, display_name, agent_id in (
        ("alice", "principal-demo-alice", "Alice Demo Principal", "agent-demo-alice"),
        ("bob", "principal-demo-bob", "Bob Demo Principal", "agent-demo-bob"),
    ):
        bundle = issuer.mint_agent(
            principal_id=principal_id, display_name=display_name, agent_id=agent_id
        )
        agents[key] = {
            "credential": bundle["credential"],
            "agent_seed_hex": bundle["agent_seed"].hex(),
            "agent_id": bundle["agent_id"],
            "principal_id": bundle["principal_id"],
        }
    (data_dir / "demo_agents.json").write_text(json.dumps(agents), encoding="utf-8")
    server = make_server(ex, host="127.0.0.1", port=0, demo_clock=clock)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    client = ExchangeClient(f"http://127.0.0.1:{port}")
    yield client, ex, clock, f"http://127.0.0.1:{port}"
    server.shutdown()
    server.server_close()


def _raw_get(base_url: str, path: str):
    """GET without client-side path normalisation (for traversal probes)."""
    hostport = base_url.split("://", 1)[1]
    conn = http.client.HTTPConnection(hostport, timeout=10)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read()
    ctype = resp.getheader("Content-Type") or ""
    conn.close()
    return resp.status, ctype, body


def _get(base_url: str, path: str):
    req = urllib.request.Request(base_url + path, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.getheader("Content-Type") or "", resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.getheader("Content-Type") or "", exc.read()


# -- static serving ------------------------------------------------------

def test_app_serves_html_shell(live):
    _, _, _, base_url = live
    status, ctype, body = _get(base_url, "/app")
    assert status == 200, body[:200]
    assert "text/html" in ctype
    html = body.decode("utf-8")
    assert "PAPER ONLY" in html
    assert "app.js" in html
    assert "ed25519.js" in html
    # The sections the milestone names.
    for section in ("markets", "order", "positions", "ledger", "audit"):
        assert section in html.lower(), f"missing section {section}"


def test_app_serves_js_files(live):
    _, _, _, base_url = live
    for name in ("app.js", "ed25519.js", "sha256.js"):
        status, ctype, body = _get(base_url, f"/app/{name}")
        assert status == 200, f"{name}: {body[:200]}"
        assert "javascript" in ctype, f"{name}: {ctype}"
        assert len(body) > 100, f"{name} suspiciously small"
    _, _, app_js = _get(base_url, "/app/app.js")
    assert b"fetch(" in app_js
    _, _, ed = _get(base_url, "/app/ed25519.js")
    assert b"sign" in ed


def test_app_unknown_file_404(live):
    _, _, _, base_url = live
    status, _, _ = _get(base_url, "/app/nope.js")
    assert status == 404
    status, _, _ = _get(base_url, "/app/sub/app.js")
    assert status == 404


def test_app_path_traversal_blocked(live):
    _, _, _, base_url = live
    for probe in ("/app/../server.py", "/app/%2e%2e/server.py", "/app/..%2fserver.py"):
        status, _, body = _raw_get(base_url, probe)
        assert status == 404, f"{probe} -> {status}: {body[:200]}"
        assert b"def make_server" not in body


def test_app_root_stays_json_info(live):
    client, _, _, _ = live
    info = client.info()
    assert info["paper_only"] is True
    assert info["name"] == "joulex"


# -- Node-side checks (the exact files the browser loads) ---------------

def _node():
    return shutil.which("node")


def test_webui_crypto_vectors():
    """RFC 8032 vectors + canonical-hash parity for the browser crypto.
    The script also prints one JS-computed signature, verified here
    with Python ed25519 — JS -> Python interop. Skipped when Node
    is unavailable."""
    node = _node()
    if node is None:
        pytest.skip("node not available")
    proc = subprocess.run(
        [node, str(ROOT / "tests" / "webui_crypto.cjs")],
        capture_output=True, text=True, timeout=60, cwd=str(ROOT),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "WEBUI CRYPTO OK" in proc.stdout
    # Cross-check: the SIG line is a JS signature over a fixed message
    # with the RFC 8032 TEST 2 seed — verify it with Python ed25519.
    sig_line = next(
        (line for line in proc.stdout.splitlines() if line.startswith("SIG ")),
        None,
    )
    assert sig_line, "crypto script printed no SIG line"
    import ed25519

    assert ed25519.verify_hex(
        sig_line.split(" ", 1)[1].strip(),
        b"joulex-webui-test",
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
    ), "Python could not verify the JS signature"


def _seed_market(client, issuer):
    """One OPEN market via the Python client (creation is already
    covered by test_api.py); returns its market_id."""
    from tests.conftest import iso, make_market_payload

    bundle = issuer.mint_agent(
        principal_id="principal-seed-test",
        display_name="Seed Test Principal",
        agent_id="agent-seed-1",
    )
    market = client.create_market(
        bundle["credential"], bundle["agent_seed"], **make_market_payload()
    )
    return market["market_id"]


def test_webui_e2e_order_via_browser_signing(live, issuer):
    """The full browser flow, driven in Node against the live server:
    fetch demo agents, GET /challenge, sign the order presentation
    in JS, POST /orders, then verify receipt + positions + ledger +
    audit log. Skipped when Node is unavailable."""
    node = _node()
    if node is None:
        pytest.skip("node not available")
    client, _, _, base_url = live
    market_id = _seed_market(client, issuer)
    proc = subprocess.run(
        [node, str(ROOT / "tests" / "webui_e2e.cjs"), base_url, market_id],
        capture_output=True, text=True, timeout=60, cwd=str(ROOT),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "WEBUI E2E OK" in proc.stdout


def test_webui_smoke_jsdom(live, issuer):
    """Headless-browser smoke of the served UI (jsdom, fetch injected):
    shell renders, markets list fills from the live API, the demo
    agent places a trade through the DOM order form, balance and
    positions update. Skipped when Node or jsdom is unavailable."""
    node = _node()
    if node is None:
        pytest.skip("node not available")
    client, _, _, base_url = live
    market_id = _seed_market(client, issuer)
    proc = subprocess.run(
        [node, str(ROOT / "tests" / "webui_smoke.cjs"), base_url, market_id],
        capture_output=True, text=True, timeout=60, cwd=str(ROOT),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "WEBUI SMOKE OK" in proc.stdout
