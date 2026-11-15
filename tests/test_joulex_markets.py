"""JouleX market vocabulary tests — M0 (written FIRST, test-first).

The JouleX adaptations over the joulex core:

- market record gains ``contract_type`` in {flex_capacity,
  compute_forward, generic} and ``resolution_type`` in {aeso_price,
  public_source};
- horizon 1–30 days (was 1–7);
- ``aeso_price`` markets carry strike + window_start + window_end and
  resolve YES iff the max hourly pool price over the window exceeds
  the strike (ties NO); ``aeso_price`` with outcomes other than
  ["YES", "NO"] is rejected at creation (400);
- the creation-payload binding includes the new fields
  (contract_type, resolution_type, strike, window_start, window_end)
  alongside the shared fields;
- multi-outcome stays public_source only;
- settlement: with the AESO oracle on, the fetched values win; oracle
  down refuses (503) and the market stays OPEN; without the oracle the
  operator-supplied value path remains.

Written before the port adaptations existed: they failed until
``joulex/markets.py`` and ``joulex/admission.py`` carried the JouleX
vocabulary.
"""

from __future__ import annotations

import pytest

from datetime import timedelta

from joulex.admission import creation_payload
from joulex.exchange import ExchangeError
from joulex.markets import new_market, resolve_outcome
from tests.conftest import (
    T0,
    create_market_admitted,
    iso,
    make_market_payload,
    make_order_payload,
)
from tests.test_aeso import (
    SMP_CSV,
    W_END,
    W_START,
    W_UNCOVERED_END,
    W_UNCOVERED_START,
    _aeso_market,
    _stub_text_fetcher,
)
from tests.test_ledger import open_market, place


def aeso_payload(**overrides):
    payload = {
        "question": "Will the Alberta pool price exceed $50/MWh during the delivery window?",
        "resolution_type": "aeso_price",
        "contract_type": "flex_capacity",
        "strike": 50.0,
        "window_start": iso(T0 + timedelta(days=1)),
        "window_end": iso(T0 + timedelta(days=1, hours=6)),
        "resolves_at": iso(T0 + timedelta(days=2)),
        "b": 100.0,
    }
    payload.update(overrides)
    return payload


def public_payload(**overrides):
    payload = {
        "question": "Will H100 spot be above $4.00/GPU-hr on 2026-11-12?",
        "resolution_type": "public_source",
        "contract_type": "compute_forward",
        "source_url": "https://example.com/gpu-prices",
        "source_description": "Published H100 spot price; YES iff above $4.00/GPU-hr.",
        "resolves_at": iso(T0 + timedelta(days=2)),
        "b": 100.0,
    }
    payload.update(overrides)
    return payload


# -- contract_type / resolution_type vocabulary --------------------------


def test_aeso_market_record_carries_joulex_fields():
    m = new_market(now=T0, **aeso_payload())
    assert m["contract_type"] == "flex_capacity"
    assert m["resolution_type"] == "aeso_price"
    assert m["strike"] == pytest.approx(50.0)
    assert m["window_start"] == iso(T0 + timedelta(days=1))
    assert m["window_end"] == iso(T0 + timedelta(days=1, hours=6))
    assert m["outcomes"] == ["YES", "NO"]
    assert "CSMPriceReportServlet" in m["resolution_source_url"]


def test_contract_type_defaults_to_generic():
    m = new_market(now=T0, **public_payload(contract_type=None))
    assert m["contract_type"] == "generic"


def test_invalid_contract_type_rejected():
    with pytest.raises(ValueError):
        new_market(now=T0, **aeso_payload(contract_type="nuclear_futures"))


def test_invalid_resolution_type_rejected():
    with pytest.raises(ValueError):
        new_market(now=T0, **aeso_payload(resolution_type="kraken_price"))


def test_aeso_market_requires_strike():
    with pytest.raises(ValueError):
        new_market(now=T0, **aeso_payload(strike=None))


def test_aeso_market_requires_window():
    with pytest.raises(ValueError):
        new_market(now=T0, **aeso_payload(window_start=None))
    with pytest.raises(ValueError):
        new_market(now=T0, **aeso_payload(window_end=None))


