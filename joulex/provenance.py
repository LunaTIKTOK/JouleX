"""Provenance — in-toto link metadata, DESIGN.md §7.4 (M1).

Every release of the ``joulex/`` code produces link metadata: the
build step records the materials (source files + hashes) and products
(the code-manifest hash = the measurement). The link file is stored
under ``data/provenance/`` and its hash is referenced by the
attestation envelope's evidence. SLSA Level 2 is the bar (signed
provenance, not yet hermetic builders).

in-toto is NOT installed in this environment (checked 2026-11-10:
``import in_toto`` fails; no pip installs under the $0/no-install
bar), so this module uses the stdlib-only link format with the
identical schema, labelled as such. The measurement does not depend
on in-toto being installed: it is computed by
``joulex.attestation.compute_measurement`` either way.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .attestation import canonical_bytes, code_manifest, compute_measurement

LINK_FORMAT = (
    "joulex-stdlib-link-0.1 (in-toto-compatible link schema; "
    "in-toto not installed — stdlib-only fallback; see DESIGN.md §7.4)"
)
STEP_NAME = "joulex-build"


def link_sha256(path: str) -> str:
    """SHA-256 hex of a link file's bytes (what the envelope evidence
    references)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def build_link(
    *,
    package_dir: str | None = None,
    data_dir: str,
    step_name: str = STEP_NAME,
    now: datetime | None = None,
    command: list[str] | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Build the provenance link for the current code tree and write
    it to ``<data_dir>/provenance/<link_sha256>.link.json`` (the file
    is named by its own content hash, so an envelope's evidence can
    reference it by hash alone).

    Returns (path, link). Materials are every ``.py`` file in the
    package with its hash; products carry the code-manifest hash (the
    measurement). Deterministic: the same tree always produces the
    same link bytes."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    measurement = compute_measurement(package_dir)
    materials = {
        rel: {"sha256": sha} for rel, sha in code_manifest(package_dir)
    }
    link: dict[str, Any] = {
        "_type": "link",
        "format": LINK_FORMAT,
        "name": step_name,
        "byproducts": {},
        "materials": materials,
        "products": {"code_manifest_hash": {"sha256": measurement}},
        "command": command or ["python3", "-m", "joulex.provenance"],
        "environment": {"python": sys.version.split()[0], "platform": sys.platform},
        "measurement": measurement,
        "created_at": current.isoformat(),
    }
    raw = canonical_bytes(link) + b"\n"
    sha = hashlib.sha256(raw).hexdigest()
    out_dir = os.path.join(data_dir, "provenance")
    os.makedirs(out_dir, exist_ok=True)
    path = Path(out_dir) / f"{sha}.link.json"
    with open(path, "wb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    return path, link


def load_link(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def verify_link(path: str, *, package_dir: str | None = None) -> tuple[bool, list[str]]:
    """Re-verify a link against the live code tree: every material
    hash must match the current file, and the product hash must equal
    the current measurement. Returns (ok, errors)."""
    errors: list[str] = []
    try:
        link = load_link(path)
    except (ValueError, OSError) as exc:
        return False, [f"unreadable link: {exc}"]
    manifest = dict(code_manifest(package_dir))
    materials = link.get("materials")
    if not isinstance(materials, dict):
        return False, ["link has no materials map"]
    for rel, entry in materials.items():
        if rel not in manifest:
            errors.append(f"material {rel} missing from code tree")
        elif not isinstance(entry, dict) or entry.get("sha256") != manifest[rel]:
            errors.append(f"material {rel} hash mismatch")
    for rel in manifest:
        if rel not in materials:
            errors.append(f"code file {rel} not recorded in link")
    try:
        product = link["products"]["code_manifest_hash"]["sha256"]
    except (KeyError, TypeError):
        return False, errors + ["link has no code_manifest_hash product"]
    if product != compute_measurement(package_dir):
        errors.append("product code_manifest_hash != current measurement")
    if link.get("measurement") != compute_measurement(package_dir):
        errors.append("link measurement != current measurement")
    return (not errors), errors
