"""HTTP service — stdlib ``http.server`` only, JSON API.

Run: ``python3 -m joulex.server [--port N] [--data-dir DIR]
[--demo-clock] [--trust-store FILE]``

- ``--demo-clock`` enables ``POST /__demo/set_time`` (sets server time).
  Without it the route does not exist (404). Demo/test only.
- ``--trust-store`` points at a JSON file
  ``{"trusted_issuers": {...}, "revocation_lists": {issuer_id: list}}``
  loaded at startup. Without it the exchange starts with an empty trust
  store and every order is rejected until the operator posts a
  revocation list and the trust store is configured — fail closed.

Threading server; the Exchange holds a re-entrant lock around mutations.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__
from .exchange import Exchange, ExchangeError
from .markets import parse_time

DEFAULT_PORT = 8472
DEFAULT_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

# M1.5: the server-served web UI lives in joulex/webui/ — static files
# only, no server-side rendering; the JS talks to the JSON API. The
# allowlist is the whole surface: anything else under /app/ (including
# ".." segments) is a 404, never a read outside this directory.
_WEBUI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui")
_WEBUI_FILES = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "ed25519.js": "text/javascript; charset=utf-8",
    "sha256.js": "text/javascript; charset=utf-8",
}


def pick_port(preferred: int = DEFAULT_PORT) -> int:
    """Free port for a demo server: ``preferred`` when nothing listens
    there, else an ephemeral port. Demo scripts use this so a stray
    service already on 8472 (another exchange, a leftover server)
    does not make the script silently talk to the WRONG server — the
    script then binds the returned port and the client connects to
    that port only. Small TOCTOU window between probe and bind is
    acceptable for a demo script; a bind failure still surfaces as
    an error, never as a trade against a stranger."""
    import socket

    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", preferred))
        return preferred
    except OSError:
        pass
    finally:
        probe.close()
    ephemeral = socket.socket()
    try:
        ephemeral.bind(("127.0.0.1", 0))
        return ephemeral.getsockname()[1]
    finally:
        ephemeral.close()
# Same cap as the Hedera relay: a body larger than this is refused
# (413) instead of being read into memory unbounded (audit 2026-11-12).
MAX_BODY_BYTES = 1_000_000
# Drain cap for refused bodies: the server consumes up to this many
# bytes of an oversized body before answering 413, so the client
# receives the 413 instead of a broken pipe (2026-11-13: the 413
# was sent while the client was still writing its 1 MB body, and
# the full-suite run failed with BrokenPipeError while the same
# test passed in isolation). Bodies larger than this are refused
# without draining; the 413 is still logged server-side.
MAX_DRAIN_BYTES = 16_000_000


def _page_params(query: dict[str, list[str]]) -> tuple[int, int]:
    """limit/offset for the paginated read routes. Defaults limit=50,
    offset=0; limit clamped to [1, 200]; garbage is a 400, never a
    silent default."""
    try:
        limit = int((query.get("limit") or ["50"])[0])
        offset = int((query.get("offset") or ["0"])[0])
    except ValueError:
        raise ExchangeError(400, "INVALID_REQUEST: limit/offset must be integers") from None
    if offset < 0:
        raise ExchangeError(400, "INVALID_REQUEST: offset must be >= 0")
    return max(1, min(limit, 200)), offset


def _optional_time(query: dict[str, list[str]], name: str):
    """One optional ISO-8601 query param, or None when absent. A
    present-but-unparseable value is a 400."""
    raw = (query.get(name) or [None])[0]
    if raw is None:
        return None
    try:
        return parse_time(raw)
    except ValueError:
        raise ExchangeError(400, f"INVALID_REQUEST: {name} must be an ISO-8601 timestamp") from None


class DemoClock:
    """Mutable clock for demo/test servers. ``--demo-clock`` only."""

    def __init__(self, start: datetime | None = None):
        self._now = start or datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self._now

    def set(self, when: datetime) -> None:
        self._now = when


def make_server(
    exchange: Exchange,
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    demo_clock: DemoClock | bool = False,
) -> ThreadingHTTPServer:
    """``demo_clock`` may be True (fresh DemoClock), False (no demo
    endpoint), or a mutable clock object with ``.set(datetime)`` — in
    which case the exchange MUST already use that same object as its
    clock, so the demo endpoint moves exchange time too."""
    if isinstance(demo_clock, bool):
        demo_clock = DemoClock() if demo_clock else None

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # keep stdout clean
            sys.stderr.write(f"{self.address_string()} {fmt % args}\n")

        # -- helpers ---------------------------------------------------
        def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            # Local demo service: allow the static demo.html page
            # (opened via file://) to call the API from any origin.
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-CF-KYA, Authorization")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            # Structured request log (audit 2026-11-12): one JSON
            # line per request on stderr — no bodies, no secrets.
            started = getattr(self, "_req_started", None)
            elapsed_ms = (
                round((time.monotonic() - started) * 1000, 1)
                if started is not None
                else None
            )
            sys.stderr.write(
                json.dumps(
                    {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "method": self.command,
                        "path": self.path,
                        "status": status,
                        "ms": elapsed_ms,
                    },
                    sort_keys=True,
                )
                + "\n"
            )

        def _send(self, status: int, obj: Any) -> None:
            self._send_bytes(status, json.dumps(obj).encode("utf-8"), "application/json")

        def do_OPTIONS(self) -> None:
            # CORS preflight: headers only, no body (204 must be empty).
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-CF-KYA, Authorization")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _bearer_token(self) -> str:
            """Session token from ``Authorization: Bearer <token>``.
            Missing or malformed is 401 ACCOUNT_UNAUTHORIZED — never
            a silent anonymous call."""
            auth = self.headers.get("Authorization") or ""
            if not auth.startswith("Bearer "):
                raise ExchangeError(401, "ACCOUNT_UNAUTHORIZED")
            token = auth[len("Bearer "):].strip()
            if not token:
                raise ExchangeError(401, "ACCOUNT_UNAUTHORIZED")
            return token

        def _read_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                # Drain (bounded) before refusing: with HTTP/1.1
                # keep-alive, answering 413 while the client is
                # still writing leaves the unread body in the socket
                # — the client sees a broken pipe instead of the
                # 413, and leftover bytes would be parsed as the
                # next request. Draining first gives the client a
                # clean 413 on every oversized POST.
                remaining = min(length, MAX_DRAIN_BYTES)
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                raise ExchangeError(
                    413, f"REQUEST_TOO_LARGE: body exceeds {MAX_BODY_BYTES} bytes"
                )
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise ExchangeError(400, "INVALID_JSON")
            if not isinstance(data, dict):
                raise ExchangeError(400, "INVALID_JSON: body must be an object")
            return data

        def _handle(self, fn) -> None:
            try:
                status, obj = fn()
            except ExchangeError as exc:
                status, obj = exc.status, {"error": "exchange_error", "reason": exc.reason}
            except Exception as exc:  # never leak a stack trace as a 500 body
                status, obj = 500, {"error": "internal", "reason": str(exc)}
            self._send(status, obj)

        # -- routing ---------------------------------------------------
        def do_GET(self) -> None:
            self._req_started = time.monotonic()
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)

            def route():
                if path == "/":
                    return 200, exchange.info(demo_clock=demo_clock is not None)
                if path == "/health":
                    # Liveness + cheap readiness (audit 2026-11-12):
                    # the process answers, the HCS mode/topic are
                    # visible, and the anchor-failure count is
                    # surfaced. The deep log-integrity check stays on
                    # GET /hcs-log/verify (it re-walks the whole log).
                    hcs = exchange.hcs.hcs_info()
                    stats = exchange.hcs.hcs_stats()
                    anchor = getattr(exchange.hcs, "anchor_stats", None)
                    anchor_counts = anchor() if callable(anchor) else None
                    return 200, {
                        "status": "ok",
                        "server_time": exchange.now().isoformat(),
                        "hcs_mode": hcs["mode"],
                        "hcs_topic": hcs["topic"],
                        "hcs_network": hcs["network"],
                        "markets": len(exchange.markets),
                        "hcs_messages": stats["messages"],
                        "hcs_submitted": stats["submitted"],
                        "hcs_failed_submits": stats["failed"],
                        "hcs_anchor_queued": (
                            anchor_counts["queued"] if anchor_counts else 0
                        ),
                        "hcs_anchor_anchored": (
                            anchor_counts["anchored"] if anchor_counts else 0
                        ),
                        "hcs_anchor_dead": (
                            anchor_counts["dead"] if anchor_counts else 0
                        ),
                        "settle_anchor_policy": exchange.settle_anchor_policy,
                        "uptime_seconds": exchange.metrics()["uptime_seconds"],
                    }
                if path == "/metrics":
                    return 200, exchange.metrics()
                if path == "/time":
                    return 200, {"server_time": exchange.now().isoformat()}
                if path == "/markets":
                    return 200, exchange.list_markets()
                if path.startswith("/markets/"):
                    return 200, exchange.get_market(path.split("/", 2)[2])
                if path == "/challenge":
                    return 200, exchange.issue_challenge()
                if path == "/positions":
                    agent_id = (query.get("agent_id") or [None])[0]
                    if not agent_id:
                        raise ExchangeError(400, "INVALID_REQUEST: agent_id required")
                    return 200, exchange.positions(agent_id)
                if path == "/ledger":
                    return 200, exchange.ledger_view()
                if path == "/reconcile":
                    # M5: read-only audit — re-derives every balance
                    # from the logs; 200 with the report either way,
                    # the report's "ok" flag is the verdict.
                    return 200, exchange.reconcile()
                if path == "/attestations":
                    # M3: attestation envelopes carried on log
                    # messages, latest first, paginated.
                    limit, offset = _page_params(query)
                    return 200, exchange.list_attestations(limit=limit, offset=offset)
                if path == "/hcs-log":
                    # M3: HCS audit-log messages, latest first.
                    # ?agent_id= filters to messages the agent sent;
                    # ?from= / ?to= bound consensus timestamps.
                    limit, offset = _page_params(query)
                    agent_id = (query.get("agent_id") or [None])[0]
                    return 200, exchange.hcs_log(
                        limit=limit,
                        offset=offset,
                        agent_id=agent_id,
                        from_ts=_optional_time(query, "from"),
                        to_ts=_optional_time(query, "to"),
                    )
                if path == "/hcs-log/verify":
                    # M3: log integrity report (hash chain, sequence,
                    # timestamp monotonicity).
                    return 200, exchange.hcs_verify()
                if path == "/hcs-log/knowledge":
                    # M3: what agent X knew at time T (log replay).
                    agent_id = (query.get("agent_id") or [None])[0]
                    at = (query.get("at") or [None])[0]
                    if not agent_id or not at:
                        raise ExchangeError(
                            400, "INVALID_REQUEST: agent_id and at required"
                        )
                    try:
                        return 200, exchange.hcs_knowledge(agent_id, at)
                    except ValueError:
                        raise ExchangeError(
                            400, "INVALID_REQUEST: at must be an ISO-8601 timestamp"
                        ) from None
                if path == "/hcs-log/provenance":
                    # M3: who told whom — messages where from_agent
                    # supplied the information to_agent's action
                    # relied on. Optional ?t0= / ?t1= time window.
                    from_agent = (query.get("from_agent") or [None])[0]
                    to_agent = (query.get("to_agent") or [None])[0]
                    if not from_agent or not to_agent:
                        raise ExchangeError(
                            400, "INVALID_REQUEST: from_agent and to_agent required"
                        )
                    return 200, exchange.hcs_provenance(
                        from_agent,
                        to_agent,
                        _optional_time(query, "t0"),
                        _optional_time(query, "t1"),
                    )
                if path == "/accounts/me":
                    # M1.4: the calling account's public view. Bearer
                    # session required — 401 without it.
                    return 200, exchange.account_for_token(self._bearer_token())
                if path == "/aeso/price":
                    # Latest AESO hourly pool price + source. Live
                    # fetch; 503 when AESO is unreachable — never a
                    # stale or guessed value.
                    from .aeso import AESOUnavailable, fetch_current_pool_price

                    try:
                        return 200, fetch_current_pool_price()
                    except AESOUnavailable as exc:
                        raise ExchangeError(503, f"{exc.code}: {exc.detail}") from exc
                if path.startswith("/audit/"):
                    # M4: third-party settlement verification — a
                    # verifier with only the settlement id re-checks
                    # the receipt, ledger entry, log chain, mirror
                    # anchor and attestation without trusting the
                    # server. 404 carries the AUDIT_NOT_FOUND error
                    # (not the generic exchange_error shape).
                    settlement_id = unquote(path.split("/", 2)[2])
                    if not settlement_id:
                        return 404, {"error": "AUDIT_NOT_FOUND"}
                    result = exchange.audit_settlement(settlement_id)
                    if not result.get("found"):
                        return 404, {"error": "AUDIT_NOT_FOUND"}
                    return 200, result
                if path == "/__demo/agents":
                    # Demo fixtures only: test credentials written by
                    # demo.py (throwaway keys, labelled in the page).
                    if demo_clock is None:
                        raise ExchangeError(404, "NOT_FOUND")
                    agents = exchange.store.load_json("demo_agents.json", None)
                    if agents is None:
                        raise ExchangeError(404, "DEMO_AGENTS_NOT_FOUND")
                    return 200, agents
                raise ExchangeError(404, "NOT_FOUND")

            # M1.5: the server-served web UI — static files only, from
            # the allowlist above. Handled before the JSON router so a
            # UI path can never fall through to an API route (or vice
            # versa). GET / stays the JSON exchange info (API contract).
            if path == "/app" or path.startswith("/app/"):
                name = "index.html" if path == "/app" else unquote(path[len("/app/"):])
                if "/" in name or "\\" in name or name not in _WEBUI_FILES:
                    self._send(404, {"error": "exchange_error", "reason": "NOT_FOUND"})
                    return
                try:
                    with open(os.path.join(_WEBUI_DIR, name), "rb") as f:
                        data = f.read()
                except OSError:
                    self._send(404, {"error": "exchange_error", "reason": "NOT_FOUND"})
                    return
                self._send_bytes(200, data, _WEBUI_FILES[name])
                return

            self._handle(route)

        def do_POST(self) -> None:
            self._req_started = time.monotonic()
            parsed = urlparse(self.path)
            path = parsed.path

            def route():
                if path == "/accounts":
                    # M1.4: registration. 201 + the public account view.
                    body = self._read_body()
                    return 201, exchange.register_account(
                        body.get("email"),
                        body.get("password"),
                        body.get("display_name"),
                        body.get("role"),
                    )
                if path == "/accounts/login":
                    # M1.4: login. 200 + session token + account.
                    body = self._read_body()
                    return 200, exchange.login_account(
                        body.get("email"), body.get("password")
                    )
                if path == "/accounts/logout":
                    # M1.4: logout. Idempotent 200; the token dies.
                    return 200, exchange.logout_account(self._bearer_token())
                if path == "/accounts/link-agent":
                    # M1.4: bind a CF-KYA L3 agent to the calling
                    # account. The presentation must be a fresh
                    # challenge-bound signature over the link payload.
                    me = exchange.account_for_token(self._bearer_token())
                    body = self._read_body()
                    return 200, exchange.link_agent_to_account(
                        me["account_id"],
                        body.get("presentation"),
                        agent_id=body.get("agent_id") or "",
                    )
                if path.startswith("/accounts/") and path.endswith("/disable"):
                    # M1.4: admin-only disable (one-way in this
                    # version). The caller must be an admin; the
                    # target's sessions are revoked.
                    me = exchange.account_for_token(self._bearer_token())
                    target = unquote(path.split("/")[2])
                    return 200, exchange.disable_account(target, by_account=me)
                if path == "/markets":
                    # Operator key, when present, is authoritative: the
                    # KYA header is not even parsed on that path, so a
                    # stale/garbage KYA header cannot shadow it.
                    operator_key = self.headers.get("X-Operator-Key")
                    presentation = None
                    if operator_key is None:
                        from kya import presentation_from_header

                        header = self.headers.get("X-CF-KYA")
                        if header:
                            try:
                                presentation = presentation_from_header(header)
                            except ValueError:
                                raise ExchangeError(401, "KYA_PRESENTATION_INVALID") from None
                    return 201, exchange.create_market(
                        presentation,
                        operator_key=operator_key,
                        **self._read_body(),
                    )
                if path == "/orders":
                    from kya import presentation_from_header

                    header = self.headers.get("X-CF-KYA")
                    presentation = None
                    if header:
                        try:
                            presentation = presentation_from_header(header)
                        except ValueError:
                            raise ExchangeError(401, "KYA_PRESENTATION_INVALID") from None
                    return 200, exchange.place_order(presentation, self._read_body())
                if path.startswith("/markets/") and path.endswith("/settle"):
                    market_id = path.split("/")[2]
                    body = self._read_body()
                    return 200, exchange.settle_market(
                        market_id,
                        operator_key=self.headers.get("X-Operator-Key"),
                        observed_value=body.get("observed_value"),
                        outcome=body.get("outcome"),
                        evidence=body.get("evidence"),
                        attestation=body.get("attestation"),
                        attestation_binding=body.get("attestation_binding"),
                    )
                if path == "/audit/checkpoint":
                    # M4: append one log-checkpoint message.
                    # Operator-key gated like settlement: when a key
                    # is configured the header must match
                    # (constant-time compare; missing is 401 too);
                    # with no key configured the route is open.
                    operator_key = self.headers.get("X-Operator-Key")
                    if exchange.operator_key is not None:
                        if operator_key is None or not hmac.compare_digest(
                            str(operator_key), exchange.operator_key
                        ):
                            raise ExchangeError(401, "OPERATOR_KEY_INVALID")
                    return 200, exchange.checkpoint_audit_log()
                if path == "/revocation-lists":
                    ok, reason = exchange.refresh_revocation_list(self._read_body())
                    if not ok:
                        raise ExchangeError(400, reason)
                    return 200, {"ok": True, "reason": reason}
                if path == "/__demo/set_time":
                    if demo_clock is None:
                        raise ExchangeError(404, "NOT_FOUND")
                    body = self._read_body()
                    try:
                        demo_clock.set(parse_time(body["time"]))
                    except (KeyError, ValueError):
                        raise ExchangeError(400, "INVALID_REQUEST: time required (ISO-8601)") from None
                    return 200, {"server_time": exchange.now().isoformat()}
                raise ExchangeError(404, "NOT_FOUND")

            self._handle(route)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def build_exchange(data_dir: str, trust_store_path: str | None, demo_clock: DemoClock | None, aeso_oracle: bool = False, operator_key: str | None = None, settle_anchor_policy: str | None = None) -> Exchange:
    trusted_issuers: dict[str, str] = {}
    revocation_lists: dict[str, dict[str, Any]] = {}
    if trust_store_path:
        with open(trust_store_path, "r", encoding="utf-8") as f:
            store = json.load(f)
        trusted_issuers = store.get("trusted_issuers", {})
        revocation_lists = store.get("revocation_lists", {})
    settlement_oracle = None
    if aeso_oracle:
        from .aeso import AESOPriceOracle

        settlement_oracle = AESOPriceOracle()
    exchange = Exchange(
        data_dir=data_dir,
        now_fn=(demo_clock or (lambda: datetime.now(timezone.utc))),
        trusted_issuers=trusted_issuers,
        settlement_oracle=settlement_oracle,
        operator_key=operator_key,
        settle_anchor_policy=(
            settle_anchor_policy
            or os.environ.get("JOULEX_SETTLE_ANCHOR_POLICY")
            or "fail_closed"
        ),
    )
    for issuer_id, rev_list in revocation_lists.items():
        ok, reason = exchange.refresh_revocation_list(rev_list)
        if not ok:
            print(f"warning: revocation list for {issuer_id} not accepted: {reason}", file=sys.stderr)
    return exchange


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="JouleX — paper-only, stdlib-only.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    parser.add_argument("--trust-store", default=None,
                        help="JSON file with trusted_issuers + revocation_lists")
    parser.add_argument("--demo-clock", action="store_true",
                        help="enable POST /__demo/set_time (demo/test only)")
    parser.add_argument("--aeso-oracle", action="store_true",
                        help="settle aeso_price markets from a live AESO pool-price fetch (M0); without it, settlement uses an operator-supplied observed_value (max pool price over the window)")
    parser.add_argument("--operator-key", default=None,
                        help="human-held secret for market creation and settlement via the X-Operator-Key header (M2; "
                        "settlement gated 2026-11-11); "
                        "prefer the JOULEX_OPERATOR_KEY env var — a flag value shows in the process list. "
                        "Without a key the operator path is closed and creation needs a CF-KYA presentation.")
    parser.add_argument("--settle-anchor-policy", default=None,
                        choices=["fail_closed", "fail_retain"],
                        help="settlement anchoring policy (M4): fail_closed (default) "
                        "never marks SETTLED without an anchor receipt; "
                        "fail_retain finalizes inline and retains the anchor "
                        "as best-effort. Overrides JOULEX_SETTLE_ANCHOR_POLICY.")
    args = parser.parse_args(argv)

    demo_clock = DemoClock() if args.demo_clock else None
    operator_key = args.operator_key or os.environ.get("JOULEX_OPERATOR_KEY")
    exchange = build_exchange(args.data_dir, args.trust_store, demo_clock, aeso_oracle=args.aeso_oracle, operator_key=operator_key, settle_anchor_policy=args.settle_anchor_policy)
    server = make_server(exchange, host=args.host, port=args.port, demo_clock=demo_clock)
    print(
        f"joulex v{__version__} listening on http://{args.host}:{args.port} "
        f"(paper-only, demo_clock={args.demo_clock})",
        flush=True,
    )
    # M4: under RealHCS the anchor worker drains the queue in the
    # background (retries + dead-letter); the exchange lock is never
    # held by an HCS submit. LocalHCS has no worker (no-op).
    start_worker = getattr(exchange.hcs, "start_anchor_worker", None)
    stop_worker = getattr(exchange.hcs, "stop_anchor_worker", None)
    if callable(start_worker):
        start_worker()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if callable(stop_worker):
            stop_worker()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