def test_aeso_market_window_must_be_ordered():
    with pytest.raises(ValueError):
        new_market(
            now=T0,
            **aeso_payload(
                window_start=iso(T0 + timedelta(days=1, hours=6)),
                window_end=iso(T0 + timedelta(days=1)),
            ),
        )


def test_aeso_market_rejects_non_binary_outcomes():
    with pytest.raises(ValueError):
        new_market(now=T0, **aeso_payload(outcomes=["High", "Low", "Flat"]))


def test_public_source_multi_outcome_still_allowed():
    m = new_market(
        now=T0, **public_payload(outcomes=["Under $4", "$4–$5", "Over $5"])
    )
    assert m["outcomes"] == ["Under $4", "$4–$5", "Over $5"]
    assert m["resolution_type"] == "public_source"


def test_public_source_still_requires_source_and_rule():
    with pytest.raises(ValueError):
        new_market(now=T0, **public_payload(source_url=None))
    with pytest.raises(ValueError):
        new_market(now=T0, **public_payload(source_description=None))


# -- horizon 1–30 days ----------------------------------------------------


def test_horizon_30_days_allowed():
    m = new_market(now=T0, **aeso_payload(resolves_at=iso(T0 + timedelta(days=30))))
    assert m["resolves_at"] == iso(T0 + timedelta(days=30))


def test_horizon_31_days_rejected():
    with pytest.raises(ValueError):
        new_market(now=T0, **aeso_payload(resolves_at=iso(T0 + timedelta(days=31))))


def test_horizon_under_1_day_rejected():
    with pytest.raises(ValueError):
        new_market(now=T0, **aeso_payload(resolves_at=iso(T0 + timedelta(hours=12))))


# -- resolution rule -------------------------------------------------------


def test_resolve_aeso_max_price_rule():
    m = new_market(now=T0, **aeso_payload(strike=50.0))
    assert resolve_outcome(m, observed_value=50.01) == "YES"
    assert resolve_outcome(m, observed_value=49.99) == "NO"
    assert resolve_outcome(m, observed_value=50.0) == "NO"  # tie resolves NO


def test_resolve_aeso_requires_observed_value():
    m = new_market(now=T0, **aeso_payload())
    with pytest.raises(ValueError):
        resolve_outcome(m)


# -- creation payload binding ----------------------------------------------


def test_creation_payload_binds_joulex_fields():
    bound = creation_payload(agent_id="agent-alice-1", **aeso_payload())
    assert bound["contract_type"] == "flex_capacity"
    assert bound["resolution_type"] == "aeso_price"
    assert bound["strike"] == pytest.approx(50.0)
    assert bound["window_start"] == iso(T0 + timedelta(days=1))
    assert bound["window_end"] == iso(T0 + timedelta(days=1, hours=6))
    assert "symbol" not in bound
    assert "direction" not in bound
    assert "resolution_date" not in bound


def test_creation_payload_differs_when_strike_differs():
    a = creation_payload(agent_id="agent-alice-1", **aeso_payload())
    b = creation_payload(agent_id="agent-alice-1", **aeso_payload(strike=60.0))
    assert a != b


def test_creation_payload_differs_when_window_differs():
    a = creation_payload(agent_id="agent-alice-1", **aeso_payload())
    b = creation_payload(
        agent_id="agent-alice-1",
        **aeso_payload(window_end=iso(T0 + timedelta(days=1, hours=12))),
    )
    assert a != b


# -- exchange-level: aeso settlement ---------------------------------------


def _trade_and_advance(exchange, agent_a, agent_b, clock, **overrides):
    m = open_market(exchange, agent_a, clock, **overrides)
    mid = m["market_id"]
    place(exchange, agent_a, make_order_payload(agent_a["agent_id"], mid, "YES", 10), clock)
    place(exchange, agent_b, make_order_payload(agent_b["agent_id"], mid, "NO", 20), clock)
    clock.advance(days=2, hours=1)
    return mid


