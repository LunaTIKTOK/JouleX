"""Testnet HBAR value rail (M4 chunk 4) — worthless test tokens only.

``TransferClient`` moves testnet HBAR from the operator account to one
recipient via ``joulex/hcs_transfer.mjs`` (``@hashgraph/sdk``), and
``record_transfer`` appends one durable line per settlement to
``data/settlement_transfers.jsonl``. ``update_receipt_transfer`` fills
the sibling chunk's ``transfer_tx_id`` field on
``data/settlement_receipts.jsonl`` once a transfer lands.

TESTNET ONLY — there is no mainnet code path anywhere in this module:
``TransferClient`` raises at construction and the Node helper exits
non-zero for any network other than ``"testnet"``. No key material is
stored or read here: the operator private key is read from
``JOULEX_HCS_PRIVATE_KEY`` by the Node helper at call time, from the
environment only — this module never reads that variable itself, it
only checks that it is set (mirroring ``hcs.py``, so the queue's
terminal-error rule treats a missing key as non-retryable).

NO LIVE KEY EXISTS in this environment (the owner's key was used
transiently and never stored), so no live transfer has ever run
through this code. Live run, owner only, key supplied transiently::

    JOULEX_HCS_OPERATOR=0.0.<operator> JOULEX_HCS_PRIVATE_KEY=<ecdsa-key> \\
        node joulex/hcs_transfer.mjs <to_account> <amount_tinybars> <settlement_id> testnet

then record it against the settlement::

    from joulex.transfer import record_transfer
    record_transfer(data_dir, settlement_id,
                    to_account=..., amount_tinybars=...,
                    memo=settlement_id)

Never fake a live receipt: a receipt is only real when the helper
prints it after a Hedera consensus receipt.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from typing import Any

TRANSFERS_FILENAME = "settlement_transfers.jsonl"
RECEIPTS_FILENAME = "settlement_receipts.jsonl"

_HCS_TRANSFER_SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "hcs_transfer.mjs"
)
_TRANSFER_TIMEOUT_SECONDS = 60
_KEY_ENV = "JOULEX_HCS_PRIVATE_KEY"
_OPERATOR_ENV = "JOULEX_HCS_OPERATOR"
_ACCOUNT_RE = re.compile(r"^0\.0\.\d+$")
_MAX_MEMO_CHARS = 100  # Hedera transaction-memo limit


def _default_runner(amount_tinybars, to_account, memo, *, operator_account_id, network):
    """Transfer via the Node helper (``@hashgraph/sdk``). The operator
    key is read from the environment by the helper at call time and
    reaches it through its environment only — never argv, never
    logged. Raises ``RuntimeError`` on non-zero exit, timeout, or
    unparseable output (same shape as ``hcs._default_runner``)."""
    env = dict(os.environ)
    env[_OPERATOR_ENV] = str(operator_account_id)
    try:
        proc = subprocess.run(
            [
                "node",
                _HCS_TRANSFER_SCRIPT,
                str(to_account),
                str(amount_tinybars),
                str(memo),
                str(network),
            ],
            capture_output=True,
            text=True,
            timeout=_TRANSFER_TIMEOUT_SECONDS,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Transfer: Hedera transfer timed out after "
            f"{_TRANSFER_TIMEOUT_SECONDS}s"
        ) from exc
    except OSError as exc:
        raise RuntimeError(
            f"Transfer: could not run the Hedera transfer helper: {exc}"
        ) from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "unknown error"
        raise RuntimeError(f"Transfer: Hedera transfer failed: {detail}")
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise RuntimeError(
            f"Transfer: unparseable transfer-helper output: {proc.stdout!r}"
        ) from exc


def _transfers_path(data_dir: str) -> str:
    return os.path.join(data_dir, TRANSFERS_FILENAME)


def read_transfers(data_dir: str) -> list[dict[str, Any]]:
    """All recorded transfers, in file order; [] when none recorded."""
    path = _transfers_path(data_dir)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def transfer_for_settlement(data_dir: str, settlement_id: str) -> dict[str, Any] | None:
    """The recorded transfer for ``settlement_id``, or None."""
    for record in read_transfers(data_dir):
        if record.get("settlement_id") == settlement_id:
            return dict(record)
    return None


class TransferClient:
    """Testnet-only HBAR transfer client.

    ``runner`` is ``_default_runner`` unless overridden (tests inject a
    fake with signature
    ``(amount_tinybars, to_account, memo, *, operator_account_id, network)``).
    """

    def __init__(self, data_dir, *, operator_account_id, network="testnet", runner=None):
        if network != "testnet":
            raise RuntimeError(
                f"TransferClient: unsupported network {network!r} "
                "(testnet only — no mainnet value rail)."
            )
        if not operator_account_id:
            raise RuntimeError(
                "TransferClient requires operator_account_id (fail closed)."
            )
        self.data_dir = data_dir
        self.operator_account_id = str(operator_account_id)
        self.network = network
        self._runner = runner or _default_runner

    def transfer(self, *, to_account, amount_tinybars, memo) -> dict[str, Any]:
        """Transfer ``amount_tinybars`` testnet HBAR to ``to_account``
        with ``memo`` (the settlement id) as the transaction memo.
        Returns the helper's receipt dict."""
        if not isinstance(to_account, str) or not _ACCOUNT_RE.match(to_account):
            raise ValueError(f"to_account must look like '0.0.N': {to_account!r}")
        if (
            isinstance(amount_tinybars, bool)
            or not isinstance(amount_tinybars, int)
            or amount_tinybars <= 0
        ):
            raise ValueError(
                f"amount_tinybars must be a positive int: {amount_tinybars!r}"
            )
        if (
            not isinstance(memo, str)
            or not memo
            or len(memo) > _MAX_MEMO_CHARS
        ):
            raise ValueError(
                f"memo must be a non-empty string of at most "
                f"{_MAX_MEMO_CHARS} chars: {memo!r}"
            )
        if not os.environ.get(_KEY_ENV):
            raise RuntimeError(
                f"Transfer: {_KEY_ENV} is not set — refusing to transfer "
                "without a signing key."
            )
        receipt = self._runner(
            amount_tinybars,
            to_account,
            memo,
            operator_account_id=self.operator_account_id,
            network=self.network,
        )
        if not isinstance(receipt, dict) or receipt.get("status") != "SUCCESS":
            raise RuntimeError(f"Transfer: bad receipt: {receipt!r}")
        if not receipt.get("transaction_id"):
            raise RuntimeError(f"Transfer: receipt has no transaction_id: {receipt!r}")
        return dict(receipt)


