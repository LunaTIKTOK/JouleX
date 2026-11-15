"""Signed, revocable delegation chains (Bind layer, v0.1).

Chain shape: principal -> agent -> sub-agent -> ... Each link is signed with
the delegator's Ed25519 key, carries its own scope and expiry, and (except
for the root) names the previous link. Verification is link by link; any
broken link BLOCKs the action. Scope may only narrow down a chain, never
widen: a sub-agent can never hold more authority than the agent that
delegated to it.

Scope semantics (fail-closed):
- intent_classes: the intent classes the delegatee may act on; "*" means all.
  An empty list means no intent class is allowed.
- tools: the tool names the delegatee may use; "*" means all. Empty = none.
- max_allocation_pct: cap on any requested allocation for the action. A
  chain link with a lower cap narrows every link below it.

Revocation is checked at verification time against a RevocationList. A
revoked delegation, or a revoked principal, invalidates every chain that
depends on it. Revocation is not retroactive: actions already executed under
a then-valid delegation are not undone by a later revocation; they remain
in the audit trail.

Timestamps are ISO-8601 with timezone information. All comparisons use a
single caller-supplied `now` (default: current UTC) so verification is
deterministic for a fixed input.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import ed25519
from principal import Principal, validate_principal

# Stable, machine-readable verification outcomes. Interceptor BLOCK reasons
# and benchmark/sidecar outputs use these strings verbatim.
OK = "OK"
NO_DELEGATION = "NO_DELEGATION"
CHAIN_BROKEN = "DELEGATION_CHAIN_BROKEN"
SIGNATURE_INVALID = "DELEGATION_SIGNATURE_INVALID"
EXPIRED = "DELEGATION_EXPIRED"
NOT_YET_VALID = "DELEGATION_NOT_YET_VALID"
REVOKED = "DELEGATION_REVOKED"
PRINCIPAL_REVOKED = "PRINCIPAL_REVOKED"
AGENT_MISMATCH = "DELEGATION_AGENT_MISMATCH"
SCOPE_EXCEEDED = "DELEGATION_SCOPE_EXCEEDED"
SCOPE_WIDENED = "DELEGATION_SCOPE_WIDENED"
UNKNOWN_PRINCIPAL = "UNKNOWN_PRINCIPAL"
UNKNOWN_DELEGATOR_KEY = "UNKNOWN_DELEGATOR_KEY"
INVALID_DELEGATION = "INVALID_DELEGATION"
# CF-KYA swarm rules (Delegation chains and swarms): depth cap, delegation
# permission, expiry narrowing, and terms-hash consistency, all enforced
# mechanically by verify_chain — narrowing is checked, never trusted.
DEPTH_EXCEEDED = "DELEGATION_DEPTH_EXCEEDED"
MAY_NOT_DELEGATE = "DELEGATION_MAY_NOT_DELEGATE"
EXPIRY_EXCEEDS_PARENT = "DELEGATION_EXPIRY_EXCEEDS_PARENT"
TERMS_MISMATCH = "DELEGATION_TERMS_MISMATCH"

# Depth cap: at most 4 delegation links, counting the principal -> agent
# link as link 1 (spec wording: "4 links including the principal"). A chain
# of 5 links is rejected. This is a builder interpretation of an ambiguous
# phrase — the constant is the single place to change it.
MAX_CHAIN_LINKS = 4


def _parse_time(ts: str) -> datetime:
    parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone information")
    return parsed


def _canonical_bytes(data: dict[str, Any]) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


@dataclass(frozen=True)
class Delegation:
    delegation_id: str
    principal_id: str
    delegator_id: str
    delegatee_id: str
    delegator_public_key_hex: str
    intent_classes: tuple[str, ...]
    tools: tuple[str, ...]
    max_allocation_pct: float
    issued_at: str
    expires_at: str
    prev_delegation_id: str | None
    signature_hex: str
    # CF-KYA swarm / binding fields (defaulted so pre-swarm serializations
    # still parse): may_delegate gates child links; terms_hash is the SHA-256
    # of the agency agreement text governing this delegation ("" = no
    # agreement referenced — crypto binding only, no contractual claim).
    may_delegate: bool = False
    terms_hash: str = ""

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "delegation_id": self.delegation_id,
            "principal_id": self.principal_id,
            "delegator_id": self.delegator_id,
            "delegatee_id": self.delegatee_id,
            "delegator_public_key_hex": self.delegator_public_key_hex,
            "intent_classes": list(self.intent_classes),
            "tools": list(self.tools),
            "max_allocation_pct": self.max_allocation_pct,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "prev_delegation_id": self.prev_delegation_id,
            "may_delegate": self.may_delegate,
            "terms_hash": self.terms_hash,
        }

    def to_dict(self) -> dict[str, Any]:
        data = self.unsigned_dict()
        data["signature_hex"] = self.signature_hex
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Delegation":
        return cls(
            delegation_id=str(data["delegation_id"]),
            principal_id=str(data["principal_id"]),
            delegator_id=str(data["delegator_id"]),
            delegatee_id=str(data["delegatee_id"]),
            delegator_public_key_hex=str(data["delegator_public_key_hex"]).lower(),
            intent_classes=tuple(str(x) for x in data.get("intent_classes", [])),
            tools=tuple(str(x) for x in data.get("tools", [])),
            max_allocation_pct=float(data["max_allocation_pct"]),
            issued_at=str(data["issued_at"]),
            expires_at=str(data["expires_at"]),
            prev_delegation_id=(str(data["prev_delegation_id"]) if data.get("prev_delegation_id") else None),
            signature_hex=str(data["signature_hex"]).lower(),
            may_delegate=bool(data.get("may_delegate", False)),
            terms_hash=str(data.get("terms_hash", "")),
        )


def validate_delegation_shape(delegation: Delegation) -> None:
    if not delegation.delegation_id.strip():
        raise ValueError("delegation_id must be non-empty")
    if not delegation.principal_id.strip() or not delegation.delegator_id.strip() or not delegation.delegatee_id.strip():
        raise ValueError("principal_id, delegator_id and delegatee_id must be non-empty")
    if delegation.delegator_id == delegation.delegatee_id:
        raise ValueError("delegator and delegatee must differ (no self-delegation)")
    if len(delegation.delegator_public_key_hex) != 64:
        raise ValueError("delegator_public_key_hex must be 64 hex characters (Ed25519)")
    bytes.fromhex(delegation.delegator_public_key_hex)
    if not (0.0 <= delegation.max_allocation_pct <= 100.0):
        raise ValueError("max_allocation_pct must be in [0, 100]")
    issued = _parse_time(delegation.issued_at)
    expires = _parse_time(delegation.expires_at)
    if expires < issued:
        raise ValueError("expires_at must be >= issued_at")
    if len(delegation.signature_hex) != 128:
        raise ValueError("signature_hex must be 128 hex characters (Ed25519)")
    if delegation.terms_hash:
        if len(delegation.terms_hash) != 64:
            raise ValueError("terms_hash must be 64 hex characters (SHA-256) when set")
        bytes.fromhex(delegation.terms_hash)


def terms_hash_of(agreement_text: str) -> str:
    """SHA-256 hex of agency agreement text. The hash is what lives in the
    delegation and the credential; the text itself is held by the parties
    (and, before real use, blessed by a lawyer — see
    docs/agency_agreement_template.md). Two parties holding the same hash
    hold the same agreement text, byte for byte."""
    import hashlib

    return hashlib.sha256(agreement_text.encode("utf-8")).hexdigest()


def issue_delegation(
    *,
    delegation_id: str,
    principal_id: str,
    delegator_id: str,
    delegatee_id: str,
    delegator_seed: bytes,
    delegator_public_key_hex: str,
    intent_classes: list[str],
    tools: list[str],
    max_allocation_pct: float,
    issued_at: str,
    expires_at: str,
    prev_delegation_id: str | None = None,
    may_delegate: bool = False,
    terms_hash: str = "",
    # Shared-budget partition wiring (v0.2, 2026-11-01): when all three are
    # given, the partition is allocated BEFORE signing, so a failed
    # allocation issues no delegation at all. The partition key is the
    # delegation_id (unique per link; delegatee_id is not — one agent can
    # hold several links). Strict verifiers must therefore pass
    # child_id = the leaf link's delegation_id for the partition to line
    # up. When omitted, issuance is unchanged and any shared-budget claim
    # on the child is advisory only (see budget_status).
    budget_ledger: Any = None,
    budget_id: str | None = None,
    partition_cap_usd: float | None = None,
) -> Delegation:
    """Create and sign a delegation link. The caller holds the delegator seed;
    the seed is never stored on the Delegation object."""
    if (budget_ledger is not None) or (budget_id is not None) or (partition_cap_usd is not None):
        if budget_ledger is None or budget_id is None or partition_cap_usd is None:
            raise ValueError("budget_ledger, budget_id and partition_cap_usd must be given together")
        budget_ledger.allocate_child(str(budget_id), delegation_id, float(partition_cap_usd))
    unsigned = {
        "delegation_id": delegation_id,
        "principal_id": principal_id,
        "delegator_id": delegator_id,
        "delegatee_id": delegatee_id,
        "delegator_public_key_hex": delegator_public_key_hex.lower(),
        "intent_classes": list(intent_classes),
        "tools": list(tools),
        "max_allocation_pct": float(max_allocation_pct),
        "issued_at": issued_at,
        "expires_at": expires_at,
        "prev_delegation_id": prev_delegation_id,
        "may_delegate": bool(may_delegate),
        "terms_hash": terms_hash,
    }
    public_key = bytes.fromhex(delegator_public_key_hex)
    signature = ed25519.sign(_canonical_bytes(unsigned), delegator_seed, public_key).hex()
    delegation = Delegation(signature_hex=signature, **{**unsigned, "intent_classes": tuple(intent_classes), "tools": tuple(tools)})
    validate_delegation_shape(delegation)
    return delegation


def verify_delegation_signature(delegation: Delegation) -> bool:
    return ed25519.verify_hex(delegation.signature_hex, _canonical_bytes(delegation.unsigned_dict()), delegation.delegator_public_key_hex)


def _class_set(classes: tuple[str, ...]) -> set[str] | None:
    if "*" in classes:
        return None  # None = all
    return set(classes)


def scope_covers(
    delegation: Delegation,
    *,
    intent_class: str,
    tool_name: str,
    allocation_pct: float | None = None,
) -> bool:
    classes = _class_set(delegation.intent_classes)
    if classes is not None and intent_class not in classes:
        return False
    tools = _class_set(delegation.tools)
    if tools is not None and tool_name not in tools:
        return False
    if allocation_pct is not None and allocation_pct > delegation.max_allocation_pct:
        return False
    return True


def scope_is_subset(child: Delegation, parent: Delegation) -> bool:
    parent_classes = _class_set(parent.intent_classes)
    if parent_classes is not None:
        child_classes = _class_set(child.intent_classes)
        if child_classes is None or not child_classes.issubset(parent_classes):
            return False
    parent_tools = _class_set(parent.tools)
    if parent_tools is not None:
        child_tools = _class_set(child.tools)
        if child_tools is None or not child_tools.issubset(parent_tools):
            return False
    if child.max_allocation_pct > parent.max_allocation_pct:
        return False
    return True


class InMemoryRevocationList:
    """Revocation registry (test / single-process use)."""

    def __init__(self) -> None:
        self._delegations: set[str] = set()
        self._principals: set[str] = set()

    def revoke_delegation(self, delegation_id: str) -> None:
        self._delegations.add(delegation_id)

    def revoke_principal(self, principal_id: str) -> None:
        self._principals.add(principal_id)

    def is_delegation_revoked(self, delegation_id: str) -> bool:
        return delegation_id in self._delegations

    def is_principal_revoked(self, principal_id: str) -> bool:
        return principal_id in self._principals

    def revoked_delegation_ids(self) -> set[str]:
        """Full revoked set, for propagation hooks: a hook that only
        asks is_*_revoked one id at a time can never discover what was
        revoked. Returns a snapshot copy."""
        return set(self._delegations)

    def revoked_principals(self) -> set[str]:
        return set(self._principals)


class SQLiteRevocationList:
    """Persistent revocation registry. Revocations survive restarts; that is
    the whole point — a restart must never resurrect a revoked delegation."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        with sqlite3.connect(self._path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS revoked_delegations(delegation_id TEXT PRIMARY KEY, revoked_at TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS revoked_principals(principal_id TEXT PRIMARY KEY, revoked_at TEXT NOT NULL)")
            conn.commit()

    def _now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    def revoke_delegation(self, delegation_id: str) -> None:
        with self._lock, sqlite3.connect(self._path) as conn:
            conn.execute(
                "INSERT INTO revoked_delegations(delegation_id, revoked_at) VALUES(?, ?) ON CONFLICT(delegation_id) DO NOTHING",
                (delegation_id, self._now()),
            )
            conn.commit()

    def revoke_principal(self, principal_id: str) -> None:
        with self._lock, sqlite3.connect(self._path) as conn:
            conn.execute(
                "INSERT INTO revoked_principals(principal_id, revoked_at) VALUES(?, ?) ON CONFLICT(principal_id) DO NOTHING",
                (principal_id, self._now()),
            )
            conn.commit()

    def is_delegation_revoked(self, delegation_id: str) -> bool:
        with self._lock, sqlite3.connect(self._path) as conn:
            return conn.execute("SELECT 1 FROM revoked_delegations WHERE delegation_id = ?", (delegation_id,)).fetchone() is not None

    def is_principal_revoked(self, principal_id: str) -> bool:
        with self._lock, sqlite3.connect(self._path) as conn:
            return conn.execute("SELECT 1 FROM revoked_principals WHERE principal_id = ?", (principal_id,)).fetchone() is not None

    def revoked_delegation_ids(self) -> set[str]:
        """Full revoked set, for propagation hooks (see the in-memory
        class: a hook that only asks is_*_revoked one id at a time can
        never discover what was revoked)."""
        with self._lock, sqlite3.connect(self._path) as conn:
            return {row[0] for row in conn.execute("SELECT delegation_id FROM revoked_delegations")}

    def revoked_principals(self) -> set[str]:
        with self._lock, sqlite3.connect(self._path) as conn:
            return {row[0] for row in conn.execute("SELECT principal_id FROM revoked_principals")}


