"""M4 chunk 2 — fail-closed settlement + settlement receipts +
decision-basis capture.

Written FIRST (test-first): these tests fail until
``joulex/decision_basis.py``, ``joulex/settlement_receipts.py`` and
the ``Exchange.settle_market`` rework exist. No real Hedera key
anywhere: the fake runner stands in for the Node submit helper
(same pattern as tests/test_hcs_commercial.py).
"""

from __future__ import annotations

import hashlib
import json

import pytest

from joulex.attestation import canonical_bytes
from joulex.exchange import Exchange
from joulex.hcs import RealHCS
from tests.conftest import make_market_payload, make_order_payload
from tests.test_joulex_markets import _trade_and_advance

RECEIPT_FIELDS = {
    "settlement_id", "market_id", "outcome", "observed_value", "evidence",
    "settled_at", "ledger_entry_hash", "hcs_topic", "topic_sequence",
    "consensus_timestamp", "tx_id", "attestation", "attestation_status",
    "attestation_binding", "inputs_hash", "decision_basis",
    "anchor_job_id", "anchor_status", "transfer_tx_id",
}

FAKE_RECEIPT = {
    "status": "SUCCESS",
    "topic_sequence": "42",
    "consensus_timestamp": "2026-11-10T12:00:00.000000000Z",
    "transaction_id": "0.0.1@1794333600.000000000",
}


def _fake_runner(receipt=None, failures=0):
    """Fails ``failures`` times (RuntimeError), then returns receipt."""
    calls = {"n": 0}

    def runner(message_bytes, *, operator_account_id, topic_id, network):
        calls["n"] += 1
        if calls["n"] <= failures:
            raise RuntimeError("network down")
        return receipt or dict(FAKE_RECEIPT)

    runner.calls = calls
    return runner


def _read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def _real_exchange(tmp_path, issuer, clock, runner, monkeypatch, **ex_kwargs):
    """Exchange whose HCS client is a RealHCS driven by ``runner``."""
    monkeypatch.setenv("JOULEX_HCS_PRIVATE_KEY", "0x" + "ab" * 32)
    max_attempts = ex_kwargs.pop("queue_max_attempts", 5)

    def factory(data_dir, **kw):
        return RealHCS(
            data_dir,
            operator_account_id="0.0.11389089",
            topic_id="0.0.11399235",
            network="testnet",
            runner=runner,
            now_fn=kw.get("now_fn"),
            on_anchor=kw.get("on_anchor"),
            queue_retry_delays=(0, 0, 0, 0),
            queue_max_attempts=max_attempts,
            retry_delays=(0, 0),
            sleep_fn=lambda s: None,
        )

    monkeypatch.setattr("joulex.exchange.make_hcs", factory)
    ex = Exchange(
        data_dir=str(tmp_path / "data"),
        now_fn=clock,
        trusted_issuers=issuer.trust_store,
        **ex_kwargs,
    )
    ok, reason = ex.verifier.refresh_revocation_list(issuer.revocation_list())
    assert ok, reason
    return ex


def _expected_market_state_hash(market_before):
    return hashlib.sha256(
        canonical_bytes(
            {
                "market_id": market_before["market_id"],
                "outcomes": market_before["outcomes"],
                "q": market_before["q"],
                "prices": market_before["prices"],
                "status": market_before["status"],
            }
        )
    ).hexdigest()


def _check_sidecar_invariants(ex, sidecar, ledger_lines, market_before):
    """Shape + recomputable hashes, shared by the inline/anchored paths."""
    assert set(sidecar.keys()) == RECEIPT_FIELDS
    assert sidecar["transfer_tx_id"] is None
    assert sidecar["settled_at"] == ex.get_market(sidecar["market_id"])["settled_at"]
    # ledger_entry_hash recomputes from the settlements.jsonl record.
    assert len(ledger_lines) == 1
    assert sidecar["ledger_entry_hash"] == hashlib.sha256(
        canonical_bytes(ledger_lines[0])
    ).hexdigest()
    assert ledger_lines[0]["settlement_id"] == sidecar["settlement_id"]
    # inputs_hash recomputes from the stored decision basis.
    basis = sidecar["decision_basis"]
    assert sidecar["inputs_hash"] == basis["inputs_hash"]
    rest = {k: v for k, v in basis.items() if k != "inputs_hash"}
    assert basis["inputs_hash"] == hashlib.sha256(
        canonical_bytes(rest)
    ).hexdigest()
    assert basis["market_state_hash"] == _expected_market_state_hash(market_before)
    return basis


