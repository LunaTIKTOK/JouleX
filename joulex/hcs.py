"""HCS-schema audit log — DESIGN.md §7.3 (M1).

Every agent action (order, market creation, settlement, attestation
verification result) appends one ordered message to
``data/hcs_log.jsonl``. The schema mirrors Hedera Consensus Service
semantics: topic, strictly increasing sequence, consensus timestamp in
HCS nanosecond format, sender, action, payload hash, attestation,
provenance (who told whom / what was known), and a hash chain.

HONEST LABELLING: the log file header records
``{"mode": "local", "hedera_topic": null}``. The timestamp is a LOCAL
simulation of a consensus timestamp, never a Hedera consensus
timestamp. Going live on real Hedera is an owner step (docs/HEDERA.md);
``RealHCS`` submits to a real topic and records the receipt alongside
the local copy, but the local log stays the fallback and replay source
either way.

The log is append-only. There is no edit, delete, or rewrite API. A
corrupt log refuses startup (fail closed), like the challenge store:
``LocalHCS`` raises ``RuntimeError`` on load when the header is wrong,
a line is malformed, or ``verify_log`` reports a break.

COMMERCIAL HARDENING (audit 2026-11-12,
AUDIT_HEDERA_COMMERCIAL_20261112.md): ``RealHCS`` records every
submission outcome durably — successes to ``hcs_receipts.jsonl``,
final failures to ``hcs_dead_letter.jsonl`` — so a failed anchor is
visible after the request returns instead of living only in a return
value no caller reads. Transient submit failures are retried with
backoff (timeouts and a missing key are not retried); after the
retries exhaust the outcome is still fail-retain (the local record
stands, the trade already executed), but the failure is now
observable via ``hcs_stats()`` and the dead-letter file.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any

from .attestation import canonical_bytes
from .anchor_queue import AnchorQueue

TOPIC = "joulex-actions"
FILENAME = "hcs_log.jsonl"
RECEIPTS_FILENAME = "hcs_receipts.jsonl"
DEAD_LETTER_FILENAME = "hcs_dead_letter.jsonl"
HEADER = {"mode": "local", "hedera_topic": None}
GENESIS_PREV_HASH = "0" * 64

# Hedera SDK defaults (docs.hedera.com, "Submit a message"): max
# chunk 1024 bytes, max chunks 20 — a message larger than ~20 KiB
# cannot be submitted with the default transaction shape. Joulex
# messages are ~1.2 KiB (attested ~2 KiB); the guard below fails
# fast instead of paying for a doomed transaction.
MAX_MESSAGE_BYTES = 20 * 1024
# Bound how long one submit can hold the exchange lock: the SDK
# receipt wait is normally seconds; 60s is generous, 180s let a hung
# submit block all trading for three minutes.
_SUBMIT_TIMEOUT_SECONDS = 60
# Retry delays after the first attempt (two retries, then fail-retain).
_RETRY_DELAYS = (1.0, 2.0)

_HCS_SUBMIT_SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "hcs_submit.mjs"
)
_NETWORKS = ("testnet", "mainnet")
_KEY_ENV = "JOULEX_HCS_PRIVATE_KEY"
_OPERATOR_ENV = "JOULEX_HCS_OPERATOR"
_MODE_ENV = "JOULEX_HCS_MODE"


def format_hcs_timestamp(dt: datetime) -> str:
    """HCS nanosecond format: ``2026-11-09T23:52:00.000000000Z``.

    Python datetimes carry microseconds; the last three digits are
    zero (nanosecond precision is not available, and the value is
    labelled LOCAL anyway — see the module docstring)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond * 1000:09d}Z"


def parse_hcs_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _parse_time(value: Any) -> datetime:
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _message_hash(message: dict[str, Any]) -> str:
    """SHA-256 over the canonical bytes of the message, excluding
    ``message_hash`` itself."""
    body = {k: v for k, v in message.items() if k != "message_hash"}
    return hashlib.sha256(canonical_bytes(body)).hexdigest()