def verify_chain(
    chain: list[Delegation],
    *,
    principals: dict[str, Principal],
    agent_public_keys: dict[str, str],
    intent_class: str,
    tool_name: str,
    agent_id: str,
    allocation_pct: float | None = None,
    revocations: InMemoryRevocationList | SQLiteRevocationList | None = None,
    now: datetime | None = None,
) -> tuple[bool, str, Delegation | None]:
    """Verify a delegation chain link by link. Returns (ok, reason, leaf).

    Swarm rules enforced mechanically here, in addition to signature,
    expiry, revocation, and scope-narrowing checks:
    - depth cap: at most MAX_CHAIN_LINKS links (4, counting the
      principal -> agent link as link 1);
    - a link may have a child link only when it carries may_delegate;
    - a child link's expiry must be no later than its parent link's;
    - all links must carry the same terms_hash (one agreement per chain;
      "" throughout means no agreement referenced).
    """
    current = now or datetime.now(timezone.utc)
    if not chain:
        return False, NO_DELEGATION, None
    if len(chain) > MAX_CHAIN_LINKS:
        return False, DEPTH_EXCEEDED, None
    revocations = revocations or InMemoryRevocationList()
    previous: Delegation | None = None
    for link in chain:
        try:
            validate_delegation_shape(link)
        except ValueError:
            return False, INVALID_DELEGATION, None
        if revocations.is_principal_revoked(link.principal_id):
            return False, PRINCIPAL_REVOKED, None
        if revocations.is_delegation_revoked(link.delegation_id):
            return False, REVOKED, None
        try:
            issued = _parse_time(link.issued_at)
            expires = _parse_time(link.expires_at)
        except ValueError:
            return False, INVALID_DELEGATION, None
        if current < issued:
            return False, NOT_YET_VALID, None
        if current > expires:
            return False, EXPIRED, None
        if previous is None:
            # Root link: delegator must be the principal, keyed by the principal.
            principal = principals.get(link.principal_id)
            if principal is None:
                return False, UNKNOWN_PRINCIPAL, None
            try:
                validate_principal(principal)
            except ValueError:
                return False, UNKNOWN_PRINCIPAL, None
            if link.delegator_id != link.principal_id:
                return False, CHAIN_BROKEN, None
            if link.delegator_public_key_hex != principal.public_key_hex.lower():
                return False, CHAIN_BROKEN, None
            if link.prev_delegation_id is not None:
                return False, CHAIN_BROKEN, None
        else:
            if link.delegator_id != previous.delegatee_id:
                return False, CHAIN_BROKEN, None
            if link.prev_delegation_id != previous.delegation_id:
                return False, CHAIN_BROKEN, None
            if link.principal_id != previous.principal_id:
                return False, CHAIN_BROKEN, None
            expected_key = agent_public_keys.get(link.delegator_id)
            if expected_key is None or expected_key.lower() != link.delegator_public_key_hex:
                return False, UNKNOWN_DELEGATOR_KEY, None
            # Swarm rules: delegation permission, expiry narrowing, one
            # agreement per chain — checked before scope and signature so a
            # structural violation reports as itself, not as a crypto failure.
            if not previous.may_delegate:
                return False, MAY_NOT_DELEGATE, None
            if expires > _parse_time(previous.expires_at):
                return False, EXPIRY_EXCEEDS_PARENT, None
            if link.terms_hash != previous.terms_hash:
                return False, TERMS_MISMATCH, None
            if not scope_is_subset(link, previous):
                return False, SCOPE_WIDENED, None
        if not verify_delegation_signature(link):
            return False, SIGNATURE_INVALID, None
        previous = link
    leaf = chain[-1]
    if leaf.delegatee_id != agent_id:
        return False, AGENT_MISMATCH, None
    if not scope_covers(leaf, intent_class=intent_class, tool_name=tool_name, allocation_pct=allocation_pct):
        return False, SCOPE_EXCEEDED, None
    return True, OK, leaf


