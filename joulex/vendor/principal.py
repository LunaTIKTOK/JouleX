"""Principal binding (Bind layer, v0.1).

A Principal is a legal person — a human or a corporation — on whose behalf an
agent acts. This module is NOT a KYC provider and NOT an identity document
store: it consumes attestations from existing identity providers and
corporate registries and stores only an *evidence reference* (a pointer to
the attestation held elsewhere), never identity documents and never PII
beyond a display name. A tenant_id is not a principal; tenancy is an
operational grouping, principalhood is legal accountability.

All test fixtures use invented principals. No real identity document, live
key, credential, or PII belongs in this module or in any fixture built on it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

PRINCIPAL_TYPES = ("human", "corporation")


def _parse_time(ts: str) -> datetime:
    parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone information")
    return parsed


@dataclass(frozen=True)
class Principal:
    principal_id: str
    principal_type: str  # "human" | "corporation"
    display_name: str
    verification_method: str  # e.g. "id_provider_attestation", "corporate_registry_record"
    verification_issuer: str  # the attesting provider / registry, by name
    verified_at: str  # ISO-8601, timezone-aware
    evidence_ref: str  # reference to the attestation held elsewhere, NOT the document
    public_key_hex: str  # Ed25519 public key, 64 lowercase hex chars

    def to_dict(self) -> dict[str, str]:
        return {
            "principal_id": self.principal_id,
            "principal_type": self.principal_type,
            "display_name": self.display_name,
            "verification_method": self.verification_method,
            "verification_issuer": self.verification_issuer,
            "verified_at": self.verified_at,
            "evidence_ref": self.evidence_ref,
            "public_key_hex": self.public_key_hex,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Principal":
        return cls(
            principal_id=str(data["principal_id"]),
            principal_type=str(data["principal_type"]),
            display_name=str(data["display_name"]),
            verification_method=str(data["verification_method"]),
            verification_issuer=str(data["verification_issuer"]),
            verified_at=str(data["verified_at"]),
            evidence_ref=str(data["evidence_ref"]),
            public_key_hex=str(data["public_key_hex"]).lower(),
        )


def validate_principal(principal: Principal) -> None:
    """Raise ValueError on any malformed principal. Never silently accept one."""
    if not principal.principal_id.strip():
        raise ValueError("principal_id must be non-empty")
    if principal.principal_type not in PRINCIPAL_TYPES:
        raise ValueError(f"principal_type must be one of {PRINCIPAL_TYPES}")
    if not principal.display_name.strip():
        raise ValueError("display_name must be non-empty")
    if not principal.verification_method.strip():
        raise ValueError("verification_method must be non-empty")
    if not principal.verification_issuer.strip():
        raise ValueError("verification_issuer must be non-empty")
    if not principal.evidence_ref.strip():
        raise ValueError("evidence_ref must be non-empty")
    try:
        _parse_time(principal.verified_at)
    except ValueError as exc:
        raise ValueError("verified_at must be a valid ISO-8601 timestamp with timezone") from exc
    key = principal.public_key_hex
    if len(key) != 64:
        raise ValueError("public_key_hex must be 64 hex characters (Ed25519)")
    try:
        bytes.fromhex(key)
    except ValueError as exc:
        raise ValueError("public_key_hex must be valid hex") from exc