# -- (a) local mode: inline finalize + complete receipt sidecar --------


def test_local_settle_finalizes_inline_with_complete_receipt(
    exchange, agent_a, agent_b, clock, tmp_path
):
    mid = _trade_and_advance(exchange, agent_a, agent_b, clock)
    market_before = exchange.get_market(mid)
    alice_before = exchange.ledger.balance(agent_a["agent_id"])
    bob_before = exchange.ledger.balance(agent_b["agent_id"])

    receipt = exchange.settle_market(mid, observed_value=55.0)

    assert receipt["status"] == "SETTLED"
    assert receipt["settlement_id"].startswith("stl_")
    assert receipt["outcome"] == "YES"
    assert exchange.get_market(mid)["status"] == "RESOLVED"
    # Payouts applied inline: Alice held 10 YES, Bob 20 NO.
    assert exchange.ledger.balance(agent_a["agent_id"]) == pytest.approx(alice_before + 10.0)
    assert exchange.ledger.balance(agent_b["agent_id"]) == pytest.approx(bob_before)
    assert exchange.pending_settlements() == []

    data_dir = tmp_path / "data"
    ledger_lines = _read_jsonl(data_dir / "settlements.jsonl")
    sidecars = _read_jsonl(data_dir / "settlement_receipts.jsonl")
    assert len(sidecars) == 1
    sidecar = sidecars[0]
    basis = _check_sidecar_invariants(exchange, sidecar, ledger_lines, market_before)
    assert sidecar["anchor_status"] == "local"
    assert sidecar["anchor_job_id"] is None
    assert sidecar["hcs_topic"] is None
    assert sidecar["tx_id"] is None
    settle_msg = [
        m for m in exchange.hcs.messages()
        if m["action"] == "settle_market"
    ][0]
    assert sidecar["topic_sequence"] == str(settle_msg["sequence"])
    assert sidecar["consensus_timestamp"] == settle_msg["consensus_timestamp"]
    # Honest decision basis: operator-supplied value, no oracle.
    assert basis["oracle"]["label"] == "OPERATOR_VALUE"
    assert basis["oracle"]["snapshot"]["max_price"] == pytest.approx(55.0)
    assert basis["credential_id"] == "operator"
    # Lookup works, and unknown ids return None.
    assert exchange.settlement_receipt(receipt["settlement_id"]) == sidecar
    assert exchange.settlement_receipt("stl_doesnotexist") is None


# -- (b) RealHCS + fail_closed: pending, then drain finalizes ----------


def test_real_hcs_fail_closed_pending_then_drain_finalizes(
    tmp_path, issuer, clock, agent_a, agent_b, monkeypatch
):
    ex = _real_exchange(
        tmp_path, issuer, clock, _fake_runner(), monkeypatch
    )
    mid = _trade_and_advance(ex, agent_a, agent_b, clock)
    market_before = ex.get_market(mid)
    alice_before = ex.ledger.balance(agent_a["agent_id"])
    bob_before = ex.ledger.balance(agent_b["agent_id"])

    result = ex.settle_market(mid, observed_value=55.0)

    assert result["status"] == "PENDING_ANCHOR"
    assert result["settlement_id"].startswith("stl_")
    assert result["anchor_job_id"].startswith("anch_")
    assert "payouts" not in result
    assert ex.get_market(mid)["status"] == "SETTLEMENT_PENDING"
    # Nothing paid out yet; no settlements.jsonl line yet.
    assert ex.ledger.balance(agent_a["agent_id"]) == alice_before
    assert ex.ledger.balance(agent_b["agent_id"]) == bob_before
    assert _read_jsonl(tmp_path / "data" / "settlements.jsonl") == []
    assert _read_jsonl(tmp_path / "data" / "settlement_receipts.jsonl") == []
    assert len(ex.pending_settlements()) == 1
    assert ex.pending_settlements()[0]["status"] == "PENDING_ANCHOR"

    done = ex.hcs.drain_anchors()
    assert done[-1]["status"] == "anchored"

    assert ex.get_market(mid)["status"] == "RESOLVED"
    assert ex.ledger.balance(agent_a["agent_id"]) == pytest.approx(alice_before + 10.0)
    assert ex.ledger.balance(agent_b["agent_id"]) == pytest.approx(bob_before)
    assert ex.pending_settlements() == []
    ledger_lines = _read_jsonl(tmp_path / "data" / "settlements.jsonl")
    sidecars = _read_jsonl(tmp_path / "data" / "settlement_receipts.jsonl")
    assert len(sidecars) == 1
    sidecar = sidecars[0]
    basis = _check_sidecar_invariants(ex, sidecar, ledger_lines, market_before)
    assert sidecar["settlement_id"] == result["settlement_id"]
    assert sidecar["anchor_status"] == "anchored"
    assert sidecar["anchor_job_id"] == result["anchor_job_id"]
    assert sidecar["hcs_topic"] == "0.0.11399235"
    assert sidecar["topic_sequence"] == "42"
    assert sidecar["consensus_timestamp"] == "2026-11-10T12:00:00.000000000Z"
    assert sidecar["tx_id"] == "0.0.1@1794333600.000000000"
    assert basis["oracle"]["label"] == "OPERATOR_VALUE"
    assert ex.settlement_receipt(result["settlement_id"]) == sidecar


