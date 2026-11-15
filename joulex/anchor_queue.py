"""Async anchor queue (M4) — settlements (and every other action) are
anchored to Hedera OFF the trade path.

The audit (2026-11-12, H3) found the synchronous submit held the
exchange lock for up to 60s per action: one hung Hedera call blocked
ALL trading. The queue fixes that structurally: ``RealHCS.append``
writes locally and enqueues; a worker thread (or an explicit
``drain_anchors()``) submits later, with bounded retry + dead-letter
reusing the receipts/dead-letter machinery in ``joulex/hcs.py``.

Durability: jobs live in ``<data_dir>/anchor_queue.json`` (atomic
temp-file + fsync + os.replace, like ``store.save_json``), so a
queued job survives a restart. Enqueue is idempotent on
``message_hash`` — a re-submitted message never creates a second
job. A job is ``queued`` (due or waiting on backoff), ``anchored``
(receipt recorded), or ``dead`` (terminal error, or attempts
exhausted — recorded in the dead-letter file, never retried again).

The queue never holds the exchange lock: it runs in its own thread
and calls only the submit function + the ``on_complete`` callback.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from .attestation import canonical_bytes

QUEUE_FILENAME = "anchor_queue.json"
_DEFAULT_RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0, 16.0)
_DEFAULT_MAX_ATTEMPTS = 5


def _is_terminal_error(message: str) -> bool:
    """Errors waiting cannot fix: a timeout means the network (or the
    helper) is hung; a missing key will never appear on retry. Same
    rule as ``RealHCS._should_retry`` (joulex/hcs.py)."""
    lowered = message.lower()
    return "timed out" in lowered or "is not set" in lowered


class AnchorQueue:
    """Durable FIFO of HCS anchor jobs.

    ``submit_fn`` takes the canonical message bytes and returns the
    submit receipt dict (``{"status": "SUCCESS", ...}``) — it may
    raise on failure; either way the queue records the outcome.
    ``on_complete`` (optional) is called after every job reaches a
    terminal state — ``(message, receipt, status)`` where status is
    ``"anchored"`` or ``"dead"`` — so the exchange can finalize
    settlements whose anchor just landed. A callback failure is
    swallowed (the anchor is already recorded; the callback must
    never lose it).
    """

    def __init__(
        self,
        data_dir: str,
        submit_fn: Callable[[bytes], dict[str, Any]],
        *,
        retry_delays: tuple[float, ...] = _DEFAULT_RETRY_DELAYS,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        sleep_fn=None,
        now_fn=None,
        on_complete: Callable[[dict[str, Any], dict[str, Any], str], None] | None = None,
    ):
        self.data_dir = data_dir
        self._submit_fn = submit_fn
        self._retry_delays = tuple(retry_delays)
        self._max_attempts = max_attempts
        self._sleep_fn = sleep_fn or time.sleep
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._on_complete = on_complete
        self._lock = threading.Lock()
        self._path = os.path.join(data_dir, QUEUE_FILENAME)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        os.makedirs(data_dir, exist_ok=True)
        self._jobs: list[dict[str, Any]] = self._load()

    # -- persistence ---------------------------------------------------
    def _load(self) -> list[dict[str, Any]]:
        if not os.path.exists(self._path):
            return []
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                state = json.load(f)
        except (ValueError, OSError):
            raise RuntimeError(
                f"refusing to start: unreadable anchor queue: {self._path}"
            ) from None
        if not isinstance(state, dict) or not isinstance(state.get("jobs"), list):
            raise RuntimeError(
                f"refusing to start: corrupt anchor queue: {self._path}"
            )
        return [dict(j) for j in state["jobs"]]

    def _save(self) -> None:
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps({"jobs": self._jobs}, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)

    def _now(self) -> datetime:
        now = self._now_fn()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now

    # -- queue operations ----------------------------------------------
    def enqueue(self, message: dict[str, Any]) -> dict[str, Any]:
        """Queue one message for anchoring. Idempotent on
        ``message_hash``: a message already queued/anchored/dead
        returns the existing job, never a duplicate."""
        message_hash = str(message.get("message_hash", ""))
        now = self._now().isoformat()
        with self._lock:
            for job in self._jobs:
                if job.get("message_hash") == message_hash:
                    return dict(job)
            job: dict[str, Any] = {
                "job_id": "anch_" + secrets.token_hex(6),
                "sequence": message.get("sequence"),
                "message_hash": message_hash,
                "message": dict(message),
                "status": "queued",
                "attempts": 0,
                "receipt": None,
                "error": None,
                "enqueued_at": now,
                "updated_at": now,
                "next_attempt_at": now,
            }
            self._jobs.append(job)
            self._save()
            return dict(job)

    def _due_job(self, now: datetime) -> dict[str, Any] | None:
        for job in self._jobs:
            if job["status"] != "queued":
                continue
            try:
                due = datetime.fromisoformat(str(job["next_attempt_at"]).replace("Z", "+00:00"))
            except ValueError:
                due = now
            if due <= now:
                return job
        return None

    def drain_once(self, now: datetime | None = None) -> dict[str, Any] | None:
        """Attempt the oldest due queued job. Returns the job (with its
        new status) or None when nothing is due. A failure leaves the
        job queued with a backoff timestamp unless it is terminal or
        attempts are exhausted — then it is dead."""
        at = now or self._now()
        with self._lock:
            job = self._due_job(at)
            if job is None:
                return None
            message = dict(job["message"])
        try:
            receipt = self._submit_fn(canonical_bytes(message))
            if not isinstance(receipt, dict):
                raise RuntimeError(f"bad anchor receipt: {receipt!r}")
            error = None
            if receipt.get("status") == "failed":
                error = str(receipt.get("error") or "anchor failed")
        except Exception as exc:  # noqa: BLE001 — the failure is data
            receipt = {"status": "failed", "error": str(exc)}
            error = str(exc)
        terminal = error is not None and _is_terminal_error(error)
        with self._lock:
            # Re-find the job: another thread may have drained it.
            job = next(
                (j for j in self._jobs if j["job_id"] == job["job_id"]), None
            )
            if job is None:
                return None
            job["attempts"] = int(job.get("attempts", 0)) + 1
            job["updated_at"] = self._now().isoformat()
            if error is None:
                job["status"] = "anchored"
                job["receipt"] = receipt
                job["error"] = None
                terminal_status = "anchored"
            elif terminal or job["attempts"] >= self._max_attempts:
                job["status"] = "dead"
                job["receipt"] = receipt
                job["error"] = error
                terminal_status = "dead"
            else:
                delay = self._retry_delays[
                    min(job["attempts"] - 1, len(self._retry_delays) - 1)
                ]
                job["status"] = "queued"
                job["receipt"] = receipt
                job["error"] = error
                job["next_attempt_at"] = (
                    self._now() + _timedelta(seconds=delay)
                ).isoformat()
                terminal_status = None
            self._save()
            out = dict(job)
        if terminal_status is not None and self._on_complete is not None:
            try:
                self._on_complete(message, dict(receipt), terminal_status)
            except Exception:  # noqa: BLE001 — never lose an anchor
                pass
        return out

    def drain_all(self, max_jobs: int | None = None) -> list[dict[str, Any]]:
        """Drain every due job, in order, until none is due (or the
        budget is hit). A job that fails with backoff is NOT retried
        in the same call — its next attempt is in the future."""
        done: list[dict[str, Any]] = []
        while max_jobs is None or len(done) < max_jobs:
            job = self.drain_once()
            if job is None:
                break
            done.append(job)
        return done

    # -- worker thread ---------------------------------------------------
    def start(self, poll_seconds: float = 2.0) -> None:
        """Start the background drain thread (daemon). Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()

        def _run() -> None:
            while not self._stop.wait(poll_seconds):
                try:
                    self.drain_all()
                except Exception:  # noqa: BLE001 — worker must not die
                    continue

        self._thread = threading.Thread(target=_run, daemon=True, name="anchor-queue")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    # -- views -----------------------------------------------------------
    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "queued": sum(1 for j in self._jobs if j["status"] == "queued"),
                "anchored": sum(1 for j in self._jobs if j["status"] == "anchored"),
                "dead": sum(1 for j in self._jobs if j["status"] == "dead"),
                "total": len(self._jobs),
            }

    def pending(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(j) for j in self._jobs if j["status"] == "queued"]

    def job_for_message_hash(self, message_hash: str) -> dict[str, Any] | None:
        with self._lock:
            for job in self._jobs:
                if job.get("message_hash") == str(message_hash):
                    return dict(job)
        return None

    def job_for_sequence(self, sequence: int) -> dict[str, Any] | None:
        with self._lock:
            for job in self._jobs:
                if job.get("sequence") == sequence:
                    return dict(job)
        return None


def _timedelta(seconds: float):
    from datetime import timedelta

    return timedelta(seconds=seconds)
