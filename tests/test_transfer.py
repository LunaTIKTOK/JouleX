"""Tests for the testnet HBAR value rail (M4 chunk 4).

``joulex/transfer.py`` + ``joulex/hcs_transfer.mjs`` move worthless
testnet HBAR only. No live key is ever used here: every test runs
against a fake runner, except the two ``node`` checks, which prove
the script parses and fails closed on a bad network *without* a key.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from joulex.transfer import (  # noqa: E402
    TRANSFERS_FILENAME,
    TransferClient,
    _default_runner,
    read_transfers,
    record_transfer,
    transfer_for_settlement,
    update_receipt_transfer,
)

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "joulex" / "hcs_transfer.mjs"

OPERATOR = "0.0.123"
RECIPIENT = "0.0.456"
DUMMY_KEY = "0x" + "ab" * 32


class FakeRunner:
    """Stand-in for the Node helper: records calls, returns a canned receipt."""

    def __init__(self, receipt=None):
        self.calls = []
        self.receipt = receipt or {
            "status": "SUCCESS",
            "transaction_id": "0.0.123@1794590000.000000000",
            "consensus_timestamp": "1794590000.000000000",
            "amount_tinybars": 1000,
            "to_account": RECIPIENT,
        }

    def __call__(self, amount_tinybars, to_account, memo, *, operator_account_id, network):
        self.calls.append(
            {
                "amount_tinybars": amount_tinybars,
                "to_account": to_account,
                "memo": memo,
                "operator_account_id": operator_account_id,
                "network": network,
            }
        )
        return dict(self.receipt)


class FailingRunner:
    """Mimics _default_runner's non-zero-exit shape: raises RuntimeError."""

    def __init__(self):
        self.calls = 0

    def __call__(self, amount_tinybars, to_account, memo, *, operator_account_id, network):
        self.calls += 1
        raise RuntimeError(
            'transfer helper failed: {"error": "INSUFFICIENT_ACCOUNT_BALANCE"}'
        )


@pytest.fixture()
def key_env(monkeypatch):
    monkeypatch.setenv("JOULEX_HCS_PRIVATE_KEY", DUMMY_KEY)
    monkeypatch.setenv("JOULEX_HCS_OPERATOR", OPERATOR)
    return monkeypatch


def make_client(tmp_path, runner=None, network="testnet"):
    return TransferClient(
        str(tmp_path),
        operator_account_id=OPERATOR,
        network=network,
        runner=runner or FakeRunner(),
    )


# -- (a) network gate ------------------------------------------------------

def test_rejects_non_testnet_network_at_construction(tmp_path):
    with pytest.raises(RuntimeError, match="testnet"):
        TransferClient(str(tmp_path), operator_account_id=OPERATOR, network="mainnet")
    with pytest.raises(RuntimeError, match="testnet"):
        TransferClient(str(tmp_path), operator_account_id=OPERATOR, network="previewnet")


# -- (b) argument validation ----------------------------------------------

@pytest.mark.parametrize("bad", ["", "0.0", "abc", "0.0.x", "1.2.3", "0.0.-1", " 0.0.5", "0.0.5 ", None, 123])
def test_rejects_bad_to_account(tmp_path, key_env, bad):
    client = make_client(tmp_path)
    with pytest.raises(ValueError, match="to_account"):
        client.transfer(to_account=bad, amount_tinybars=1000, memo="stl_1")


@pytest.mark.parametrize("bad", [0, -1, -100, 1.5, "100", True, False, None])
def test_rejects_bad_amount(tmp_path, key_env, bad):
    client = make_client(tmp_path)
    with pytest.raises(ValueError, match="amount_tinybars"):
        client.transfer(to_account=RECIPIENT, amount_tinybars=bad, memo="stl_1")


@pytest.mark.parametrize("bad", ["", "x" * 101, None, 123])
def test_rejects_bad_memo(tmp_path, key_env, bad):
    client = make_client(tmp_path)
    with pytest.raises(ValueError, match="memo"):
        client.transfer(to_account=RECIPIENT, amount_tinybars=1000, memo=bad)


