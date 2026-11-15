"""M1 — in-toto provenance link tests (test-first).

Covers DESIGN.md §7.4. in-toto is NOT installed in this environment,
so the provenance module uses the stdlib-only link format with the
identical schema, labelled as such (DESIGN.md §7.4 fallback). The
measurement never depends on in-toto.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

import pytest

from joulex.attestation import canonical_bytes, compute_measurement
from joulex.provenance import (
    LINK_FORMAT,
    build_link,
    link_sha256,
    verify_link,
)

T0 = datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc)


def test_build_link_matches_measurement(tmp_path):
    pkg = tmp_path / "joulex"
    pkg.mkdir()
    (pkg / "a.py").write_text("x = 1\n")
    path, link = build_link(package_dir=str(pkg), data_dir=str(tmp_path / "data"), now=T0)
    measurement = compute_measurement(package_dir=str(pkg))
    assert link["products"]["code_manifest_hash"]["sha256"] == measurement
    assert link["measurement"] == measurement
    assert link["materials"]["a.py"]["sha256"] is not None
    assert path.is_file()
    assert "in-toto" in link["format"].lower() or "stdlib" in link["format"].lower()
    assert LINK_FORMAT in link["format"]


def test_verify_link_ok_and_tampered(tmp_path):
    pkg = tmp_path / "joulex"
    pkg.mkdir()
    (pkg / "a.py").write_text("x = 1\n")
    path, _ = build_link(package_dir=str(pkg), data_dir=str(tmp_path / "data"), now=T0)
    ok, errors = verify_link(str(path), package_dir=str(pkg))
    assert ok, errors
    (pkg / "a.py").write_text("x = 2\n")
    ok, errors = verify_link(str(path), package_dir=str(pkg))
    assert not ok and errors


def test_link_is_stdlib_only_labelled(tmp_path):
    pkg = tmp_path / "joulex"
    pkg.mkdir()
    (pkg / "a.py").write_text("x = 1\n")
    _, link = build_link(package_dir=str(pkg), data_dir=str(tmp_path / "data"), now=T0)
    # Honest label: no in-toto install here, so the link says so.
    assert "in-toto not installed" in link["format"] or "stdlib-only" in link["format"]


def test_envelope_evidence_references_link(tmp_path):
    """An envelope whose evidence names a real link verifies; remove
    the link and the same envelope fails evidence verification."""
    from joulex.attestation import mint_envelope, verify_envelope

    pkg = tmp_path / "joulex"
    pkg.mkdir()
    (pkg / "a.py").write_text("x = 1\n")
    data_dir = str(tmp_path / "data")
    path, link = build_link(package_dir=str(pkg), data_dir=data_dir, now=T0)
    measurement = link["measurement"]
    evidence = base64.b64encode(
        canonical_bytes({"measurement": measurement, "link_sha256": link_sha256(str(path))})
    ).decode()
    env = mint_envelope(
        credential_id="c", challenge_nonce="n", payload_hash="p",
        now=T0, measurement=measurement, evidence=evidence,
    )
    ok, reason, _ = verify_envelope(
        env, credential_id="c", challenge_nonce="n", payload_hash="p",
        now=T0, provenance_dir=str(tmp_path / "data" / "provenance"),
    )
    assert ok, reason
    path.unlink()
    ok, reason, _ = verify_envelope(
        env, credential_id="c", challenge_nonce="n", payload_hash="p",
        now=T0, provenance_dir=str(tmp_path / "data" / "provenance"),
    )
    assert not ok and reason == "JOULEX_VC_EVIDENCE"
