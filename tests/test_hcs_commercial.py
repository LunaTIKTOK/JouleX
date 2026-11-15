"""Hedera commercial-scale hardening — receipts, retry, dead-letter,
config fail-closed, message-size guard, hcs_info/hcs_stats.

Audit 2026-11-12 (AUDIT_HEDERA_COMMERCIAL_20261112.md): a failed
HCS submit left no durable trace (the receipt lived only in the
returned dict, which no caller reads), there was no retry, a mixed
mode/config silently degraded to local, and Exchange.info() reported
hcs_mode "local" even when live. These tests are the failing-first
contract for the fixes. No real key material anywhere: the fake
runner stands in for the Node submit helper.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from joulex.hcs import (
    MAX_MESSAGE_BYTES,
    LocalHCS,
    RealHCS,
    load_hcs_config,
    make_hcs,
)

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


def _real(tmp_path, runner, monkeypatch, sleep_fn=None, **kw):
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
        retry_delays=(0, 0),
        sleep_fn=sleep_fn or (lambda s: None),
        **kwargs,
    )


def _read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_receipt_recorded_on_success(tmp_path, monkeypatch):
    real = _real(tmp_path, _fake_runner(), monkeypatch)
    msg = real.append(sender="a", action="place_order", payload={"x": 1}, now=T0)
    # M4: append enqueues — nothing is submitted on the trade path.
    assert msg["hedera"]["status"] == "queued"
    assert msg["hedera"]["job_id"].startswith("anch_")
    assert _read_jsonl(tmp_path / "hcs_receipts.jsonl") == []
    # The worker drains the job; the receipt lands durably.
    assert real.drain_anchors()[0]["status"] == "anchored"
    receipts = _read_jsonl(tmp_path / "hcs_receipts.jsonl")
    assert len(receipts) == 1
    rec = receipts[0]
    assert rec["sequence"] == 1
    assert rec["status"] == "SUCCESS"
    assert rec["topic_sequence"] == "42"
    assert rec["transaction_id"] == "0.0.1@1794333600.000000000"
    assert rec["message_hash"] == msg["message_hash"]
    assert rec["attempts"] == 1
    # Dead-letter stays empty on success.
    assert _read_jsonl(tmp_path / "hcs_dead_letter.jsonl") == []
    # Lookup by local sequence.
    assert real.receipt_for(1)["status"] == "SUCCESS"
    assert real.receipt_for(99) is None


def test_dead_letter_on_final_failure(tmp_path, monkeypatch):
    runner = _fake_runner(failures=99)
    real = _real(tmp_path, runner, monkeypatch)
    msg = real.append(sender="a", action="place_order", payload={"x": 1}, now=T0)
    assert msg["hedera"]["status"] == "queued"
    done = real.drain_anchors()
    assert done[-1]["status"] == "dead"
    assert done[-1]["error"] and "network down" in done[-1]["error"]
    # Retried: 1 initial + 2 retries (queue_max_attempts=3).
    assert runner.calls["n"] == 3
    # The local record survives (fail-retain, unchanged semantics).
    assert real.verify_log()["ok"] is True
    # No success receipt; one dead-letter line with the failure.
    assert _read_jsonl(tmp_path / "hcs_receipts.jsonl") == []
    dead = _read_jsonl(tmp_path / "hcs_dead_letter.jsonl")
    assert len(dead) == 1
    assert dead[0]["sequence"] == 1
    assert dead[0]["status"] == "failed"
    assert "network down" in dead[0]["error"]
    assert dead[0]["attempts"] == 3
    assert real.receipt_for(1)["status"] == "failed"


def test_retry_transient_then_success(tmp_path, monkeypatch):
    runner = _fake_runner(failures=2)
    real = _real(tmp_path, runner, monkeypatch)
    msg = real.append(sender="a", action="place_order", payload={"x": 1}, now=T0)
    assert msg["hedera"]["status"] == "queued"
    done = real.drain_anchors()
    # Zero backoff: the queue retries inside one drain (2 failures,
    # then success).
    assert [d["status"] for d in done] == ["queued", "queued", "anchored"]
    assert runner.calls["n"] == 3
    assert len(_read_jsonl(tmp_path / "hcs_receipts.jsonl")) == 1
    assert _read_jsonl(tmp_path / "hcs_dead_letter.jsonl") == []


def test_retry_backoff_delays(tmp_path, monkeypatch):
    # M4: backoff lives in the queue (next_attempt_at), not in sleeps
    # on the trade path. A failed job is not due again until its
    # backoff elapses.
    from datetime import timedelta

    runner = _fake_runner(failures=2)
    monkeypatch.setenv("JOULEX_HCS_PRIVATE_KEY", "0x" + "ab" * 32)
    now = {"t": T0}
    real = RealHCS(
        str(tmp_path),
        operator_account_id="0.0.11389089",
        topic_id="0.0.11399235",
        runner=runner,
        queue_retry_delays=(1.0, 2.0),
        queue_max_attempts=3,
        queue_now_fn=lambda: now["t"],
    )
    real.append(sender="a", action="place_order", payload={"x": 1}, now=T0)
    assert real.drain_anchors()[0]["status"] == "queued"
    assert runner.calls["n"] == 1
    assert real.drain_anchors() == []  # backoff: not due yet
    now["t"] = T0 + timedelta(seconds=1.5)
    assert real.drain_anchors()[0]["status"] == "queued"
    assert runner.calls["n"] == 2
    now["t"] = T0 + timedelta(seconds=4)
    assert real.drain_anchors()[0]["status"] == "anchored"
    assert runner.calls["n"] == 3


def test_no_retry_on_timeout(tmp_path, monkeypatch):
    def runner(message_bytes, *, operator_account_id, topic_id, network):
        runner.calls += 1
        raise RuntimeError("RealHCS: Hedera submission timed out after 60s")

    runner.calls = 0
    real = _real(tmp_path, runner, monkeypatch)
    msg = real.append(sender="a", action="place_order", payload={"x": 1}, now=T0)
    assert msg["hedera"]["status"] == "queued"
    done = real.drain_anchors()
    assert done[0]["status"] == "dead"
    assert runner.calls == 1  # a timeout is not retried: waiting longer helps nothing
    assert len(_read_jsonl(tmp_path / "hcs_dead_letter.jsonl")) == 1


def test_message_size_guard(tmp_path, monkeypatch):
    runner = _fake_runner()
    real = _real(tmp_path, runner, monkeypatch)
    big = {"x": "y" * (MAX_MESSAGE_BYTES + 1000)}
    msg = real.append(sender="a", action="place_order", payload=big, now=T0)
    assert msg["hedera"]["status"] == "failed"
    assert "exceeds" in msg["hedera"]["error"]
    assert runner.calls["n"] == 0  # never submitted: fail fast, don't pay for a doomed tx
    assert len(real.messages()) == 1  # local record still kept
    assert len(_read_jsonl(tmp_path / "hcs_dead_letter.jsonl")) == 1


def test_hcs_info(tmp_path, monkeypatch):
    local = LocalHCS(str(tmp_path))
    assert local.hcs_info() == {"mode": "local", "topic": None, "network": None}
    real = _real(tmp_path, _fake_runner(), monkeypatch)
    assert real.hcs_info() == {
        "mode": "hedera",
        "topic": "0.0.11399235",
        "network": "testnet",
    }


def test_hcs_stats(tmp_path, monkeypatch):
    local = LocalHCS(str(tmp_path / "a"))
    assert local.hcs_stats() == {"messages": 0, "submitted": 0, "failed": 0}
    local.append(sender="a", action="x", payload={}, now=T0)
    assert local.hcs_stats()["messages"] == 1
    real = _real(tmp_path / "b", _fake_runner(), monkeypatch)
    real.append(sender="a", action="x", payload={}, now=T0)
    # M4: queued, not yet submitted — the receipt lands on drain.
    assert real.hcs_stats() == {"messages": 1, "submitted": 0, "failed": 0}
    real.drain_anchors()
    stats = real.hcs_stats()
    assert stats == {"messages": 1, "submitted": 1, "failed": 0}
    runner = _fake_runner(failures=99)
    real2 = _real(tmp_path / "b", runner, monkeypatch)
    real2.append(sender="a", action="x", payload={}, now=T0)
    real2.drain_anchors()
    stats2 = real2.hcs_stats()
    assert stats2["messages"] == 2
    assert stats2["submitted"] == 1
    assert stats2["failed"] == 1


def test_make_hcs_mixed_mode_fails_closed(tmp_path, monkeypatch):
    # Env demands hedera but the config says local: refuse, never
    # silently run local and lose anchoring.
    (tmp_path / "hcs_config.json").write_text(
        json.dumps({"mode": "local", "hedera_topic": None, "network": "testnet"})
    )
    monkeypatch.setenv("JOULEX_HCS_MODE", "hedera")
    monkeypatch.setenv("JOULEX_HCS_OPERATOR", "0.0.11389089")
    with pytest.raises(RuntimeError, match="hcs_config.json"):
        make_hcs(str(tmp_path))


def test_make_hcs_hedera_env_missing_config_fails_closed(tmp_path, monkeypatch):
    # No config file at all + env hedera: the default config is local,
    # which disagrees with the env flag — refuse, don't silently go local.
    monkeypatch.setenv("JOULEX_HCS_MODE", "hedera")
    monkeypatch.setenv("JOULEX_HCS_OPERATOR", "0.0.11389089")
    assert load_hcs_config(str(tmp_path))["mode"] == "local"
    with pytest.raises(RuntimeError):
        make_hcs(str(tmp_path))


def test_make_hcs_config_hedera_without_env_stays_local(tmp_path, monkeypatch):
    # The normal deploy shape: config names a real topic but the env
    # flag is unset — the server stays local (Render default). This
    # must NOT raise.
    (tmp_path / "hcs_config.json").write_text(
        json.dumps({"mode": "hedera", "hedera_topic": "0.0.11399235", "network": "testnet"})
    )
    monkeypatch.delenv("JOULEX_HCS_MODE", raising=False)
    assert isinstance(make_hcs(str(tmp_path)), LocalHCS)
