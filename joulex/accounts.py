"""Human accounts (M1.4) — registration, login, sessions, roles, and
the link from a human account to a CF-KYA agent credential.

Design (delivery track M1.4; see DESIGN.md §14):

- Passwords: ``hashlib.scrypt`` (n=2**14, r=8, p=1, dklen=32) with a
  fresh 32-byte salt per account. Only the salt + hash are stored,
  never the password; verification is ``hmac.compare_digest``.
  Minimum password length is 12 characters.
- Sessions: opaque ``secrets.token_urlsafe(32)`` bearer tokens, stored
  only as their SHA-256 hash, 24h expiry. Logout deletes the session;
  expiry is enforced on every authentication. A token never appears
  in any stored file or log line.
- Roles: buyer / generator / provider / operator / admin. The role is
  a plain field on the account; enforcement (admin-only disable)
  happens in the Exchange methods, not here.
- CF-KYA link: an account links an agent credential only through a
  fresh challenge-bound presentation (``admission.link_payload``),
  verified by the ExchangeVerifier — a bare credential proves
  nothing about key control, so it never links. One credential links
  to at most one account, so orders by that agent attribute to
  exactly one human.
- Login/logout are NOT written to the HCS audit log (session noise);
  registration, agent links and disables ARE (Exchange appends them
  before the state change, so a log failure is a 503 that executes
  nothing). This module owns state only; the Exchange owns the audit
  appends and the verifier calls.

Persistence: ``accounts.json`` / ``sessions.json`` under the exchange
data dir, via the atomic ``JsonStore``. Corrupt state refuses to load
(fail closed), like the market/ledger load.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from .errors import ExchangeError
from .store import JsonStore

ROLES = ("buyer", "generator", "provider", "operator", "admin")
MIN_PASSWORD_LENGTH = 12
# Upper bound: scrypt cost grows with input size, and the server
# body cap is 1 MB — a multi-hundred-KB password would burn CPU per
# login for nothing. 256 characters is far above any real password.
MAX_PASSWORD_LENGTH = 256
SESSION_TTL_HOURS = 24
MAX_EMAIL_LENGTH = 254
MAX_DISPLAY_NAME_LENGTH = 100

ACCOUNTS_FILENAME = "accounts.json"
SESSIONS_FILENAME = "sessions.json"

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# scrypt work factor: ~50 ms per hash on this cell — slow enough to
# resist offline guessing, fast enough for an interactive login.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
# Fixed salt for the dummy hash on unknown-email logins (timing
# equalisation only — never a real account's salt).
_DUMMY_SALT = bytes(32)


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    ).hex()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def public_view(record: dict[str, Any]) -> dict[str, Any]:
    """The account as the API returns it — never includes password
    material."""
    return {
        "account_id": record["account_id"],
        "email": record["email"],
        "display_name": record["display_name"],
        "role": record["role"],
        "status": record["status"],
        "linked_agents": [dict(a) for a in record.get("linked_agents", [])],
        "created_at": record["created_at"],
    }


class AccountStore:
    """Human-account state: persistence + password/session logic.

    The Exchange wraps this with the HCS audit appends and the
    CF-KYA verifier calls; this class never touches either.
    """

    def __init__(self, store: JsonStore, *, now_fn=None, session_ttl_hours: int = SESSION_TTL_HOURS):
        self._store = store
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.session_ttl = timedelta(hours=session_ttl_hours)
        try:
            accounts = self._store.load_json(ACCOUNTS_FILENAME, {})
            sessions = self._store.load_json(SESSIONS_FILENAME, {})
        except (ValueError, OSError) as exc:
            raise RuntimeError(f"refusing to start: unreadable account state: {exc}") from exc
        if not isinstance(accounts, dict) or not isinstance(sessions, dict):
            raise RuntimeError("refusing to start: corrupt account state (not objects)")
        self._accounts: dict[str, dict[str, Any]] = accounts
        self._sessions: dict[str, dict[str, Any]] = sessions

    def now(self) -> datetime:
        return self._now_fn()

    # -- persistence ---------------------------------------------------
    def _save(self) -> None:
        self._store.save_json(ACCOUNTS_FILENAME, self._accounts)
        self._store.save_json(SESSIONS_FILENAME, self._sessions)

    # -- validation (pure: no mutation, so the Exchange can validate
    #    before its HCS audit append and then commit) ------------------
    def validate_registration(
        self, email: Any, password: Any, display_name: Any, role: Any
    ) -> tuple[str, str, str]:
        """Validate a registration body. Returns the normalized
        (email, display_name, role). Raises ExchangeError on any bad
        field; duplicate emails are a 409, everything else a 400."""
        if not isinstance(email, str) or not isinstance(password, str):
            raise ExchangeError(400, "ACCOUNT_VALIDATION: email and password required")
        email = email.strip().lower()
        if (
            len(email) == 0
            or len(email) > MAX_EMAIL_LENGTH
            or not _EMAIL_RE.match(email)
        ):
            raise ExchangeError(400, "ACCOUNT_VALIDATION: email is not a valid address")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ExchangeError(
                400,
                f"ACCOUNT_VALIDATION: password must be at least {MIN_PASSWORD_LENGTH} characters",
            )
        if len(password) > MAX_PASSWORD_LENGTH:
            raise ExchangeError(
                400,
                f"ACCOUNT_VALIDATION: password must be at most {MAX_PASSWORD_LENGTH} characters",
            )
        if not isinstance(display_name, str) or not display_name.strip():
            raise ExchangeError(400, "ACCOUNT_VALIDATION: display_name required")
        display_name = display_name.strip()
        if len(display_name) > MAX_DISPLAY_NAME_LENGTH:
            raise ExchangeError(400, "ACCOUNT_VALIDATION: display_name too long")
        if role not in ROLES:
            raise ExchangeError(
                400, f"ACCOUNT_ROLE_INVALID: role must be one of {list(ROLES)}"
            )
        if self.find_by_email(email) is not None:
            raise ExchangeError(409, "ACCOUNT_EXISTS")
        return email, display_name, role

    def create_account(
        self,
        email: str,
        password: str,
        display_name: str,
        role: str,
        *,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        """Create + persist the account. Re-validates (the store is
        the authority); callers that already validated pay one cheap
        re-check. ``account_id`` is supplied by the Exchange so its
        HCS audit event can name the id before the account exists;
        when omitted the store mints one."""
        email, display_name, role = self.validate_registration(
            email, password, display_name, role
        )
        salt = secrets.token_bytes(32)
        account_id = account_id or "acct_" + secrets.token_hex(8)
        record = {
            "account_id": account_id,
            "email": email,
            "display_name": display_name,
            "role": role,
            "status": "active",
            "password": {
                "scheme": "scrypt",
                "n": _SCRYPT_N,
                "r": _SCRYPT_R,
                "p": _SCRYPT_P,
                "salt_hex": salt.hex(),
                "hash_hex": _hash_password(password, salt),
            },
            "linked_agents": [],
            "created_at": self.now().isoformat(),
        }
        self._accounts[account_id] = record
        self._save()
        return record

    # -- lookup ---------------------------------------------------------
    def find_by_email(self, email: str) -> dict[str, Any] | None:
        for record in self._accounts.values():
            if hmac.compare_digest(record["email"], email):
                return record
        return None

    def get_account(self, account_id: str) -> dict[str, Any] | None:
        return self._accounts.get(account_id)

    # -- sessions -------------------------------------------------------
    def login(self, email: Any, password: Any) -> dict[str, Any]:
        """Authenticate and mint a session. Unknown email and wrong
        password give the identical 401 — no user enumeration. A
        correct password on a disabled account is a 403 (checked
        after the password, so the 401 path leaks nothing)."""
        if not isinstance(email, str) or not isinstance(password, str):
            raise ExchangeError(401, "ACCOUNT_INVALID_CREDENTIALS")
        record = self.find_by_email(email.strip().lower())
        if record is None:
            # Timing equalisation (Groq review 2026-11-14): a missing
            # account must cost the same ~50 ms scrypt as a real one,
            # or the response time leaks whether the email exists.
            # The dummy hash is discarded; the answer is the same 401.
            _hash_password(password, _DUMMY_SALT)
            raise ExchangeError(401, "ACCOUNT_INVALID_CREDENTIALS")
        stored = record["password"]
        candidate = _hash_password(password, bytes.fromhex(stored["salt_hex"]))
        if not hmac.compare_digest(candidate, stored["hash_hex"]):
            raise ExchangeError(401, "ACCOUNT_INVALID_CREDENTIALS")
        if record["status"] != "active":
            raise ExchangeError(403, "ACCOUNT_DISABLED")
        self._prune_expired()
        token = secrets.token_urlsafe(32)
        expires_at = self.now() + self.session_ttl
        self._sessions[_token_hash(token)] = {
            "account_id": record["account_id"],
            "created_at": self.now().isoformat(),
            "expires_at": expires_at.isoformat(),
        }
        self._save()
        return {
            "session_token": token,
            "expires_at": expires_at.isoformat(),
            "account": public_view(record),
        }

    def authenticate(self, token: Any) -> dict[str, Any]:
        """Resolve a bearer token to its account's public view.
        Unknown token: 401 ACCOUNT_UNAUTHORIZED. Expired: the session
        is deleted and the answer is 401 SESSION_EXPIRED."""
        if not isinstance(token, str) or not token:
            raise ExchangeError(401, "ACCOUNT_UNAUTHORIZED")
        session = self._sessions.get(_token_hash(token))
        if session is None:
            raise ExchangeError(401, "ACCOUNT_UNAUTHORIZED")
        try:
            expires_at = datetime.fromisoformat(session["expires_at"])
        except (KeyError, ValueError):
            raise ExchangeError(401, "ACCOUNT_UNAUTHORIZED") from None
        if self.now() > expires_at:
            del self._sessions[_token_hash(token)]
            self._save()
            raise ExchangeError(401, "SESSION_EXPIRED")
        record = self._accounts.get(session["account_id"])
        if record is None or record["status"] != "active":
            raise ExchangeError(401, "ACCOUNT_UNAUTHORIZED")
        return public_view(record)

    def logout(self, token: Any) -> dict[str, Any]:
        """Revoke one session. Idempotent: an unknown token is still
        a 200 — there is nothing to revoke."""
        if isinstance(token, str) and token:
            if self._sessions.pop(_token_hash(token), None) is not None:
                self._save()
        return {"ok": True}

    def _prune_expired(self) -> None:
        now = self.now()
        dead = [
            key
            for key, session in self._sessions.items()
            if now > datetime.fromisoformat(session["expires_at"])
        ]
        for key in dead:
            del self._sessions[key]

    # -- CF-KYA link ----------------------------------------------------
    def check_linkable(self, account_id: str, credential_id: str) -> dict[str, Any]:
        """Pure pre-check for a link: the account must exist and the
        credential must not already be linked to any account (one
        credential = one human, so attribution is unambiguous)."""
        record = self._accounts.get(account_id)
        if record is None:
            raise ExchangeError(404, "ACCOUNT_NOT_FOUND")
        for other in self._accounts.values():
            for linked in other.get("linked_agents", []):
                if linked["credential_id"] == credential_id:
                    raise ExchangeError(409, "ACCOUNT_AGENT_ALREADY_LINKED")
        return record

    def add_linked_agent(self, account_id: str, entry: dict[str, Any]) -> dict[str, Any]:
        record = self.check_linkable(account_id, entry["credential_id"])
        record.setdefault("linked_agents", []).append(dict(entry))
        self._save()
        return record

    # -- disable --------------------------------------------------------
    def disable(self, account_id: str) -> dict[str, Any]:
        """Disable an account and revoke all its sessions. One-way in
        this version: there is no enable endpoint (an operator edits
        accounts.json to re-enable — documented in docs/API.md)."""
        record = self._accounts.get(account_id)
        if record is None:
            raise ExchangeError(404, "ACCOUNT_NOT_FOUND")
        record["status"] = "disabled"
        for key, session in list(self._sessions.items()):
            if session["account_id"] == account_id:
                del self._sessions[key]
        self._save()
        return record
