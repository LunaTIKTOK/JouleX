"""AESO oracle tests — JouleX M0 (written FIRST, test-first).

The AESO oracle fetches the Alberta pool price from AESO's public ETS
reports (keyless). The live report that works from this VM is the
System Marginal Price (SMP) CSV — minute-level marginal prices; the
hourly pool price is the average of the 60 one-minute SMPs in the hour
(AESO's own definition). The historical Pool Price report servlet
returns HTTP 403 from this VM (recorded in BUILD_LOG.md) — the oracle
implements the SMP path live and documents the pool-price report as
the upgrade path.

Fail-closed: any fetch failure raises AESOUnavailable; a window the
fetched series does not cover raises AESOUnavailable (NO_DATA). The
settlement path refuses (503) and leaves the market OPEN — no outcome
is ever guessed.

These tests were written before ``joulex/aeso.py`` existed: they failed
on import until the module landed.
"""

from __future__ import annotations

import pytest

from datetime import datetime, timedelta, timezone

from joulex.aeso import (
    AESOPriceOracle,
    AESOUnavailable,
    fetch_current_pool_price,
    fetch_pool_price_series,
    parse_smp_csv,
)

# Real CSMPriceReportServlet CSV shape (minute SMP rows), captured live
# 2026-11-10 00:02 CST from
# http://ets.aeso.ca/ets_web/ip/Market/Reports/CSMPriceReportServlet?contentType=csv
# Times are AESO local (Mountain). Date (HE) is the hour-ending; Time is
# the minute within that hour.
SMP_CSV = """System Marginal Price

"Projected Pool Price for Hour Ending 01 is $24.68 as of 24:03."
Date (HE),Time,Price ($),Volume (MW)

"11/09/2026 24","23:00","17.04","31"
"11/09/2026 24","23:05","20.26","50"
"11/09/2026 24","23:21","19.15","35"
"11/09/2026 24","23:48","25.07","70"
"11/10/2026 01","24:00","24.68","73"
"""

# A window fully covered by SMP_CSV: 23:00–24:00 on 2026-11-09 (Mountain).
# Windows are built in the AESO timezone, not with a hardcoded offset:
# the container tzdata's UTC offset for America/Edmonton is not the
# true MST offset, so a hardcoded -07:00 would denote a different
# instant than the parser's 23:00 wall-clock hour (see joulex/aeso.py
# _local_naive). Building both sides from the same tz keeps the test
# correct on any system.
try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

_EDMONTON = ZoneInfo("America/Edmonton") if ZoneInfo is not None else timezone.utc
W_START = datetime(2026, 11, 9, 23, 0, tzinfo=_EDMONTON).isoformat()
W_END = datetime(2026, 11, 10, 0, 0, tzinfo=_EDMONTON).isoformat()
W_UNCOVERED_START = datetime(2026, 11, 9, 20, 0, tzinfo=_EDMONTON).isoformat()
W_UNCOVERED_END = datetime(2026, 11, 9, 23, 0, tzinfo=_EDMONTON).isoformat()


def _stub_text_fetcher(text=SMP_CSV, exc=None):
    seen = {}

    def fetch(url):
        seen["url"] = url
        if exc is not None:
            raise exc
        return text

    fetch.seen = seen
    return fetch


def _aeso_market(**overrides):
    m = {
        "market_id": "mkt_test",
        "question": "Will the pool price exceed $20/MWh in the window?",
        "resolution_type": "aeso_price",
        "contract_type": "flex_capacity",
        "strike": 20.0,
        "window_start": W_START,
        "window_end": W_END,
        "outcomes": ["YES", "NO"],
        "status": "OPEN",
    }
    m.update(overrides)
    return m


# -- CSV parsing --------------------------------------------------------


def test_parse_smp_csv_reads_minute_rows():
    rows = parse_smp_csv(SMP_CSV)
    assert len(rows) == 5
    ts, price = rows[0]
    assert price == pytest.approx(17.04)
    # First row: 2026-11-09 23:00 Mountain.
    assert (ts.year, ts.month, ts.day, ts.hour, ts.minute) == (2026, 11, 9, 23, 0)
    assert ts.tzinfo is not None
    assert rows[-1][1] == pytest.approx(24.68)


def test_parse_smp_csv_skips_header_and_blank_lines():
    rows = parse_smp_csv('System Marginal Price\n\n"Projected Pool Price for Hour Ending 01 is $1.00 as of 00:00."\nDate (HE),Time,Price ($),Volume (MW)\n\n"11/09/2026 24","23:00","17.04","31"\n')
    assert len(rows) == 1