def test_settle_aeso_with_oracle_uses_fetched_max(exchange, agent_a, agent_b, clock):
    from joulex.aeso import AESOPriceOracle

    # Window matches the fixture CSV: 23:00–24:00 2026-11-09 Mountain.
    # Fixture hourly max is 20.38; strike 20.0 -> YES.
    mid = _trade_and_advance(
        exchange, agent_a, agent_b, clock,
        window_start=W_START,
        window_end=W_END,
        strike=20.0,
    )
    oracle = AESOPriceOracle(fetcher=_stub_text_fetcher())
    receipt = exchange.settle_market(mid, oracle=oracle)
    assert receipt["outcome"] == "YES"
    assert receipt["observed_value"] == pytest.approx(20.38)
    assert "CSMPriceReportServlet" in receipt["oracle_source_url"]
    assert exchange.get_market(mid)["status"] == "RESOLVED"


def test_settle_aeso_oracle_down_refuses_503_and_market_stays_open(
    exchange, agent_a, agent_b, clock
):
    from joulex.aeso import AESOPriceOracle

    mid = _trade_and_advance(exchange, agent_a, agent_b, clock)
    before_a = exchange.ledger.balance(agent_a["agent_id"])
    oracle = AESOPriceOracle(fetcher=_stub_text_fetcher(exc=ConnectionError("down")))
    with pytest.raises(ExchangeError) as excinfo:
        exchange.settle_market(mid, oracle=oracle)
    assert excinfo.value.status == 503
    assert "AESO_UNAVAILABLE" in excinfo.value.reason
    market = exchange.get_market(mid)
    assert market["status"] == "OPEN"
    assert market["outcome"] is None
    assert exchange.ledger.balance(agent_a["agent_id"]) == before_a


def test_settle_aeso_oracle_no_data_refuses_503(exchange, agent_a, agent_b, clock):
    from joulex.aeso import AESOPriceOracle

    # Window outside the fixture series: no hourly data -> NO_DATA.
    mid = _trade_and_advance(
        exchange, agent_a, agent_b, clock,
        window_start=W_UNCOVERED_START,
        window_end=W_UNCOVERED_END,
    )
    oracle = AESOPriceOracle(fetcher=_stub_text_fetcher())
    with pytest.raises(ExchangeError) as excinfo:
        exchange.settle_market(mid, oracle=oracle)
    assert excinfo.value.status == 503
    assert "AESO_NO_DATA" in excinfo.value.reason
    assert exchange.get_market(mid)["status"] == "OPEN"


def test_settle_aeso_without_oracle_operator_path_unchanged(
    exchange, agent_a, agent_b, clock
):
    # Operator-supplied max pool price still works when no oracle is
    # configured (demo + tests). The oracle path replaces it only
    # when an oracle is passed.
    mid = _trade_and_advance(exchange, agent_a, agent_b, clock)
    receipt = exchange.settle_market(mid, observed_value=55.0)
    assert receipt["outcome"] == "YES"
    assert "oracle_source_url" not in receipt


def test_settle_aeso_with_oracle_ignores_operator_value(
    exchange, agent_a, agent_b, clock
):
    from joulex.aeso import AESOPriceOracle

    mid = _trade_and_advance(
        exchange, agent_a, agent_b, clock,
        window_start=W_START,
        window_end=W_END,
        strike=20.0,
    )
    oracle = AESOPriceOracle(fetcher=_stub_text_fetcher())
    receipt = exchange.settle_market(mid, observed_value=1.0, oracle=oracle)
    assert receipt["outcome"] == "YES"
    assert receipt["observed_value"] == pytest.approx(20.38)


def test_create_aeso_market_via_admitted_path(exchange, agent_a, clock):
    m = create_market_admitted(exchange, agent_a, aeso_payload(), clock)
    assert m["contract_type"] == "flex_capacity"
    assert m["resolution_type"] == "aeso_price"
    assert m["created_by"] == "agent-alice-1"


def test_create_compute_forward_via_admitted_path(exchange, agent_a, clock):
    m = create_market_admitted(exchange, agent_a, public_payload(), clock)
    assert m["contract_type"] == "compute_forward"
    assert m["resolution_type"] == "public_source"


def test_aeso_state_reconciles(exchange, agent_a, agent_b, clock):
    from joulex.reconcile import reconcile

    mid = _trade_and_advance(exchange, agent_a, agent_b, clock)
    exchange.settle_market(mid, observed_value=55.0)
    report = reconcile(exchange.store.data_dir)
    assert report["ok"], report["mismatches"]
