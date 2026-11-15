"""Settlement receipts (M4 chunk 2) — the sidecar audit record for
every finalized settlement.

One JSON line per settlement in ``data/settlement_receipts.jsonl``
(append-only, fsync per line, same pattern as ``hcs.py``
``_append_record``). The record shape is EXACT — the value-rail
chunk depends on it:

    {"settlement_id", "market_id", "outcome", "observed_value",
     "evidence", "settled_at", "ledger_entry_hash", "hcs_topic",
     "topic_sequence", "consensus_timestamp", "tx_id", "attestation",
     "attestation_status", "attestation_binding", "inputs_hash",
     "decision_basis", "anchor_job_id", "anchor_status",
     "transfer_tx_id"}

- ``ledger_entry_hash``: SHA-256 of the canonical bytes of the
  ``settlements.jsonl`` record for this settlement.
- Local mode: ``hcs_topic`` None, ``topic_sequence`` the local HCS
  sequence (string), ``consensus_timestamp`` the local HCS timestamp,
  ``tx_id`` None, ``anchor_status`` ``"local"``, ``anchor_job_id`` None.
- Anchored (Hedera) mode: ``topic_sequence`` /
  ``consensus_timestamp`` / ``tx_id`` from the anchor receipt,
  ``anchor_status`` ``"anchored"``.
- Fail-retain Hedera mode: ``anchor_status`` ``"queued"`` with the
  ``anchor_job_id`` from the append return; no Hedera topic sequence
  exists yet, so ``topic_sequence`` is None.
- ``transfer_tx_id`` is None until the value-rail chunk fills it.
"""

from __future__ import annotations

import json
import os
from typing import Any

RECEIPTS_FILENAME = "settlement_receipts.jsonl"

RECEIPT_FIELDS = (
    "settlement_id",
    "market_id",
    "outcome",
    "observed_value",
    "evidence",
    "settled_at",
    "ledger_entry_hash",
    "hcs_topic",
    "topic_sequence",
    "consensus_timestamp",
    "tx_id",
    "attestation",
    "attestation_status",
    "attestation_binding",
    "inputs_hash",
    "decision_basis",
    "anchor_job_id",
    "anchor_status",
    "transfer_tx_id",
)


def append_receipt(data_dir: str, receipt: dict[str, Any]) -> None:
    """Append one receipt line, fsync per line."""
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, RECEIPTS_FILENAME)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(receipt, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())


def read_receipts(data_dir: str) -> list[dict[str, Any]]:
    """All settlement receipts, in file order."""
    path = os.path.join(data_dir, RECEIPTS_FILENAME)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def receipt_for_settlement(
    data_dir: str, settlement_id: str
) -> dict[str, Any] | None:
    """The receipt for one settlement id, or None."""
    for receipt in read_receipts(data_dir):
        if receipt.get("settlement_id") == settlement_id:
            return receipt
    return None
