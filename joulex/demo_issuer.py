"""Demo L3 issuer — test principals only.

Mints *real* CF-KYA L3 credentials (real Ed25519 signatures, real
delegation chains, real signed revocation lists) so the admission path
under test is the production path. The differences from a production
issuer are labelled, not hidden:

- Issuer id ``demo-issuer.joulex.local``; verification method
  ``corporate_registry_record`` is ASSERTED, not independently verified.
  No registry, bank, or IdP checked anything. Production requires a real
  attestation behind that method string and a trust-store entry added by
  a human who checked it.
- Keys are generated fresh per DemoIssuer instance and never persisted.
- Credentials are scoped to TRADE / joulex.place_order +
  joulex.create_market only, max 24h life (kya-enforced). The
  issuer can mint L1/L2 too, but only so tests can prove the exchange
  rejects them for TRADE. A credential minted with an explicit
  ``tools`` list covers only those tools, so tests can prove a
  trade-only credential cannot create a market (and vice versa).
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import ed25519
from delegation import issue_delegation
from kya import credential_unsigned_dict, issue_credential
from kya import issue_revocation_list as _issue_revocation_list

from .admission import CREATE_TOOL_NAME, INTENT_CLASS, TOOL_NAME
from .attestation import canonical_bytes

DEMO_ISSUER_ID = "demo-issuer.joulex.local"
DEMO_VERIFICATION_METHOD = "corporate_registry_record"


class DemoIssuer:
    """An independent issuer for test principals. DEMO ONLY."""

    def __init__(self, *, now_fn=None):
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.issuer_seed, issuer_pub = ed25519.generate_keypair()
        self.issuer_public_key_hex = issuer_pub.hex()

    @property
    def issuer_id(self) -> str:
        return DEMO_ISSUER_ID

    @property
    def trust_store(self) -> dict[str, str]:
        """The trust-store entry the exchange pins for this demo issuer."""
        return {DEMO_ISSUER_ID: self.issuer_public_key_hex}

    def now(self) -> datetime:
        return self._now_fn()

    def mint_agent(
        self,
        *,
        principal_id: str,
        display_name: str,
        agent_id: str,
        assurance_level: str = "L3",
        intent_classes: list[str] | None = None,
        tools: list[str] | None = None,
        credential_tools: list[str] | None = None,
        max_allocation_pct: float = 100.0,
        validity_hours: float = 24.0,
        domain: str = "example.invalid",
        attestation_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Mint a credential for one test agent. Returns the bundle the
        agent needs: credential, agent_seed, agent key, principal key
        material (test-only — a real principal never hands its seed to
        the issuer like this; here principal and issuer are both test
        fixtures in one process).

        ``attestation_policy`` (M1, DESIGN.md §7.2) is placed inside
        the credential's signed content and the credential is
        re-signed with the issuer seed, so the issuer's signature
        covers the policy exactly as a production quorum's would —
        tampering with the policy afterwards breaks the signature.
        """
        if validity_hours <= 0 or validity_hours > 24:
            raise ValueError("validity_hours must be in (0, 24]")
        principal_seed, principal_pub = ed25519.generate_keypair()
        agent_seed, agent_pub = ed25519.generate_keypair()
        principal_pub_hex = principal_pub.hex()
        agent_pub_hex = agent_pub.hex()
        now = self.now()
        issued_at = now.isoformat()
        expires_at = (now + timedelta(hours=validity_hours)).isoformat()
        intent_classes = intent_classes or [INTENT_CLASS]
        tools = tools or [TOOL_NAME, CREATE_TOOL_NAME]

        root = issue_delegation(
            delegation_id=f"del-{agent_id}-{secrets.token_hex(4)}",
            principal_id=principal_id,
            delegator_id=principal_id,
            delegatee_id=agent_id,
            delegator_seed=principal_seed,
            delegator_public_key_hex=principal_pub_hex,
            intent_classes=intent_classes,
            tools=tools,
            max_allocation_pct=max_allocation_pct,
            issued_at=issued_at,
            expires_at=expires_at,
            may_delegate=False,
            terms_hash="",
        )

        if assurance_level == "L3":
            issuer_id = DEMO_ISSUER_ID
            issuer_seed = self.issuer_seed
            issuer_pub_hex = self.issuer_public_key_hex
            method = DEMO_VERIFICATION_METHOD
        elif assurance_level == "L1":
            # Self-signed: the principal is its own issuer. Only valid
            # so tests can prove the exchange rejects L1 for TRADE.
            issuer_id = principal_id
            issuer_seed = principal_seed
            issuer_pub_hex = principal_pub_hex
            method = "self_asserted"
        elif assurance_level == "L2":
            issuer_id = DEMO_ISSUER_ID
            issuer_seed = self.issuer_seed
            issuer_pub_hex = self.issuer_public_key_hex
            method = "domain_record"
        else:
            raise ValueError(f"unknown assurance_level {assurance_level!r}")

        credential = issue_credential(
            credential_id=f"cred-{agent_id}-{secrets.token_hex(4)}",
            principal_id=principal_id,
            principal_type="corporation",
            display_name=display_name,
            domain=domain,
            agent_id=agent_id,
            agent_public_key_hex=agent_pub_hex,
            intent_classes=intent_classes,
            tools=credential_tools if credential_tools is not None else tools,
            max_allocation_pct=max_allocation_pct,
            spend_cap_usd=None,
            issuer_id=issuer_id,
            issuer_public_key_hex=issuer_pub_hex,
            issuer_seed=issuer_seed,
            assurance_level=assurance_level,
            verification_method=method,
            issued_at=issued_at,
            expires_at=expires_at,
            delegation_chain=[root.to_dict()],
            terms_hash="",
        )
        if attestation_policy is not None:
            # Inside the signed content: insert, then re-sign with the
            # issuer seed over the canonical unsigned bytes (the same
            # bytes kya verifies), so the policy is quorum-attested.
            credential["attestation_policy"] = dict(attestation_policy)
            unsigned = credential_unsigned_dict(credential)
            credential["signature_hex"] = ed25519.sign(
                canonical_bytes(unsigned),
                issuer_seed,
                bytes.fromhex(issuer_pub_hex),
            ).hex()
        return {
            "credential": credential,
            "credential_id": credential["credential_id"],
            "agent_seed": agent_seed,
            "agent_public_key_hex": agent_pub_hex,
            "agent_id": agent_id,
            "principal_id": principal_id,
            "principal_seed": principal_seed,
            "principal_public_key_hex": principal_pub_hex,
            "delegation": root.to_dict(),
        }

    def revocation_list_for(
        self,
        *,
        issuer_id: str,
        issuer_seed: bytes,
        issuer_public_key_hex: str,
        revoked_credential_ids: list[str] | None = None,
        revoked_delegation_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        return _issue_revocation_list(
            issuer_id=issuer_id,
            issuer_seed=issuer_seed,
            issuer_public_key_hex=issuer_public_key_hex,
            revoked_credential_ids=revoked_credential_ids or [],
            revoked_delegation_ids=revoked_delegation_ids or [],
            issued_at=self.now().isoformat(),
        )

    def revocation_list(
        self,
        revoked_credential_ids: list[str] | None = None,
        revoked_delegation_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Fresh signed revocation list from the demo issuer."""
        return self.revocation_list_for(
            issuer_id=DEMO_ISSUER_ID,
            issuer_seed=self.issuer_seed,
            issuer_public_key_hex=self.issuer_public_key_hex,
            revoked_credential_ids=revoked_credential_ids,
            revoked_delegation_ids=revoked_delegation_ids,
        )
