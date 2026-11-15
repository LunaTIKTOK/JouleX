"""M1.4 — human accounts: registration + login (password hashing,
sessions), role field, and the CF-KYA link from a human account to a
trading agent credential.

Test-first: this file was written before ``joulex/accounts.py`` existed
(collection failed on the missing import), then built green.
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import kya as kya_module  # noqa: E402
from joulex.admission import link_payload, link_payload_hash  # noqa: E402
from joulex.exchange import Exchange, ExchangeError  # noqa: E402

from conftest import FakeClock, T0, iso  # noqa: E402


def _link_presentation(bundle, challenge, account_id, clock):
    """Agent-side: sign (challenge + link-payload-hash + timestamp),
    proving the agent key holder consents to the binding."""
    payload = link_payload(account_id=account_id, agent_id=bundle["agent_id"])
    return kya_module.create_presentation(
        bundle["credential"],
        agent_seed=bundle["agent_seed"],
        challenge=challenge,
        payload_hash=link_payload_hash(payload),
        timestamp=iso(clock()),
    )


def _link(exchange, bundle, account_id, clock):
    challenge = exchange.issue_challenge()["challenge"]
    pres = _link_presentation(bundle, challenge, account_id, clock)
    return exchange.link_agent_to_account(
        account_id, pres, agent_id=bundle["agent_id"]
    )


def _register(exchange, **overrides):
    body = {
        "email": "alice@example.com",
        "password": "correct-horse-battery",
        "display_name": "Alice",
        "role": "buyer",
    }
    body.update(overrides)
    return exchange.register_account(**body)


# -- registration ------------------------------------------------------


def test_register_returns_public_view_without_password(exchange):
    acct = _register(exchange)
    assert acct["account_id"].startswith("acct_")
    assert acct["email"] == "alice@example.com"
    assert acct["display_name"] == "Alice"
    assert acct["role"] == "buyer"
    assert acct["status"] == "active"
    assert acct["linked_agents"] == []
    assert "created_at" in acct
    assert "password" not in acct
    assert "password_hash" not in json.dumps(acct)


def test_register_normalises_email_case(exchange):
    acct = _register(exchange, email="Alice@Example.COM")
    assert acct["email"] == "alice@example.com"


def test_register_duplicate_email_conflicts_case_insensitive(exchange):
    _register(exchange)
    with pytest.raises(ExchangeError) as exc:
        _register(exchange, email="ALICE@example.com")
    assert exc.value.status == 409
    assert exc.value.reason == "ACCOUNT_EXISTS"


def test_register_bad_email_400(exchange):
    for bad in ["not-an-email", "a@b", "@example.com", "a b@example.com", ""]:
        with pytest.raises(ExchangeError) as exc:
            _register(exchange, email=bad)
        assert exc.value.status == 400
        assert exc.value.reason.startswith("ACCOUNT_VALIDATION")


def test_register_short_password_400(exchange):
    with pytest.raises(ExchangeError) as exc:
        _register(exchange, password="too-short")
    assert exc.value.status == 400
    assert exc.value.reason.startswith("ACCOUNT_VALIDATION")


def test_register_overlong_password_400(exchange):
    # Groq review 2026-11-14: no upper bound let a huge password
    # burn scrypt CPU per login; capped at 256 characters.
    with pytest.raises(ExchangeError) as exc:
        _register(exchange, password="x" * 257)
    assert exc.value.status == 400
    assert exc.value.reason.startswith("ACCOUNT_VALIDATION")


def test_register_bad_role_400(exchange):
    with pytest.raises(ExchangeError) as exc:
        _register(exchange, role="superuser")
    assert exc.value.status == 400
    assert exc.value.reason.startswith("ACCOUNT_ROLE_INVALID")


def test_register_each_role_allowed(exchange):
    for i, role in enumerate(["buyer", "generator", "provider", "operator", "admin"]):
        acct = _register(exchange, email=f"user{i}@example.com", role=role)
        assert acct["role"] == role


def test_register_missing_fields_400(exchange):
    with pytest.raises(ExchangeError) as exc:
        exchange.register_account(email="x@example.com", password="long-enough-password")
    assert exc.value.status == 400


def test_password_never_stored_plaintext(exchange, tmp_path):
    _register(exchange)
    raw = (Path(exchange.store.data_dir) / "accounts.json").read_text(encoding="utf-8")
    assert "correct-horse-battery" not in raw
    stored = json.loads(raw)
    record = next(iter(stored.values()))
    assert record["password"]["scheme"] == "scrypt"
    assert record["password"]["hash_hex"] != "correct-horse-battery"
    assert len(record["password"]["salt_hex"]) == 64  # 32 random bytes


def test_register_appends_hcs_audit_event(exchange):
    acct = _register(exchange)
    msgs = [m for m in exchange.hcs.messages() if m["action"] == "account_register"]
    assert len(msgs) == 1
    assert msgs[0]["sender"] == acct["account_id"]
    assert msgs[0]["payload"]["role"] == "buyer"


# -- login / sessions --------------------------------------------------


def test_login_returns_token_and_account(exchange):
    acct = _register(exchange)
    out = exchange.login_account("alice@example.com", "correct-horse-battery")
    assert out["session_token"]
    assert len(out["session_token"]) >= 32
    assert out["account"]["account_id"] == acct["account_id"]
    assert "expires_at" in out
    # The token authenticates.
    me = exchange.account_for_token(out["session_token"])
    assert me["account_id"] == acct["account_id"]


def test_login_wrong_password_401(exchange):
    _register(exchange)
    with pytest.raises(ExchangeError) as exc:
        exchange.login_account("alice@example.com", "wrong-password-long")
    assert exc.value.status == 401
    assert exc.value.reason == "ACCOUNT_INVALID_CREDENTIALS"


def test_login_unknown_email_same_401_no_enumeration(exchange):
    _register(exchange)
    with pytest.raises(ExchangeError) as exc:
        exchange.login_account("nobody@example.com", "correct-horse-battery")
    assert exc.value.status == 401
    assert exc.value.reason == "ACCOUNT_INVALID_CREDENTIALS"


def test_session_token_never_stored_raw(exchange):
    _register(exchange)
    out = exchange.login_account("alice@example.com", "correct-horse-battery")
    raw = (Path(exchange.store.data_dir) / "sessions.json").read_text(encoding="utf-8")
    assert out["session_token"] not in raw
    assert hashlib.sha256(out["session_token"].encode()).hexdigest() in raw


def test_unknown_token_401(exchange):
    with pytest.raises(ExchangeError) as exc:
        exchange.account_for_token("no-such-token")
    assert exc.value.status == 401
    assert exc.value.reason == "ACCOUNT_UNAUTHORIZED"


def test_session_expires_after_ttl(exchange, clock):
    _register(exchange)
    out = exchange.login_account("alice@example.com", "correct-horse-battery")
    clock.advance(hours=25)
    with pytest.raises(ExchangeError) as exc:
        exchange.account_for_token(out["session_token"])
    assert exc.value.status == 401
    assert exc.value.reason == "SESSION_EXPIRED"


def test_logout_invalidates_token(exchange):
    _register(exchange)
    out = exchange.login_account("alice@example.com", "correct-horse-battery")
    assert exchange.logout_account(out["session_token"]) == {"ok": True}
    with pytest.raises(ExchangeError) as exc:
        exchange.account_for_token(out["session_token"])
    assert exc.value.status == 401
    # Logout is idempotent: a second logout of a dead token still 200s.
    assert exchange.logout_account(out["session_token"]) == {"ok": True}


def test_account_and_session_survive_restart(tmp_path, issuer, clock):
    data_dir = str(tmp_path / "data")

    def build():
        ex = Exchange(
            data_dir=data_dir, now_fn=clock, trusted_issuers=issuer.trust_store
        )
        ok, reason = ex.verifier.refresh_revocation_list(issuer.revocation_list())
        assert ok, reason
        return ex

    ex1 = build()
    acct = _register(ex1)
    out = ex1.login_account("alice@example.com", "correct-horse-battery")
    ex2 = build()
    me = ex2.account_for_token(out["session_token"])
    assert me["account_id"] == acct["account_id"]
    assert me["email"] == "alice@example.com"


# -- CF-KYA link --------------------------------------------------------


def test_link_agent_happy_path(exchange, agent_a, clock):
    acct = _register(exchange)
    linked = _link(exchange, agent_a, acct["account_id"], clock)
    assert len(linked["linked_agents"]) == 1
    entry = linked["linked_agents"][0]
    assert entry["agent_id"] == agent_a["agent_id"]
    assert entry["credential_id"] == agent_a["credential_id"]
    assert entry["principal_id"] == agent_a["principal_id"]
    assert "linked_at" in entry
    msgs = [m for m in exchange.hcs.messages() if m["action"] == "account_link_agent"]
    assert len(msgs) == 1
    assert msgs[0]["payload"]["credential_id"] == agent_a["credential_id"]


def test_link_agent_requires_live_presentation(exchange, agent_a, clock):
    """A bare credential dict is not proof of key control — only a
    fresh challenge-bound presentation links an agent."""
    acct = _register(exchange)
    with pytest.raises(ExchangeError) as exc:
        exchange.link_agent_to_account(
            acct["account_id"], agent_a["credential"], agent_id=agent_a["agent_id"]
        )
    assert exc.value.status == 401


def test_link_agent_rejects_l1_credential(exchange, issuer, clock):
    acct = _register(exchange)
    bundle = issuer.mint_agent(
        principal_id="principal-l1-test",
        display_name="L1 Test",
        agent_id="agent-l1-1",
        assurance_level="L1",
    )
    with pytest.raises(ExchangeError) as exc:
        _link(exchange, bundle, acct["account_id"], clock)
    assert exc.value.status == 401


def test_link_agent_wrong_agent_id_rejected(exchange, agent_a, clock):
    acct = _register(exchange)
    challenge = exchange.issue_challenge()["challenge"]
    pres = _link_presentation(agent_a, challenge, acct["account_id"], clock)
    with pytest.raises(ExchangeError) as exc:
        exchange.link_agent_to_account(
            acct["account_id"], pres, agent_id="agent-someone-else"
        )
    assert exc.value.status == 401


def test_link_presentation_is_single_use(exchange, agent_a, clock):
    """Groq review 2026-11-14 asked whether a captured link
    presentation replays: it cannot — the challenge is burned on
    first success (kya replay defence), so the second use is a 401."""
    acct = _register(exchange)
    challenge = exchange.issue_challenge()["challenge"]
    pres = _link_presentation(agent_a, challenge, acct["account_id"], clock)
    exchange.link_agent_to_account(
        acct["account_id"], pres, agent_id=agent_a["agent_id"]
    )
    with pytest.raises(ExchangeError) as exc:
        exchange.link_agent_to_account(
            acct["account_id"], pres, agent_id=agent_a["agent_id"]
        )
    assert exc.value.status == 401


def test_link_presentation_bound_to_other_account_rejected(exchange, agent_a, clock):
    """The signed payload binds (account_id, agent_id): a presentation
    minted for account A cannot link account B — the server
    recomputes the hash from the target account."""
    acct_a = _register(exchange, email="a@example.com")
    acct_b = _register(exchange, email="b@example.com")
    challenge = exchange.issue_challenge()["challenge"]
    pres = _link_presentation(agent_a, challenge, acct_a["account_id"], clock)
    with pytest.raises(ExchangeError) as exc:
        exchange.link_agent_to_account(
            acct_b["account_id"], pres, agent_id=agent_a["agent_id"]
        )
    assert exc.value.status == 401


def test_link_agent_duplicate_409(exchange, agent_a, clock):
    acct = _register(exchange)
    _link(exchange, agent_a, acct["account_id"], clock)
    with pytest.raises(ExchangeError) as exc:
        _link(exchange, agent_a, acct["account_id"], clock)
    assert exc.value.status == 409
    assert exc.value.reason == "ACCOUNT_AGENT_ALREADY_LINKED"


def test_link_same_credential_to_second_account_409(exchange, agent_a, clock):
    acct1 = _register(exchange, email="one@example.com")
    acct2 = _register(exchange, email="two@example.com")
    _link(exchange, agent_a, acct1["account_id"], clock)
    with pytest.raises(ExchangeError) as exc:
        _link(exchange, agent_a, acct2["account_id"], clock)
    assert exc.value.status == 409
    assert exc.value.reason == "ACCOUNT_AGENT_ALREADY_LINKED"


def test_link_unknown_account_404(exchange, agent_a, clock):
    with pytest.raises(ExchangeError) as exc:
        _link(exchange, agent_a, "acct_no-such", clock)
    assert exc.value.status == 404
    assert exc.value.reason == "ACCOUNT_NOT_FOUND"


# -- disable ------------------------------------------------------------


def test_disable_revokes_sessions_and_blocks_login(exchange, clock):
    admin = _register(exchange, email="admin@example.com", role="admin")
    victim = _register(exchange, email="victim@example.com")
    out = exchange.login_account("victim@example.com", "correct-horse-battery")
    disabled = exchange.disable_account(victim["account_id"], by_account=admin)
    assert disabled["status"] == "disabled"
    with pytest.raises(ExchangeError) as exc:
        exchange.account_for_token(out["session_token"])
    assert exc.value.status == 401
    with pytest.raises(ExchangeError) as exc:
        exchange.login_account("victim@example.com", "correct-horse-battery")
    assert exc.value.status == 403
    assert exc.value.reason == "ACCOUNT_DISABLED"


def test_disable_requires_admin(exchange):
    admin = _register(exchange, email="admin@example.com", role="admin")
    buyer = _register(exchange, email="buyer@example.com", role="buyer")
    with pytest.raises(ExchangeError) as exc:
        exchange.disable_account(admin["account_id"], by_account=buyer)
    assert exc.value.status == 403
    assert exc.value.reason == "ACCOUNT_FORBIDDEN"


# -- HTTP ---------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def live_server():
    sys.path.insert(0, str(ROOT))
    from joulex.demo_issuer import DemoIssuer  # noqa: E402
    from joulex.server import DemoClock, make_server  # noqa: E402

    import tempfile  # noqa: E402

    clock = DemoClock(datetime.now(timezone.utc))
    issuer = DemoIssuer(now_fn=clock)
    tmp = tempfile.mkdtemp(prefix="joulex-accounts-")
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
        yield base, exchange, issuer, clock
    finally:
        server.shutdown()
        server.server_close()


def _raw(base, method, path, body=None, headers=None):
    req = urllib.request.Request(
        base + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except ValueError:
            return exc.code, {"reason": raw.decode("utf-8", "replace")}


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_http_register_login_me_logout(live_server):
    base, _, _, _ = live_server
    status, body = _raw(base, "POST", "/accounts", {
        "email": "http-alice@example.com",
        "password": "correct-horse-battery",
        "display_name": "HTTP Alice",
        "role": "generator",
    })
    assert status == 201, body
    assert body["role"] == "generator"
    assert "password" not in body

    status, body = _raw(base, "POST", "/accounts/login", {
        "email": "http-alice@example.com", "password": "correct-horse-battery",
    })
    assert status == 200, body
    token = body["session_token"]

    status, body = _raw(base, "GET", "/accounts/me")
    assert status == 401 and body["reason"] == "ACCOUNT_UNAUTHORIZED", body

    status, body = _raw(base, "GET", "/accounts/me", headers=_bearer(token))
    assert status == 200, body
    assert body["email"] == "http-alice@example.com"

    status, body = _raw(base, "POST", "/accounts/logout", {}, headers=_bearer(token))
    assert status == 200 and body == {"ok": True}, body
    status, body = _raw(base, "GET", "/accounts/me", headers=_bearer(token))
    assert status == 401, body


def test_http_register_validation_errors(live_server):
    base, _, _, _ = live_server
    status, body = _raw(base, "POST", "/accounts", {"email": "bad", "password": "x"})
    assert status == 400, body
    status, body = _raw(base, "POST", "/accounts/login", {
        "email": "nobody@example.com", "password": "correct-horse-battery",
    })
    assert status == 401 and body["reason"] == "ACCOUNT_INVALID_CREDENTIALS", body


def test_http_link_agent_and_admin_disable(live_server):
    base, exchange, issuer, clock = live_server
    # Admin account + session.
    status, _ = _raw(base, "POST", "/accounts", {
        "email": "http-admin@example.com",
        "password": "correct-horse-battery",
        "display_name": "HTTP Admin", "role": "admin",
    })
    assert status == 201
    _, login = _raw(base, "POST", "/accounts/login", {
        "email": "http-admin@example.com", "password": "correct-horse-battery",
    })
    admin_token = login["session_token"]
    admin_id = login["account"]["account_id"]

    # A buyer links a demo L3 agent over HTTP.
    status, _ = _raw(base, "POST", "/accounts", {
        "email": "http-buyer@example.com",
        "password": "correct-horse-battery",
        "display_name": "HTTP Buyer", "role": "buyer",
    })
    assert status == 201
    _, buyer_login = _raw(base, "POST", "/accounts/login", {
        "email": "http-buyer@example.com", "password": "correct-horse-battery",
    })
    buyer_token = buyer_login["session_token"]
    buyer_id = buyer_login["account"]["account_id"]

    bundle = issuer.mint_agent(
        principal_id="principal-http-link",
        display_name="HTTP Link",
        agent_id="agent-http-link-1",
    )
    _, challenge_body = _raw(base, "GET", "/challenge")
    # The HTTP server runs on wall-clock time, so the presentation
    # timestamp must be near-now, not the test clock.
    pres = _link_presentation(
        bundle, challenge_body["challenge"], buyer_id,
        FakeClock(datetime.now(timezone.utc)),
    )
    status, body = _raw(base, "POST", "/accounts/link-agent", {
        "presentation": pres, "agent_id": bundle["agent_id"],
    }, headers=_bearer(buyer_token))
    assert status == 200, body
    assert body["linked_agents"][0]["agent_id"] == "agent-http-link-1"

    # Non-admin cannot disable; admin can.
    status, body = _raw(base, "POST", f"/accounts/{buyer_id}/disable", {},
                        headers=_bearer(buyer_token))
    assert status == 403 and body["reason"] == "ACCOUNT_FORBIDDEN", body
    status, body = _raw(base, "POST", f"/accounts/{buyer_id}/disable", {},
                        headers=_bearer(admin_token))
    assert status == 200 and body["status"] == "disabled", body
    status, body = _raw(base, "POST", "/accounts/login", {
        "email": "http-buyer@example.com", "password": "correct-horse-battery",
    })
    assert status == 403 and body["reason"] == "ACCOUNT_DISABLED", body
    # The admin account itself is untouched.
    assert admin_id != buyer_id
