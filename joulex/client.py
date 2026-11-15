"""Agent-side client helper (stdlib urllib only).

Builds CF-KYA presentations and talks to the exchange's JSON API. Used
by demo.py, the API tests, and any agent that wants a ready-made caller.
The presentation timestamp comes from the server (``GET /challenge``
returns ``server_time``) so the client and the exchange agree on "now"
even when the server runs on a demo clock.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

import kya
from kya import presentation_to_header

from .admission import (
    creation_payload,
    creation_payload_hash,
    link_payload,
    link_payload_hash,
    order_payload,
    payload_hash,
)
from .attestation import mint_envelope, payload_hash_for_settlement


class ClientError(Exception):
    def __init__(self, status: int, payload: dict[str, Any]):
        super().__init__(f"HTTP {status}: {payload}")
        self.status = status
        self.payload = payload


class ExchangeClient:
    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # -- transport -----------------------------------------------------
    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base_url + path,
            method=method,
            data=data,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                payload = json.loads(raw) if raw else {}
            except ValueError:
                payload = {"error": "bad_response", "reason": raw.decode("utf-8", "replace")}
            raise ClientError(exc.code, payload) from exc

    # -- read endpoints -------------------------------------------------
    def info(self) -> dict[str, Any]:
        return self._request("GET", "/")

    def server_time(self) -> str:
        return self._request("GET", "/time")["server_time"]

    def list_markets(self) -> list[dict[str, Any]]:
        return self._request("GET", "/markets")

    def get_market(self, market_id: str) -> dict[str, Any]:
        return self._request("GET", f"/markets/{market_id}")

    def positions(self, agent_id: str) -> dict[str, Any]:
        return self._request("GET", f"/positions?agent_id={agent_id}")

    def ledger(self) -> dict[str, Any]:
        return self._request("GET", "/ledger")

    # -- markets ---------------------------------------------------------
    def create_market(
        self,
        credential: dict[str, Any] | None = None,
        agent_seed: bytes | None = None,
        *,
        operator_key: str | None = None,
        attested: bool = False,
        **fields: Any,
    ) -> dict[str, Any]:
        """Create a market, admitted (M2). Pass a credential + agent
        seed for the CF-KYA path (challenge -> sign creation payload ->
        submit with the X-CF-KYA header), or ``operator_key`` for the
        operator path. With neither, the request goes out bare and the
        server answers 401. ``attested=True`` mints a software
        attestation envelope (DESIGN.md §7.1) bound to this creation
        and attaches it to the presentation."""
        headers: dict[str, str] = {}
        body = dict(fields)
        if operator_key is not None:
            headers["X-Operator-Key"] = operator_key
        elif credential is not None:
            if agent_seed is None:
                raise ValueError("agent_seed required with credential")
            agent_id = str(credential["agent"]["agent_id"])
            body["agent_id"] = agent_id
            challenge = self.get_challenge()
            presentation = self.sign_creation(
                credential,
                agent_seed,
                challenge=challenge["challenge"],
                fields=fields,
                agent_id=agent_id,
                timestamp=challenge["server_time"],
            )
            if attested:
                presentation["attestation"] = mint_envelope(
                    credential_id=str(credential["credential_id"]),
                    challenge_nonce=challenge["challenge"],
                    payload_hash=creation_payload_hash(
                        creation_payload(agent_id=agent_id, **fields)
                    ),
                    now=datetime.fromisoformat(challenge["server_time"]),
                )
            headers["X-CF-KYA"] = presentation_to_header(presentation)
        return self._request("POST", "/markets", body=body, headers=headers)

    @staticmethod
    def sign_creation(
        credential: dict[str, Any],
        agent_seed: bytes,
        *,
        challenge: str,
        fields: dict[str, Any],
        agent_id: str,
        timestamp: str,
    ) -> dict[str, Any]:
        """Build a presentation for one market-creation payload. Raises
        ValueError if the agent seed does not match the credential's
        agent key. The credential must cover tool
        ``joulex.create_market``."""
        payload = creation_payload(agent_id=agent_id, **fields)
        return kya.create_presentation(
            credential,
            agent_seed=agent_seed,
            challenge=challenge,
            payload_hash=creation_payload_hash(payload),
            timestamp=timestamp,
        )

    def settle(
        self,
        market_id: str,
        *,
        operator_key: str | None = None,
        credential: dict[str, Any] | None = None,
        attested: bool = False,
        **fields: Any,
    ) -> dict[str, Any]:
        """Settle a market (authenticated — see
        ``Exchange.settle_market``). ``operator_key`` sends the
        ``X-Operator-Key`` header: REQUIRED when the server is
        configured with one (missing/wrong is 401 and nothing
        settles). ``attested=True`` with a ``credential`` mints a
        software attestation envelope (DESIGN.md §7.1) bound to the
        settlement payload hash and attaches it as ``attestation`` +
        ``attestation_binding``: REQUIRED for ``public_source``
        markets, where the supplied ``outcome`` IS the resolved
        outcome, so the binding matches exactly. For ``aeso_price``
        markets the envelope is optional; the hash is computed from
        the supplied fields, so with the oracle on (its fetched
        values decide the settlement) an attested settle verifies
        only when the supplied values match the oracle's."""
        headers: dict[str, str] = {}
        body = dict(fields)
        if operator_key is not None:
            headers["X-Operator-Key"] = operator_key
        if attested:
            if credential is None:
                raise ValueError("credential required with attested=True")
            challenge = self.get_challenge()
            body["attestation"] = mint_envelope(
                credential_id=str(credential["credential_id"]),
                challenge_nonce=challenge["challenge"],
                payload_hash=payload_hash_for_settlement(
                    market_id=market_id,
                    outcome=fields.get("outcome"),
                    observed_value=fields.get("observed_value"),
                    evidence=fields.get("evidence"),
                ),
                now=datetime.fromisoformat(challenge["server_time"]),
            )
            body["attestation_binding"] = {
                "credential_id": str(credential["credential_id"]),
                "challenge_nonce": challenge["challenge"],
            }
        return self._request(
            "POST", f"/markets/{market_id}/settle", body=body, headers=headers
        )

    # -- admission + orders ---------------------------------------------
    def get_challenge(self) -> dict[str, Any]:
        return self._request("GET", "/challenge")

    @staticmethod
    def sign_order(
        credential: dict[str, Any],
        agent_seed: bytes,
        *,
        challenge: str,
        payload: dict[str, Any],
        timestamp: str,
    ) -> dict[str, Any]:
        """Build a presentation for one order payload. Raises ValueError
        if the agent seed does not match the credential's agent key."""
        return kya.create_presentation(
            credential,
            agent_seed=agent_seed,
            challenge=challenge,
            payload_hash=payload_hash(payload),
            timestamp=timestamp,
        )

    def place_order(
        self, credential: dict[str, Any], agent_seed: bytes, payload: dict[str, Any],
        *, attested: bool = False,
    ) -> dict[str, Any]:
        """Full order path: fetch a fresh challenge, sign, submit.
        ``attested=True`` mints a software attestation envelope
        (DESIGN.md §7.1) bound to this order and attaches it to the
        presentation."""
        challenge = self.get_challenge()
        bound = order_payload(
            agent_id=str(payload["agent_id"]),
            market_id=str(payload["market_id"]),
            side=str(payload["side"]),
            shares=float(payload["shares"]),
            action=str(payload.get("action", "buy")),
        )
        presentation = self.sign_order(
            credential,
            agent_seed,
            challenge=challenge["challenge"],
            payload=bound,
            timestamp=challenge["server_time"],
        )
        if attested:
            presentation["attestation"] = mint_envelope(
                credential_id=str(credential["credential_id"]),
                challenge_nonce=challenge["challenge"],
                payload_hash=payload_hash(bound),
                now=datetime.fromisoformat(challenge["server_time"]),
            )
        return self.place_order_raw(bound, presentation_to_header(presentation))

    def place_order_raw(self, payload: dict[str, Any], header_value: str) -> dict[str, Any]:
        return self._request(
            "POST", "/orders", body=payload, headers={"X-CF-KYA": header_value}
        )

    # -- accounts (M1.4) ------------------------------------------------
    def register_account(
        self, *, email: str, password: str, display_name: str, role: str
    ) -> dict[str, Any]:
        """Register a human account. Returns the public account view
        (never password material)."""
        return self._request("POST", "/accounts", body={
            "email": email, "password": password,
            "display_name": display_name, "role": role,
        })

    def login(self, *, email: str, password: str) -> dict[str, Any]:
        """Log in. Returns session_token + expires_at + account."""
        return self._request("POST", "/accounts/login", body={
            "email": email, "password": password,
        })

    def me(self, session_token: str) -> dict[str, Any]:
        """The calling account's public view for a session token."""
        return self._request(
            "GET", "/accounts/me",
            headers={"Authorization": f"Bearer {session_token}"},
        )

    def logout(self, session_token: str) -> dict[str, Any]:
        """Log out (revoke the session). Idempotent."""
        return self._request(
            "POST", "/accounts/logout", body={},
            headers={"Authorization": f"Bearer {session_token}"},
        )

    @staticmethod
    def sign_link(
        credential: dict[str, Any],
        agent_seed: bytes,
        *,
        challenge: str,
        account_id: str,
        agent_id: str,
        timestamp: str,
    ) -> dict[str, Any]:
        """Build a presentation binding (account_id, agent_id) — the
        agent key holder's consent to the link. Raises ValueError if
        the agent seed does not match the credential's agent key."""
        payload = link_payload(account_id=account_id, agent_id=agent_id)
        return kya.create_presentation(
            credential,
            agent_seed=agent_seed,
            challenge=challenge,
            payload_hash=link_payload_hash(payload),
            timestamp=timestamp,
        )

    def link_agent(
        self,
        session_token: str,
        credential: dict[str, Any],
        agent_seed: bytes,
        *,
        account_id: str,
    ) -> dict[str, Any]:
        """Full link path: fetch a fresh challenge, sign the link
        payload, submit. Returns the updated public account view."""
        agent_id = str(credential["agent"]["agent_id"])
        challenge = self.get_challenge()
        presentation = self.sign_link(
            credential,
            agent_seed,
            challenge=challenge["challenge"],
            account_id=account_id,
            agent_id=agent_id,
            timestamp=challenge["server_time"],
        )
        return self._request(
            "POST", "/accounts/link-agent",
            body={"presentation": presentation, "agent_id": agent_id},
            headers={"Authorization": f"Bearer {session_token}"},
        )

    def disable_account(self, session_token: str, account_id: str) -> dict[str, Any]:
        """Admin-only: disable an account (one-way in this version)."""
        return self._request(
            "POST", f"/accounts/{account_id}/disable", body={},
            headers={"Authorization": f"Bearer {session_token}"},
        )

    # -- operator / demo endpoints --------------------------------------
    def refresh_revocation_list(self, revocation_list: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/revocation-lists", body=revocation_list)

    def set_demo_time(self, when: datetime | str) -> dict[str, Any]:
        if isinstance(when, datetime):
            when = when.isoformat()
        return self._request("POST", "/__demo/set_time", body={"time": when})
