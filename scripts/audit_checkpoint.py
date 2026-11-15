"""Append one log-checkpoint message to the HCS audit log.

The checkpoint records the log head (last sequence, its message hash,
and the sha256 over the ordered message hashes) BEFORE the checkpoint
message itself, so a later verifier can confirm the log's state at
that point. Works in local mode; in hedera mode the checkpoint
appends like any message (enqueued for anchoring by RealHCS).

No keys are read by this script: RealHCS reads the operator key from
the environment at submit time, through the anchor queue worker.

Usage:
    python3 scripts/audit_checkpoint.py [--data-dir data]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from joulex.audit import checkpoint_log  # noqa: E402
from joulex.hcs import make_hcs  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = REPO_ROOT / "data"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    args = parser.parse_args(argv)
    hcs = make_hcs(args.data_dir)
    record = checkpoint_log(args.data_dir, hcs)
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
