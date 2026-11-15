"""AESO pool-price feed — the JouleX price oracle (M0).

Fetches the Alberta pool price from the AESO's public ETS reports
(keyless, no account). Live-verified from this VM 2026-11-10:

- ``CSMPriceReportServlet?contentType=csv`` (System Marginal Price,
  minute-level) — HTTP 200. The hourly pool price is the average of
  the 60 one-minute SMPs in the hour — that is AESO's own definition
  of the hourly pool price (average of 60 one-minute SMPs). The
  report returns only the recent hours (~3h at the time of the
  check), so a settlement window the fetched series does not cover
  is a NO_DATA refusal, not a guess.
- ``PoolPriceReportServlet`` (the historical hourly pool-price
  report) — HTTP 403 from this VM in every form tried (no params,
  startDate/endDate, beginDate/endDate). It is the upgrade path for
  multi-day windows; see ``POOL_PRICE_CSV_URL`` and BUILD_LOG.md.
  The Azure APIM gateway (``https://apimgw.aeso.ca/public``) needs a
  free self-serve key — the keyed path is the other upgrade; no key
  is created or stored by this module.

Fail-closed, always (DESIGN.md §11): any fetch failure raises
``AESOUnavailable`` — the settlement path refuses (503) and the
market stays OPEN. No outcome is ever guessed, and a series that
does not cover the requested window is ``AESO_NO_DATA``.

AESO terms (from https://www.aeso.ca/legal/, verified in the Phase 1
research): AESO data is free to access, but commercial use needs
AESO's written permission — there is no open-data licence. This
prototype is NON-COMMERCIAL RESEARCH ONLY. It never republishes AESO
data as a commercial feed, and any commercial use must first obtain
AESO's written permission.

Times in the ETS reports are AESO local time (Mountain,
America/Edmonton). The parser attaches that timezone; all returned
timestamps are timezone-aware.
"""

from __future__ import annotations

import csv
import io
import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.9 fallback
    ZoneInfo = None  # type: ignore[assignment]

from .markets import resolve_outcome

# Live-verified 2026-11-10: HTTP 200 from this VM.
SMP_CSV_URL = (
    "http://ets.aeso.ca/ets_web/ip/Market/Reports/"
    "CSMPriceReportServlet?contentType=csv"
)
# Historical hourly pool-price report. HTTP 403 from this VM in every
# form tried 2026-11-10 (see BUILD_LOG.md); the upgrade path for
# multi-day settlement windows, once reachable.
POOL_PRICE_CSV_URL = (
    "http://ets.aeso.ca/ets_web/ip/Market/Reports/"
    "PoolPriceReportServlet?contentType=csv"
)
# Keyed Azure APIM gateway — the other upgrade path. Needs a free
# self-serve key from https://developer-apim.aeso.ca/ ; no key is
# created or stored here.
APIM_GATEWAY_URL = "https://apimgw.aeso.ca/public"

DEFAULT_TIMEOUT_SECONDS = 10
AESO_TZ_NAME = "America/Edmonton"


