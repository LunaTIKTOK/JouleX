"""Async anchor queue (M4) — the exchange lock must NEVER be held by
an HCS submit again.

RealHCS.append now writes locally and ENQUEUES the anchor job; a
worker (background thread, or an explicit drain) submits it off the
trade path with bounded retry + dead-letter. These tests are the
failing-first contract. No real key material: the fake runner stands
in for the Node submit helper.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from joulex.anchor_queue import AnchorQueue
from joulex.hcs import RealHCS

T0 = datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc)


def _fake_runner(receipt=None, failures=0):
    """Fails ``failures`` times (RuntimeError), then returns receipt."""
    calls = {"n": 0}

    def runner(message_bytes, *, operator_account_id, topic_id, network):
        calls["n"] += 1
        if calls["n"] <= failures:
            raise RuntimeError("network down")
        return receipt or {
            "status": "SUCCESS",
            "topic_sequence": "42",
            "consensus_timestamp": "2026-11-10T12:00:00.000000000Z",
            "transaction_id": "0.0.1@1794333600.000000000",
        }

    runner.calls = calls
    return runner


def _real(tmp_path, runner, monkeypatch, **kw):
    monkeypatch.setenv("JOULEX_HCS_PRIVATE_KEY", "0x" + "ab" * 32)
    kwargs = {
        "queue_retry_delays": (0, 0, 0, 0),
        "queue_max_attempts": 3,
    }
    kwargs.update(kw)
    return RealHCS(
        str(tmp_path),
        operator_account_id="0.0.11389089",
        topic_id="0.0.11399235",
        network="testnet",
        runner=runner,
        **kwargs,
    )


def _read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


class _Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now = self.now + timedelta(seconds=seconds)


def test_append_enqueues_without_submitting(tmp_path, monkeypatch):
    runner = _fake_runner()
    real = _real(tmp_path, runner, monkeypatch)
    msg = real.append(sender="a", action="place_order", payload={"x": 1}, now=T0)
    # No submit yet: the trade path never waits on Hedera.
    assert runner.calls["n"] == 0
    assert msg["hedera"]["status"] == "queued"
    assert msg["hedera"]["job_id"].startswith("anch_")
    assert msg["hedera"]["topic"] == "0.0.11399235"
    # The local record stands (fail-retain semantics unchanged).
    assert real.verify_log()["ok"] is True
    assert real.receipt_for(1) is None  # nothing anchored yet
    assert real.anchor_stats() == {"queued": 1, "anchored": 0, "dead": 0, "total": 1}


def test_drain_anchors_success(tmp_path, monkeypatch):
    runner = _fake_runner()
    real = _real(tmp_path, runner, monkeypatch)
    msg = real.append(sender="a", action="place_order", payload={"x": 1}, now=T0)
    done = real.drain_anchors()
    assert len(done) == 1
    assert done[0]["status"] == "anchored"
    assert runner.calls["n"] == 1
    receipts = _read_jsonl(tmp_path / "hcs_receipts.jsonl")
    assert len(receipts) == 1
    assert receipts[0]["sequence"] == 1
    assert receipts[0]["status"] == "SUCCESS"
    assert receipts[0]["topic_sequence"] == "42"
    assert receipts[0]["message_hash"] == msg["message_hash"]
    assert real.receipt_for(1)["status"] == "SUCCESS"
    assert real.anchor_stats() == {"queued": 0, "anchored": 1, "dead": 0, "total": 1}


def test_enqueue_idempotent_on_message_hash(tmp_path, monkeypatch):
    runner = _fake_runner()
    real = _real(tmp_path, runner, monkeypatch)
    queue = real.anchor_queue
    message = {"sequence": 7, "message_hash": "abc", "payload": {}}
    first = queue.enqueue(message)
    second = queue.enqueue(dict(message))
    assert first["job_id"] == second["job_id"]
    assert queue.stats()["total"] == 1


def test_retry_then_success_across_drains(tmp_path, monkeypatch):
    runner = _fake_runner(failures=1)
    real = _real(tmp_path, runner, monkeypatch)
    real.append(sender="a", action="place_order", payload={"x": 1}, now=T0)
    # Zero backoff: one drain_all call retries immediately (failed
    # attempt re-queues, then succeeds — both in the same drain).
    done = real.drain_anchors()
    assert [d["status"] for d in done] == ["queued", "anchored"]
    assert runner.calls["n"] == 2
    assert len(_read_jsonl(tmp_path / "hcs_receipts.jsonl")) == 1
    assert real.anchor_stats() == {"queued": 0, "anchored": 1, "dead": 0, "total": 1}


def test_terminal_error_goes_dead_immediately(tmp_path, monkeypatch):
    def runner(message_bytes, *, operator_account_id, topic_id, network):
        runner.calls += 1
        raise RuntimeError("RealHCS: Hedera submission timed out after 60s")

    runner.calls = 0
    real = _real(tmp_path, runner, monkeypatch)
    real.append(sender="a", action="place_order", payload={"x": 1}, now=T0)
    done = real.drain_anchors()
    assert done[0]["status"] == "dead"  # a timeout is not retried
    assert runner.calls == 1
    dead = _read_jsonl(tmp_path / "hcs_dead_letter.jsonl")
    assert len(dead) == 1
    assert "timed out" in dead[0]["error"]


def test_max_attempts_then_dead(tmp_path, monkeypatch):
    runner = _fake_runner(failures=99)
    real = _real(tmp_path, runner, monkeypatch)
    real.append(sender="a", action="place_order", payload={"x": 1}, now=T0)
    for _ in range(3):
        real.drain_anchors()
    assert real.anchor_stats() == {"queued": 0, "anchored": 0, "dead": 1, "total": 1}
    assert runner.calls["n"] == 3
    assert len(_read_jsonl(tmp_path / "hcs_dead_letter.jsonl")) == 1
    # A dead job is never drained again.
    assert real.drain_anchors() == []


def test_backoff_defers_retry_until_due(tmp_path, monkeypatch):
    clock = _Clock(T0)
    runner = _fake_runner(failures=1)
    real = _real(
        tmp_path, runner, monkeypatch,
        queue_retry_delays=(60.0, 120.0),
        queue_max_attempts=3,
        queue_now_fn=clock,
    )
    real.append(sender="a", action="place_order", payload={"x": 1}, now=T0)
    assert real.drain_anchors()[0]["status"] == "queued"
    # Not due yet: a second drain does nothing (no extra submit).
    assert real.drain_anchors() == []
    assert runner.calls["n"] == 1
    clock.advance(61)
    assert real.drain_anchors()[0]["status"] == "anchored"
    assert runner.calls["n"] == 2


def test_on_anchor_callback_invoked(tmp_path, monkeypatch):
    seen = []
    runner = _fake_runner()
    real = _real(
        tmp_path, runner, monkeypatch,
        on_anchor=lambda message, receipt: seen.append((message["sequence"], receipt["status"])),
    )
    real.append(sender="a", action="place_order", payload={"x": 1}, now=T0)
    real.drain_anchors()
    assert seen == [(1, "SUCCESS")]


def test_worker_thread_drains_in_background(tmp_path, monkeypatch):
    runner = _fake_runner()
    real = _real(tmp_path, runner, monkeypatch)
    real.start_anchor_worker(poll_seconds=0.05)
    try:
        real.append(sender="a", action="place_order", payload={"x": 1}, now=T0)
        deadline = time.time() + 5
        while real.anchor_stats()["anchored"] == 0 and time.time() < deadline:
            time.sleep(0.05)
        assert real.anchor_stats()["anchored"] == 1
        assert real.receipt_for(1)["status"] == "SUCCESS"
    finally:
        real.stop_anchor_worker()


def test_receipt_visible_once_stats_say_anchored(tmp_path, monkeypatch):
    """REGRESSION (flake 2026-11-14 08:01 run): the full suite failed
    ``test_worker_thread_drains_in_background`` with TypeError at
    ``receipt_for(1)["status"]`` — stats said anchored == 1 while
    receipt_for returned None.

    Root cause: ``AnchorQueue.drain_once`` marks the job anchored and
    persists the queue BEFORE invoking ``on_complete``; the receipts
    file is only written inside that callback
    (``RealHCS._anchor_completed`` -> ``_record_outcome``). A consumer
    polling stats and then reading the receipt can observe the window
    between the two. The receipt must be visible the moment stats say
    anchored — no window where a stats poller reads None.

    Deterministic repro: block ``_record_outcome`` on an event, drain
    in a thread, and read the receipt while the callback is still
    gated."""
    from joulex.hcs import RealHCS as _RealHCS

    runner = _fake_runner()
    real = _real(tmp_path, runner, monkeypatch)
    gate = threading.Event()
    orig_record = _RealHCS._record_outcome

    def blocking_record(self, local, *, receipt, attempts):
        gate.wait(timeout=10)
        return orig_record(self, local, receipt=receipt, attempts=attempts)

    monkeypatch.setattr(_RealHCS, "_record_outcome", blocking_record)
    real.append(sender="a", action="place_order", payload={"x": 1}, now=T0)
    t = threading.Thread(target=real.drain_anchors)
    t.start()
    try:
        deadline = time.time() + 5
        while real.anchor_stats()["anchored"] == 0 and time.time() < deadline:
            time.sleep(0.01)
        assert real.anchor_stats()["anchored"] == 1
        rec = real.receipt_for(1)
        assert rec is not None
        assert rec["status"] == "SUCCESS"
        assert rec["topic_sequence"] == "42"
    finally:
        gate.set()
        t.join(timeout=10)


def test_queue_persists_across_restart(tmp_path, monkeypatch):
    runner = _fake_runner()
    real = _real(tmp_path, runner, monkeypatch)
    real.append(sender="a", action="place_order", payload={"x": 1}, now=T0)
    # A fresh queue object over the same data dir sees the queued job.
    queue2 = AnchorQueue(str(tmp_path), real._submit_bytes, retry_delays=(0,))
    assert queue2.stats() == {"queued": 1, "anchored": 0, "dead": 0, "total": 1}
    assert len(queue2.drain_all()) == 1
    assert queue2.stats()["anchored"] == 1


def test_queue_thread_safe_enqueue(tmp_path, monkeypatch):
    queue = AnchorQueue(str(tmp_path), lambda b: {"status": "SUCCESS"})
    messages = [{"sequence": i, "message_hash": f"h{i}", "payload": {}} for i in range(20)]
    threads = [
        threading.Thread(target=lambda m=m: queue.enqueue(m)) for m in messages
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert queue.stats()["total"] == 20
