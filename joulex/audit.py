"""Third-party auditability (M4 chunk 3) — verify a settlement without
trusting the server.

A verifier holding only a settlement id (plus, for Hedera-anchored
settlements, a topic id) can re-check the full chain from the files
in ``<data_dir>``:

- the settlement receipt sidecar (``data/settlement_receipts.jsonl``,
  written by the fail-closed settlement chunk),
- the ledger entry (``data/settlements.jsonl``) it hashes,
- the local HCS log (``data/hcs_log.jsonl``) and its hash chain,
- the mirror node, when the settlement was anchored to a real topic,
- the attestation envelope, when one was stapled to the settlement.

No keys, no network except the optional mirror-node fetch (which is
injectable for tests and degrades to PARTIAL, never a crash, when the
mirror is unreachable). Nothing here writes to any log.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable

from .attestation import canonical_bytes, payload_hash_for_settlement, verify_envelope
from .hcs import GENESIS_PREV_HASH, verify_log_file

RECEIPTS_FILENAME = "settlement_receipts.jsonl"
SETTLEMENTS_FILENAME = "settlements.jsonl"
LOG_FILENAME = "hcs_log.jsonl"

DEFAULT_MIRROR_BASE = "https://testnet.mirrornode.hedera.com"
_MIRROR_TIMEOUT_SECONDS = 10


# -- file readers ------------------------------------------------------

def _read_jsonl(path: str) -> list[dict[str, Any]]:
    if not os.path.exists(path):
        return []
    out: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                out.append(record)
    return out


def _read_log_messages(data_dir: str) -> list[dict[str, Any]]:
    """HCS log messages from disk (header line skipped)."""
    path = os.path.join(data_dir, LOG_FILENAME)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    messages: list[dict[str, Any]] = []
    for line in lines[1:]:
        if not line.strip():
            continue
        messages.append(json.loads(line))
    return messages


def _message_hash(message: dict[str, Any]) -> str:
    """SHA-256 over the canonical bytes of the message, excluding
    ``message_hash`` itself (same rule as joulex/hcs.py)."""
    body = {k: v for k, v in message.items() if k != "message_hash"}
    return hashlib.sha256(canonical_bytes(body)).hexdigest()


def _log_hash(message_hashes: list[str]) -> str:
    """SHA-256 over the canonical bytes of the ordered message hashes."""
    return hashlib.sha256(canonical_bytes(message_hashes)).hexdigest()


# -- mirror node ---------------------------------------------------------

def _default_mirror_fetch(mirror_base: str, topic: str, sequence: str) -> dict[str, Any] | None:
    """GET one message from the mirror node. Raises on any failure —
    the caller turns that into ``fetched: False``, never a crash."""
    url = f"{mirror_base.rstrip('/')}/api/v1/topics/{topic}/messages/{sequence}"
    req = urllib.request.Request(
        url, headers={"User-Agent": "joulex/0.1 (audit verification)"}
    )
    try:
        with urllib.request.urlopen(req, timeout=_MIRROR_TIMEOUT_SECONDS) as resp:
            parsed = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RuntimeError(f"mirror node unreachable: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("mirror node returned a non-object response")
    return parsed


def _verify_mirror(
    receipt: dict[str, Any],
    local_message: dict[str, Any] | None,
    *,
    mirror_base: str,
    mirror_fetcher: Callable[[str, str], dict[str, Any] | None] | None,
) -> dict[str, Any]:
    """Check the anchored message against the mirror node.

    Returns the mirror block: ``fetched: True`` with the comparison
    results, or ``fetched: False`` with a reason on any failure."""
    topic = receipt.get("hcs_topic")
    if not topic:
        return {"fetched": False, "reason": "local_only_no_topic"}
    sequence = str(receipt.get("topic_sequence"))
    fetch = mirror_fetcher or (
        lambda t, s: _default_mirror_fetch(mirror_base, t, s)
    )
    try:
        raw = fetch(str(topic), sequence)
    except Exception as exc:  # noqa: BLE001 — mirror failure is data
        return {"fetched": False, "reason": f"mirror_fetch_failed: {exc}"}
    if raw is None:
        return {"fetched": False, "reason": "mirror_message_not_found"}
    if not isinstance(raw, dict) or not raw.get("message"):
        return {"fetched": False, "reason": "mirror_message_missing"}
    try:
        decoded = base64.b64decode(str(raw["message"]), validate=True)
        message = json.loads(decoded.decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        return {"fetched": False, "reason": f"mirror_decode_error: {exc}"}
    if not isinstance(message, dict):
        return {"fetched": False, "reason": "mirror_message_not_object"}
    local_hash = (
        str(local_message["message_hash"])
        if isinstance(local_message, dict) and local_message.get("message_hash")
        else None
    )
    message_hash_match = local_hash is not None and _message_hash(message) == local_hash
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    payload_matches_receipt = all(
        payload.get(key) == receipt.get(key)
        for key in ("market_id", "outcome", "observed_value", "evidence")
    )
    return {
        "fetched": True,
        "topic_sequence": sequence,
        "message_hash_match": message_hash_match,
        "consensus_timestamp": raw.get("consensus_timestamp"),
        "transaction_id": raw.get("transaction_id"),
        "payload_matches_receipt": payload_matches_receipt,
    }


# -- attestation ---------------------------------------------------------

def _verify_attestation(
    receipt: dict[str, Any], *, now: datetime | None
) -> dict[str, Any]:
    """Re-verify the receipt's attestation envelope from scratch.

    The payload hash is recomputed from the receipt's own settlement
    fields, so a stapled envelope from another settlement cannot
    pass. A missing binding is reported as unverifiable — honest,
    never claimed verified."""
    envelope = receipt.get("attestation")
    if envelope is None:
        return {"status": "absent"}
    binding = receipt.get("attestation_binding")
    if not isinstance(binding, dict):
        return {"status": "unverifiable", "reason": "attestation_binding_absent"}
    try:
        credential_id = str(binding["credential_id"])
        challenge_nonce = str(binding["challenge_nonce"])
    except (KeyError, TypeError):
        return {"status": "unverifiable", "reason": "attestation_binding_absent"}
    payload_hash = payload_hash_for_settlement(
        market_id=str(receipt.get("market_id")),
        outcome=receipt.get("outcome"),
        observed_value=receipt.get("observed_value"),
        evidence=receipt.get("evidence"),
    )
    ok, reason, _info = verify_envelope(
        envelope,
        credential_id=credential_id,
        challenge_nonce=challenge_nonce,
        payload_hash=payload_hash,
        now=now or datetime.now(timezone.utc),
    )
    if ok:
        return {"status": "verified", "reason": "OK"}
    return {"status": "invalid", "reason": reason}


# -- settlement verification ---------------------------------------------

def verify_settlement(
    settlement_id: str,
    data_dir: str,
    *,
    mirror_base: str = DEFAULT_MIRROR_BASE,
    fetch_mirror: bool = True,
    mirror_fetcher: Callable[[str, str], dict[str, Any] | None] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify one settlement end to end, without trusting the server.

    ``now`` is the verification time for attestation expiry (default:
    real now; tests pass a fixed clock). Verdicts:

    - ``VERIFIED`` — receipt found, ledger hash recomputes, log chain
      intact, attestation absent-or-verified, and the anchor is
      confirmed (mirror message hash matches, or local-only with the
      local message present).
    - ``PARTIAL`` — found, ledger + chain check out, but the anchor
      could not be confirmed (mirror unreachable, or local-only with
      no linked local message, or attestation unverifiable).
    - ``NOT_FOUND`` — no receipt for the settlement id.
    - ``FAILED`` — anything contradicts: ledger hash mismatch, broken
      log chain, mirror message hash mismatch, attestation invalid,
      or the receipt has no matching ledger entry.
    """
    receipts = _read_jsonl(os.path.join(data_dir, RECEIPTS_FILENAME))
    receipt = next(
        (r for r in receipts if r.get("settlement_id") == settlement_id),
        None,
    )
    if receipt is None:
        return {
            "settlement_id": settlement_id,
            "found": False,
            "receipt": None,
            "ledger_entry": None,
            "ledger_entry_hash_ok": None,
            "local_message": None,
            "chain": verify_log_file(os.path.join(data_dir, LOG_FILENAME)),
            "mirror": {"fetched": False, "reason": "no_receipt"},
            "attestation": {"status": "absent"},
            "verdict": "NOT_FOUND",
        }

    # -- ledger entry: the settlements.jsonl record this receipt hashes.
    settlements = _read_jsonl(os.path.join(data_dir, SETTLEMENTS_FILENAME))
    ledger_entry: dict[str, Any] | None = None
    for record in settlements:
        if record.get("market_id") != receipt.get("market_id"):
            continue
        if receipt.get("settled_at") is not None and record.get("settled_at") != receipt.get("settled_at"):
            continue
        ledger_entry = record
    if ledger_entry is None:
        # Fall back to the last record for the market (a receipt
        # without settled_at cannot pin one down exactly).
        market_records = [
            r for r in settlements if r.get("market_id") == receipt.get("market_id")
        ]
        ledger_entry = market_records[-1] if market_records else None
    ledger_entry_hash_ok: bool | None = None
    if ledger_entry is not None:
        recomputed = hashlib.sha256(
            canonical_bytes(ledger_entry)
        ).hexdigest()
        ledger_entry_hash_ok = recomputed == receipt.get("ledger_entry_hash")

    # -- local HCS message: by payload settlement_id, by settle action
    # + market_id, or (local mode only) by local sequence.
    messages = _read_log_messages(data_dir)
    local_message: dict[str, Any] | None = None
    for msg in messages:
        payload = msg.get("payload")
        if isinstance(payload, dict) and payload.get("settlement_id") == settlement_id:
            local_message = msg
            break
    if local_message is None:
        for msg in messages:
            payload = msg.get("payload")
            if (
                msg.get("action") == "settle_market"
                and isinstance(payload, dict)
                and payload.get("market_id") == receipt.get("market_id")
            ):
                local_message = msg
                break
    if local_message is None and receipt.get("hcs_topic") is None:
        try:
            wanted = int(str(receipt.get("topic_sequence")))
        except (TypeError, ValueError):
            wanted = None
        if wanted is not None:
            local_message = next(
                (m for m in messages if m.get("sequence") == wanted), None
            )

    chain = verify_log_file(os.path.join(data_dir, LOG_FILENAME))
    if fetch_mirror:
        mirror = _verify_mirror(
            receipt, local_message,
            mirror_base=mirror_base, mirror_fetcher=mirror_fetcher,
        )
    else:
        mirror = {"fetched": False, "reason": "mirror_fetch_disabled"}
    attestation = _verify_attestation(receipt, now=now)

    # -- verdict ---------------------------------------------------------
    if ledger_entry is None or ledger_entry_hash_ok is False:
        verdict = "FAILED"
    elif not chain.get("ok"):
        verdict = "FAILED"
    elif attestation["status"] == "invalid":
        verdict = "FAILED"
    elif mirror.get("fetched") and not mirror.get("message_hash_match"):
        verdict = "FAILED"
    elif (
        mirror.get("fetched")
        and mirror.get("message_hash_match")
        and not mirror.get("payload_matches_receipt")
    ):
        verdict = "FAILED"
    elif attestation["status"] == "unverifiable":
        verdict = "PARTIAL"
    elif mirror.get("fetched") and mirror.get("message_hash_match"):
        verdict = "VERIFIED"
    elif receipt.get("hcs_topic") is None and local_message is not None:
        verdict = "VERIFIED"
    else:
        verdict = "PARTIAL"

    return {
        "settlement_id": settlement_id,
        "found": True,
        "receipt": dict(receipt),
        "ledger_entry": dict(ledger_entry) if ledger_entry is not None else None,
        "ledger_entry_hash_ok": ledger_entry_hash_ok,
        "local_message": dict(local_message) if local_message is not None else None,
        "chain": chain,
        "mirror": mirror,
        "attestation": attestation,
        "verdict": verdict,
    }


