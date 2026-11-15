"""M0 — README + API surface checks.

M0 ships a README skeleton (full docs are M3), so this file checks
what M0 owns:

- README.md exists and covers run / test / demo;
- every M0 API route exists on the live server (no stale route);
- every CLI flag named in the README exists in
  ``joulex.server --help``;
- the quickstart actually runs discovery -> first paper trade in
  under ten minutes, measured wall-clock.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# (method, path-template) — the full v1 surface (DESIGN.md §10,
# plus POST /revocation-lists and GET /__demo/agents, which are
# implemented but not in the DESIGN table — see docs/API.md
# "Deviations"). The five M1 HCS/attestation rows were missing from
# the server until M3 implemented them (tests/test_hcs_api.py).
ALL_ROUTES = [
    ("GET", "/"),
    ("GET", "/time"),
    ("GET", "/markets"),
    ("POST", "/markets"),
    ("GET", "/markets/<id>"),
    ("GET", "/challenge"),
    ("POST", "/orders"),
    ("GET", "/positions"),
    ("GET", "/ledger"),
    ("GET", "/reconcile"),
    ("POST", "/markets/<id>/settle"),
    ("GET", "/attestations"),
    ("GET", "/hcs-log"),
    ("GET", "/hcs-log/verify"),
    ("GET", "/hcs-log/knowledge"),
    ("GET", "/hcs-log/provenance"),
    ("GET", "/aeso/price"),
    ("POST", "/revocation-lists"),
    ("POST", "/__demo/set_time"),
    ("GET", "/__demo/agents"),
    ("POST", "/accounts"),
    ("POST", "/accounts/login"),
    ("GET", "/accounts/me"),
    ("POST", "/accounts/logout"),
    ("POST", "/accounts/link-agent"),
    ("POST", "/accounts/<id>/disable"),
    ("GET", "/app"),
    ("GET", "/app/app.js"),
    ("GET", "/app/ed25519.js"),
    ("GET", "/app/sha256.js"),
]

# Routes that need query params to answer (a bare call is a 400 for
# missing params, which still proves the route exists — but a valid
# call proves more, so probe them properly).
ROUTE_PROBES = {
    "/positions": "/positions?agent_id=agent-doc-test",
    "/hcs-log/knowledge": "/hcs-log/knowledge?agent_id=agent-doc-test&at=2026-11-05T12%3A00%3A00%2B00%3A00",
    "/hcs-log/provenance": "/hcs-log/provenance?from_agent=exchange&to_agent=agent-doc-test",
}

# Files DESIGN.md §13 requires under docs/.
REQUIRED_DOCS = ["QUICKSTART.md", "API.md", "ADMISSION.md", "HEDERA.md"]

# Scripts a reader creates by following the docs (not repo files).
READER_CREATED = {"first_trade.py"}


def test_readme_exists_and_covers_run_test_demo():
    path = ROOT / "README.md"
    assert path.is_file(), "README.md missing — M0 deliverable"
    text = path.read_text(encoding="utf-8")
    assert len(text) > 800, f"README.md is suspiciously short ({len(text)} chars)"
    for needle in ["how to run", "test", "demo", "8472", "quickstart.py", "paper"]:
        assert needle.lower() in text.lower(), f"README.md does not mention {needle!r}"


@pytest.fixture(scope="module")
def live_server():
    """Real server on an ephemeral port, demo clock, demo issuer trust
    store — the same wiring quickstart.py and demo.py use."""
    sys.path.insert(0, str(ROOT))
    from joulex.demo_issuer import DemoIssuer  # noqa: E402
    from joulex.server import DemoClock, make_server  # noqa: E402
    from joulex.exchange import Exchange  # noqa: E402

    from datetime import datetime, timezone  # noqa: E402
    import threading  # noqa: E402

    clock = DemoClock(datetime.now(timezone.utc))
    issuer = DemoIssuer(now_fn=clock)
    tmp = tempfile.mkdtemp(prefix="joulex-docs-")
    exchange = Exchange(
        data_dir=str(Path(tmp) / "data"),
        now_fn=clock,
        trusted_issuers=issuer.trust_store,
    )
    ok, reason = exchange.verifier.refresh_revocation_list(issuer.revocation_list())
    assert ok, reason
    server = make_server(exchange, host="127.0.0.1", port=0, demo_clock=clock)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base
    finally:
        server.shutdown()
        server.server_close()


def _raw(base: str, method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(
        base + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read() or b"{}"
            try:
                return resp.status, json.loads(raw)
            except ValueError:
                # Non-JSON body (the /app UI shell + JS files): the
                # route exists iff the status is not a 404 NOT_FOUND.
                return resp.status, {"reason": raw.decode("utf-8", "replace")}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except ValueError:
            return exc.code, {"reason": raw.decode("utf-8", "replace")}


@pytest.mark.parametrize("method,path", ALL_ROUTES)
def test_route_exists(live_server, method, path):
    """Every v1 route must exist on the live server: a missing route
    answers 404/NOT_FOUND; a real route answers anything else (200 for
    reads, 400/401/403/409/503 for a deliberately empty call). Path
    params are filled with a nonexistent id, so a 404 there must carry
    MARKET_NOT_FOUND, not NOT_FOUND."""
    probe = path.replace("<id>", "no-such-market")
    probe = ROUTE_PROBES.get(path, probe)
    status, payload = _raw(live_server, method, probe, {} if method == "POST" else None)
    reason = payload.get("reason") if isinstance(payload, dict) else None
    assert reason != "NOT_FOUND", (
        f"{method} {path} is a v1 route but the server has no such route "
        f"(HTTP {status}: {payload})"
    )
    if method == "GET":
        assert status in (200, 400, 401, 403, 404, 503), f"GET {path} -> unexpected {status}: {payload}"


def test_docs_files_exist():
    """DESIGN.md §13: docs/ holds QUICKSTART.md, API.md, ADMISSION.md,
    HEDERA.md — all four, non-trivial."""
    for name in REQUIRED_DOCS:
        path = ROOT / "docs" / name
        assert path.is_file(), f"docs/{name} missing — DESIGN.md §13"
        assert len(path.read_text(encoding="utf-8")) > 1000, (
            f"docs/{name} is suspiciously short"
        )


def _api_md_routes() -> list[tuple[str, str]]:
    """(method, path) rows from the Routes table in docs/API.md —
    the table under the '## Routes' heading only."""
    text = (ROOT / "docs" / "API.md").read_text(encoding="utf-8")
    section = text.split("## Routes", 1)[1]
    section = section.split("\n## ", 1)[0]
    routes = []
    for line in section.splitlines():
        m = re.match(r"\|\s*`([A-Z]+)\s+([^`]+)`\s*\|", line)
        if m:
            # Table cells carry the query shape (e.g. /positions?agent_id=);
            # the route is the path before "?".
            routes.append((m.group(1), m.group(2).strip().split("?")[0]))
    assert routes, "docs/API.md Routes table parsed to zero rows"
    return routes


def test_api_md_documents_real_routes_only(live_server):
    """Every route in the API.md Routes table must exist on the live
    server (or the doc is lying). No endpoint is documented from
    memory alone."""
    for method, path in _api_md_routes():
        probe = path.replace("<id>", "no-such-market")
        probe = ROUTE_PROBES.get(path, probe)
        status, payload = _raw(
            live_server, method, probe, {} if method == "POST" else None
        )
        reason = payload.get("reason") if isinstance(payload, dict) else None
        assert reason != "NOT_FOUND", (
            f"docs/API.md documents {method} {path} but the live server "
            f"has no such route (HTTP {status}: {payload})"
        )


def test_api_md_covers_full_surface():
    """The API.md Routes table must cover the whole implemented
    surface — a route the server has but the doc omits is a doc gap."""
    documented = {(m, p) for m, p in _api_md_routes()}
    for route in ALL_ROUTES:
        assert route in documented, (
            f"{route[0]} {route[1]} exists on the server but docs/API.md "
            "does not document it"
        )


def test_quickstart_commands_are_real():
    """Every command docs/QUICKSTART.md tells a stranger to run must
    name a real script/module/file. Reader-created files (the doc's
    own first_trade.py snippet) are exempt — and covered by
    test_quickstart_hand_snippet_runs."""
    text = (ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
    for token in sorted(set(re.findall(r"python3\s+(?:-m\s+)?([A-Za-z0-9_./-]+)", text))):
        if token.startswith("-"):
            continue
        if token == "pytest":
            assert (ROOT / "tests").is_dir(), "QUICKSTART names pytest but tests/ is missing"
            continue
        if token in READER_CREATED:
            continue
        if token.endswith(".py"):
            assert (ROOT / token).is_file(), f"QUICKSTART names {token} but it does not exist"
        elif "." in token:  # dotted module, e.g. joulex.server
            assert (ROOT / (token.replace(".", "/") + ".py")).is_file(), (
                f"QUICKSTART names module {token} but it does not exist"
            )
        else:
            raise AssertionError(f"QUICKSTART command token {token!r} not checked")
    for doc in sorted(set(re.findall(r"docs/([A-Za-z0-9_.-]+\.md)", text))):
        assert (ROOT / "docs" / doc).is_file(), f"QUICKSTART names docs/{doc} but it does not exist"


def test_quickstart_hand_snippet_runs():
    """The by-hand snippet in QUICKSTART.md §4 (the single ```python
    block) must run end to end as written: discovery -> first paper
    trade. It starts its own server on port 8472."""
    text = (ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, re.S)
    assert len(blocks) == 1, f"expected exactly one python block in QUICKSTART.md, got {len(blocks)}"
    with tempfile.TemporaryDirectory(prefix="joulex-docs-snippet-") as tmp:
        script = Path(tmp) / "first_trade.py"
        script.write_text(blocks[0], encoding="utf-8")
        # The doc tells the reader to save the snippet in the repo
        # root; the test keeps it in a temp dir instead and supplies
        # the repo on PYTHONPATH, which is the same import outcome.
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        result = subprocess.run(
            [sys.executable, str(script)],
            cwd=ROOT, capture_output=True, text=True, timeout=120, env=env,
        )
    assert result.returncode == 0, f"QUICKSTART §4 snippet failed:\n{result.stdout}\n{result.stderr}"
    assert "balance_after" in result.stdout, result.stdout
    assert "994.875052" in result.stdout, result.stdout


def test_readme_honest_labels():
    """README must carry the honest labels: paper-only, software
    attestation (never hardware), local HCS log (never Hedera
    consensus)."""
    text = (ROOT / "README.md").read_text(encoding="utf-8").lower()
    assert "paper-only" in text or "paper only" in text
    assert "software" in text and "attestation" in text
    assert "local" in text and "hcs" in text
    assert "synthetic" in text
    # The "not" side must be explicit too, not just implied.
    assert "no hardware tee" in text
    assert "no live hedera" in text


def test_readme_flags_exist_in_server_help():
    result = subprocess.run(
        [sys.executable, "-m", "joulex.server", "--help"],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for flag in sorted(set(re.findall(r"--[a-z][a-z0-9-]+", text))):
        assert flag in result.stdout, f"README names {flag} but server --help does not"


def test_quickstart_runs_under_ten_minutes():
    """Run the documented quickstart end to end and measure it:
    discovery -> first paper trade must complete in < 600 s and print
    a trade receipt."""
    start = time.monotonic()
    result = subprocess.run(
        [sys.executable, "quickstart.py"],
        cwd=ROOT, capture_output=True, text=True, timeout=600,
    )
    elapsed = time.monotonic() - start
    assert result.returncode == 0, (
        f"quickstart.py failed after {elapsed:.1f}s:\n{result.stdout}\n{result.stderr}"
    )
    assert "TRADE OK" in result.stdout, result.stdout
    assert "first paper trade" in result.stdout.lower(), result.stdout
    assert elapsed < 600, f"quickstart took {elapsed:.1f}s — over the 10-minute bar"