def test_memo_at_limit_ok(tmp_path, key_env):
    runner = FakeRunner()
    client = make_client(tmp_path, runner=runner)
    receipt = client.transfer(to_account=RECIPIENT, amount_tinybars=1000, memo="x" * 100)
    assert receipt["status"] == "SUCCESS"
    assert len(runner.calls) == 1


# -- (c) missing key -------------------------------------------------------

def test_missing_key_raises_at_call_time(tmp_path, monkeypatch):
    monkeypatch.delenv("JOULEX_HCS_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("JOULEX_HCS_OPERATOR", OPERATOR)
    runner = FakeRunner()
    client = make_client(tmp_path, runner=runner)
    with pytest.raises(RuntimeError, match="is not set"):
        client.transfer(to_account=RECIPIENT, amount_tinybars=1000, memo="stl_1")
    assert runner.calls == []  # key check precedes any submit


# -- (d) fake-runner success + sidecar record ------------------------------

def test_fake_runner_success_receipt_passes_through(tmp_path, key_env):
    runner = FakeRunner()
    client = make_client(tmp_path, runner=runner)
    receipt = client.transfer(to_account=RECIPIENT, amount_tinybars=1000, memo="stl_abc")
    assert receipt["status"] == "SUCCESS"
    assert receipt["transaction_id"] == "0.0.123@1794590000.000000000"
    assert receipt["consensus_timestamp"] == "1794590000.000000000"
    assert receipt["amount_tinybars"] == 1000
    assert receipt["to_account"] == RECIPIENT
    assert runner.calls == [
        {
            "amount_tinybars": 1000,
            "to_account": RECIPIENT,
            "memo": "stl_abc",
            "operator_account_id": OPERATOR,
            "network": "testnet",
        }
    ]


def test_record_transfer_appends_sidecar_line(tmp_path, key_env):
    runner = FakeRunner()
    record = record_transfer(
        str(tmp_path),
        "stl_abc",
        to_account=RECIPIENT,
        amount_tinybars=1000,
        memo="stl_abc",
        runner=runner,
        operator_account_id=OPERATOR,
    )
    assert record["settlement_id"] == "stl_abc"
    assert record["transfer_tx_id"] == "0.0.123@1794590000.000000000"
    assert record["consensus_timestamp"] == "1794590000.000000000"
    assert record["amount_tinybars"] == 1000
    assert record["from_account"] == OPERATOR
    assert record["to_account"] == RECIPIENT
    assert record["network"] == "testnet"
    assert record["memo"] == "stl_abc"
    assert record["recorded_at"]

    path = tmp_path / TRANSFERS_FILENAME
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == record


def test_record_transfer_uses_env_operator_when_none_given(tmp_path, key_env):
    runner = FakeRunner()
    record = record_transfer(
        str(tmp_path),
        "stl_env",
        to_account=RECIPIENT,
        amount_tinybars=500,
        memo="stl_env",
        runner=runner,
    )
    assert record["from_account"] == OPERATOR
    assert runner.calls[0]["operator_account_id"] == OPERATOR


# -- (e) idempotency -------------------------------------------------------

def test_record_transfer_idempotent(tmp_path, key_env):
    runner = FakeRunner()
    first = record_transfer(
        str(tmp_path),
        "stl_dup",
        to_account=RECIPIENT,
        amount_tinybars=1000,
        memo="stl_dup",
        runner=runner,
        operator_account_id=OPERATOR,
    )
    second = record_transfer(
        str(tmp_path),
        "stl_dup",
        to_account=RECIPIENT,
        amount_tinybars=1000,
        memo="stl_dup",
        runner=runner,
        operator_account_id=OPERATOR,
    )
    assert second == first
    assert len(runner.calls) == 1  # no second transfer
    assert len(read_transfers(str(tmp_path))) == 1


# -- (f) lookup + receipts-sidecar update ----------------------------------

def test_transfer_for_settlement_lookup(tmp_path, key_env):
    assert transfer_for_settlement(str(tmp_path), "stl_missing") is None
    record_transfer(
        str(tmp_path),
        "stl_look",
        to_account=RECIPIENT,
        amount_tinybars=1000,
        memo="stl_look",
        runner=FakeRunner(),
        operator_account_id=OPERATOR,
    )
    found = transfer_for_settlement(str(tmp_path), "stl_look")
    assert found is not None
    assert found["settlement_id"] == "stl_look"
    assert found["transfer_tx_id"] == "0.0.123@1794590000.000000000"


def test_update_receipt_transfer_rewrites_line(tmp_path):
    receipts = tmp_path / "settlement_receipts.jsonl"
    receipts.write_text(
        "\n".join(
            [
                json.dumps({"settlement_id": "stl_a", "transfer_tx_id": None, "status": "settled"}),
                json.dumps({"settlement_id": "stl_b", "transfer_tx_id": None, "status": "settled"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    assert update_receipt_transfer(str(tmp_path), "stl_b", "0.0.123@1794590000.000000000") is True
    lines = [json.loads(line) for line in receipts.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["transfer_tx_id"] is None  # untouched
    assert lines[1]["transfer_tx_id"] == "0.0.123@1794590000.000000000"
    assert lines[1]["settlement_id"] == "stl_b"


def test_update_receipt_transfer_returns_false_when_absent(tmp_path):
    # No receipts file at all.
    assert update_receipt_transfer(str(tmp_path), "stl_nope", "0.0.123@1.1") is False
    # File exists but no matching settlement.
    (tmp_path / "settlement_receipts.jsonl").write_text(
        json.dumps({"settlement_id": "stl_other", "transfer_tx_id": None}) + "\n",
        encoding="utf-8",
    )
    assert update_receipt_transfer(str(tmp_path), "stl_nope", "0.0.123@1.1") is False


# -- (g) runner failure ----------------------------------------------------

def test_runner_failure_raises_and_writes_nothing(tmp_path, key_env):
    runner = FailingRunner()
    with pytest.raises(RuntimeError):
        record_transfer(
            str(tmp_path),
            "stl_fail",
            to_account=RECIPIENT,
            amount_tinybars=1000,
            memo="stl_fail",
            runner=runner,
            operator_account_id=OPERATOR,
        )
    assert runner.calls == 1
    assert not (tmp_path / TRANSFERS_FILENAME).exists()
    assert transfer_for_settlement(str(tmp_path), "stl_fail") is None


def test_non_success_receipt_raises(tmp_path, key_env):
    bad = FakeRunner(
        receipt={"status": "failed", "transaction_id": None, "error": "boom"}
    )
    client = make_client(tmp_path, runner=bad)
    with pytest.raises(RuntimeError):
        client.transfer(to_account=RECIPIENT, amount_tinybars=1000, memo="stl_1")


# -- real script: syntax + fail-closed network check (no key needed) ------

@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_hcs_transfer_script_parses():
    proc = subprocess.run(
        ["node", "--check", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_hcs_transfer_bad_network_fails_without_key(monkeypatch):
    env = {k: v for k, v in os.environ.items() if not k.startswith("JOULEX_HCS_")}
    proc = subprocess.run(
        ["node", str(SCRIPT), RECIPIENT, "1000", "stl_probe", "mainnet"],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert proc.returncode != 0
    err = json.loads(proc.stderr.strip().splitlines()[-1])
    assert "testnet" in err["error"]  # network check fires before any key check
    assert proc.stdout == ""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_default_runner_bad_network_raises_runtime_error(monkeypatch):
    # Exercises the real runner against the real script: a non-testnet
    # network exits non-zero, which the runner turns into RuntimeError.
    monkeypatch.setenv("JOULEX_HCS_PRIVATE_KEY", DUMMY_KEY)
    with pytest.raises(RuntimeError):
        _default_runner(1000, RECIPIENT, "stl_probe", operator_account_id=OPERATOR, network="mainnet")