# -- log checkpoints -------------------------------------------------------

def checkpoint_log(data_dir: str, hcs_client) -> dict[str, Any]:
    """Append a checkpoint message recording the log head BEFORE it.

    The head is computed from ``hcs_client.messages()`` (the live
    in-memory view, same source the log file holds): the last
    sequence, its message hash, and the sha256 over the canonical
    bytes of the ordered list of message hashes. The checkpoint
    message itself is appended via the client (in hedera mode it
    enqueues like any append) and is NOT part of the head it records.
    """
    messages = hcs_client.messages()
    head_sequence = int(messages[-1]["sequence"]) if messages else 0
    head_message_hash = (
        str(messages[-1]["message_hash"]) if messages else GENESIS_PREV_HASH
    )
    log_hash = _log_hash(
        [str(m["message_hash"]) for m in messages]
    )
    checkpoint = hcs_client.append(
        sender="exchange",
        action="log_checkpoint",
        payload={
            "head_sequence": head_sequence,
            "head_message_hash": head_message_hash,
            "log_hash": log_hash,
        },
    )
    return {
        "head_sequence": head_sequence,
        "head_message_hash": head_message_hash,
        "log_hash": log_hash,
        "checkpoint_sequence": checkpoint["sequence"],
        "checkpoint_message_hash": checkpoint["message_hash"],
    }


