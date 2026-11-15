"""Backup script test (audit 2026-11-12): the archive holds every
data file plus a SHA-256 manifest, and excludes temp files.
"""

from __future__ import annotations

import hashlib
import json
import os
import tarfile

from scripts.backup_data import backup


def test_backup_archive_with_manifest(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "a.json").write_text('{"x": 1}')
    (data / "b.jsonl").write_text('{"y": 2}\n')
    (data / "c.tmp").write_text("partial")
    arc = backup(str(data), str(tmp_path / "backups"))
    assert os.path.exists(arc)
    with tarfile.open(arc) as tar:
        names = tar.getnames()
        assert "manifest.json" in names
        assert "a.json" in names and "b.jsonl" in names
        assert "c.tmp" not in names
        man = json.loads(tar.extractfile("manifest.json").read())
    assert man["a.json"] == hashlib.sha256(b'{"x": 1}').hexdigest()
    assert man["b.jsonl"] == hashlib.sha256(b'{"y": 2}\n').hexdigest()
