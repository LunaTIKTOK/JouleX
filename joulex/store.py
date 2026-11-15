"""JSON persistence under the data directory.

Atomic-ish writes: write a temp file in the same directory, fsync, then
``os.replace``. A crash mid-write leaves either the old file or the new
one, never a half-written file. Append-only logs (trades, settlements)
open in append mode and flush + fsync per line.
"""

from __future__ import annotations

import json
import os
from typing import Any


class JsonStore:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)

    def _path(self, name: str) -> str:
        return os.path.join(self.data_dir, name)

    def load_json(self, name: str, default: Any) -> Any:
        path = self._path(name)
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save_json(self, name: str, obj: Any) -> None:
        path = self._path(name)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def append_jsonl(self, name: str, obj: Any) -> None:
        path = self._path(name)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def read_jsonl(self, name: str) -> list[Any]:
        path = self._path(name)
        if not os.path.exists(path):
            return []
        out = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out