# -- (c) idempotent re-settle while pending -----------------------------


def test_resettle_while_pending_returns_same_id_no_second_message(
    tmp_path, issuer, clock, agent_a, agent_b, monkeypatch
):
    ex = _real_exchange(
        tmp_path, issuer, clock, _fake_runner(), monkeypatch
    )
    mid = _trade_and_advance(ex, agent_a, agent_b, clock)
    first = ex.settle_market(mid, observed_value=55.0)
    assert first["status"] == "PENDING_ANCHOR"
    n_messages = len(ex.hcs.messages())
    n_jobs = ex.hcs.anchor_stats()["total"]

    second = ex.settle_market(mid, observed_value=55.0)

    assert second["settlement_id"] == first["settlement_id"]
    assert second["status"] == "PENDING_ANCHOR"
    assert len(ex.hcs.messages()) == n_messages  # no second HCS message
    assert ex.hcs.anchor_stats()["total"] == n_jobs  # no second anchor job
    assert len(ex.pending_settlements()) == 1


# -- (d) dead anchor -> ANCHOR_FAILED, fresh settle gets a new id ------


def test_dead_anchor_marks_failed_and_fresh_settle_supersedes(
    tmp_path, issuer, clock, agent_a, agent_b, monkeypatch
):
    runner = _fake_runner(failures=99)
    ex = _real_exchange(
        tmp_path, issuer, clock, runner, monkeypatch,
        queue_max_attempts=1,
    )
    mid = _trade_and_advance(ex, agent_a, agent_b, clock)
    first = ex.settle_market(mid, observed_value=55.0)
    assert first["status"] == "PENDING_ANCHOR"

    done = ex.hcs.drain_anchors()
    assert done[-1]["status"] == "dead"
    # One attempt per queued message (create + 2 orders + settle),
    # max_attempts=1: no retry on any of them.
    assert runner.calls["n"] == 4

    pending = ex.pending_settlements()
    assert len(pending) == 1
    assert pending[0]["settlement_id"] == first["settlement_id"]
    assert pending[0]["status"] == "ANCHOR_FAILED"
    assert ex.get_market(mid)["status"] == "SETTLEMENT_PENDING"
    assert _read_jsonl(tmp_path / "data" / "settlements.jsonl") == []

    second = ex.settle_market(mid, observed_value=55.0)
    assert second["status"] == "PENDING_ANCHOR"
    assert second["settlement_id"] != first["settlement_id"]
    assert len(ex.pending_settlements()) == 1  # old record superseded
    assert ex.pending_settlements()[0]["settlement_id"] == second["settlement_id"]


# -- (e) fail_retain: inline finalize with anchor queued ----------------


def test_fail_retain_finalizes_inline_with_anchor_queued(
    tmp_path, issuer, clock, agent_a, agent_b, monkeypatch
):
    ex = _real_exchange(
        tmp_path, issuer, clock, _fake_runner(), monkeypatch,
        settle_anchor_policy="fail_retain",
    )
    mid = _trade_and_advance(ex, agent_a, agent_b, clock)
    alice_before = ex.ledger.balance(agent_a["agent_id"])

    receipt = ex.settle_market(mid, observed_value=55.0)

    assert receipt["status"] == "SETTLED"
    assert receipt["settlement_id"].startswith("stl_")
    assert ex.get_market(mid)["status"] == "RESOLVED"
    assert ex.ledger.balance(agent_a["agent_id"]) == pytest.approx(alice_before + 10.0)
    sidecars = _read_jsonl(tmp_path / "data" / "settlement_receipts.jsonl")
    assert len(sidecars) == 1
    assert sidecars[0]["anchor_status"] == "queued"
    assert sidecars[0]["anchor_job_id"].startswith("anch_")
    assert sidecars[0]["hcs_topic"] == "0.0.11399235"
    # Draining the queued anchor afterwards must NOT pay out twice.
    ex.hcs.drain_anchors()
    assert ex.ledger.balance(agent_a["agent_id"]) == pytest.approx(alice_before + 10.0)
    assert len(_read_jsonl(tmp_path / "data" / "settlements.jsonl")) == 1
    assert len(_read_jsonl(tmp_path / "data" / "settlement_receipts.jsonl")) == 1