def _payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def verify_log_file(path: str) -> dict[str, Any]:
    """Re-walk a log file from disk: header, sequence continuity,
    hash-chain integrity, payload-hash integrity, timestamp
    monotonicity. Returns ``{"ok", "messages", "errors"}``; any break
    is a hard failure, reported, never silently repaired."""
    errors: list[str] = []
    messages: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError as exc:
        return {"ok": False, "messages": 0, "errors": [f"unreadable log: {exc}"]}
    if not lines:
        return {"ok": False, "messages": 0, "errors": ["empty log: missing header"]}
    try:
        header = json.loads(lines[0])
    except ValueError as exc:
        return {"ok": False, "messages": 0, "errors": [f"corrupt header: {exc}"]}
    if header != HEADER:
        errors.append(f"bad header: {header!r} (expected {HEADER!r})")
    for i, line in enumerate(lines[1:], start=2):
        if not line.strip():
            errors.append(f"line {i}: blank line in log")
            continue
        try:
            msg = json.loads(line)
        except ValueError as exc:
            errors.append(f"line {i}: malformed JSON: {exc}")
            continue
        if not isinstance(msg, dict):
            errors.append(f"line {i}: message is not an object")
            continue
        messages.append(msg)
    prev_hash = GENESIS_PREV_HASH
    prev_ts: datetime | None = None
    for idx, msg in enumerate(messages):
        n = idx + 1
        if msg.get("sequence") != n:
            errors.append(
                f"message {n}: sequence {msg.get('sequence')!r} != expected {n}"
            )
        if msg.get("topic") != TOPIC:
            errors.append(f"message {n}: topic {msg.get('topic')!r} != {TOPIC!r}")
        if msg.get("prev_hash") != prev_hash:
            errors.append(f"message {n}: prev_hash breaks the chain")
        if msg.get("message_hash") != _message_hash(msg):
            errors.append(f"message {n}: message_hash mismatch (tampered content)")
        payload = msg.get("payload")
        if isinstance(payload, dict):
            if msg.get("payload_hash") != _payload_hash(payload):
                errors.append(f"message {n}: payload_hash mismatch")
        else:
            errors.append(f"message {n}: payload is not an object")
        try:
            ts = parse_hcs_timestamp(str(msg.get("consensus_timestamp")))
        except ValueError:
            errors.append(f"message {n}: bad consensus_timestamp")
            ts = None
        if ts is not None and prev_ts is not None and ts < prev_ts:
            errors.append(f"message {n}: consensus_timestamp goes backwards")
        if ts is not None:
            prev_ts = ts
        if isinstance(msg.get("message_hash"), str):
            prev_hash = str(msg["message_hash"])
    return {"ok": not errors, "messages": len(messages), "errors": errors}


