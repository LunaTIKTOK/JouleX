"""M5 — Reconciliation + audit trail.

The reconcile command re-derives every balance, position, pool total
and market share-count from the append-only logs (trades.jsonl +
settlements.jsonl) and fails loudly on any mismatch with the stored
ledger.json / markets.json. These tests build real state through the
Exchange, then tamper with one file at a time and require the
reconciler to catch it.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from datetime import timedelta
from pathlib import Path

import pytest

from joulex.reconcile import ReconciliationError, reconcile
from tests.conftest import T0, iso, make_market_payload, make_order_payload
from tests.test_ledger import open_market, place

REPO_ROOT = str(Path(__file__).resolve().parent.parent)


def _data_dir(exchange):
    return exchange.store.data_dir


def _read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True) + "\n")


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True)


def _trade_and_settle(exchange, agent_a, agent_b, clock):
    """Alice buys 10 YES, Bob buys 20 NO, market settles YES.
    Returns (market, trades, settlements)."""
    market = open_market(exchange, agent_a, clock)
    place(exchange, agent_a, make_order_payload(agent_a["agent_id"], market["market_id"], "YES", 10), clock)
    place(exchange, agent_b, make_order_payload(agent_b["agent_id"], market["market_id"], "NO", 20), clock)
    clock.advance(days=1, minutes=1)
    receipt = exchange.settle_market(market["market_id"], observed_value=101500)
    assert receipt["outcome"] == "YES"
    return market, receipt


# -- clean state -------------------------------------------------------

def test_clean_state_reconciles(exchange, agent_a, agent_b, clock):
    market, _ = _trade_and_settle(exchange, agent_a, agent_b, clock)
    report = reconcile(_data_dir(exchange))
    assert report["ok"] is True
    assert report["mismatches"] == []
    assert report["counts"]["trades"] == 2
    assert report["counts"]["settlements"] == 1
    assert report["counts"]["markets"] == 1
    assert report["counts"]["agents"] == 2
    assert report["conservation"]["ok"] is True


def test_empty_state_reconciles(exchange):
    report = reconcile(_data_dir(exchange))
    assert report["ok"] is True
    assert report["mismatches"] == []
    assert report["counts"] == {"trades": 0, "settlements": 0, "markets": 0, "agents": 0}


def test_unsettled_trades_reconcile(exchange, agent_a, agent_b, clock):
    market = open_market(exchange, agent_a, clock)
    place(exchange, agent_a, make_order_payload(agent_a["agent_id"], market["market_id"], "YES", 10), clock)
    place(exchange, agent_b, make_order_payload(agent_b["agent_id"], market["market_id"], "NO", 5), clock)
    # Sell some back: refund path must replay too.
    place(exchange, agent_a, make_order_payload(agent_a["agent_id"], market["market_id"], "YES", 4, action="sell"), clock)
    report = reconcile(_data_dir(exchange))
    assert report["ok"] is True
    assert report["mismatches"] == []
    assert report["counts"]["trades"] == 3


def test_exchange_reconcile_method(exchange, agent_a, agent_b, clock):
    _trade_and_settle(exchange, agent_a, agent_b, clock)
    assert exchange.reconcile()["ok"] is True


# -- tampered ledger ---------------------------------------------------

def test_tampered_balance_detected(exchange, agent_a, agent_b, clock):
    _trade_and_settle(exchange, agent_a, agent_b, clock)
    path = f"{_data_dir(exchange)}/ledger.json"
    ledger = _read_json(path)
    ledger["balances"][agent_a["agent_id"]] += 1.0
    _write_json(path, ledger)
    report = reconcile(_data_dir(exchange))
    assert report["ok"] is False
    assert any("balance" in m for m in report["mismatches"])


def test_tampered_pool_detected(exchange, agent_a, agent_b, clock):
    _trade_and_settle(exchange, agent_a, agent_b, clock)
    path = f"{_data_dir(exchange)}/ledger.json"
    ledger = _read_json(path)
    ledger["pool"] += 0.5
    _write_json(path, ledger)
    report = reconcile(_data_dir(exchange))
    assert report["ok"] is False
    assert any("pool" in m for m in report["mismatches"])


def test_tampered_position_detected(exchange, agent_a, agent_b, clock):
    market = open_market(exchange, agent_a, clock)
    place(exchange, agent_a, make_order_payload(agent_a["agent_id"], market["market_id"], "YES", 10), clock)
    path = f"{_data_dir(exchange)}/ledger.json"
    ledger = _read_json(path)
    key = f"{agent_a['agent_id']}|{market['market_id']}|YES"
    ledger["positions"][key] = 9.0
    _write_json(path, ledger)
    report = reconcile(_data_dir(exchange))
    assert report["ok"] is False
    assert any("position" in m for m in report["mismatches"])


def test_tampered_market_q_detected(exchange, agent_a, agent_b, clock):
    market, _ = _trade_and_settle(exchange, agent_a, agent_b, clock)
    path = f"{_data_dir(exchange)}/markets.json"
    markets = _read_json(path)
    markets[market["market_id"]]["q_yes"] += 1.0
    _write_json(path, markets)
    report = reconcile(_data_dir(exchange))
    assert report["ok"] is False
    assert any("q_yes" in m for m in report["mismatches"])


# -- tampered logs -----------------------------------------------------

def test_tampered_trade_cost_detected(exchange, agent_a, agent_b, clock):
    _trade_and_settle(exchange, agent_a, agent_b, clock)
    path = f"{_data_dir(exchange)}/trades.jsonl"
    trades = _read_jsonl(path)
    trades[0]["cost_points"] += 0.25
    _write_jsonl(path, trades)
    report = reconcile(_data_dir(exchange))
    assert report["ok"] is False
    assert any("cost" in m for m in report["mismatches"])


def test_duplicate_trade_id_detected(exchange, agent_a, agent_b, clock):
    _trade_and_settle(exchange, agent_a, agent_b, clock)
    path = f"{_data_dir(exchange)}/trades.jsonl"
    trades = _read_jsonl(path)
    trades.append(dict(trades[0]))
    _write_jsonl(path, trades)
    report = reconcile(_data_dir(exchange))
    assert report["ok"] is False
    assert any("trade_id" in m for m in report["mismatches"])


def test_tampered_settlement_payout_detected(exchange, agent_a, agent_b, clock):
    _trade_and_settle(exchange, agent_a, agent_b, clock)
    path = f"{_data_dir(exchange)}/settlements.jsonl"
    receipts = _read_jsonl(path)
    receipts[0]["payouts"][0]["payout_points"] += 5.0
    _write_jsonl(path, receipts)
    report = reconcile(_data_dir(exchange))
    assert report["ok"] is False
    assert any("payout" in m for m in report["mismatches"])


def test_wrong_settlement_outcome_detected(exchange, agent_a, agent_b, clock):
    # Outcome on the receipt contradicts the observed close vs strike.
    _trade_and_settle(exchange, agent_a, agent_b, clock)
    path = f"{_data_dir(exchange)}/settlements.jsonl"
    receipts = _read_jsonl(path)
    receipts[0]["outcome"] = "NO"
    _write_jsonl(path, receipts)
    report = reconcile(_data_dir(exchange))
    assert report["ok"] is False
    assert any("outcome" in m for m in report["mismatches"])


def test_double_settlement_receipt_detected(exchange, agent_a, agent_b, clock):
    _trade_and_settle(exchange, agent_a, agent_b, clock)
    path = f"{_data_dir(exchange)}/settlements.jsonl"
    receipts = _read_jsonl(path)
    receipts.append(dict(receipts[0]))
    _write_jsonl(path, receipts)
    report = reconcile(_data_dir(exchange))
    assert report["ok"] is False
    assert any("settlement" in m.lower() for m in report["mismatches"])


def test_missing_settlement_receipt_detected(exchange, agent_a, agent_b, clock):
    _trade_and_settle(exchange, agent_a, agent_b, clock)
    import os

    os.remove(f"{_data_dir(exchange)}/settlements.jsonl")
    report = reconcile(_data_dir(exchange))
    assert report["ok"] is False
    assert any("settlement" in m.lower() for m in report["mismatches"])


def test_trade_after_settlement_detected(exchange, agent_a, agent_b, clock):
    market, _ = _trade_and_settle(exchange, agent_a, agent_b, clock)
    path = f"{_data_dir(exchange)}/trades.jsonl"
    trades = _read_jsonl(path)
    late = dict(trades[0])
    late["trade_id"] = "trd_late00000001"
    late["timestamp"] = iso(clock() + timedelta(hours=1))
    trades.append(late)
    _write_jsonl(path, trades)
    report = reconcile(_data_dir(exchange))
    assert report["ok"] is False
    assert any("after settlement" in m for m in report["mismatches"])


def test_trade_for_resolved_market_without_settled_at(exchange, agent_a, agent_b, clock):
    # Review catch: a RESOLVED market with settled_at wiped must not
    # let a late trade replay silently.
    market, _ = _trade_and_settle(exchange, agent_a, agent_b, clock)
    markets_path = f"{_data_dir(exchange)}/markets.json"
    markets = _read_json(markets_path)
    markets[market["market_id"]]["settled_at"] = None
    _write_json(markets_path, markets)
    path = f"{_data_dir(exchange)}/trades.jsonl"
    trades = _read_jsonl(path)
    late = dict(trades[0])
    late["trade_id"] = "trd_late00000002"
    late["timestamp"] = iso(clock() + timedelta(hours=1))
    trades.append(late)
    _write_jsonl(path, trades)
    report = reconcile(_data_dir(exchange))
    assert report["ok"] is False
    assert any("no settled_at" in m for m in report["mismatches"])


def test_negative_stored_balance_detected(exchange, agent_a, agent_b, clock):
    _trade_and_settle(exchange, agent_a, agent_b, clock)
    path = f"{_data_dir(exchange)}/ledger.json"
    ledger = _read_json(path)
    ledger["balances"][agent_a["agent_id"]] = -5.0
    _write_json(path, ledger)
    report = reconcile(_data_dir(exchange))
    assert report["ok"] is False
    assert any("negative balance" in m for m in report["mismatches"])


def test_negative_replayed_balance_detected(exchange, agent_a, clock):
    # A hand-crafted log line whose LMSR cost is internally consistent
    # but spends more than the 1,000-point endowment: the replay must
    # flag the negative balance, not bless the arithmetic.
    from joulex.lmsr import trade_cost

    market = open_market(exchange, agent_a, clock)
    cost = trade_cost(0.0, 0.0, market["b"], "YES", 5000.0)
    assert cost > 1000.0
    forged = {
        "trade_id": "trd_forged000001",
        "timestamp": iso(clock()),
        "market_id": market["market_id"],
        "agent_id": agent_a["agent_id"],
        "side": "YES",
        "action": "buy",
        "shares": 5000.0,
        "cost_points": cost,
        "price_yes_after": 1.0,
        "balance_after": round(1000.0 - cost, 6),
    }
    with open(f"{_data_dir(exchange)}/trades.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(forged, sort_keys=True) + "\n")
    report = reconcile(_data_dir(exchange))
    assert report["ok"] is False
    assert any("negative balance" in m for m in report["mismatches"])


def test_corrupt_trades_file_raises(exchange, agent_a, clock):
    market = open_market(exchange, agent_a, clock)
    place(exchange, agent_a, make_order_payload(agent_a["agent_id"], market["market_id"], "YES", 10), clock)
    with open(f"{_data_dir(exchange)}/trades.jsonl", "a", encoding="utf-8") as f:
        f.write("{not json\n")
    with pytest.raises(ReconciliationError):
        reconcile(_data_dir(exchange))


def test_corrupt_ledger_file_raises(exchange):
    with open(f"{_data_dir(exchange)}/ledger.json", "w", encoding="utf-8") as f:
        f.write("{not json")
    with pytest.raises(ReconciliationError):
        reconcile(_data_dir(exchange))


# -- CLI ---------------------------------------------------------------

def test_cli_exits_zero_on_clean_state(exchange, agent_a, agent_b, clock):
    _trade_and_settle(exchange, agent_a, agent_b, clock)
    proc = subprocess.run(
        [sys.executable, "-m", "joulex.reconcile", "--data-dir", _data_dir(exchange)],
        cwd=REPO_ROOT,
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["ok"] is True


def test_cli_exits_nonzero_on_mismatch(exchange, agent_a, agent_b, clock):
    _trade_and_settle(exchange, agent_a, agent_b, clock)
    path = f"{_data_dir(exchange)}/ledger.json"
    ledger = _read_json(path)
    ledger["pool"] += 1.0
    _write_json(path, ledger)
    proc = subprocess.run(
        [sys.executable, "-m", "joulex.reconcile", "--data-dir", _data_dir(exchange)],
        cwd=REPO_ROOT,
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 1
    assert json.loads(proc.stdout)["ok"] is False


# -- HTTP endpoint -----------------------------------------------------

def test_get_reconcile_endpoint(tmp_path, issuer, clock, agent_a, agent_b):
    from joulex.exchange import Exchange
    from joulex.server import make_server

    ex = Exchange(data_dir=str(tmp_path / "data"), now_fn=clock, trusted_issuers=issuer.trust_store)
    ex.verifier.refresh_revocation_list(issuer.revocation_list())
    _trade_and_settle(ex, agent_a, agent_b, clock)
    server = make_server(ex, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        import urllib.request

        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/reconcile", timeout=10) as resp:
            assert resp.status == 200
            body = json.loads(resp.read())
        assert body["ok"] is True
        assert body["counts"]["trades"] == 2
    finally:
        server.shutdown()
        server.server_close()
