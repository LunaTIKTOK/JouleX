"""Mirror-node replay check — verify the local HCS log against the
Hedera topic (audit 2026-11-12).

The HCS topic is the anchor, not the state store: the local log
(``data/hcs_log.jsonl``) is the replay source, and the receipts
sidecar (``hcs_receipts.jsonl``) says which local messages were
anchored. This tool fetches every message on the configured topic
from the public mirror node (read-only, no key), decodes each, and
matches it to the local log by ``message_hash``.

Verdict:
- ``matched`` — on-chain and in the local log with the same hash.
- ``local_only`` — in the local log, never submitted (no receipt):
  expected for local-only operation, not a failure.
- ``local_only_with_receipt`` — has a SUCCESS receipt but is absent
  from the topic: the anchor is not where the receipt says it is.
  FAILURE.
- ``mirror_only`` — on the topic but not in the local log: an
  unexpected poster (the testnet topic is open — anyone can post)
  or a lost local log. FAILURE.
- ``undecodable`` — a mirror message that is not base64 JSON.
  FAILURE.

Exit 0 when the verdict is ok, 1 otherwise. Usage:

    python3 scripts/hcs_mirror_replay.py [--data-dir data] [--topic 0.0.x] [--network testnet|mainnet]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from joulex.attestation import canonical_bytes  # noqa: E402
from joulex.hcs import load_hcs_config, verify_log_file  # noqa: E402

MIRROR_BASES = {
    "testnet": "https://testnet.mirrornode.hedera.com/api/v1",
    "mainnet": "https://mainnet.mirrornode.hedera.com/api/v1",
}


def _message_hash(message: dict[str, Any]) -> str:
    import hashlib

    body = {k: v for k, v in message.items() if k != "message_hash"}
    return hashlib.sha256(canonical_bytes(body)).hexdigest()


def decode_mirror_message(raw: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Decode one mirror-node message row into the Joulex message
    dict. Returns (message, None) or (None, error)."""
    try:
        decoded = base64.b64decode(str(raw.get("message", "")), validate=True)
        message = json.loads(decoded.decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        return None, f"undecodable mirror message: {exc}"
    if not isinstance(message, dict):
        return None, "mirror message is not a JSON object"
    return message, None


def fetch_mirror_messages(
    topic: str, network: str, *, opener=None
) -> list[dict[str, Any]]:
    """All messages on ``topic``, oldest first, following the mirror
    node's pagination. ``opener`` is an injectable ``(url) -> parsed
    JSON`` for tests; the default is a stdlib urllib GET."""
    base = MIRROR_BASES[network]
    url = f"{base}/topics/{topic}/messages?limit=100&order=asc"
    out: list[dict[str, Any]] = []
    fetch = opener or (lambda u: _default_fetch(u))
    while url:
        page = fetch(url)
        out.extend(page.get("messages") or [])
        nxt = (page.get("links") or {}).get("next")
        url = f"{base}{nxt}" if nxt else None
    return out


def _default_fetch(url: str) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "joulex/0.1 (mirror replay check)"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RuntimeError(f"mirror node unreachable: {exc}") from exc


def compare_messages(
    local: list[dict[str, Any]],
    mirror_raw: list[dict[str, Any]],
    receipted_seqs: set[int],
) -> dict[str, Any]:
    """Match decoded mirror messages to local messages by
    ``message_hash``. Pure function — no network."""
    local_by_hash = {m.get("message_hash"): m for m in local}
    mirror_by_hash: dict[str, dict[str, Any]] = {}
    undecodable: list[int] = []
    for row in mirror_raw:
        msg, error = decode_mirror_message(row)
        if error is not None:
            undecodable.append(int(row.get("sequence_number") or -1))
            continue
        mirror_by_hash[msg.get("message_hash")] = row
    matched = len(set(local_by_hash) & set(mirror_by_hash))
    mirror_only = sorted(
        int(row.get("sequence_number") or -1)
        for h, row in mirror_by_hash.items()
        if h not in local_by_hash
    )
    local_only = sorted(
        int(m.get("sequence"))
        for h, m in local_by_hash.items()
        if h not in mirror_by_hash
    )
    local_only_with_receipt = [s for s in local_only if s in receipted_seqs]
    ok = (
        not mirror_only
        and not local_only_with_receipt
        and not undecodable
    )
    return {
        "ok": ok,
        "mirror_count": len(mirror_raw),
        "local_count": len(local),
        "matched": matched,
        "mirror_only": mirror_only,
        "local_only": local_only,
        "local_only_with_receipt": local_only_with_receipt,
        "undecodable": undecodable,
    }


def _read_receipted_seqs(data_dir: str) -> set[int]:
    seqs: set[int] = set()
    path = os.path.join(data_dir, "hcs_receipts.jsonl")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        seqs.add(int(json.loads(line).get("sequence")))
                    except (ValueError, TypeError):
                        continue
    return seqs


def run(data_dir: str, topic: str, network: str) -> tuple[dict[str, Any], int]:
    """Full check: local log integrity + mirror comparison. Returns
    (report, exit_code)."""
    log_report = verify_log_file(os.path.join(data_dir, "hcs_log.jsonl"))
    if not log_report["ok"]:
        return {
            "ok": False,
            "error": "local log failed verification",
            "log_errors": log_report["errors"],
        }, 1
    with open(os.path.join(data_dir, "hcs_log.jsonl"), "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    local = [json.loads(line) for line in lines[1:] if line.strip()]
    mirror_raw = fetch_mirror_messages(topic, network)
    report = compare_messages(local, mirror_raw, _read_receipted_seqs(data_dir))
    report["topic"] = topic
    report["network"] = network
    report["log_ok"] = True
    report["log_messages"] = len(local)
    return report, 0 if report["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--topic", default=None)
    parser.add_argument("--network", default=None, choices=["testnet", "mainnet"])
    args = parser.parse_args(argv)
    cfg = load_hcs_config(args.data_dir)
    topic = args.topic or cfg.get("hedera_topic")
    if not topic:
        print("no topic: pass --topic or set hedera_topic in hcs_config.json", file=sys.stderr)
        return 2
    network = args.network or str(cfg.get("network") or "testnet")
    if network not in MIRROR_BASES:
        print(f"unsupported network {network!r}", file=sys.stderr)
        return 2
    try:
        report, code = run(args.data_dir, str(topic), network)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
