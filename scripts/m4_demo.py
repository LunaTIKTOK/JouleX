#!/usr/bin/env python3
"""M4 settlement-layer + auditability demo: one full cycle, end to end.

Runs the REAL Exchange with a RealHCS client whose submit runner is a
FAKE (it returns a canned SUCCESS receipt and never touches the
network). The operator key is a DUMMY string used only to construct
RealHCS in test mode. **No real Hedera call, no real key, no live
transfer happens here** — the live testnet cycle (same steps, real
runner, owner-supplied key) is documented in MILESTONES.md §M4 and
still needs the owner to supply the operator key transiently.

Flow (all against a fresh temp data dir):
  1. Two demo agents trade one aeso_price market (same setup as the
     M0 demo's first market).
  2. Settle -> PENDING_ANCHOR (fail-closed: nothing is paid out yet,
     market frozen as SETTLEMENT_PENDING).
  3. drain_anchors() -> the anchor lands, the settlement finalizes,
     the receipt sidecar is written.
  4. verify_settlement() with a stub mirror (the local message,
     base64-encoded, exactly as a mirror node would return it) ->
     VERIFIED. Then the same check over HTTP: GET /audit/<id> -> 200.
  5. Audit bundle for the run's time range (create + verify), and one
     HCS checkpoint of the audit-log head (append + verify).
  6. Value rail (SIMULATED): a testnet HBAR transfer leg is recorded
     for the settlement id with a fake runner — the transfer_tx_id is
     written back onto the receipt. Labelled SIMULATED; no live
     transfer exists.
  7. Queue-throughput probe: 50 anchor jobs enqueued + drained
     locally, timed (queue mechanics only — the Hedera-side bound is
     the receipt latency, ~2-5s observed on testnet 2026-11-10).

Prints the settlement id, the receipt, and the third-party
verification walkthrough a stranger could repeat with only the
settlement id + topic id.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from joulex.attestation import canonical_bytes  # noqa: E402
from joulex.audit import verify_checkpoint, verify_settlement  # noqa: E402
from joulex.demo_issuer import DemoIssuer  # noqa: E402
from joulex.exchange import Exchange  # noqa: E402
from joulex.hcs import RealHCS  # noqa: E402
from joulex.server import DemoClock, make_server  # noqa: E402
from joulex.transfer import record_transfer, update_receipt_transfer  # noqa: E402
from scripts.audit_bundle import create_bundle, verify_bundle  # noqa: E402
from tests.conftest import T0, FakeClock, make_market_payload, make_order_payload  # noqa: E402

FAKE_RECEIPT = {
    "status": "SUCCESS",
    "topic_sequence": "42",
    "consensus_timestamp": "2026-11-10T12:00:00.000000000Z",
    "transaction_id": "0.0.11389089@1794335407.470921104",
}
TOPIC = "0.0.11399235"
OPERATOR = "0.0.11389089"


def _fake_runner(receipt=None):
    def runner(message_bytes, *, operator_account_id, topic_id, network):
        return receipt or dict(FAKE_RECEIPT)

    return runner


def _read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f.read().splitlines() if l.strip()]


def main() -> int:
    # Fake clock anchored at the test-suite epoch T0: the market
    # payloads resolve T0+1d, so a real-now clock would reject them.
    clock = FakeClock()
    issuer = DemoIssuer(now_fn=clock)
    tmp = tempfile.mkdtemp(prefix="joulex-m4-demo-")
    data_dir = os.path.join(tmp, "data")
    os.environ["JOULEX_HCS_PRIVATE_KEY"] = "0x" + "ab" * 32  # DUMMY, test-only

    def factory(dd, **kw):
        return RealHCS(
            dd,
            operator_account_id=OPERATOR,
            topic_id=TOPIC,
            network="testnet",
            runner=_fake_runner(),
            now_fn=kw.get("now_fn"),
            on_anchor=kw.get("on_anchor"),
            queue_retry_delays=(0, 0),
            queue_max_attempts=3,
            retry_delays=(0, 0),
            sleep_fn=lambda s: None,
        )

    import joulex.exchange as _ex_mod

    _ex_mod.make_hcs = factory
    ex = Exchange(
        data_dir=data_dir, now_fn=clock, trusted_issuers=issuer.trust_store
    )
    ok, reason = ex.verifier.refresh_revocation_list(issuer.revocation_list())
    assert ok, reason

    alice = issuer.mint_agent(
        principal_id="principal-demo-alice",
        display_name="Alice Demo Principal",
        agent_id="agent-demo-alice",
    )
    bob = issuer.mint_agent(
        principal_id="principal-demo-bob",
        display_name="Bob Demo Principal",
        agent_id="agent-demo-bob",
    )

    # 1. Market + trades (same shape as the M0 demo's flex market).
    from tests.test_joulex_markets import _trade_and_advance

    mid = _trade_and_advance(ex, alice, bob, clock)
    market_before = ex.get_market(mid)
    print(f"  market {mid} traded; price_yes {market_before['price_yes']:.4f}")

    # 2. Settle -> PENDING_ANCHOR (fail-closed).
    result = ex.settle_market(mid, observed_value=55.0)
    assert result["status"] == "PENDING_ANCHOR"
    assert ex.get_market(mid)["status"] == "SETTLEMENT_PENDING"
    assert _read_jsonl(os.path.join(data_dir, "settlements.jsonl")) == []
    print(f"  settle -> {result['status']} (settlement_id {result['settlement_id']}, "
          f"anchor job {result['anchor_job_id']}); nothing paid out yet")

    # 3. Drain the queue -> anchor lands, settlement finalizes.
    done = ex.hcs.drain_anchors()
    assert done[-1]["status"] == "anchored"
    assert ex.get_market(mid)["status"] == "RESOLVED"
    receipts = _read_jsonl(os.path.join(data_dir, "settlement_receipts.jsonl"))
    assert len(receipts) == 1
    receipt = receipts[0]
    sid = receipt["settlement_id"]
    print(f"  anchor landed (topic seq {receipt['topic_sequence']}, "
          f"consensus {receipt['consensus_timestamp']}); settlement finalized")
    print("  receipt:")
    print(f"    settlement_id      {sid}")
    print(f"    ledger_entry_hash  {receipt['ledger_entry_hash'][:16]}…")
    print(f"    hcs_topic          {receipt['hcs_topic']}")
    print(f"    topic_sequence     {receipt['topic_sequence']}")
    print(f"    consensus_timestamp {receipt['consensus_timestamp']}")
    print(f"    tx_id              {receipt['tx_id']}")
    print(f"    attestation        {receipt['attestation_status']} "
          f"({receipt['attestation']['kind'] if receipt['attestation'] else 'absent'})")
    print(f"    inputs_hash        {receipt['inputs_hash'][:16]}… "
          f"(oracle label {receipt['decision_basis']['oracle']['label']})")
    print(f"    anchor_status      {receipt['anchor_status']}")

    # 4. Third-party verification: stub mirror = the local message,
    #    base64-encoded, exactly as a mirror node returns it.
    messages = _read_jsonl(os.path.join(data_dir, "hcs_log.jsonl"))
    local_msg = next(
        m for m in messages
        if isinstance(m.get("payload"), dict)
        and m["payload"].get("settlement_id") == sid
    )
    mirror_row = {
        "message": base64.b64encode(canonical_bytes(local_msg)).decode("ascii"),
        "consensus_timestamp": local_msg["consensus_timestamp"],
        "sequence_number": local_msg["sequence"],
        "transaction_id": receipt["tx_id"],
    }
    verdict = verify_settlement(
        sid, data_dir, mirror_fetcher=lambda t, s: mirror_row
    )
    assert verdict["verdict"] == "VERIFIED", verdict
    print(f"  verify_settlement -> {verdict['verdict']} "
          f"(ledger_entry_hash_ok={verdict['ledger_entry_hash_ok']}, "
          f"chain ok={verdict['chain']['ok']}, "
          f"mirror message_hash_match={verdict['mirror']['message_hash_match']}, "
          f"attestation={verdict['attestation']['status']})")

    # 4b. Same check over HTTP: GET /audit/<settlement_id>.
    server = make_server(ex, host="127.0.0.1", port=0, demo_clock=clock)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/audit/{sid}", timeout=10
        ) as resp:
            http_body = json.loads(resp.read())
        # The HTTP route does a REAL mirror fetch against testnet:
        # the fake-runner receipt's seq 42 does not exist on the real
        # topic, so the mirror honestly reports not-found and the
        # verdict is PARTIAL here. The VERIFIED proof is the
        # in-process check above, where the stub mirror stands in for
        # the mirror node exactly as the fake runner stands in for
        # Hedera. The route itself (200 + full chain) is what this
        # step proves.
        assert resp.status == 200
        assert http_body["verdict"] in ("VERIFIED", "PARTIAL")
        print(f"  GET /audit/{sid} -> 200 {http_body['verdict']} "
              f"(mirror: {http_body['mirror'].get('reason', 'fetched')}; "
              f"the fake receipt's seq 42 is not on the real topic — "
              f"see VERIFIED check above)")
    finally:
        server.shutdown()
        server.server_close()

    # 5. Audit bundle + checkpoint. The log timestamps are T0-based
    # (fake clock), so the bundle range is anchored on the fake now.
    now = clock()
    from pathlib import Path

    bundle_dir = os.path.join(tmp, "bundle")
    bundle = create_bundle(
        Path(data_dir), T0 - timedelta(hours=1), now + timedelta(days=1),
        Path(bundle_dir),
    )
    manifest, _rc = verify_bundle(Path(bundle_dir))
    assert manifest["messages"] >= 1
    print(f"  audit bundle: {manifest['messages']} messages, "
          f"hash chain verified (bundle.json + manifest.json)")
    checkpoint = ex.checkpoint_audit_log()
    assert verify_checkpoint(checkpoint, data_dir) is True
    print(f"  checkpoint: log head seq {checkpoint['head_sequence']} "
          f"anchored (checkpoint msg seq {checkpoint['checkpoint_sequence']}); "
          f"verify_checkpoint -> True")

    # 6. Value rail (SIMULATED): fake transfer runner, no live HBAR.
    def fake_transfer_runner(amount_tinybars, to_account, memo, *, operator_account_id, network):
        return {
            "status": "SUCCESS",
            "transaction_id": f"{OPERATOR}@1794335407.470921104",
            "memo": memo,
        }

    transfer = record_transfer(
        data_dir, sid, to_account="0.0.6097565",
        amount_tinybars=100_000, memo=sid,
        runner=fake_transfer_runner, operator_account_id=OPERATOR,
    )
    assert update_receipt_transfer(data_dir, sid, transfer["transfer_tx_id"])
    print(f"  value rail (SIMULATED, no live transfer): "
          f"{transfer['amount_tinybars']} tinybars -> 0.0.6097565, "
          f"tx {transfer['transfer_tx_id']}; transfer_tx_id written onto receipt")

    # 7. Queue-throughput probe (local mechanics only).
    t0 = time.monotonic()
    for i in range(50):
        ex.hcs.anchor_queue.enqueue(
            {"sequence": 1000 + i, "message_hash": f"probe-{i}",
             "message": {"action": "probe", "payload": {"i": i}}},
        )
    drained = ex.hcs.drain_anchors()
    dt = time.monotonic() - t0
    anchored = sum(
        1 for d in drained
        if d["status"] == "anchored"
        and str(d.get("message_hash", "")).startswith("probe-")
    )
    print(f"  queue probe: 50 probe jobs enqueued + drained in {dt:.2f}s "
          f"({anchored} probe jobs anchored; the checkpoint's own job also "
          f"drained) — local mechanics only; Hedera-side bound "
          f"is receipt latency (~2-5s/submit observed on testnet 2026-11-10)")

    print()
    print("M4 DEMO OK — fail-closed settlement + receipt + third-party verification.")
    print("  Third-party walkthrough (needs only the settlement id + topic id):")
    print(f"    1. GET /audit/{sid}  -> verdict + full chain")
    print(f"    2. Mirror: GET https://testnet.mirrornode.hedera.com/api/v1/topics/"
          f"{TOPIC}/messages/{receipt['topic_sequence']}")
    print("       (base64 message decodes to the settle_market record; its hash")
    print("        must equal the local message_hash, and its payload must match")
    print("        the receipt's market_id/outcome/observed_value/evidence)")
    print(f"    3. Ledger: sha256(canonical settlements.jsonl record) must equal")
    print(f"       {receipt['ledger_entry_hash'][:16]}…")
    print(f"  (demo state kept at {data_dir}; fake runner only — no live Hedera call)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