def record_transfer(
    data_dir,
    settlement_id,
    *,
    to_account,
    amount_tinybars,
    memo,
    client=None,
    runner=None,
    operator_account_id=None,
) -> dict[str, Any]:
    """Perform the transfer for ``settlement_id`` and append one JSON
    line to ``data/settlement_transfers.jsonl`` (fsync per line).
    Returns the appended record.

    Idempotent: if a record for ``settlement_id`` already exists it is
    returned WITHOUT transferring again. ``operator_account_id``
    defaults to ``JOULEX_HCS_OPERATOR`` from the environment when not
    given (and when no ``client`` is supplied)."""
    existing = transfer_for_settlement(data_dir, settlement_id)
    if existing is not None:
        return existing
    if client is None:
        operator = operator_account_id or os.environ.get(_OPERATOR_ENV)
        if not operator:
            raise RuntimeError(
                "record_transfer requires operator_account_id or "
                f"{_OPERATOR_ENV} in the environment (fail closed)."
            )
        client = TransferClient(
            data_dir,
            operator_account_id=operator,
            runner=runner,
        )
    receipt = client.transfer(
        to_account=to_account,
        amount_tinybars=amount_tinybars,
        memo=memo,
    )
    record: dict[str, Any] = {
        "settlement_id": settlement_id,
        "transfer_tx_id": receipt["transaction_id"],
        "consensus_timestamp": receipt.get("consensus_timestamp"),
        "amount_tinybars": amount_tinybars,
        "from_account": client.operator_account_id,
        "to_account": to_account,
        "network": "testnet",
        "memo": memo,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    os.makedirs(data_dir, exist_ok=True)
    with open(_transfers_path(data_dir), "a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return dict(record)


def update_receipt_transfer(data_dir, settlement_id, transfer_tx_id) -> bool:
    """Set ``transfer_tx_id`` on the matching line of
    ``data/settlement_receipts.jsonl`` (the sibling chunk's file).
    Rewrites the file atomically via temp + ``os.replace``. Returns
    True when a line was updated, False when the file does not exist
    or no receipt for ``settlement_id`` exists yet."""
    path = os.path.join(data_dir, RECEIPTS_FILENAME)
    if not os.path.exists(path):
        return False
    with open(path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    updated = False
    out: list[str] = []
    for line in lines:
        if not line.strip():
            out.append(line)
            continue
        record = json.loads(line)
        if record.get("settlement_id") == settlement_id:
            record["transfer_tx_id"] = transfer_tx_id
            updated = True
        out.append(json.dumps(record, sort_keys=True))
    if not updated:
        return False
    fd, tmp_path = tempfile.mkstemp(
        dir=data_dir, prefix=".settlement_receipts.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return True