# -- (f) decision basis: oracle + public_source labels ------------------


def test_decision_basis_oracle_label_and_snapshot(
    exchange, agent_a, agent_b, clock
):
    from joulex.aeso import AESOPriceOracle
    from tests.test_aeso import W_END, W_START, _stub_text_fetcher
    from tests.test_joulex_markets import _trade_and_advance as _ta

    mid = _ta(
        exchange, agent_a, agent_b, clock,
        window_start=W_START, window_end=W_END, strike=20.0,
    )
    oracle = AESOPriceOracle(fetcher=_stub_text_fetcher())
    receipt = exchange.settle_market(mid, oracle=oracle)
    assert receipt["outcome"] == "YES"
    assert receipt["observed_value"] == pytest.approx(20.38)
    sidecar = exchange.settlement_receipt(receipt["settlement_id"])
    assert sidecar is not None
    basis = sidecar["decision_basis"]
    assert basis["oracle"]["label"] == "AESO-live"
    assert basis["oracle"]["snapshot"]["max_price"] == pytest.approx(20.38)
    assert "CSMPriceReportServlet" in basis["oracle"]["snapshot"]["source_url"]
    # No fetched_at is exposed by the oracle — never invented.
    assert basis["oracle"]["snapshot"]["fetched_at"] is None
    # inputs_hash recomputes from the stored basis.
    rest = {k: v for k, v in basis.items() if k != "inputs_hash"}
    assert basis["inputs_hash"] == hashlib.sha256(
        canonical_bytes(rest)
    ).hexdigest()


def test_decision_basis_public_source_label(
    exchange, agent_a, clock
):
    from tests.conftest import create_market_admitted, make_public_market_payload
    from tests.test_settle_auth import _settle_envelope

    evidence = "https://example.com/gpu-prices (H100 spot $4.20/GPU-hr)"
    market = create_market_admitted(
        exchange, agent_a, make_public_market_payload(), clock
    )
    mid = market["market_id"]
    clock.advance(days=1, minutes=1)
    env, binding = _settle_envelope(
        mid, outcome="YES", observed_value=None, evidence=evidence, clock=clock
    )
    receipt = exchange.settle_market(
        mid, outcome="YES", evidence=evidence,
        attestation=env, attestation_binding=binding,
    )
    assert receipt["status"] == "SETTLED"
    sidecar = exchange.settlement_receipt(receipt["settlement_id"])
    basis = sidecar["decision_basis"]
    assert basis["oracle"]["label"] == "PUBLIC_SOURCE"
    assert basis["oracle"]["snapshot"] == {
        "evidence_hash": hashlib.sha256(evidence.encode("utf-8")).hexdigest()
    }
    assert basis["credential_id"] == "joulex-settlement"
    assert sidecar["attestation_status"] == "verified"
    assert sidecar["attestation_binding"] == binding


# -- policy validation ---------------------------------------------------


def test_invalid_settle_anchor_policy_raises(tmp_path, issuer, clock, monkeypatch):
    with pytest.raises(ValueError):
        Exchange(
            data_dir=str(tmp_path / "bad-policy"),
            now_fn=clock,
            trusted_issuers=issuer.trust_store,
            settle_anchor_policy="bogus",
        )
    monkeypatch.setenv("JOULEX_SETTLE_ANCHOR_POLICY", "bogus")
    with pytest.raises(ValueError):
        Exchange(
            data_dir=str(tmp_path / "bad-env-policy"),
            now_fn=clock,
            trusted_issuers=issuer.trust_store,
        )


