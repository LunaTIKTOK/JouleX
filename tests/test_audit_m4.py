"""M4 chunk 3 — auditability: verify_settlement, checkpoints, bundles, /audit routes.

Test-first contract for joulex/audit.py, scripts/audit_bundle.py,
scripts/audit_checkpoint.py and the GET /audit/<id> + POST
/audit/checkpoint routes. All fixtures are local-mode (no Hedera key,
no network except stubbed mirror fetchers); the mirror node is stubbed
everywhere it is consulted.

Settlements are driven through the real Exchange settle path in local
mode, which (M4 chunk 2) writes the settlement receipt sidecar itself
— the tests read that sidecar, never hand-forge one.
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from joulex.attestation import canonical_bytes, mint_envelope, payload_hash_for_settlement  # noqa: E402
from joulex.audit import checkpoint_log, verify_checkpoint, verify_settlement  # noqa: E402
from joulex.client import ExchangeClient  # noqa: E402
from joulex.exchange import Exchange  # noqa: E402
from joulex.server import make_server  # noqa: E402
from tests.conftest import T0, create_market_admitted, iso, make_market_payload  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


# -- helpers -----------------------------------------------------------

def _read_jsonl(path):
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _rewrite_jsonl(path, mutate):
    records = _read_jsonl(path)
    mutate(records)
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
    return records


def _sidecar_for(data_dir, settlement_id):
    records = _read_jsonl(Path(data_dir) / "settlement_receipts.jsonl")
    return next(r for r in records if r["settlement_id"] == settlement_id)


def settle_market(exchange, bundle, clock, *, attestation=None, binding=None):
    """Create + settle one aeso_price market via the real settle path.

    Returns (market_id, settle_receipt). In local mode the settle
    finalizes inline and writes the receipt sidecar itself.
    """
    market = create_market_admitted(
        exchange, bundle, make_market_payload(), clock
    )
    mid = market["market_id"]
    clock.advance(days=1, minutes=1)
    receipt = exchange.settle_market(
        mid,
        observed_value=101000,
        attestation=attestation,
        attestation_binding=binding,
    )
    assert receipt["outcome"] == "YES"
    assert receipt["settlement_id"].startswith("stl_")
    return mid, receipt


def _settle_attested(exchange, bundle, clock):
    """Settle with a live attestation envelope. Returns
    (market_id, settlement_id, envelope, binding)."""
    market = create_market_admitted(
        exchange, bundle, make_market_payload(), clock
    )
    mid = market["market_id"]
    clock.advance(days=1, minutes=1)
    payload_hash = payload_hash_for_settlement(
        market_id=mid, outcome="YES", observed_value=101000, evidence=None
    )
    envelope = mint_envelope(
        credential_id="cred-test", challenge_nonce="nonce-test",
        payload_hash=payload_hash, now=clock(),
    )
    binding = {"credential_id": "cred-test", "challenge_nonce": "nonce-test"}
    receipt = exchange.settle_market(
        mid, observed_value=101000,
        attestation=envelope, attestation_binding=binding,
    )
    return mid, receipt["settlement_id"], envelope, binding


@pytest.fixture()
def settled(exchange, agent_a, clock):
    """A settled market + its real sidecar receipt in a tmp data dir.

    Returns dict with exchange, data_dir, settlement_id, receipt
    (sidecar), ledger_record, settle_msg.
    """
    mid, settle_receipt = settle_market(exchange, agent_a, clock)
    data_dir = exchange.store.data_dir
    sid = settle_receipt["settlement_id"]
    ledger_record = next(
        r for r in _read_jsonl(Path(data_dir) / "settlements.jsonl")
        if r["settlement_id"] == sid
    )
    settle_msg = next(
        m for m in exchange.hcs.messages()
        if m["action"] == "settle_market"
        and m["payload"].get("settlement_id") == sid
    )
    return {
        "exchange": exchange, "data_dir": data_dir, "settlement_id": sid,
        "receipt": _sidecar_for(data_dir, sid),
        "ledger_record": ledger_record,
        "settle_msg": settle_msg, "market_id": mid,
    }


def raw_request(client, method, path, body=None, headers=None):
    req = urllib.request.Request(
        client.base_url + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


# -- (a) local-mode verification ---------------------------------------

def test_verify_settlement_local_verified(settled):
    result = verify_settlement(settled["settlement_id"], settled["data_dir"])
    assert result["found"] is True
    assert result["settlement_id"] == settled["settlement_id"]
    assert result["receipt"]["settlement_id"] == settled["settlement_id"]
    assert result["ledger_entry"]["market_id"] == settled["market_id"]
    assert result["ledger_entry_hash_ok"] is True
    assert result["local_message"] is not None
    assert result["local_message"]["sequence"] == settled["settle_msg"]["sequence"]
    assert result["chain"]["ok"] is True
    assert result["mirror"] == {
        "fetched": False, "reason": "local_only_no_topic",
    }
    assert result["attestation"] == {"status": "absent"}
    assert result["verdict"] == "VERIFIED"


def test_verify_settlement_not_found(settled):
    result = verify_settlement("stl_does-not-exist", settled["data_dir"])
    assert result["found"] is False
    assert result["verdict"] == "NOT_FOUND"
    assert result["receipt"] is None
    assert result["ledger_entry"] is None
    assert result["ledger_entry_hash_ok"] is None


# -- (b) tampered ledger -------------------------------------------------

def test_verify_settlement_tampered_ledger_failed(settled):
    path = Path(settled["data_dir"]) / "settlements.jsonl"

    def tamper(records):
        for rec in records:
            if rec["settlement_id"] == settled["settlement_id"]:
                rec["observed_value"] = 1.0

    _rewrite_jsonl(path, tamper)
    result = verify_settlement(settled["settlement_id"], settled["data_dir"])
    assert result["found"] is True
    assert result["ledger_entry_hash_ok"] is False
    assert result["verdict"] == "FAILED"


# -- (c) mirror verification --------------------------------------------

def _mirror_row_for(message):
    return {
        "message": base64.b64encode(canonical_bytes(message)).decode("ascii"),
        "consensus_timestamp": message["consensus_timestamp"],
        "sequence_number": message["sequence"],
        "transaction_id": "0.0.123@1234567890.000000000",
    }


def _set_topic(settled):
    """Point the real sidecar at a (fake) hedera topic, in place."""
    path = Path(settled["data_dir"]) / "settlement_receipts.jsonl"

    def mutate(records):
        for rec in records:
            if rec["settlement_id"] == settled["settlement_id"]:
                rec["hcs_topic"] = "0.0.98765"
                rec["anchor_status"] = "anchored"

    _rewrite_jsonl(path, mutate)


def test_verify_settlement_mirror_match(settled):
    _set_topic(settled)
    msg = settled["settle_msg"]
    seen = {}

    def stub(topic, sequence):
        seen["topic"] = topic
        seen["sequence"] = sequence
        return _mirror_row_for(msg)

    result = verify_settlement(
        settled["settlement_id"], settled["data_dir"], mirror_fetcher=stub
    )
    assert seen == {"topic": "0.0.98765", "sequence": str(msg["sequence"])}
    assert result["mirror"]["fetched"] is True
    assert result["mirror"]["message_hash_match"] is True
    assert result["mirror"]["payload_matches_receipt"] is True
    assert result["mirror"]["transaction_id"] == "0.0.123@1234567890.000000000"
    assert result["verdict"] == "VERIFIED"


def test_verify_settlement_mirror_mismatch(settled):
    _set_topic(settled)
    other = dict(settled["settle_msg"])
    other["payload"] = dict(other["payload"], outcome="NO")
    other["message_hash"] = hashlib.sha256(
        canonical_bytes({k: v for k, v in other.items() if k != "message_hash"})
    ).hexdigest()

    result = verify_settlement(
        settled["settlement_id"], settled["data_dir"],
        mirror_fetcher=lambda topic, seq: _mirror_row_for(other),
    )
    assert result["mirror"]["fetched"] is True
    assert result["mirror"]["message_hash_match"] is False
    assert result["verdict"] == "FAILED"


def test_verify_settlement_mirror_fetch_raises_partial(settled):
    _set_topic(settled)

    def boom(topic, sequence):
        raise RuntimeError("mirror node down")

    result = verify_settlement(
        settled["settlement_id"], settled["data_dir"], mirror_fetcher=boom
    )
    assert result["mirror"]["fetched"] is False
    assert "reason" in result["mirror"]
    # A dead mirror degrades to PARTIAL — the local chain + ledger
    # still check out; the failure never crashes verification.
    assert result["verdict"] == "PARTIAL"


# -- (d) attestation ------------------------------------------------------

def test_verify_settlement_attestation_absent_still_verified(settled):
    result = verify_settlement(settled["settlement_id"], settled["data_dir"])
    assert result["attestation"]["status"] == "absent"
    assert result["verdict"] == "VERIFIED"


def test_verify_settlement_attestation_verified(exchange, agent_a, clock):
    mid, sid, envelope, binding = _settle_attested(exchange, agent_a, clock)
    result = verify_settlement(sid, exchange.store.data_dir, now=clock())
    assert result["attestation"]["status"] == "verified"
    assert result["verdict"] == "VERIFIED"


def test_verify_settlement_attestation_binding_absent_unverifiable(
    exchange, agent_a, clock
):
    mid, sid, envelope, binding = _settle_attested(exchange, agent_a, clock)
    path = Path(exchange.store.data_dir) / "settlement_receipts.jsonl"

    def drop_binding(records):
        for rec in records:
            if rec["settlement_id"] == sid:
                rec["attestation_binding"] = None

    _rewrite_jsonl(path, drop_binding)
    result = verify_settlement(sid, exchange.store.data_dir, now=clock())
    assert result["attestation"]["status"] == "unverifiable"
    assert result["attestation"]["reason"] == "attestation_binding_absent"
    # Honest, never claims verified; the ledger + chain still check
    # out, so this degrades to PARTIAL rather than FAILED.
    assert result["verdict"] == "PARTIAL"


# -- (e) audit bundle ------------------------------------------------------

def _run_bundle(args, **kwargs):
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "audit_bundle.py"), *args],
        capture_output=True, text=True, cwd=str(REPO_ROOT), **kwargs,
    )


def test_bundle_create_verify_roundtrip(settled, tmp_path):
    out_dir = str(tmp_path / "bundle")
    proc = _run_bundle([
        "--data-dir", settled["data_dir"],
        "--from", "2026-11-01T00:00:00Z", "--to", "2027-01-01T00:00:00Z",
        "--out", out_dir,
    ])
    assert proc.returncode == 0, proc.stderr
    bundle = json.loads((Path(out_dir) / "bundle.json").read_text())
    manifest = json.loads((Path(out_dir) / "manifest.json").read_text())
    assert bundle["messages"], "settlement messages must be in range"
    assert bundle["receipts"], "receipt must be in range"
    assert bundle["settlements"], "settlement record must be in range"
    assert bundle["log_head"]["sequence"] >= 1
    assert manifest["bundle_hash"] == bundle["bundle_hash"]
    proc = _run_bundle(["--verify", out_dir])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    assert report["ok"] is True


def test_bundle_verify_fails_on_tamper(settled, tmp_path):
    out_dir = str(tmp_path / "bundle")
    proc = _run_bundle([
        "--data-dir", settled["data_dir"],
        "--from", "2026-11-01T00:00:00Z", "--to", "2027-01-01T00:00:00Z",
        "--out", out_dir,
    ])
    assert proc.returncode == 0, proc.stderr
    bundle_path = Path(out_dir) / "bundle.json"
    bundle = json.loads(bundle_path.read_text())
    bundle["messages"][0]["payload"]["market_id"] = "mkt_tampered"
    bundle_path.write_text(json.dumps(bundle, indent=2, sort_keys=True))
    proc = _run_bundle(["--verify", out_dir])
    assert proc.returncode == 1, proc.stdout
    report = json.loads(proc.stdout)
    assert report["ok"] is False
    assert report["errors"], "tampering must be reported"


# -- (f) checkpoints --------------------------------------------------------

def test_checkpoint_append_and_verify(settled):
    exchange = settled["exchange"]
    data_dir = settled["data_dir"]
    record = checkpoint_log(data_dir, exchange.hcs)
    assert record["head_sequence"] >= 1
    assert record["checkpoint_sequence"] == record["head_sequence"] + 1
    assert len(record["log_hash"]) == 64
    assert len(record["checkpoint_message_hash"]) == 64
    # The checkpoint records the head BEFORE itself: the settle
    # message is the last message before the checkpoint append.
    assert record["head_sequence"] == settled["settle_msg"]["sequence"]
    assert record["head_message_hash"] == settled["settle_msg"]["message_hash"]
    msg = [
        m for m in exchange.hcs.messages()
        if m["sequence"] == record["checkpoint_sequence"]
    ][0]
    assert msg["action"] == "log_checkpoint"
    assert verify_checkpoint(msg, data_dir) is True
    # The checkpoint record itself resolves to the same message.
    assert verify_checkpoint(record, data_dir) is True


def test_checkpoint_verify_false_after_tamper(settled):
    exchange = settled["exchange"]
    data_dir = settled["data_dir"]
    record = checkpoint_log(data_dir, exchange.hcs)
    msg = [
        m for m in exchange.hcs.messages()
        if m["sequence"] == record["checkpoint_sequence"]
    ][0]
    tampered = json.loads(json.dumps(msg))
    tampered["payload"] = dict(
        tampered["payload"], log_hash="0" * 64,
    )
    assert verify_checkpoint(tampered, data_dir) is False


def test_audit_checkpoint_script(settled, tmp_path):
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "audit_checkpoint.py"),
         "--data-dir", settled["data_dir"]],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    record = json.loads(proc.stdout)
    assert record["checkpoint_sequence"] >= 1
    assert len(record["log_hash"]) == 64


# -- (g) HTTP routes ---------------------------------------------------------

@pytest.fixture()
def live(tmp_path, issuer, clock):
    data_dir = str(tmp_path / "data")
    ex = Exchange(data_dir=data_dir, now_fn=clock, trusted_issuers=issuer.trust_store)
    ex.verifier.refresh_revocation_list(issuer.revocation_list())
    server = make_server(ex, host="127.0.0.1", port=0, demo_clock=clock)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    client = ExchangeClient(f"http://127.0.0.1:{port}")
    yield client, ex, clock
    server.shutdown()
    server.server_close()


def test_get_audit_route_200_and_404(live, agent_a, clock):
    client, ex, _ = live
    mid, settle_receipt = settle_market(ex, agent_a, clock)
    sid = settle_receipt["settlement_id"]
    status, body = raw_request(client, "GET", f"/audit/{sid}")
    assert status == 200, body
    assert body["verdict"] == "VERIFIED"
    assert body["settlement_id"] == sid
    status, body = raw_request(client, "GET", "/audit/stl_nope")
    assert status == 404, body
    assert body == {"error": "AUDIT_NOT_FOUND"}


def test_post_audit_checkpoint_route_open_without_operator_key(live):
    client, ex, _ = live
    status, body = raw_request(client, "POST", "/audit/checkpoint", body={})
    assert status == 200, body
    assert body["checkpoint_sequence"] >= 1
    assert len(body["log_hash"]) == 64


def test_post_audit_checkpoint_route_operator_key_gated(tmp_path, issuer, clock):
    data_dir = str(tmp_path / "data-op")
    ex = Exchange(
        data_dir=data_dir, now_fn=clock,
        trusted_issuers=issuer.trust_store, operator_key="op-secret",
    )
    ex.verifier.refresh_revocation_list(issuer.revocation_list())
    server = make_server(ex, host="127.0.0.1", port=0, demo_clock=clock)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = ExchangeClient(f"http://127.0.0.1:{server.server_address[1]}")
        status, body = raw_request(client, "POST", "/audit/checkpoint", body={})
        assert status == 401, body
        assert body["reason"] == "OPERATOR_KEY_INVALID"
        status, body = raw_request(
            client, "POST", "/audit/checkpoint", body={},
            headers={"X-Operator-Key": "wrong"},
        )
        assert status == 401, body
        status, body = raw_request(
            client, "POST", "/audit/checkpoint", body={},
            headers={"X-Operator-Key": "op-secret"},
        )
        assert status == 200, body
        assert body["checkpoint_sequence"] >= 1
    finally:
        server.shutdown()
        server.server_close()