class HCSClient:
    """Interface for the audit-log sink. ``LocalHCS`` is the local
    simulation; ``RealHCS`` submits to a real Hedera HCS topic (see
    docs/HEDERA.md) while keeping the local log as the fallback and
    replay source."""

    def append(self, *, sender: str, action: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError

    def submit(self, message: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def messages(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def what_did_agent_know(self, agent_id: str, t: datetime | str) -> dict[str, Any]:
        raise NotImplementedError

    def who_told_whom(
        self,
        agent_a: str,
        agent_b: str,
        t0: datetime | str | None = None,
        t1: datetime | str | None = None,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    def verify_log(self) -> dict[str, Any]:
        raise NotImplementedError

    def hcs_info(self) -> dict[str, Any]:
        """Live HCS mode for status endpoints: mode/topic/network."""
        raise NotImplementedError

    def hcs_stats(self) -> dict[str, Any]:
        """Submission counters: messages, submitted, failed."""
        raise NotImplementedError

    def receipt_for(self, sequence: int) -> dict[str, Any] | None:
        """The durable submission outcome for local ``sequence`` —
        None when never submitted (local-only mode)."""
        raise NotImplementedError


class LocalHCS(HCSClient):
    """LIVE in v1: the local HCS-schema log at ``<data_dir>/hcs_log.jsonl``.

    The first line is the header ``{"mode": "local", "hedera_topic":
    null}``. Startup re-walks the whole log (``verify_log_file``) and
    refuses to start on any corruption — fail closed."""

    def __init__(self, data_dir: str, *, now_fn=None):
        self.data_dir = data_dir
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        os.makedirs(data_dir, exist_ok=True)
        self._path = os.path.join(data_dir, FILENAME)
        if not os.path.exists(self._path):
            with open(self._path, "w", encoding="utf-8") as f:
                f.write(json.dumps(HEADER, sort_keys=True) + "\n")
                f.flush()
                os.fsync(f.fileno())
        report = verify_log_file(self._path)
        if not report["ok"]:
            raise RuntimeError(
                "refusing to start: corrupt HCS audit log: "
                + "; ".join(report["errors"])
            )
        self._messages: list[dict[str, Any]] = self._read_messages()

    def _read_messages(self) -> list[dict[str, Any]]:
        with open(self._path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        return [json.loads(line) for line in lines[1:] if line.strip()]

    def now(self) -> datetime:
        return self._now_fn()

    # -- append --------------------------------------------------------
    def append(
        self,
        *,
        sender: str,
        action: str,
        payload: dict[str, Any],
        attestation: dict[str, Any] | None = None,
        provenance: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Append one ordered message. ``provenance`` is
        ``{"told_by": <agent_id or 'exchange' or 'aeso-oracle'>,
        "known_facts": [<payload hashes the actor had seen>]}``.
        Returns the appended message (with sequence, consensus
        timestamp, hashes assigned)."""
        ts = now or self.now()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        prev = self._messages[-1] if self._messages else None
        message: dict[str, Any] = {
            "topic": TOPIC,
            "sequence": (prev["sequence"] + 1) if prev else 1,
            "consensus_timestamp": format_hcs_timestamp(ts),
            "sender": str(sender),
            "action": str(action),
            "payload_hash": _payload_hash(payload),
            "payload": payload,
            "attestation": attestation,
            "provenance": provenance or {"told_by": None, "known_facts": []},
            "prev_hash": prev["message_hash"] if prev else GENESIS_PREV_HASH,
        }
        message["message_hash"] = _message_hash(message)
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(message, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self._messages.append(message)
        return dict(message)

    def submit(self, message: dict[str, Any]) -> dict[str, Any]:
        """Local submit == append with the caller-supplied fields.
        (The name exists so LocalHCS and the future RealHCS share the
        HCSClient shape; the exchange uses ``append``.)"""
        return self.append(
            sender=str(message.get("sender", "")),
            action=str(message.get("action", "")),
            payload=dict(message.get("payload", {})),
            attestation=message.get("attestation"),
            provenance=message.get("provenance"),
        )

    # -- queries ---------------------------------------------------------
    def messages(self) -> list[dict[str, Any]]:
        return [dict(m) for m in self._messages]

    def what_did_agent_know(self, agent_id: str, t: datetime | str) -> dict[str, Any]:
        """Replay the log up to timestamp ``t``; return the facts the
        agent had received by then — the payload hashes (plus the
        messages) where the agent was the sender or the ``told_by``
        target — and the ``known_facts`` those messages record the
        agent as having seen at mint time."""
        as_of = _parse_time(t) if isinstance(t, str) else t
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=timezone.utc)
        facts: set[str] = set()
        seen: list[dict[str, Any]] = []
        for msg in self._messages:
            ts = parse_hcs_timestamp(str(msg["consensus_timestamp"]))
            if ts > as_of:
                continue
            told_by = (msg.get("provenance") or {}).get("told_by")
            if msg.get("sender") == agent_id or told_by == agent_id:
                seen.append(dict(msg))
                facts.add(str(msg["payload_hash"]))
                for fact in (msg.get("provenance") or {}).get("known_facts") or []:
                    facts.add(str(fact))
        return {
            "agent_id": agent_id,
            "as_of": format_hcs_timestamp(as_of),
            "known_facts": sorted(facts),
            "messages": seen,
        }

    def who_told_whom(
        self,
        agent_a: str,
        agent_b: str,
        t0: datetime | str | None = None,
        t1: datetime | str | None = None,
    ) -> list[dict[str, Any]]:
        """All messages where A told B: B is the acting agent (sender)
        and the message's provenance names A as ``told_by`` — A
        supplied the information B's action relied on. In order, with
        timestamps; optional [t0, t1] window (inclusive)."""
        start = _parse_time(t0) if isinstance(t0, str) else t0
        end = _parse_time(t1) if isinstance(t1, str) else t1
        out: list[dict[str, Any]] = []
        for msg in self._messages:
            if msg.get("sender") != agent_b:
                continue
            if (msg.get("provenance") or {}).get("told_by") != agent_a:
                continue
            ts = parse_hcs_timestamp(str(msg["consensus_timestamp"]))
            if start is not None and ts < start:
                continue
            if end is not None and ts > end:
                continue
            out.append(dict(msg))
        return out

    def verify_log(self) -> dict[str, Any]:
        """Re-walk the log from disk (single source of truth)."""
        return verify_log_file(self._path)

    def hcs_info(self) -> dict[str, Any]:
        return {"mode": "local", "topic": None, "network": None}

    def hcs_stats(self) -> dict[str, Any]:
        return {
            "messages": len(self._messages),
            "submitted": 0,
            "failed": 0,
        }

    def receipt_for(self, sequence: int) -> dict[str, Any] | None:
        return None  # local-only: nothing is ever submitted


def _default_runner(message_bytes, *, operator_account_id, topic_id, network):
    """Submit via the Node helper (``@hashgraph/sdk``). The operator key
    is read from the environment by the caller and reaches the helper
    through its environment only — never argv, never logged."""
    env = dict(os.environ)
    env["JOULEX_HCS_OPERATOR"] = operator_account_id
    try:
        proc = subprocess.run(
            ["node", _HCS_SUBMIT_SCRIPT, topic_id, network, message_bytes.decode("utf-8")],
            capture_output=True,
            text=True,
            timeout=_SUBMIT_TIMEOUT_SECONDS,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"RealHCS: Hedera submission timed out after "
            f"{_SUBMIT_TIMEOUT_SECONDS}s"
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"RealHCS: could not run the Hedera submit helper: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "unknown error"
        raise RuntimeError(f"RealHCS: Hedera submission failed: {detail}")
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise RuntimeError(
            f"RealHCS: unparseable submit-helper output: {proc.stdout!r}"
        ) from exc


def load_hcs_config(data_dir: str) -> dict[str, Any]:
    """Read ``<data_dir>/hcs_config.json``; missing or unreadable
    config means local mode (never a silent live claim)."""
    default = {"mode": "local", "hedera_topic": None, "network": "testnet"}
    try:
        with open(os.path.join(data_dir, "hcs_config.json"), "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return dict(default)
    if not isinstance(cfg, dict):
        return dict(default)
    out = dict(default)
    for key in ("mode", "hedera_topic", "network"):
        if cfg.get(key) is not None:
            out[key] = cfg[key]
    return out


def make_hcs(data_dir: str, *, runner=None, now_fn=None, on_anchor=None) -> HCSClient:
    """Factory: ``JOULEX_HCS_MODE=hedera`` plus a ``hedera`` config
    returns a live ``RealHCS``; anything else returns ``LocalHCS``.
    A hedera request without operator/topic fails closed — it never
    silently falls back to local. A hedera request whose CONFIG
    disagrees (env says hedera, config says local — a stale or
    missing config) also fails closed: silently losing anchoring is
    worse than refusing to start. The reverse shape — config names a
    real topic but the env flag is unset — stays local by design
    (the Render default: no flag, no anchoring).

    ``on_anchor`` (optional) is called as
    ``on_anchor(message, receipt)`` when an anchor job reaches a
    terminal state — the exchange uses it to finalize settlements
    whose anchor just landed (M4 fail-closed settlement)."""
    cfg = load_hcs_config(data_dir)
    env_hedera = os.environ.get(_MODE_ENV) == "hedera"
    if env_hedera and cfg.get("mode") != "hedera":
        raise RuntimeError(
            "JOULEX_HCS_MODE=hedera but hcs_config.json mode is "
            f"{cfg.get('mode')!r} (expected 'hedera'): refusing to run "
            "local — fix the config or unset the flag (fail closed, "
            "never silently local)."
        )
    if not env_hedera or cfg.get("mode") != "hedera":
        return LocalHCS(data_dir, now_fn=now_fn)
    operator = os.environ.get(_OPERATOR_ENV)
    topic = cfg.get("hedera_topic")
    if not operator or not topic:
        raise RuntimeError(
            "JOULEX_HCS_MODE=hedera but no operator/topic: set "
            "JOULEX_HCS_OPERATOR and a real hedera_topic in "
            "hcs_config.json (fail closed, never silently local)."
        )
    return RealHCS(
        data_dir,
        operator_account_id=operator,
        topic_id=str(topic),
        network=str(cfg.get("network") or "testnet"),
        runner=runner,
        now_fn=now_fn,
        on_anchor=on_anchor,
    )


class RealHCS(HCSClient):
    """LIVE Hedera adapter (testnet first — see docs/HEDERA.md).

    Writes locally FIRST through ``LocalHCS`` (same append-only log,
    same hash chain — the local log is the fallback and replay source),
    then submits the same message to the configured HCS topic via
    ``joulex/hcs_submit.mjs`` (``@hashgraph/sdk``). The returned
    message carries a ``hedera`` block: the receipt on success, or
    ``{"status": "failed", "error": ...}`` on submission failure — a
    failed submission never loses the local record and never blocks the
    action from being audited locally.

    ASYNC ANCHORING (M4): ``append`` no longer submits synchronously
    — the audit found the sync submit held the exchange lock for up
    to 60s per action (H3). ``append`` writes locally and enqueues
    an anchor job on ``self.anchor_queue`` (``joulex/anchor_queue.py``)
    and returns immediately with ``hedera.status == "queued"``. The
    queue's worker thread (started by the server, or an explicit
    ``drain_anchors()``) submits off the trade path with bounded
    retry + dead-letter. Every submission outcome is ALSO recorded
    durably (audit 2026-11-12): successes to
    ``hcs_receipts.jsonl``, final failures to
    ``hcs_dead_letter.jsonl`` — one line per message, keyed by
    local sequence. A failure used to live only in the returned dict,
    which no caller reads; now it is visible via ``receipt_for()`` /
    ``hcs_stats()`` and re-submittable by hand from the dead-letter
    file. Transient failures are retried (queue backoff); a timeout
    or a missing key is not retried.

    The operator private key is NEVER stored: it is read from
    ``JOULEX_HCS_PRIVATE_KEY`` at submit time and passed to the helper
    through its environment only. Reads (messages / knowledge /
    provenance / verify) come from the local log."""

    def __init__(
        self,
        data_dir: str,
        *,
        operator_account_id: str,
        topic_id: str,
        network: str = "testnet",
        runner=None,
        key_env: str = _KEY_ENV,
        now_fn=None,
        retry_delays: tuple[float, ...] = _RETRY_DELAYS,
        sleep_fn=None,
        on_anchor=None,
        queue_retry_delays: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0),
        queue_max_attempts: int = 5,
        queue_poll_seconds: float = 2.0,
        queue_now_fn=None,
    ):
        if not operator_account_id or not topic_id:
            raise RuntimeError(
                "RealHCS requires operator_account_id and topic_id (fail closed)."
            )
        if network not in _NETWORKS:
            raise RuntimeError(
                f"RealHCS: unsupported network {network!r} (expected testnet/mainnet)."
            )
        self.data_dir = data_dir
        self.operator_account_id = str(operator_account_id)
        self.topic_id = str(topic_id)
        self.network = network
        self._key_env = key_env
        self._runner = runner or _default_runner
        self._retry_delays = tuple(retry_delays)
        self._sleep_fn = sleep_fn or time.sleep
        self._local = LocalHCS(data_dir, now_fn=now_fn)
        # M4: the anchor queue owns submission retry. ``on_anchor``
        # fires as on_anchor(message, receipt) when a job anchors or
        # goes dead — the exchange finalizes fail-closed settlements
        # on it. It may be assigned after construction too.
        self.on_anchor = on_anchor
        self._queue_poll_seconds = queue_poll_seconds
        self.anchor_queue = AnchorQueue(
            data_dir,
            self._submit_bytes,
            retry_delays=queue_retry_delays,
            max_attempts=queue_max_attempts,
            now_fn=queue_now_fn or now_fn,
            on_complete=self._anchor_completed,
        )

    # -- async anchor queue (M4) --------------------------------------
    def _anchor_completed(
        self, message: dict[str, Any], receipt: dict[str, Any], status: str
    ) -> None:
        """A queued job reached a terminal state. Record the outcome
        durably (receipts / dead-letter, same files as the old sync
        path), then notify the exchange via ``on_anchor`` so a
        fail-closed settlement can finalize. A callback failure is
        swallowed — the anchor is already recorded."""
        attempts = 0
        job = self.anchor_queue.job_for_message_hash(str(message.get("message_hash", "")))
        if job is not None:
            attempts = int(job.get("attempts", 0))
        self._record_outcome(message, receipt=receipt, attempts=attempts)
        if self.on_anchor is not None:
            try:
                self.on_anchor(message, receipt)
            except Exception:  # noqa: BLE001 — never lose an anchor
                pass

    def drain_anchors(self, max_jobs: int | None = None) -> list[dict[str, Any]]:
        """Synchronously drain every due anchor job (tests + manual
        re-anchor). The server normally runs the background worker
        instead — see ``start_anchor_worker``."""
        return self.anchor_queue.drain_all(max_jobs=max_jobs)

    def start_anchor_worker(self, poll_seconds: float | None = None) -> None:
        """Start the background anchor worker (daemon thread)."""
        self.anchor_queue.start(
            poll_seconds if poll_seconds is not None else self._queue_poll_seconds
        )

    def stop_anchor_worker(self) -> None:
        self.anchor_queue.stop()

    def anchor_stats(self) -> dict[str, int]:
        """Anchor-queue counts: queued / anchored / dead / total."""
        return self.anchor_queue.stats()

    # -- durable submission records -----------------------------------
    def _append_record(self, filename: str, record: dict[str, Any]) -> None:
        path = os.path.join(self.data_dir, filename)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def _read_records(self, filename: str) -> list[dict[str, Any]]:
        path = os.path.join(self.data_dir, filename)
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def _record_outcome(
        self, local: dict[str, Any], *, receipt: dict[str, Any], attempts: int
    ) -> None:
        record = {
            "sequence": local["sequence"],
            "message_hash": local["message_hash"],
            "status": str(receipt.get("status", "failed")),
            "topic_sequence": receipt.get("topic_sequence"),
            "consensus_timestamp": receipt.get("consensus_timestamp"),
            "transaction_id": receipt.get("transaction_id"),
            "error": receipt.get("error"),
            "attempts": attempts,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        if record["status"] == "failed":
            self._append_record(DEAD_LETTER_FILENAME, record)
        else:
            self._append_record(RECEIPTS_FILENAME, record)

    def receipt_for(self, sequence: int) -> dict[str, Any] | None:
        """The durable submission outcome for local ``sequence`` —
        from the receipts file, else the dead-letter file, else None
        (never submitted — local-only mode, or a pre-audit message).

        Fallback: a job the queue has already marked anchored/dead
        carries its receipt in the queue state, but the receipts file
        is only written by the ``on_complete`` callback, which runs
        AFTER the queue persists the terminal status — so a consumer
        polling ``anchor_stats()`` and then reading the receipt could
        briefly see None (flake 2026-11-14). The queue job's stored
        receipt closes that window: once stats say anchored, the
        receipt is visible, in the same record shape the file row
        carries. The file row wins when it exists (it is the durable
        record); the queue fallback is only the in-flight view."""
        for filename in (RECEIPTS_FILENAME, DEAD_LETTER_FILENAME):
            for record in self._read_records(filename):
                if record.get("sequence") == sequence:
                    return dict(record)
        job = self.anchor_queue.job_for_sequence(sequence)
        if job is not None and job.get("status") in ("anchored", "dead"):
            receipt = job.get("receipt")
            if isinstance(receipt, dict):
                return {
                    "sequence": sequence,
                    "message_hash": job.get("message_hash"),
                    "status": str(receipt.get("status", "failed")),
                    "topic_sequence": receipt.get("topic_sequence"),
                    "consensus_timestamp": receipt.get("consensus_timestamp"),
                    "transaction_id": receipt.get("transaction_id"),
                    "error": receipt.get("error"),
                    "attempts": int(job.get("attempts", 0)),
                    "recorded_at": job.get("updated_at"),
                }
        return None

    def hcs_stats(self) -> dict[str, Any]:
        return {
            "messages": len(self._local.messages()),
            "submitted": len(self._read_records(RECEIPTS_FILENAME)),
            "failed": len(self._read_records(DEAD_LETTER_FILENAME)),
        }

    def hcs_info(self) -> dict[str, Any]:
        return {"mode": "hedera", "topic": self.topic_id, "network": self.network}

    # -- submission ----------------------------------------------------
    @staticmethod
    def _should_retry(exc: Exception) -> bool:
        """Retry transient failures only. A timeout means the network
        (or the helper) is hung — waiting longer helps nothing. A
        missing key will never appear on retry."""
        msg = str(exc).lower()
        return "timed out" not in msg and "is not set" not in msg

    def _submit_with_retry(self, message_bytes: bytes) -> tuple[dict[str, Any], int]:
        """Submit with bounded retries. Returns (receipt, attempts).
        The receipt is the runner's dict on success, or
        ``{"status": "failed", "error": ...}`` after the retries
        exhaust — never raises for a submit failure (fail-retain)."""
        last_exc: Exception | None = None
        attempts = 0
        for attempt in range(1 + len(self._retry_delays)):
            attempts = attempt + 1
            try:
                receipt = self._submit_bytes(message_bytes)
                if not isinstance(receipt, dict):
                    raise RuntimeError(f"RealHCS: bad receipt: {receipt!r}")
                return dict(receipt), attempts
            except Exception as exc:  # noqa: BLE001 — the failure is data
                last_exc = exc
                if attempt >= len(self._retry_delays) or not self._should_retry(exc):
                    break
                self._sleep_fn(self._retry_delays[attempt])
        return {"status": "failed", "error": str(last_exc)}, attempts

    def _submit_bytes(self, message_bytes: bytes) -> dict[str, Any]:
        if not os.environ.get(self._key_env):
            raise RuntimeError(
                f"RealHCS: {self._key_env} is not set — refusing to submit "
                "without a signing key."
            )
        return self._runner(
            message_bytes,
            operator_account_id=self.operator_account_id,
            topic_id=self.topic_id,
            network=self.network,
        )

    def append(
        self,
        *,
        sender: str,
        action: str,
        payload: dict[str, Any],
        attestation: dict[str, Any] | None = None,
        provenance: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Write locally, then ENQUEUE the anchor job and return
        immediately — the exchange lock is never held by an HCS
        submit (M4). The returned message carries
        ``hedera == {"status": "queued", "job_id": ...}``; the
        receipt lands in ``hcs_receipts.jsonl`` when the worker
        drains the job (or the dead-letter file on final failure).
        Oversize messages still fail fast here (never submitted,
        dead-lettered at once) — the local record stands."""
        local = self._local.append(
            sender=sender,
            action=action,
            payload=payload,
            attestation=attestation,
            provenance=provenance,
            now=now,
        )
        message_bytes = canonical_bytes(local)
        if len(message_bytes) > MAX_MESSAGE_BYTES:
            # Fail fast: the SDK default (20 chunks x 1024 bytes)
            # cannot carry this message. The local record stands
            # (fail-retain); the failure is dead-lettered, not lost.
            receipt: dict[str, Any] = {
                "status": "failed",
                "error": (
                    f"message {len(message_bytes)} bytes exceeds HCS "
                    f"limit {MAX_MESSAGE_BYTES} bytes"
                ),
            }
            self._record_outcome(local, receipt=receipt, attempts=0)
            return {
                **local,
                "hedera": {
                    "topic": self.topic_id,
                    "network": self.network,
                    **receipt,
                },
            }
        job = self.anchor_queue.enqueue(local)
        return {
            **local,
            "hedera": {
                "topic": self.topic_id,
                "network": self.network,
                "status": "queued",
                "job_id": job["job_id"],
            },
        }

    def submit(self, message: dict[str, Any]) -> dict[str, Any]:
        receipt = self._submit_bytes(canonical_bytes(message))
        return {
            "message": dict(message),
            "hedera": {"topic": self.topic_id, "network": self.network, **receipt},
        }

    def messages(self) -> list[dict[str, Any]]:
        return self._local.messages()

    def what_did_agent_know(self, agent_id: str, t: datetime | str) -> dict[str, Any]:
        return self._local.what_did_agent_know(agent_id, t)

    def who_told_whom(
        self,
        agent_a: str,
        agent_b: str,
        t0: datetime | str | None = None,
        t1: datetime | str | None = None,
    ) -> list[dict[str, Any]]:
        return self._local.who_told_whom(agent_a, agent_b, t0, t1)

    def verify_log(self) -> dict[str, Any]:
        return self._local.verify_log()