def verify_chain_dicts(
    chain_dicts: list[dict[str, Any]],
    *,
    principals: dict[str, Any],
    agent_public_keys: dict[str, str],
    intent_class: str,
    tool_name: str,
    agent_id: str,
    allocation_pct: float | None = None,
    revoked_delegations: list[str] | None = None,
    revoked_principals: list[str] | None = None,
    now: datetime | None = None,
) -> tuple[bool, str, Delegation | None]:
    """Dict-level wrapper for verify_chain, for interceptor/sidecar/CLI use.
    Principals may be Principal objects or plain dicts."""
    try:
        chain = [Delegation.from_dict(d) for d in chain_dicts]
    except (KeyError, ValueError, TypeError):
        return False, INVALID_DELEGATION, None
    parsed_principals: dict[str, Principal] = {}
    try:
        for pid, raw in principals.items():
            parsed_principals[pid] = raw if isinstance(raw, Principal) else Principal.from_dict(raw)
    except (KeyError, ValueError, TypeError):
        return False, UNKNOWN_PRINCIPAL, None
    revocations = InMemoryRevocationList()
    for did in revoked_delegations or []:
        revocations.revoke_delegation(did)
    for pid in revoked_principals or []:
        revocations.revoke_principal(pid)
    return verify_chain(
        chain,
        principals=parsed_principals,
        agent_public_keys=agent_public_keys,
        intent_class=intent_class,
        tool_name=tool_name,
        agent_id=agent_id,
        allocation_pct=allocation_pct,
        revocations=revocations,
        now=now,
    )


def chain_hash(chain_dicts: list[dict[str, Any]]) -> str:
    """SHA-256 over the canonical JSON of an ordered delegation-chain list.

    This is the delegation-chain hash carried on CF-KYA receipts: anyone
    holding the chain can recompute it, and any reordering, link swap, or
    field change produces a different hash. Chain dicts are used verbatim
    (including signature_hex), so the hash commits to the signed links.
    """
    import hashlib

    canonical = json.dumps(chain_dicts, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
