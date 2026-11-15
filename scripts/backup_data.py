"""One-command backup of the JouleX data directory (audit 2026-11-12).

Render's free tier wipes ``data/`` on redeploy; the exchange state
(markets, ledger, challenge store, HCS log + receipts + dead-letter,
measurements, provenance) lives there. This script tars it into a
timestamped archive with a SHA-256 manifest, so the operator can copy
one file off the box and restore from it. It creates the archive
only — where it is stored (object storage, a second host) is the
operator's decision; see AUDIT_HEDERA_COMMERCIAL_20261112.md.

Usage:
    python3 scripts/backup_data.py [--data-dir data] [--out-dir backups]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tarfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def backup(data_dir: str, out_dir: str) -> str:
    """Tar ``data_dir`` (files only, sorted, no temp files) into
    ``<out_dir>/joulex-data-<UTC timestamp>.tar.gz`` with a
    ``manifest.json`` (relative path -> sha256) inside the archive.
    Returns the archive path."""
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    archive = os.path.join(out_dir, f"joulex-data-{stamp}.tar.gz")
    manifest: dict[str, str] = {}
    files: list[str] = []
    for root, dirs, names in os.walk(data_dir):
        dirs.sort()
        for name in sorted(names):
            if name.endswith(".tmp"):
                continue
            files.append(os.path.join(root, name))
    for path in files:
        manifest[os.path.relpath(path, data_dir)] = _sha256_file(path)
    manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    import io

    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest_bytes)
        info.mtime = int(datetime.now(timezone.utc).timestamp())
        tar.addfile(info, io.BytesIO(manifest_bytes))
        for path in files:
            tar.add(path, arcname=os.path.relpath(path, data_dir))
    return archive


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Back up the JouleX data directory.")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--out-dir", default="backups")
    args = parser.parse_args(argv)
    if not os.path.isdir(args.data_dir):
        print(f"no data directory: {args.data_dir}", file=sys.stderr)
        return 2
    archive = backup(args.data_dir, args.out_dir)
    print(archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
