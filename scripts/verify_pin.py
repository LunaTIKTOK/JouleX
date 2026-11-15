#!/usr/bin/env python3
"""Verify the pinned code measurement + newest provenance link.

Recomputes the code-manifest measurement from the live ``joulex/``
tree and compares it with ``data/measurements.json``; then verifies
the newest provenance link (by mtime) against the same tree. Exit 0
on match, 1 on any mismatch.

CI runs this before pytest so a stale pin — a ``joulex/*.py`` change
landed without the re-pin procedure in ``docs/HEDERA.md`` — fails
fast instead of silently passing (the 2026-11-11 02:01 incident:
the suite stayed green while the pinned measurement was stale).

Usage: python3 scripts/verify_pin.py [--root PATH]
"""

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=None,
                        help="repo root (default: parent of scripts/)")
    args = parser.parse_args()
    root = (Path(args.root).resolve() if args.root
            else Path(__file__).resolve().parent.parent)
    sys.path.insert(0, str(root))

    from joulex.attestation import compute_measurement
    from joulex.provenance import verify_link

    package_dir = root / "joulex"
    live = compute_measurement(str(package_dir))

    pinned_path = root / "data" / "measurements.json"
    try:
        pinned = json.loads(pinned_path.read_text(encoding="utf-8"))
        pinned_value = pinned["measurements"][0]
    except (OSError, ValueError, KeyError, IndexError) as exc:
        print(f"PIN FAIL: cannot read pinned measurement: {exc}")
        return 1
    if live != pinned_value:
        print(f"PIN MISMATCH: live {live} != pinned {pinned_value} "
              f"(re-pin per docs/HEDERA.md)")
        return 1

    links = sorted((root / "data" / "provenance").glob("*.link.json"),
                   key=lambda p: p.stat().st_mtime)
    if not links:
        print("PIN FAIL: no provenance link in data/provenance/")
        return 1
    ok, errors = verify_link(str(links[-1]), package_dir=str(package_dir))
    if not ok:
        print(f"LINK FAIL: newest link {links[-1].name} does not verify: "
              + "; ".join(errors))
        return 1

    print(f"PIN OK: measurement {live} pinned; "
          f"link {links[-1].name} verifies")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
