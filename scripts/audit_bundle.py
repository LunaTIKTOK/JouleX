"""Audit bundle — export a time-ranged, self-verifying bundle of the
audit trail, and verify one offline.

Create mode writes ``bundle.json`` + ``manifest.json`` into --out:

- ``bundle.json``: {"range": {"from", "to"}, "generated_at",
  "messages": [...hcs messages in range...],
  "receipts": [...settlement receipts in range by settled_at...],
  "settlements": [...settlements.jsonl records in range...],
  "log_head": {"sequence", "message_hash"},
  "bundle_hash": sha256 over canonical bytes of everything except
  bundle_hash}.
- ``manifest.json``: {"bundle_hash", "files": {"bundle.json": sha256
  of the bundle.json file bytes}}.

Verify mode (--verify <bundle-dir>) re-reads bundle.json,
recomputes bundle_hash, re-walks the message hash chain within the
bundle, and recomputes each receipt's ledger_entry_hash against the
bundled settlement records. No network, no keys. Exit 0 with a JSON
report {"ok": true, ...} or exit 1 with {"ok": false, "errors": [...]}.

Usage:
    python3 scripts/audit_bundle.py --data-dir data \
        --from 2026-11-01T00:00:00Z --to 2026-12-01T00:00:00Z \
        --out /tmp/bundle-202611
    python3 scripts/audit_bundle.py --verify /tmp/bundle-202611
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from joulex.attestation import canonical_bytes  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = REPO_ROOT / "data"


def _parse_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_hcs_timestamp(value: str) -> datetime:
    return _parse_time(value)  # HCS format is ISO-8601 with nanoseconds


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                record = json.loads(line)
                if isinstance(record, dict):
                    out.append(record)
    return out


def _read_log_messages(data_dir: Path) -> list[dict[str, Any]]:
    path = data_dir / "hcs_log.jsonl"
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    return [json.loads(line) for line in lines[1:] if line.strip()]


def _in_range(ts: datetime | None, start: datetime | None, end: datetime | None) -> bool:
    if ts is None:
        return False
    if start is not None and ts < start:
        return False
    if end is not None and ts > end:
        return False
    return True


def _message_hash(message: dict[str, Any]) -> str:
    body = {k: v for k, v in message.items() if k != "message_hash"}
    return hashlib.sha256(canonical_bytes(body)).hexdigest()


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def create_bundle(data_dir: Path, start: datetime | None, end: datetime | None,
                  out_dir: Path) -> dict[str, Any]:
    messages = _read_log_messages(data_dir)
    receipts = _read_jsonl(data_dir / "settlement_receipts.jsonl")
    settlements = _read_jsonl(data_dir / "settlements.jsonl")
    in_range_messages = [
        m for m in messages
        if _in_range(_parse_hcs_timestamp(str(m["consensus_timestamp"])), start, end)
    ]
    in_range_receipts = [
        r for r in receipts
        if _in_range(_parse_time(r.get("settled_at")), start, end)
    ]
    in_range_settlements = [
        s for s in settlements
        if _in_range(_parse_time(s.get("settled_at")), start, end)
    ]
    log_head = (
        {"sequence": messages[-1]["sequence"], "message_hash": messages[-1]["message_hash"]}
        if messages else {"sequence": 0, "message_hash": None}
    )
    bundle: dict[str, Any] = {
        "range": {
            "from": start.isoformat() if start else None,
            "to": end.isoformat() if end else None,
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "messages": in_range_messages,
        "receipts": in_range_receipts,
        "settlements": in_range_settlements,
        "log_head": log_head,
    }
    bundle_hash = hashlib.sha256(canonical_bytes(bundle)).hexdigest()
    bundle["bundle_hash"] = bundle_hash
    out_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = out_dir / "bundle.json"
    with open(bundle_path, "w", encoding="utf-8") as f:
        json.dump(bundle, f, indent=2, sort_keys=True)
        f.write("\n")
    manifest = {
        "bundle_hash": bundle_hash,
        "files": {"bundle.json": _file_sha256(bundle_path)},
    }
    with open(out_dir / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write("\n")
    return {
        "ok": True,
        "bundle_dir": str(out_dir),
        "bundle_hash": bundle_hash,
        "messages": len(in_range_messages),
        "receipts": len(in_range_receipts),
        "settlements": len(in_range_settlements),
        "log_head": log_head,
    }


def verify_bundle(bundle_dir: Path) -> tuple[dict[str, Any], int]:
    errors: list[str] = []
    bundle_path = bundle_dir / "bundle.json"
    try:
        with open(bundle_path, "r", encoding="utf-8") as f:
            bundle = json.load(f)
    except (OSError, ValueError) as exc:
        return {"ok": False, "errors": [f"unreadable bundle.json: {exc}"]}, 1
    if not isinstance(bundle, dict):
        return {"ok": False, "errors": ["bundle.json is not an object"]}, 1
    stored_hash = bundle.get("bundle_hash")
    body = {k: v for k, v in bundle.items() if k != "bundle_hash"}
    recomputed = hashlib.sha256(canonical_bytes(body)).hexdigest()
    if stored_hash != recomputed:
        errors.append("bundle_hash mismatch (bundle tampered or corrupt)")
    manifest_path = bundle_dir / "manifest.json"
    if manifest_path.exists():
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            if manifest.get("bundle_hash") != recomputed:
                errors.append("manifest bundle_hash mismatch")
            expected_file_hash = (manifest.get("files") or {}).get("bundle.json")
            if expected_file_hash is not None and expected_file_hash != _file_sha256(bundle_path):
                errors.append("manifest bundle.json file hash mismatch")
        except (OSError, ValueError) as exc:
            errors.append(f"unreadable manifest.json: {exc}")
    messages = bundle.get("messages") or []
    settlements = bundle.get("settlements") or []
    receipts = bundle.get("receipts") or []
    # Re-walk the message hash chain within the bundle. The first
    # bundled message's prev_hash points outside the bundle and is
    # not checkable here; linkage is enforced between consecutive
    # bundled messages, plus each message's own hash.
    prev: dict[str, Any] | None = None
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            errors.append(f"message {idx}: not an object")
            prev = None
            continue
        if msg.get("message_hash") != _message_hash(msg):
            errors.append(
                f"message {idx} (sequence {msg.get('sequence')}): "
                "message_hash mismatch (tampered content)"
            )
        if prev is not None:
            if msg.get("prev_hash") != prev.get("message_hash"):
                errors.append(
                    f"message {idx} (sequence {msg.get('sequence')}): "
                    "prev_hash breaks the chain"
                )
            if (
                isinstance(msg.get("sequence"), int)
                and isinstance(prev.get("sequence"), int)
                and msg["sequence"] != prev["sequence"] + 1
            ):
                errors.append(
                    f"message {idx} (sequence {msg.get('sequence')}): "
                    "sequence gap in bundle"
                )
        prev = msg
    # Each receipt's ledger_entry_hash against the bundled settlements.
    by_market: dict[str, list[dict[str, Any]]] = {}
    for record in settlements:
        if isinstance(record, dict):
            by_market.setdefault(str(record.get("market_id")), []).append(record)
    for receipt in receipts:
        if not isinstance(receipt, dict):
            errors.append("receipt is not an object")
            continue
        sid = receipt.get("settlement_id")
        candidates = by_market.get(str(receipt.get("market_id")), [])
        entry = next(
            (r for r in candidates if r.get("settled_at") == receipt.get("settled_at")),
            candidates[-1] if candidates else None,
        )
        if entry is None:
            errors.append(
                f"receipt {sid}: no matching settlement record in bundle"
            )
            continue
        if receipt.get("ledger_entry_hash") is None:
            errors.append(f"receipt {sid}: missing ledger_entry_hash")
            continue
        recomputed_entry = hashlib.sha256(canonical_bytes(entry)).hexdigest()
        if recomputed_entry != receipt["ledger_entry_hash"]:
            errors.append(
                f"receipt {sid}: ledger_entry_hash mismatch (ledger entry tampered)"
            )
    if errors:
        return {
            "ok": False,
            "errors": errors,
            "messages": len(messages),
            "receipts": len(receipts),
            "settlements": len(settlements),
            "bundle_hash": recomputed,
        }, 1
    return {
        "ok": True,
        "messages": len(messages),
        "receipts": len(receipts),
        "settlements": len(settlements),
        "bundle_hash": recomputed,
    }, 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--from", dest="from_ts", default=None,
                        help="range start, ISO-8601 (inclusive)")
    parser.add_argument("--to", dest="to_ts", default=None,
                        help="range end, ISO-8601 (inclusive)")
    parser.add_argument("--out", default=None, help="output directory (create mode)")
    parser.add_argument("--verify", default=None, help="bundle directory to verify")
    args = parser.parse_args(argv)
    if args.verify is not None:
        report, code = verify_bundle(Path(args.verify))
        print(json.dumps(report, indent=2, sort_keys=True))
        return code
    if args.out is None:
        print("create mode requires --out <dir>", file=sys.stderr)
        return 2
    try:
        start = _parse_time(args.from_ts)
        end = _parse_time(args.to_ts)
    except ValueError as exc:
        print(f"invalid --from/--to timestamp: {exc}", file=sys.stderr)
        return 2
    report = create_bundle(Path(args.data_dir), start, end, Path(args.out))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