def verify_checkpoint(checkpoint_message: dict[str, Any], data_dir: str) -> bool:
    """Recompute the log head the checkpoint claims and compare.

    ``checkpoint_message`` is the checkpoint HCS message (with a
    ``payload``), or the checkpoint record returned by
    ``checkpoint_log`` (resolved to its message from the log file).
    The head is recomputed from ``hcs_log.jsonl`` messages with
    sequence <= the checkpoint's ``head_sequence``. Any read or
    shape problem is False, never an exception.
    """
    try:
        payload = checkpoint_message.get("payload") or {}
        if not payload and "checkpoint_sequence" in checkpoint_message:
            wanted = int(checkpoint_message["checkpoint_sequence"])
            messages = _read_log_messages(data_dir)
            found = next(
                (m for m in messages if m.get("sequence") == wanted), None
            )
            if found is None:
                return False
            payload = found.get("payload") or {}
        head_sequence = payload.get("head_sequence")
        log_hash = payload.get("log_hash")
        head_message_hash = payload.get("head_message_hash")
        if head_sequence is None or log_hash is None:
            return False
        messages = _read_log_messages(data_dir)
        head = [
            m for m in messages
            if isinstance(m.get("sequence"), int)
            and m["sequence"] <= int(head_sequence)
        ]
        if _log_hash([str(m["message_hash"]) for m in head]) != log_hash:
            return False
        if head:
            return str(head[-1]["message_hash"]) == str(head_message_hash)
        return head_message_hash in (None, GENESIS_PREV_HASH)
    except (OSError, ValueError, TypeError, KeyError):
        return False