class AESOUnavailable(Exception):
    """An AESO fetch failed. ``code`` is AESO_UNAVAILABLE (network /
    HTTP / unparseable) or AESO_NO_DATA (answered fine but no series
    covers the requested window). The message carries the detail."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _aeso_tz():
    if ZoneInfo is not None:
        try:
            return ZoneInfo(AESO_TZ_NAME)
        except Exception:
            pass
    # Fallback: Mountain Standard Time (UTC-7). The ETS reports are
    # Mountain local; DST shifts of one hour are a known limitation of
    # this fallback and are labelled in the docstring of the caller.
    return timezone(timedelta(hours=-7))


def _parse_row_timestamp(date_he: str, time_hhmm: str) -> datetime:
    """One SMP CSV row's timestamp.

    ``date_he`` is ``MM/DD/YYYY HH`` (hour-ending, 01–24);
    ``time_hhmm`` is ``HH:MM`` within that date. AESO writes midnight
    as 24:00 — the minute at the START of the date (00:00), e.g. the
    row for hour-ending 01 on 11/10 with time 24:00 is the 00:00
    minute on 11/10."""
    date_part = date_he.strip().split()[0]
    month, day, year = (int(x) for x in date_part.split("/"))
    hour, minute = (int(x) for x in time_hhmm.strip().split(":"))
    if hour == 24:
        hour = 0
    return datetime(year, month, day, hour, minute, tzinfo=_aeso_tz())


def parse_smp_csv(text: str) -> list[tuple[datetime, float]]:
    """Parse the CSMPriceReportServlet CSV into [(timestamp, smp), ...]
    minute rows, oldest first. Raises AESOUnavailable (AESO_NO_DATA)
    when the text is not a parseable SMP report."""
    rows: list[tuple[datetime, float]] = []
    reader = csv.reader(io.StringIO(text))
    for fields in reader:
        if len(fields) < 3:
            continue
        try:
            ts = _parse_row_timestamp(fields[0], fields[1])
            price = float(fields[2])
        except (ValueError, IndexError):
            continue
        rows.append((ts, price))
    if not rows:
        raise AESOUnavailable("AESO_NO_DATA", "SMP report has no price rows")
    rows.sort(key=lambda r: r[0])
    return rows


def _hourly_pool_prices(rows: list[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
    """Average minute SMPs into hourly pool prices. One entry per hour
    with at least one minute sample: (hour_start, mean price). Hours
    are AESO-local wall-clock hours (see _local_naive)."""
    buckets: dict[datetime, list[float]] = {}
    for ts, price in rows:
        hour = _local_naive(ts).replace(minute=0, second=0, microsecond=0)
        buckets.setdefault(hour, []).append(price)
    out = []
    for hour, prices in sorted(buckets.items()):
        out.append((hour.replace(tzinfo=_aeso_tz()), sum(prices) / len(prices)))
    return out


def _local_naive(dt: datetime) -> datetime:
    """A datetime as naive AESO-local wall clock.

    Wall-clock matching is deliberate: the ETS report times are
    Mountain local, and the container tzdata's UTC offset for
    America/Edmonton is not the true MST offset, so comparing
    instants across tzinfo objects would shift hours. Matching on
    wall-clock fields in the AESO timezone is stable on any system
    where ZoneInfo is self-consistent."""
    return dt.astimezone(_aeso_tz()).replace(tzinfo=None)


def _default_text_fetcher(url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": "joulex/0.1 (non-commercial research prototype)"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                raise AESOUnavailable("AESO_UNAVAILABLE", f"AESO HTTP {resp.status}")
            return resp.read().decode("utf-8")
    except AESOUnavailable:
        raise
    except (urllib.error.URLError, OSError, ValueError, UnicodeDecodeError) as exc:
        raise AESOUnavailable("AESO_UNAVAILABLE", f"fetch failed: {exc}") from exc


def _parse_window(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise AESOUnavailable("AESO_NO_DATA", "window bounds must carry timezone information")
    return parsed


def fetch_pool_price_series(
    window_start: str,
    window_end: str,
    fetcher: Callable[[str], str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[list[tuple[datetime, float]], str]:
    """Hourly pool prices over [window_start, window_end).

    Returns (series, source_url) where series is a list of
    (hour_start, price $/MWh), one entry per hour in the window.
    Raises AESOUnavailable when the fetch fails (AESO_UNAVAILABLE)
    or the fetched series does not cover every hour in the window
    (AESO_NO_DATA). ``fetcher`` is a callable ``(url) -> raw CSV text``;
    inject a stub in tests. The default fetcher is a stdlib urllib GET
    of the live SMP report with a 10s timeout.
    """
    start = _local_naive(_parse_window(window_start))
    end = _local_naive(_parse_window(window_end))
    if end <= start:
        raise AESOUnavailable("AESO_NO_DATA", "window_end must be after window_start")
    fetch = fetcher or (lambda url: _default_text_fetcher(url, timeout=timeout))
    try:
        text = fetch(SMP_CSV_URL)
    except AESOUnavailable:
        raise
    except Exception as exc:  # stub/network failures are all unavailable
        raise AESOUnavailable("AESO_UNAVAILABLE", f"fetch failed: {exc}") from exc
    if not isinstance(text, str):
        raise AESOUnavailable("AESO_UNAVAILABLE", "AESO payload is not text")
    hourly = _hourly_pool_prices(parse_smp_csv(text))
    # Compare on naive AESO-local wall-clock hours (see _local_naive).
    by_hour = {_local_naive(ts): price for ts, price in hourly}
    series: list[tuple[datetime, float]] = []
    hour = start.replace(minute=0, second=0, microsecond=0)
    while hour < end:
        if hour not in by_hour:
            raise AESOUnavailable(
                "AESO_NO_DATA",
                f"no pool price for hour starting {hour.isoformat()} "
                "(report covers only recent hours)",
            )
        series.append((hour.replace(tzinfo=_aeso_tz()), by_hour[hour]))
        hour += timedelta(hours=1)
    return series, SMP_CSV_URL


def fetch_current_pool_price(
    fetcher: Callable[[str], str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Latest hourly pool price: the most recent hour with SMP data.

    Returns {"timestamp", "price", "source_url", "fetched_at"}.
    Raises AESOUnavailable on any fetch failure."""
    fetch = fetcher or (lambda url: _default_text_fetcher(url, timeout=timeout))
    try:
        text = fetch(SMP_CSV_URL)
    except AESOUnavailable:
        raise
    except Exception as exc:
        raise AESOUnavailable("AESO_UNAVAILABLE", f"fetch failed: {exc}") from exc
    if not isinstance(text, str):
        raise AESOUnavailable("AESO_UNAVAILABLE", "AESO payload is not text")
    hourly = _hourly_pool_prices(parse_smp_csv(text))
    ts, price = hourly[-1]
    return {
        "timestamp": ts.isoformat(),
        "price": price,
        "source_url": SMP_CSV_URL,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


class AESOPriceOracle:
    """Fetch AESO pool prices for ``aeso_price`` settlement.

    ``fetcher`` is a callable ``(url) -> raw CSV text``; inject a stub
    in tests. The default fetcher is the live SMP report (stdlib
    urllib, 10s timeout).
    """

    def __init__(
        self,
        fetcher: Callable[[str], str] | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        self._fetcher = fetcher
        self._timeout = timeout

    def fetch_max_pool_price(
        self, window_start: str, window_end: str
    ) -> tuple[float, str]:
        """Return (max hourly pool price over the window, source_url)."""
        series, source_url = fetch_pool_price_series(
            window_start, window_end, fetcher=self._fetcher, timeout=self._timeout
        )
        return max(price for _ts, price in series), source_url

    def resolve_market(self, market: dict[str, Any]) -> tuple[str, float, str]:
        """Fetch the max pool price for an ``aeso_price`` market and
        derive its outcome. Returns (outcome, max_price, source_url).
        Raises AESOUnavailable on any fetch failure or uncovered
        window — the caller refuses settlement and leaves the market
        OPEN."""
        if market["resolution_type"] != "aeso_price":
            raise AESOUnavailable(
                "AESO_UNAVAILABLE", "oracle only resolves aeso_price markets"
            )
        max_price, source_url = self.fetch_max_pool_price(
            market["window_start"], market["window_end"]
        )
        return resolve_outcome(market, observed_value=max_price), max_price, source_url