def test_parse_smp_csv_bad_text_raises_unavailable():
    with pytest.raises(AESOUnavailable) as excinfo:
        parse_smp_csv("<html><body>not a report</body></html>")
    assert excinfo.value.code == "AESO_NO_DATA"


# -- hourly aggregation --------------------------------------------------


def test_fetch_series_averages_minutes_into_hourly_pool_price():
    series, source_url = fetch_pool_price_series(
        W_START, W_END, fetcher=_stub_text_fetcher()
    )
    assert len(series) == 1
    ts, price = series[0]
    assert (ts.hour, ts.minute) == (23, 0)
    # (17.04 + 20.26 + 19.15 + 25.07) / 4
    assert price == pytest.approx(20.38)
    assert "CSMPriceReportServlet" in source_url


def test_fetch_series_window_not_covered_is_no_data():
    with pytest.raises(AESOUnavailable) as excinfo:
        fetch_pool_price_series(
            W_UNCOVERED_START,
            W_UNCOVERED_END,
            fetcher=_stub_text_fetcher(),
        )
    assert excinfo.value.code == "AESO_NO_DATA"


def test_fetch_series_empty_window_rejected():
    with pytest.raises(AESOUnavailable):
        fetch_pool_price_series(W_END, W_START, fetcher=_stub_text_fetcher())


def test_fetch_network_error_is_unavailable():
    with pytest.raises(AESOUnavailable) as excinfo:
        fetch_pool_price_series(W_START, W_END, fetcher=_stub_text_fetcher(exc=ConnectionError("down")))
    assert excinfo.value.code == "AESO_UNAVAILABLE"


# -- current price -------------------------------------------------------


def test_fetch_current_pool_price_returns_latest_hour():
    result = fetch_current_pool_price(fetcher=_stub_text_fetcher())
    assert result["price"] == pytest.approx(24.68)  # latest hour: 24:00 row
    assert "timestamp" in result
    assert "CSMPriceReportServlet" in result["source_url"]
    assert "fetched_at" in result


def test_fetch_current_pool_price_fetch_failure_is_unavailable():
    with pytest.raises(AESOUnavailable) as excinfo:
        fetch_current_pool_price(fetcher=_stub_text_fetcher(exc=ConnectionError("down")))
    assert excinfo.value.code == "AESO_UNAVAILABLE"


# -- oracle: resolve_market ----------------------------------------------


def test_oracle_resolves_yes_when_max_exceeds_strike():
    oracle = AESOPriceOracle(fetcher=_stub_text_fetcher())
    outcome, max_price, source_url = oracle.resolve_market(_aeso_market(strike=20.0))
    assert outcome == "YES"
    assert max_price == pytest.approx(20.38)
    assert "CSMPriceReportServlet" in source_url


def test_oracle_resolves_no_when_max_below_strike():
    oracle = AESOPriceOracle(fetcher=_stub_text_fetcher())
    outcome, max_price, _ = oracle.resolve_market(_aeso_market(strike=25.0))
    assert outcome == "NO"
    assert max_price == pytest.approx(20.38)


def test_oracle_tie_resolves_no():
    oracle = AESOPriceOracle(fetcher=_stub_text_fetcher())
    outcome, max_price, _ = oracle.resolve_market(_aeso_market(strike=20.38))
    assert outcome == "NO"
    assert max_price == pytest.approx(20.38)


def test_oracle_refuses_non_aeso_market():
    oracle = AESOPriceOracle(fetcher=_stub_text_fetcher())
    with pytest.raises(AESOUnavailable) as excinfo:
        oracle.resolve_market(_aeso_market(resolution_type="public_source"))
    assert excinfo.value.code == "AESO_UNAVAILABLE"


def test_oracle_fetch_failure_is_unavailable():
    oracle = AESOPriceOracle(fetcher=_stub_text_fetcher(exc=ConnectionError("down")))
    with pytest.raises(AESOUnavailable) as excinfo:
        oracle.resolve_market(_aeso_market())
    assert excinfo.value.code == "AESO_UNAVAILABLE"


def test_oracle_window_not_covered_is_no_data():
    oracle = AESOPriceOracle(fetcher=_stub_text_fetcher())
    with pytest.raises(AESOUnavailable) as excinfo:
        oracle.resolve_market(
            _aeso_market(
                window_start=W_UNCOVERED_START,
                window_end=W_UNCOVERED_END,
            )
        )
    assert excinfo.value.code == "AESO_NO_DATA"


# -- licence honesty ------------------------------------------------------


def test_aeso_module_states_noncommercial_terms():
    import joulex.aeso as aeso_module

    doc = aeso_module.__doc__ or ""
    assert "written permission" in doc
    assert "non-commercial" in doc.lower()
