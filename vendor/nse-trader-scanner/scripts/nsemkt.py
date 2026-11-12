"""nsemkt - shared NSE / broker-neutral utilities for nse-trader-* skills.

Standard library only. Windows-safe: IST is computed as a fixed +05:30 offset
(no zoneinfo / tzdata dependency). Every network call is best-effort with a
clear error message; callers decide whether to degrade.

Conventions
-----------
- Symbols are NSE tickers ("RELIANCE", "TATAMOTORS"); Yahoo needs ".NS".
- Money is INR. The default price step is a conservative Rs 0.05; callers
  must verify the instrument's current exchange tick with their broker/NSE.
- Session times are IST: pre-open auction 09:00, trading 09:15-15:30,
  MIS square-off 15:15, no new entries after 15:00.
- State lives in ./nse-trader/ relative to the working directory.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
# Conservative/common NSE order-price step. Some lower-priced cash instruments
# can have a Rs 0.01 tick under NSE's monthly tick-size regime; Rs 0.05 remains
# valid but coarser. Always verify the instrument's current tick.
TICK = 0.05

PREOPEN_START = (9, 0)      # pre-open auction begins
OPEN = (9, 15)              # continuous trading begins
ENTRY_CUTOFF = (15, 0)      # no new entries after this
MIS_SQUAREOFF = (15, 15)    # brokers start squaring off MIS near here
CLOSE = (15, 30)            # continuous trading ends

STATE_DIR = os.environ.get("NSE_TRADER_STATE_DIR", "nse-trader")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Bundled fallback for offline operation. The live NSE holiday master is cached
# by nse_holidays(); this table prevents a network outage from misclassifying
# known 2026 holidays.
HOLIDAYS = {
    "2026-01-15": "Municipal Corporation Election - Maharashtra",
    "2026-01-26": "Republic Day",
    "2026-02-15": "Mahashivratri",
    "2026-03-03": "Holi",
    "2026-03-21": "Id-Ul-Fitr (Ramadan Eid)",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-08-15": "Independence Day",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-08": "Diwali Laxmi Pujan*",
    "2026-11-10": "Diwali-Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}

HOLIDAY_SOURCE_URL = "https://www.nseindia.com/api/holiday-master?type=trading"
HOLIDAY_CACHE_HOURS = 24 * 7
CHARGE_SCHEDULE_DATE = "2026-11-01"
# Backward compatibility for callers from the initial Groww-specific release.
GROWW_CHARGE_SCHEDULE_DATE = CHARGE_SCHEDULE_DATE

# --------------------------------------------------------------------------
# Time / session helpers
# --------------------------------------------------------------------------


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST)


def today_ist() -> dt.date:
    return now_ist().date()


def _parse_nse_holiday_date(value: str) -> dt.date | None:
    try:
        return dt.datetime.strptime(value, "%d-%b-%Y").date()
    except (TypeError, ValueError):
        return None


def nse_holidays(refresh: bool = False, allow_network: bool = False) -> dict[str, str]:
    """Return known NSE capital-market holidays, optionally refreshing cache.

    The bundled fallback is always included. The cache is stored under
    ``./nse-trader/cache``. Network refresh is explicit (including the rare
    year-missing case in ``holiday_name``) and deliberately bounded: a short
    timeout/attempt budget plus a week-long cache TTL means a data year unknown
    to both fallback and cache costs at most one refresh per cache cycle. Set
    ``NSE_TRADER_OFFLINE=1`` to suppress every refresh attempt.
    """
    merged = dict(HOLIDAYS)
    path = os.path.join(STATE_DIR, "cache", "nse_holidays.json")
    cached = load_json(path, {}) or {}
    cached_holidays = cached.get("holidays", {}) if isinstance(cached, dict) else {}
    if isinstance(cached_holidays, dict):
        merged.update({str(k): str(v) for k, v in cached_holidays.items()})

    offline = os.environ.get("NSE_TRADER_OFFLINE", "").strip().lower() in (
        "1", "true", "yes", "on"
    )
    should_fetch = allow_network and not offline and (
        refresh or not _cache_fresh(path, HOLIDAY_CACHE_HOURS)
    )
    if not should_fetch:
        return merged

    official: dict[str, str] = {}
    error = None
    try:
        # Bounded on purpose: classification paths must never hang on a
        # calendar refresh; a miss leaves merged data intact plus an error
        # note in the cache.
        payload = http_json(HOLIDAY_SOURCE_URL, timeout=8, retries=1)
        rows = payload.get("CM", []) if isinstance(payload, dict) else []
        for row in rows:
            day = _parse_nse_holiday_date(row.get("tradingDate"))
            name = str(row.get("description") or "NSE holiday").strip()
            if day:
                official[day.isoformat()] = name
        if not official:
            raise ValueError("NSE holiday response contained no CM dates")
    except Exception as exc:  # retain fallback and record why refresh failed
        error = str(exc)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    save_json(path, {
        "fetched_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "source": HOLIDAY_SOURCE_URL,
        "holidays": official,
        "error": error,
    })
    merged.update(official)
    return merged


def holiday_name(d: dt.date | None = None, refresh: bool = False) -> str | None:
    d = d or today_ist()
    initial = nse_holidays(refresh=False, allow_network=False)
    known_years = {int(value[:4]) for value in initial if len(value) >= 4 and value[:4].isdigit()}
    holidays = initial
    if refresh or d.year not in known_years:
        holidays = nse_holidays(refresh=refresh, allow_network=True)
    return holidays.get(d.isoformat())


def holiday_calendar_verified(year: int, allow_network: bool = True) -> bool:
    """Whether fallback/cache contains at least one holiday for ``year``.

    A coverage signal, not a certificate: a year whose calendar has never been
    refreshed still returns ``False``. The first lookup is always local, and
    only a locally-unverifiable year may trigger one bounded refresh attempt
    (the pre-existing design; covered years never cause network traffic and
    never write the cache). With ``allow_network=False`` the check is strictly
    local — use that for paths, such as FII/DII freshness classification,
    that must stay fast and offline-safe.
    """
    holidays = nse_holidays(allow_network=False)
    if not any(key.startswith(f"{year:04d}-") for key in holidays) and allow_network:
        holidays = nse_holidays(refresh=False, allow_network=True)
    return any(key.startswith(f"{year:04d}-") for key in holidays)


def _calendar_year_coverage(years, holidays: dict[str, str] | None = None) -> dict[int, bool]:
    """Per-year holiday coverage from the local calendar (fallback + cache)."""
    source = holidays if holidays is not None else nse_holidays(allow_network=False)
    return {
        int(year): any(key.startswith(f"{int(year):04d}-") for key in source)
        for year in years
    }


def is_trading_day(d: dt.date | None = None, holidays: dict[str, str] | None = None) -> bool:
    """Weekday + not an NSE holiday.

    Pass a local ``holidays`` map (fallback + cache) for network-free
    classification of many days, e.g. FII/DII session walks.
    """
    d = d or today_ist()
    if d.weekday() >= 5:
        return False
    if holidays is None:
        return holiday_name(d) is None
    return d.isoformat() not in holidays


def next_trading_day(d: dt.date | None = None, include_today: bool = False) -> dt.date:
    d = d or today_ist()
    if not include_today:
        d = d + dt.timedelta(days=1)
    while not is_trading_day(d):
        d = d + dt.timedelta(days=1)
    return d


def _at(d: dt.date, hm: tuple[int, int]) -> dt.datetime:
    return dt.datetime(d.year, d.month, d.day, hm[0], hm[1], tzinfo=IST)


def session_state(now: dt.datetime | None = None) -> dict:
    """Return a dict describing the NSE session right now (all IST)."""
    now = now or now_ist()
    d = now.date()
    base = {
        "now_ist": now.isoformat(timespec="seconds"),
        "date": d.isoformat(),
        "flat_by": _at(d, MIS_SQUAREOFF).isoformat(timespec="minutes"),
        "entry_cutoff": _at(d, ENTRY_CUTOFF).isoformat(timespec="minutes"),
        "holiday_calendar_verified": holiday_calendar_verified(d.year),
    }
    if d.weekday() >= 5:
        return {**base, "state": "WEEKEND", "is_trading_day": False,
                "next_open": _at(next_trading_day(d), OPEN).isoformat(timespec="minutes")}
    hol = holiday_name(d)
    if hol:
        return {**base, "state": "HOLIDAY", "is_trading_day": False, "holiday": hol,
                "next_open": _at(next_trading_day(d), OPEN).isoformat(timespec="minutes")}

    t = now.time()
    if t < dt.time(*PREOPEN_START):
        state = "PRE_MARKET"
    elif t < dt.time(*OPEN):
        state = "PRE_OPEN"
    elif t < dt.time(*MIS_SQUAREOFF):
        state = "OPEN"
    elif t < dt.time(*CLOSE):
        state = "SQUAREOFF"
    else:
        state = "POST_CLOSE"

    close_dt = _at(d, CLOSE)
    return {
        **base,
        "state": state,
        "is_trading_day": True,
        "minutes_to_close": int((close_dt - now).total_seconds() // 60),
        "next_open": _at(next_trading_day(d), OPEN).isoformat(timespec="minutes"),
    }


def can_enter_now(now: dt.datetime | None = None) -> tuple[bool, str]:
    """Whether a new entry is allowed right now, with a one-line reason."""
    now = now or now_ist()
    ss = session_state(now)
    if ss["state"] == "WEEKEND":
        return False, "weekend - market closed"
    if ss["state"] == "HOLIDAY":
        return False, f"NSE holiday: {ss['holiday']}"
    if ss["state"] in ("PRE_MARKET", "PRE_OPEN"):
        return False, f"market not open yet ({ss['state'].lower()})"
    if ss["state"] == "SQUAREOFF":
        return False, "past 15:00 entry cutoff - squaring-off window only"
    if ss["state"] == "POST_CLOSE":
        return False, "market closed for the day"
    if now.time() >= dt.time(*ENTRY_CUTOFF):
        return False, "past 15:00 entry cutoff"
    return True, "open for entries"


# --------------------------------------------------------------------------
# HTTP + JSON
# --------------------------------------------------------------------------


def http_get(url: str, timeout: int = 25, retries: int = 2) -> str:
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            if attempt < retries:
                time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(f"GET failed: {url} ({last})")


def http_json(url: str, timeout: int = 25, retries: int = 2):
    return json.loads(http_get(url, timeout=timeout, retries=retries))


# --------------------------------------------------------------------------
# Yahoo Finance (NSE data, no key)
# --------------------------------------------------------------------------


def yahoo_symbol(symbol: str) -> str:
    s = str(symbol).strip().upper()
    if not s.startswith("^") and "." not in s:
        s += ".NS"
    return s


def yahoo_chart(symbol: str, interval: str = "1d", range_: str = "6mo") -> dict:
    """Fetch OHLCV bars. Returns {'symbol', 'meta', 'bars': [{t,o,h,l,c,v}]} with t in IST."""
    sym = yahoo_symbol(symbol)
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{urllib.parse.quote(sym)}?interval={interval}&range={range_}"
        "&includePrePost=false&events=div%2Csplits"
    )
    data = http_json(url)
    res = ((data or {}).get("chart") or {}).get("result") or []
    if not res:
        raise RuntimeError(f"no chart data for {sym}")
    r = res[0]
    ts = r.get("timestamp") or []
    quote = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    vols = quote.get("volume") or []
    bars = []
    for i, t in enumerate(ts):
        try:
            o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        except IndexError:
            continue
        if None in (o, h, l, c):
            continue
        bars.append({
            "t": dt.datetime.fromtimestamp(t, IST),
            "o": float(o), "h": float(h), "l": float(l), "c": float(c),
            "v": float(vols[i] or 0) if i < len(vols) else 0.0,
        })
    return {"symbol": sym, "meta": r.get("meta") or {}, "bars": bars}


def yahoo_quote(symbol: str) -> dict:
    """Last price and previous close, with timestamp. Raises on failure."""
    sym = yahoo_symbol(symbol)
    q = yahoo_chart(sym, interval="1d", range_="5d")
    bars = q["bars"]
    meta = q["meta"]
    if not bars:
        raise RuntimeError(f"no quote for {sym}")
    last = bars[-1]
    ltp = float(meta.get("regularMarketPrice") or last["c"])
    fallback_prev = bars[-2]["c"] if len(bars) > 1 else last["o"]
    prev = float(meta.get("chartPreviousClose") or meta.get("previousClose") or fallback_prev)
    ts_ms = meta.get("regularMarketTime")
    ts = dt.datetime.fromtimestamp(ts_ms, IST) if ts_ms else last["t"]
    chg = ltp - prev
    return {
        "symbol": sym.replace(".NS", ""),
        "ltp": ltp,
        "prev_close": prev,
        "change": chg,
        "pct": (chg / prev * 100.0) if prev else 0.0,
        "ts_ist": ts.isoformat(timespec="seconds"),
        "currency": meta.get("currency", "INR"),
        "exchange": meta.get("fullExchangeName", "NSE"),
    }


def atr_daily(symbol: str, period: int = 14) -> float:
    """ATR(period) from daily bars (Wilder). Returns 0.0 if not enough data."""
    bars = yahoo_chart(symbol, "1d", "6mo")["bars"]
    if len(bars) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return 0.0
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def sma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        ch = closes[i] - closes[i - 1]
        gains += max(ch, 0.0)
        losses += max(-ch, 0.0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(ch, 0.0)) / n
        al = (al * (n - 1) + max(-ch, 0.0)) / n
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


# --------------------------------------------------------------------------
# NSE reference data
# --------------------------------------------------------------------------


def _cache_path(name: str) -> str:
    return os.path.join(STATE_DIR, "cache", name)


def _cache_fresh(path: str, hours: int = 12) -> bool:
    try:
        return (time.time() - os.path.getmtime(path)) < hours * 3600
    except OSError:
        return False


def nse_symbols(refresh: bool = False) -> list[dict]:
    """NSE equity master (EQUITY_L.csv). Cached 12h under nse-trader/cache/."""
    path = _cache_path("equity_master.json")
    if not refresh and _cache_fresh(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    text = http_get("https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv")
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        sym = (row.get("SYMBOL") or "").strip()
        if not sym:
            continue
        rows.append({
            "symbol": sym,
            "name": (row.get("NAME OF COMPANY") or "").strip(),
            "series": (row.get("SERIES") or "").strip(),
            "isin": (row.get("ISIN NUMBER") or "").strip(),
        })
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f)
    return rows


def fo_lots(refresh: bool = False) -> dict:
    """Current-month F&O lot sizes from NSE fo_mktlots.csv -> {symbol: lot}."""
    path = _cache_path("fo_mktlots.json")
    if not refresh and _cache_fresh(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    text = http_get("https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv")
    reader = csv.reader(io.StringIO(text))
    header = next(reader)
    # header: UNDERLYING, SYMBOL, NOV-26, DEC-26, ...
    try:
        lot_col = 2
        _ = header[2]
    except IndexError:
        raise RuntimeError("unexpected fo_mktlots.csv header")
    lots: dict[str, int] = {}
    for row in reader:
        if not row or len(row) <= lot_col:
            continue
        sym = (row[1] or "").strip().upper()
        raw = (row[lot_col] or "").strip()
        if not sym or not raw:
            continue
        try:
            lots[sym] = int(float(raw))
        except ValueError:
            continue
        und = (row[0] or "").strip().upper()
        if und and und not in lots:
            lots[und] = lots[sym]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(lots, f)
    return lots


# --------------------------------------------------------------------------
# FII/DII daily cash flows: session-date metadata and freshness
# --------------------------------------------------------------------------

FIIDII_URL = "https://www.nseindia.com/api/fiidiiTradeReact"
FIIDII_DATA_STATUSES = (
    "LIVE", "CURRENT_SESSION", "PREVIOUS_SESSION", "END_OF_DAY", "STALE", "UNKNOWN",
)
# NSE fiidiiTradeReact is a daily aggregate published after market close. No
# skill in this pack receives live intraday institutional flow, so only
# CURRENT_SESSION/END_OF_DAY-style labels below are produced, and every value
# is reported with its own session date.
FIIDII_STATUS_NOTES = {
    "LIVE": "live intraday activity from a source that genuinely publishes it",
    "CURRENT_SESSION": "dated to the session in progress; provisional daily aggregate, not live tick-level flow",
    "PREVIOUS_SESSION": "latest completed/published session; broader market context only",
    "END_OF_DAY": "published after market close for that session; not live intraday flow",
    "STALE": "older than the latest completed trading session; historical context only",
    "UNKNOWN": "source date missing or unreliable; the session date is not assumed",
}
# calendar age is informational; trading-session distance is what matters.
FIIDII_FRESHNESS_STATUSES = ("FRESH", "DELAYED", "STALE", "UNKNOWN")
FIIDII_FRESHNESS_NOTES = {
    "FRESH": "Latest available completed session",
    "DELAYED": "Delayed - a newer completed trading session exists",
    "STALE": "Stale - multiple completed trading sessions behind the latest",
    "UNKNOWN": "Unknown - trading-session age cannot be confirmed",
}


def parse_nse_flow_date(value) -> dt.date | None:
    """Parse NSE flow dates such as ``30-Oct-2026``, ``30 Oct 2026`` or ISO."""
    if value is None:
        return None
    text = str(value).strip()
    for fmt in ("%d-%b-%Y", "%d %b %Y", "%d-%B-%Y", "%d %B %Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def last_trading_day(d: dt.date | None = None, holidays: dict[str, str] | None = None) -> dt.date:
    """Most recent trading day strictly before ``d`` (default: today).

    Pass a local ``holidays`` map for network-free walks.
    """
    day = (d or today_ist()) - dt.timedelta(days=1)
    for _ in range(31):
        if is_trading_day(day, holidays):
            return day
        day -= dt.timedelta(days=1)
    return day


def _trading_sessions_between(
    start_d: dt.date, end_d: dt.date, holidays: dict[str, str] | None = None,
) -> list[dt.date] | None:
    """Trading days in ``(start_d, end_d]``; ``None`` when the span is absurd.

    Uses the exchange calendar via ``is_trading_day`` (weekends AND NSE
    holidays — pass ``holidays``), so a Monday holiday or Diwali week is
    counted correctly instead of by calendar days.
    """
    if (end_d - start_d).days > 400:
        return None
    days: list[dt.date] = []
    cursor = start_d + dt.timedelta(days=1)
    while cursor <= end_d:
        if is_trading_day(cursor, holidays):
            days.append(cursor)
        cursor += dt.timedelta(days=1)
    return days


def format_nse_flow_session(value) -> str:
    """Human trading-session label such as ``Friday, 30 Oct 2026``, else UNKNOWN."""
    parsed = parse_nse_flow_date(value)
    return parsed.strftime("%A, %d %b %Y") if parsed else "UNKNOWN"


def fii_dii_freshness(data_date, now: dt.datetime | None = None) -> dict:
    """Classify a daily FII/DII observation against real NSE trading sessions.

    Returns ``data_date`` (ISO when parseable), ``market_session_date``,
    ``latest_completed_session``, ``sessions_behind`` (completed trading
    sessions between the data date and the latest completed session),
    ``freshness_status`` (FRESH / DELAYED / STALE / UNKNOWN),
    ``data_status``, ``is_live``, ``is_previous_session``,
    ``calendar_verified`` and ``calendar_age_days`` plus warnings. Freshness
    follows the exchange calendar (weekends AND NSE holidays), never calendar
    days alone: Friday figures form the latest completed session on the
    following Monday, and still so on the Tuesday after a Monday holiday.
    The data is never assumed to be current: a missing or unparseable source
    date is ``UNKNOWN``; a newer completed session than the data makes it
    ``STALE`` data with ``DELAYED``/``STALE`` freshness. An unverified holiday
    calendar is reported as ``UNKNOWN`` freshness, not silently trusted.
    Classification is strictly local (bundled calendar + cache): no network
    call is made on this path, so a data year unknown to the local calendar
    degrades to ``UNKNOWN`` freshness instead of blocking on a refresh.
    """
    now = now or now_ist()
    state = session_state(now).get("state") or "UNKNOWN"
    today = now.date()
    warnings: list[str] = []
    parsed = parse_nse_flow_date(data_date)
    # Classification must stay fast and offline-safe: use the LOCAL calendar
    # (bundled fallback + cache) for every day/step walked here. The rare,
    # bounded official refresh is session_state's responsibility; an
    # unverified year surfaces as UNKNOWN freshness, never a silent assumption.
    local_holidays = nse_holidays(allow_network=False)
    trading_today = today.weekday() < 5 and today.isoformat() not in local_holidays
    in_progress = state in ("OPEN", "SQUAREOFF")
    post_close = trading_today and state == "POST_CLOSE"
    # Most recent completed NSE trading session per the exchange calendar;
    # today is completed only after the 15:30 IST close.
    latest_completed = (today if post_close
                        else last_trading_day(today, local_holidays))

    if parsed is None:
        parsed = None
        data_status = "UNKNOWN"
        warnings.append(
            "FII/DII source date is missing or unparseable"
            + (f" (raw value: {data_date!r})" if str(data_date or "").strip() else "")
            + "; the session date is not assumed"
        )
    elif parsed > today:
        data_status = "UNKNOWN"
        warnings.append(
            f"FII/DII date {parsed.isoformat()} is ahead of {today.isoformat()}; treated as UNKNOWN"
        )
    elif trading_today and parsed == today and in_progress:
        data_status = "CURRENT_SESSION"
        warnings.append(
            "FII/DII aggregates are published after the session; a current-session "
            "figure is provisional, not live intraday flow"
        )
    elif trading_today and parsed == today and state in ("PRE_MARKET", "PRE_OPEN"):
        data_status = "UNKNOWN"
        warnings.append(
            "today's daily FII/DII aggregates do not exist before the market opens; treated as UNKNOWN"
        )
    elif post_close and parsed == today:
        data_status = "END_OF_DAY"
    elif parsed == latest_completed:
        data_status = "PREVIOUS_SESSION"
    elif parsed < latest_completed:
        data_status = "STALE"
        warnings.append(
            f"FII/DII date {parsed.isoformat()} is older than the latest completed "
            f"trading session ({latest_completed.isoformat()}); historical context only"
        )
    else:
        data_status = "UNKNOWN"
        warnings.append(
            f"FII/DII date {parsed.isoformat()} does not match a recent trading session; treated as UNKNOWN"
        )

    # Trading-session distance (not calendar distance) relative to the latest
    # completed session, per the NSE exchange calendar.
    sessions_behind: int | None = None
    if data_status not in ("UNKNOWN",) and parsed is not None and parsed <= today:
        completed_between = _trading_sessions_between(parsed, latest_completed, local_holidays)
        sessions_behind = None if completed_between is None else len(completed_between)
        if completed_between is None:
            warnings.append(
                "FII/DII-to-latest-session gap exceeds ~one calendar year; "
                "exact completed-session count is not computed"
            )
    calendar_age_days = (today - parsed).days if parsed is not None and parsed <= today else None

    calendar_years = {latest_completed.year, today.year}
    if parsed is not None:
        calendar_years.add(parsed.year)
    coverage = _calendar_year_coverage(calendar_years, local_holidays)
    calendar_verified = all(coverage.values())
    if not calendar_verified:
        warnings.append(
            "NSE trading calendar is not verified for the years involved "
            + sorted(calendar_years).__repr__()
            + "; freshness cannot be confirmed and is reported as UNKNOWN"
        )

    if data_status == "UNKNOWN" or not calendar_verified:
        freshness_status = "UNKNOWN"
    elif sessions_behind == 0:
        freshness_status = "FRESH"
    elif sessions_behind == 1:
        freshness_status = "DELAYED"
        warnings.append(
            "FII/DII data is one completed trading session behind the latest "
            "(delayed publication); use as context, not the freshest completed-session figure"
        )
    else:  # sessions_behind > 1, or not computed (year-long gap)
        freshness_status = "STALE"
        if sessions_behind:
            warnings.append(
                f"FII/DII data is {sessions_behind} completed trading sessions behind "
                f"the latest completed session; historical context only"
            )
        else:
            warnings.append(
                "FII/DII data is far older than the latest completed trading session; "
                "historical context only"
            )

    market_session = today if data_status in ("CURRENT_SESSION", "END_OF_DAY") else latest_completed
    return {
        "data_date": parsed.isoformat() if parsed else None,
        "market_session_date": market_session.isoformat(),
        "latest_completed_session": latest_completed.isoformat(),
        "sessions_behind": sessions_behind,
        "calendar_age_days": calendar_age_days,
        "freshness_status": freshness_status,
        "data_status": data_status,
        # NSE publishes daily sums post-close; no live intraday feed exists here.
        "is_live": data_status == "LIVE",
        "is_previous_session": data_status in ("PREVIOUS_SESSION", "STALE"),
        "calendar_verified": calendar_verified,
        "calendar_years": sorted(coverage),
        "calendar_verified_years": sorted(year for year, ok in coverage.items() if ok),
        "session_note": FIIDII_STATUS_NOTES[data_status],
        "freshness_note": FIIDII_FRESHNESS_NOTES[freshness_status],
        "warnings": warnings,
    }


def _flow_float(value) -> float | None:
    try:
        return round(float(str(value).replace(",", "").strip()), 2)
    except (TypeError, ValueError):
        return None


def fii_dii_dataset(rows, now: dt.datetime | None = None) -> dict:
    """Normalize raw ``fiidiiTradeReact`` rows with their session metadata.

    The date travels with the numbers: the returned payload never contains an
    FII/DII net value without ``data_date``/``market_session_date``,
    ``retrieved_at``, ``data_status``, ``is_live`` and ``is_previous_session``
    so previous-session or stale figures cannot pass as live intraday flow.
    ``fii_net_cr`` / ``dii_net_cr`` / ``combined_net_cr`` are kept as legacy
    aliases (Rs crore) alongside the canonical ``fii_net`` / ``dii_net``.
    """
    now = now or now_ist()
    warnings: list[str] = []
    best: dict[str, dict] = {}
    for row in (rows if isinstance(rows, list) else []):
        if not isinstance(row, dict):
            continue
        category = str(row.get("category") or "").upper().strip().rstrip("*").strip()
        if category.startswith("FII"):
            key = "FII"
        elif category.startswith("DII"):
            key = "DII"
        else:
            continue
        current = best.get(key)
        if current is None or (parse_nse_flow_date(row.get("date")) or dt.date.min) > (
            parse_nse_flow_date(current.get("date")) or dt.date.min
        ):
            best[key] = row
    fii = best.get("FII") or {}
    dii = best.get("DII") or {}

    if not fii and not dii:
        meta = fii_dii_freshness(None, now)
        payload = {
            "date": None,
            **meta,
            "retrieved_at": now.isoformat(timespec="seconds"),
            "fii_net": None,
            "dii_net": None,
        }
        payload["warnings"] = [*warnings, *meta["warnings"], "no FII/DII records returned by the source"]
        fii_net = dii_net = None
    else:
        date_raw = fii.get("date") or dii.get("date")
        meta = fii_dii_freshness(date_raw, now)
        if fii.get("date") and dii.get("date") and str(fii.get("date")) != str(dii.get("date")):
            warnings.append(
                f"FII and DII rows carry different source dates "
                f"({fii.get('date')!r} vs {dii.get('date')!r}); the newer row per category is used"
            )
        fii_net = _flow_float(fii.get("netValue"))
        dii_net = _flow_float(dii.get("netValue"))
        payload = {
            "date": date_raw,
            **meta,
            "retrieved_at": now.isoformat(timespec="seconds"),
            "fii_net": fii_net,
            "dii_net": dii_net,
            "fii_buy_cr": _flow_float(fii.get("buyValue")),
            "fii_sell_cr": _flow_float(fii.get("sellValue")),
            "dii_buy_cr": _flow_float(dii.get("buyValue")),
            "dii_sell_cr": _flow_float(dii.get("sellValue")),
        }
        payload["warnings"] = [*warnings, *meta["warnings"]]

    combined = None if fii_net is None and dii_net is None else round(
        (fii_net or 0.0) + (dii_net or 0.0), 2
    )
    payload.update({
        "combined_net_cr": combined,
        # Legacy aliases used by earlier snapshots (Rs crore).
        "fii_net_cr": fii_net,
        "dii_net_cr": dii_net,
    })
    return payload


def fii_dii_freshness_line(payload: dict) -> str:
    """One-line freshness summary distinguishing calendar age from sessions."""
    status = payload.get("freshness_status") or "UNKNOWN"
    behind = payload.get("sessions_behind")
    age = payload.get("calendar_age_days")
    extras = ""
    if payload.get("calendar_verified") is False:
        extras += " | calendar: unverified"
    return (
        f"Freshness: {FIIDII_FRESHNESS_NOTES[status]}"
        f" | Calendar age: {age if age is not None else 'unknown'} day(s)"
        f" | Trading sessions behind: {behind if behind is not None else 'unknown'}"
        f" | Live intraday flow: No{extras}"
    )


def fii_dii_display_lines(payload: dict) -> list[str]:
    """Markdown lines where every FII/DII number carries its session date.

    Values are never reported without a human-readable trading-session label,
    the data status and the trading-session freshness summary.
    """
    label = format_nse_flow_session(payload.get("data_date") or payload.get("date"))
    status = payload.get("data_status") or "UNKNOWN"
    lines = [
        f"- Data session: **{label}** (data_date {payload.get('data_date') or 'UNKNOWN'}; {status})",
        f"- {fii_dii_freshness_line(payload)}",
    ]
    for name, key in (
        ("FII/FPI net", "fii_net_cr"),
        ("DII net", "dii_net_cr"),
        ("Combined", "combined_net_cr"),
    ):
        value = payload.get(key)
        shown = f"Rs {value:+,.2f} crore" if value is not None else "value unavailable"
        lines.append(f"- {name}: {shown} | Session: {label}")
    return lines


# --------------------------------------------------------------------------
# Broker/statutory charge model (estimates; verify against the contract note)
# --------------------------------------------------------------------------


BROKER_PROFILES = {
    # Portable placeholder, not guaranteed conservative for every broker.
    # Users should provide their actual brokerage rule.
    "generic": {
        "name": "generic-flat-20",
        "cash_intraday": {"flat": 20.0},
        "cash_delivery": {"flat": 20.0},
        "futures": {"flat": 20.0},
        "options": {"flat": 20.0},
    },
    "groww": {
        "name": "groww-2026-11-01",
        "cash_intraday": {"rate": 0.001, "minimum": 5.0, "maximum": 20.0},
        "cash_delivery": {"rate": 0.001, "minimum": 5.0, "maximum": 20.0},
        "futures": {"flat": 20.0},
        "options": {"flat": 20.0},
    },
}


def resolve_broker_profile(broker: str = "generic", custom: dict | None = None) -> dict:
    """Resolve and validate a built-in or custom brokerage profile.

    A custom profile may override any of ``cash_intraday``, ``cash_delivery``,
    ``futures`` and ``options``. Each rule uses either ``flat`` or ``rate``
    with optional ``minimum`` / ``maximum`` values.
    """
    key = str(broker or "generic").strip().lower()
    if key not in BROKER_PROFILES:
        raise ValueError(f"unknown broker profile {broker!r}; use generic, groww, or a custom file")
    base = BROKER_PROFILES[key]
    profile = {
        "name": base["name"],
        **{name: dict(base[name]) for name in (
            "cash_intraday", "cash_delivery", "futures", "options"
        )},
    }
    if custom:
        if not isinstance(custom, dict):
            raise ValueError("custom broker profile must be a JSON object")
        profile["name"] = str(custom.get("name") or "custom")
        for name in ("cash_intraday", "cash_delivery", "futures", "options"):
            if name in custom:
                if not isinstance(custom[name], dict):
                    raise ValueError(f"broker rule {name} must be an object")
                profile[name] = dict(custom[name])
    for name in ("cash_intraday", "cash_delivery", "futures", "options"):
        rule = profile[name]
        if "flat" not in rule and "rate" not in rule:
            raise ValueError(f"broker rule {name} needs flat or rate")
        for field in ("flat", "rate", "minimum", "maximum"):
            if field in rule and float(rule[field]) < 0:
                raise ValueError(f"broker rule {name}.{field} cannot be negative")
    return profile


def load_broker_profile(path: str | None) -> dict | None:
    if not path:
        return None
    profile = load_json(path)
    if profile is None:
        raise ValueError(f"could not read broker profile: {path}")
    return profile


def _brokerage(turnover: float, rule: dict) -> float:
    if "flat" in rule:
        return float(rule["flat"])
    amount = float(rule.get("rate", 0.0)) * turnover
    if "minimum" in rule:
        amount = max(float(rule["minimum"]), amount)
    if "maximum" in rule:
        amount = min(float(rule["maximum"]), amount)
    return amount


def broker_charges(segment: str, side: str, qty: float, price: float,
                   product: str = "MIS", exchange: str = "NSE",
                   broker: str = "generic", broker_profile: dict | None = None) -> dict:
    """Estimate one-leg Indian broker/statutory charges in INR.

    The default generic profile assumes Rs 20 brokerage per executed order.
    The optional ``groww`` profile uses Groww's published 2026 schedule.
    A custom JSON profile can model another broker's brokerage while retaining
    the exchange/statutory estimate below.

    Equity regulatory estimate: intraday STT 0.025% sell, stamp 0.003% buy,
    NSE transaction charge 0.00297%, SEBI 0.0001%, IPFT 0.0001%, and GST 18%
    on brokerage + transaction + SEBI + IPFT.
    F&O regulatory estimate (retrieved 2026-11-01):
    futures STT 0.05% sell, options STT 0.15% of premium sell, futures NSE
    transaction charge 0.00173%, options 0.03503% of premium, stamp 0.002%
    futures / 0.003% options buy, and GST 18%.
    DP charges on delivery sells are excluded (billed separately).
    """
    seg = str(segment).upper()
    side = str(side).upper()
    prod = str(product).upper()
    turnover = float(qty) * float(price)
    profile = resolve_broker_profile(broker, broker_profile)
    if seg not in ("CASH", "FNO"):
        raise ValueError(f"unknown segment {segment!r}")
    if side not in ("BUY", "SELL"):
        raise ValueError(f"unknown transaction side {side!r}")

    if seg == "CASH":
        if prod == "MIS":
            brokerage = _brokerage(turnover, profile["cash_intraday"])
            stt = 0.00025 * turnover if side == "SELL" else 0.0
            stamp = 0.00003 * turnover if side == "BUY" else 0.0
        else:  # CNC / delivery
            brokerage = _brokerage(turnover, profile["cash_delivery"])
            stt = 0.001 * turnover
            stamp = 0.00015 * turnover if side == "BUY" else 0.0
        exch = (0.0000297 if exchange == "NSE" else 0.0000345) * turnover
        sebi = 0.000001 * turnover
        ipft = 0.000001 * turnover
    else:  # FNO (futures/options), turnover = contract value or premium value
        is_opt = "OPT" in prod
        if is_opt:
            brokerage = _brokerage(turnover, profile["options"])
            stt = 0.0015 * turnover if side == "SELL" else 0.0
            stamp = 0.00003 * turnover if side == "BUY" else 0.0
            exch = 0.0003503 * turnover
            ipft = 0.000005 * turnover
        else:
            brokerage = _brokerage(turnover, profile["futures"])
            stt = 0.0005 * turnover if side == "SELL" else 0.0
            stamp = 0.00002 * turnover if side == "BUY" else 0.0
            exch = 0.0000173 * turnover
            ipft = 0.000001 * turnover
        sebi = 0.000001 * turnover

    gst = 0.18 * (brokerage + exch + sebi + ipft)
    total = brokerage + stt + stamp + exch + sebi + ipft + gst
    return {
        "broker_profile": profile["name"],
        "schedule_date": CHARGE_SCHEDULE_DATE,
        "turnover": round(turnover, 2),
        "brokerage": round(brokerage, 2),
        "stt": round(stt, 2),
        "stamp": round(stamp, 2),
        "exchange": round(exch, 2),
        "sebi": round(sebi, 2),
        "ipft": round(ipft, 2),
        "gst": round(gst, 2),
        "total": round(total, 2),
    }


def groww_charges(segment: str, side: str, qty: float, price: float,
                  product: str = "MIS", exchange: str = "NSE") -> dict:
    """Backward-compatible wrapper for the optional Groww profile."""
    return broker_charges(
        segment, side, qty, price, product, exchange, broker="groww"
    )


def round_trip_charges(segment: str, qty: float, entry: float, exit_price: float,
                       product: str = "MIS", broker: str = "generic",
                       broker_profile: dict | None = None) -> dict:
    """Buy at entry + sell at exit_price; returns summed charges."""
    buy = broker_charges(
        segment, "BUY", qty, entry, product, broker=broker,
        broker_profile=broker_profile,
    )
    sell = broker_charges(
        segment, "SELL", qty, exit_price, product, broker=broker,
        broker_profile=broker_profile,
    )
    return {
        "broker_profile": buy["broker_profile"],
        "schedule_date": CHARGE_SCHEDULE_DATE,
        "buy": buy,
        "sell": sell,
        "total": round(buy["total"] + sell["total"], 2),
    }


# --------------------------------------------------------------------------
# Misc helpers
# --------------------------------------------------------------------------


def round_tick(price: float, mode: str = "nearest") -> float:
    """Round to the conservative Rs 0.05 step. mode: nearest|down|up."""
    if not price:
        return 0.0
    x = float(price) / TICK
    if mode == "down":
        return math.floor(x + 1e-9) * TICK
    if mode == "up":
        return math.ceil(x - 1e-9) * TICK
    return round(x) * TICK


def state_path(name: str) -> str:
    os.makedirs(STATE_DIR, exist_ok=True)
    return os.path.join(STATE_DIR, name)


def load_json(path: str, default=None):
    try:
        # utf-8-sig accepts ordinary UTF-8 plus the BOM Windows PowerShell can
        # add when users create a rules file with Set-Content.
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path: str, obj) -> str:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, default=str)
    return path


def append_event(kind: str, payload: dict) -> None:
    rec = {"ts_ist": now_ist().isoformat(timespec="seconds"), "kind": kind, **payload}
    with open(state_path("events.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, default=str) + "\n")


def fmt_inr(x: float) -> str:
    return f"Rs {x:,.2f}"


def pct(x: float, digits: int = 1) -> str:
    return f"{x:+.{digits}f}%"


def json_cli_dump(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))
