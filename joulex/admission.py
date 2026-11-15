"""Admission: CF-KYA verification at order time.

Thin wrapper over the vendored ``kya`` module. The exchange issues a
fresh single-use challenge per order request, then verifies the
presentation with ``require_l3=True`` (TRADE is consequential — L1 and
L2 never trade here) against its trust store and a fresh signed
revocation list per issuer. Fail closed at every step.

The order payload bound by the presentation is exactly
``{"agent_id", "market_id", "shares", "side", "action"}`` — canonical
JSON, SHA-256. The server recomputes the hash from those five fields;
a presentation signed over any other payload fails with
``KYA_PAYLOAD_MISMATCH``.

The market-creation payload bound by a creation presentation is
exactly the fields in :func:`creation_payload` — the market-defining
fields the creator supplies, plus ``agent_id``. The server recomputes
the hash from the same normalized fields; ``resolves_at`` is hashed
verbatim as sent (the client hashes the exact string it sends), so no
timestamp re-formatting can diverge the hash. ``outcomes`` binds as
the normalized list (``["YES", "NO"]`` when omitted), so a presentation
signed for one outcome set cannot create a market with another. A
presentation signed over a different creation fails with
``KYA_PAYLOAD_MISMATCH``. The JouleX fields (``contract_type``,
``strike``, ``window_start``, ``window_end``) bind alongside the
shared fields; server-derived fields (the aeso_price source
URL/description) are NOT bound — the server derives them
deterministically from the bound fields.

Challenge store: persistent in v1 (M3). The verifier loads issued +
seen challenges from ``data/challenges.json`` through
:class:`PersistentChallengeStore` and saves after every mutation, so
the replay defence survives a restart. With no store supplied the
verifier falls back to in-memory only (used by unit tests that do not
need persistence). A corrupt store refuses startup (fail closed).
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import kya
from kya import verify_presentation, verify_revocation_list

from .attestation import verify_with_policy
from .challenges import PersistentChallengeStore
from .lmsr import DEFAULT_B
from .markets import normalize_outcomes

INTENT_CLASS = "TRADE"
TOOL_NAME = "joulex.place_order"
CREATE_TOOL_NAME = "joulex.create_market"
CHALLENGE_TTL_SECONDS = 300
PAYLOAD_FIELDS = ("agent_id", "market_id", "shares", "side", "action")
# Fields of a market-creation request that the presentation binds, in
# canonical (sorted-key) order. ``b`` defaults to DEFAULT_B when the
# client omits it; absent optional fields bind as None; ``outcomes``
# binds as the normalized list (["YES", "NO"] when omitted).
# ``contract_type`` defaults to "generic" when omitted (mirrors
# ``new_market``); ``window_start``/``window_end`` bind verbatim as
# sent, like ``resolves_at``.
CREATE_PAYLOAD_FIELDS = (
    "agent_id",
    "b",
    "contract_type",
    "outcomes",
    "question",
    "resolution_type",
    "resolves_at",
    "source_description",
    "source_url",
    "strike",
    "window_end",
    "window_start",
)


def canonical_bytes(data: dict[str, Any]) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def order_payload(
    *, agent_id: str, market_id: str, side: str, shares: float, action: str = "buy"
) -> dict[str, Any]:
    """The exact payload a presentation binds. Both client and server
    build it from these five fields — nothing else is hashed."""
    return {
        "agent_id": agent_id,
        "market_id": market_id,
        "shares": shares,
        "side": side,
        "action": action,
    }


def payload_hash(payload: dict[str, Any]) -> str:
    """SHA-256 over the canonical JSON of the five payload fields.

    Normalizes through ``order_payload`` first (shares coerced to float,
    action defaulted), so a client signing ``shares: 10`` and a server
    recomputing from ``shares: 10.0`` hash the same bytes."""
    bound = order_payload(
        agent_id=str(payload["agent_id"]),
        market_id=str(payload["market_id"]),
        side=str(payload["side"]),
        shares=float(payload["shares"]),
        action=str(payload.get("action", "buy")),
    )
    return hashlib.sha256(canonical_bytes(bound)).hexdigest()


def link_payload(*, account_id: str, agent_id: str) -> dict[str, Any]:
    """The exact payload a link presentation binds (M1.4): the agent
    key holder consents to bind this agent to this human account.
    The ``type`` tag keeps a link hash from ever colliding with an
    order or creation hash."""
    return {
        "type": "joulex.account_link",
        "account_id": account_id,
        "agent_id": agent_id,
    }


def link_payload_hash(payload: dict[str, Any]) -> str:
    """SHA-256 over the canonical JSON of the normalized link payload
    (same normalization rule as ``payload_hash``: both sides hash the
    same bytes regardless of how the caller spelled the fields)."""
    bound = link_payload(
        account_id=str(payload["account_id"]),
        agent_id=str(payload["agent_id"]),
    )
    return hashlib.sha256(canonical_bytes(bound)).hexdigest()


def creation_payload(
    *,
    agent_id: str,
    question: str,
    resolution_type: str,
    resolves_at: str,
    contract_type: str | None = None,
    strike: float | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
    source_url: str | None = None,
    source_description: str | None = None,
    outcomes: list[str] | None = None,
    b: float = DEFAULT_B,
) -> dict[str, Any]:
    """The exact payload a creation presentation binds.

    The market-defining fields the creator supplies, plus ``agent_id``.
    Normalization mirrors ``new_market`` (question stripped, strike/b
    coerced to float, outcomes normalized via ``normalize_outcomes`` —
    the same function, so the two can never diverge) so a client signing
    ``strike: 50`` and a server recomputing from ``strike: 50.0`` hash
    the same bytes. ``resolves_at``, ``window_start`` and
    ``window_end`` are kept verbatim as sent — the client hashes the
    exact strings it sends, so no timestamp re-formatting can diverge
    the hash. Server-derived fields (the aeso_price source
    URL/description) are NOT bound: the server derives them
    deterministically from the bound fields."""
    return {
        "agent_id": str(agent_id),
        "question": str(question).strip(),
        "resolution_type": str(resolution_type),
        "resolves_at": str(resolves_at),
        "contract_type": str(contract_type) if contract_type else "generic",
        "strike": float(strike) if strike is not None else None,
        "window_start": str(window_start) if window_start else None,
        "window_end": str(window_end) if window_end else None,
        "source_url": str(source_url).strip() if source_url else None,
        "source_description": (
            str(source_description).strip() if source_description else None
        ),
        "outcomes": normalize_outcomes(outcomes),
        "b": float(b),
    }


def creation_payload_hash(payload: dict[str, Any]) -> str:
    """SHA-256 over the canonical JSON of the creation payload fields.

    Normalizes through ``creation_payload`` first, so a client and the
    server hash the same bytes for the same creation."""
    bound = creation_payload(
        agent_id=str(payload["agent_id"]),
        question=str(payload["question"]),
        resolution_type=str(payload["resolution_type"]),
        resolves_at=str(payload["resolves_at"]),
        contract_type=payload.get("contract_type"),
        strike=payload.get("strike"),
        window_start=payload.get("window_start"),
        window_end=payload.get("window_end"),
        source_url=payload.get("source_url"),
        source_description=payload.get("source_description"),
        outcomes=payload.get("outcomes"),
        b=payload.get("b", DEFAULT_B),
    )
    return hashlib.sha256(canonical_bytes(bound)).hexdigest()


class ExchangeVerifier:
    """The exchange's admission judge."""

    def __init__(
        self,
        *,
        trusted_issuers: dict[str, str],
        domain_keys: dict[str, str] | None = None,
        now_fn=None,
        challenge_ttl_seconds: int = CHALLENGE_TTL_SECONDS,
        challenge_store: PersistentChallengeStore | None = None,
        pinned_measurements: list[str] | None = None,
        provenance_dir: str | None = None,
    ):
        self.trusted_issuers = dict(trusted_issuers)
        self.domain_keys = dict(domain_keys or {})
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._challenge_ttl = timedelta(seconds=challenge_ttl_seconds)
        # M1: the verifier's pinned measurement set (operator-maintained
        # data/measurements.json, loaded by the exchange) and the
        # provenance directory for link-backed evidence. Empty/None
        # means no verifier-level pinning is enforced; a credential's
        # attestation_policy.measurements still applies when present.
        self.pinned_measurements = list(pinned_measurements or [])
        self.provenance_dir = provenance_dir
        self._issued_challenges: dict[str, datetime] = {}
        self._seen_challenges: set[str] = set()
        # Expiry per seen challenge, so pruning can forget a seen
        # challenge once its challenge expires (a replay then fails
        # as unknown anyway). The set is what kya checks; the dict is
        # what gets persisted.
        self._seen_expiries: dict[str, datetime] = {}
        self._challenge_store = challenge_store
        if challenge_store is not None:
            issued, seen = challenge_store.load()
            self._issued_challenges = issued
            self._seen_expiries = seen
            self._seen_challenges = set(seen)
        self._revocation_lists: dict[str, dict[str, Any]] = {}

    def now(self) -> datetime:
        return self._now_fn()

    # -- challenges ----------------------------------------------------
    def _persist_challenges(self) -> None:
        """Save issued + seen maps after a mutation (M3). No-op when
        the verifier runs without a store (in-memory tests)."""
        if self._challenge_store is not None:
            self._challenge_store.save(
                self._issued_challenges, self._seen_expiries
            )

    def issue_challenge(self) -> str:
        now = self.now()
        self._issued_challenges = {
            n: exp for n, exp in self._issued_challenges.items() if exp > now
        }
        self._seen_expiries = {
            n: exp for n, exp in self._seen_expiries.items() if exp > now
        }
        self._seen_challenges = {
            n for n in self._seen_challenges if n in self._seen_expiries
        }
        nonce = secrets.token_hex(16)
        self._issued_challenges[nonce] = now + self._challenge_ttl
        self._persist_challenges()
        return nonce

    # -- revocation lists ----------------------------------------------
    def refresh_revocation_list(
        self, revocation_list: dict[str, Any], *, issuer_public_key_hex: str | None = None
    ) -> tuple[bool, str]:
        """Accept a signed revocation list only if it verifies against
        the pinned key for its issuer (or an operator-supplied key for
        issuers outside the trust store, e.g. an L1 self-issuer). A bad
        or stale list is rejected and the previous list stands — and
        with no list at all, verification fails closed."""
        if not isinstance(revocation_list, dict):
            return False, "KYA_REVOCATION_LIST_INVALID"
        issuer_id = str(revocation_list.get("issuer_id", ""))
        key = self.trusted_issuers.get(issuer_id) or issuer_public_key_hex
        if not key:
            return False, "EXCHANGE_ISSUER_UNKNOWN"
        ok, reason = verify_revocation_list(
            revocation_list,
            issuer_public_key_hex=key,
            issuer_id=issuer_id,
            now=self.now(),
        )
        if not ok:
            return False, reason
        self._revocation_lists[issuer_id] = revocation_list
        return True, "OK"

    # -- verification --------------------------------------------------
    def _verify(
        self,
        presentation: dict[str, Any] | None,
        expected_hash: str,
        *,
        intent_class: str,
        tool_name: str,
        agent_id: str,
    ) -> tuple[bool, str, dict[str, Any]]:
        """Shared presentation verification for one bound payload hash.

        Challenge checks run first (unknown/expired before any crypto),
        then ``kya.verify_presentation`` with ``require_l3=True`` — the
        intent class is consequential, so L1/L2 never pass here. A
        failed verification does NOT consume the challenge (the kya
        module only marks it seen on success), so the agent can retry
        with a correct presentation. Returns (ok, reason, info)."""
        empty: dict[str, Any] = {}
        if presentation is None:
            return False, "KYA_PRESENTATION_MISSING", empty
        if not isinstance(presentation, dict) or "challenge" not in presentation:
            return False, "KYA_PRESENTATION_INVALID", empty
        challenge = str(presentation["challenge"])
        expiry = self._issued_challenges.get(challenge)
        if expiry is None:
            return False, "EXCHANGE_CHALLENGE_UNKNOWN", empty
        if self.now() > expiry:
            return False, "EXCHANGE_CHALLENGE_EXPIRED", empty
        credential = presentation.get("credential")
        if not isinstance(credential, dict):
            return False, "KYA_PRESENTATION_INVALID", empty
        try:
            cred_agent = credential["agent"]
            cred_agent_id = str(cred_agent["agent_id"])
            cred_agent_key = str(cred_agent["public_key_hex"]).lower()
            issuer_id = str(credential["issuer"]["id"])
        except (KeyError, TypeError):
            return False, "KYA_PRESENTATION_INVALID", empty
        # Chain verification needs the leaf agent's key; it comes from
        # the credential itself (the credential signature, checked by
        # kya, covers the whole chain — same pattern as the bridge).
        agent_public_keys = {cred_agent_id: cred_agent_key}
        ok, reason, info = verify_presentation(
            presentation,
            expected_payload_hash=expected_hash,
            expected_challenge=challenge,
            intent_class=intent_class,
            tool_name=tool_name,
            agent_id=agent_id,
            agent_public_keys=agent_public_keys,
            trusted_issuers=self.trusted_issuers,
            domain_keys=self.domain_keys or None,
            revocation_list=self._revocation_lists.get(issuer_id),
            require_l3=True,
            seen_challenges=self._seen_challenges,
            now=self.now(),
        )
        if not ok:
            return False, reason, empty
        # M1 attestation (DESIGN.md §7.2; VERIFIABLE_COMPUTE.md §5.3
        # step 16 — after the cheap cryptographic checks, before any
        # irreversible consumption). The policy rides inside the
        # credential's signed content, so the quorum attests to it;
        # the envelope rides on the presentation. A failed attestation
        # check un-burns the challenge (kya marked it seen on its own
        # success), preserving the "failed verification never burns a
        # challenge" invariant, so the agent can retry with a fresh
        # envelope.
        credential_id = str(credential.get("credential_id", ""))
        policy = credential.get("attestation_policy")
        envelope = presentation.get("attestation")
        att_ok, att_reason, att_info = verify_with_policy(
            envelope,
            credential_id=credential_id,
            challenge_nonce=challenge,
            payload_hash=expected_hash,
            policy=policy,
            pinned_measurements=self.pinned_measurements or None,
            now=self.now(),
            provenance_dir=self.provenance_dir,
        )
        if not att_ok:
            self._seen_challenges.discard(challenge)
            self._seen_expiries.pop(challenge, None)
            self._persist_challenges()
            return False, att_reason, empty
        info = dict(info)
        info["attestation"] = att_info
        # Success: kya has just added the challenge to
        # ``self._seen_challenges``. Record its expiry and persist, so
        # the replay defence survives a restart (M3). A failed
        # verification never reaches here, so it never burns a
        # challenge.
        self._seen_expiries[challenge] = self._issued_challenges.get(
            challenge, self.now() + self._challenge_ttl
        )
        self._persist_challenges()
        return True, "OK", info

    def verify_order(
        self,
        presentation: dict[str, Any] | None,
        payload: dict[str, Any],
        *,
        agent_id: str,
    ) -> tuple[bool, str, dict[str, Any]]:
        """Verify one order's presentation. Returns (ok, reason, info).
        Reasons are the kya reason strings verbatim, so operators and
        tests can match on them."""
        return self._verify(
            presentation,
            payload_hash(payload),
            intent_class=INTENT_CLASS,
            tool_name=TOOL_NAME,
            agent_id=agent_id,
        )

    def verify_creation(
        self,
        presentation: dict[str, Any] | None,
        payload: dict[str, Any],
        *,
        agent_id: str,
    ) -> tuple[bool, str, dict[str, Any]]:
        """Verify one market-creation presentation (M2). Same admission
        as orders — L3 only, fresh single-use challenge, payload bound
        — but the credential must cover tool ``joulex.create_market``
        instead of ``joulex.place_order``. Returns (ok, reason, info)."""
        return self._verify(
            presentation,
            creation_payload_hash(payload),
            intent_class=INTENT_CLASS,
            tool_name=CREATE_TOOL_NAME,
            agent_id=agent_id,
        )

    def verify_link(
        self,
        presentation: dict[str, Any] | None,
        *,
        account_id: str,
        agent_id: str,
    ) -> tuple[bool, str, dict[str, Any]]:
        """Verify one account-link presentation (M1.4). Same admission
        as orders — L3 only, fresh single-use challenge, payload bound
        to (account_id, agent_id) — and the credential must cover tool
        ``joulex.place_order``, so the linked agent is one that can
        actually trade. A bare credential (no presentation) proves no
        key control and fails here. Returns (ok, reason, info)."""
        return self._verify(
            presentation,
            link_payload_hash(link_payload(account_id=account_id, agent_id=agent_id)),
            intent_class=INTENT_CLASS,
            tool_name=TOOL_NAME,
            agent_id=agent_id,
        )