def test_pending_settlements_survive_restart(
    tmp_path, issuer, clock, agent_a, agent_b, monkeypatch
):
    ex = _real_exchange(
        tmp_path, issuer, clock, _fake_runner(), monkeypatch
    )
    mid = _trade_and_advance(ex, agent_a, agent_b, clock)
    first = ex.settle_market(mid, observed_value=55.0)
    assert first["status"] == "PENDING_ANCHOR"
    # A fresh Exchange over the same data dir reloads the pending record
    # (and the market stays SETTLEMENT_PENDING, frozen).
    ex2 = _real_exchange(
        tmp_path, issuer, clock, _fake_runner(), monkeypatch
    )
    assert ex2.get_market(mid)["status"] == "SETTLEMENT_PENDING"
    assert len(ex2.pending_settlements()) == 1
    assert ex2.pending_settlements()[0]["settlement_id"] == first["settlement_id"]
    second = ex2.settle_market(mid, observed_value=55.0)
    assert second["settlement_id"] == first["settlement_id"]


# -- (h) oversize settlement envelope: defined refusal, market stays OPEN

def test_oversize_settlement_refused_before_anchor(
    tmp_path, issuer, clock, agent_a, agent_b, monkeypatch
):
    """Integration fix (M4): an oversize HCS message fails in
    RealHCS.append WITHOUT a queue job, so on_anchor never fires.
    A fail-closed settlement must therefore refuse BEFORE appending
    — never sit PENDING_ANCHOR forever. The market stays OPEN, no
    pending record, no settlements.jsonl line, no HCS message."""
    from joulex.exchange import ExchangeError
    from tests.conftest import create_market_admitted, make_public_market_payload
    from tests.test_settle_auth import _settle_envelope

    ex = _real_exchange(
        tmp_path, issuer, clock, _fake_runner(), monkeypatch
    )
    market = create_market_admitted(
        ex, agent_a, make_public_market_payload(), clock
    )
    mid = market["market_id"]
    clock.advance(days=1, minutes=1)
    huge_evidence = "x" * 25_000
    env, binding = _settle_envelope(
        mid, outcome="YES", observed_value=None, evidence=huge_evidence,
        clock=clock,
    )
    n_messages = len(ex.hcs.messages())

    with pytest.raises(ExchangeError) as exc:
        ex.settle_market(
            mid, outcome="YES", evidence=huge_evidence,
            attestation=env, attestation_binding=binding,
        )

    assert exc.value.status == 503
    assert exc.value.reason.startswith("HCS_ANCHOR_FAILED")
    assert ex.get_market(mid)["status"] == "OPEN"
    assert ex.pending_settlements() == []
    assert _read_jsonl(tmp_path / "data" / "settlements.jsonl") == []
    assert _read_jsonl(tmp_path / "data" / "settlement_receipts.jsonl") == []
    assert len(ex.hcs.messages()) == n_messages  # no settle message logged
    assert ex.hcs.anchor_stats()["total"] == 1  # only the create_market job


# -- (i) server wiring: flag + /health anchor fields --------------------

def test_server_flag_and_health_anchor_fields(tmp_path, monkeypatch):
    """Integration: ``--settle-anchor-policy`` reaches the Exchange
    and ``/health`` exposes anchor counts + the active policy.
    Runs the real server entry point in a subprocess (local mode:
    no worker, anchor counts 0)."""
    import socket
    import subprocess
    import sys
    import time
    import urllib.request

    def free_port():
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    port = free_port()
    data_dir = str(tmp_path / "srvdata")
    proc = subprocess.Popen(
        [sys.executable, "-m", "joulex.server",
         "--port", str(port), "--data-dir", data_dir,
         "--settle-anchor-policy", "fail_retain"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 15
        body = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health", timeout=2
                ) as resp:
                    body = json.loads(resp.read())
                break
            except Exception:
                time.sleep(0.2)
        assert body is not None, "server did not come up"
        assert body["status"] == "ok"
        assert body["hcs_mode"] == "local"
        assert body["settle_anchor_policy"] == "fail_retain"
        assert body["hcs_anchor_queued"] == 0
        assert body["hcs_anchor_anchored"] == 0
        assert body["hcs_anchor_dead"] == 0
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_build_exchange_settle_policy_from_env(tmp_path, monkeypatch):
    """build_exchange honours JOULEX_SETTLE_ANCHOR_POLICY."""
    from joulex.server import build_exchange

    monkeypatch.setenv("JOULEX_SETTLE_ANCHOR_POLICY", "fail_retain")
    ex = build_exchange(str(tmp_path / "data"), None, None)
    assert ex.settle_anchor_policy == "fail_retain"
