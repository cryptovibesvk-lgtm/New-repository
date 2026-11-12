"""Import a year (or more) of candles from Upstox's public historical-candle API.

Upstox serves historical 1-minute and 5-minute candles for NSE/BSE indices and stocks
without a login, so the backtest can cover a full year. Read-only market data.

    python upstox.py --days 400                # NIFTY, SENSEX, INDIA VIX: 1m + 5m
    python upstox.py --days 400 --stocks       # also the Nifty 50 stocks (5m)
    python upstox.py --from 2025-09-01 --to 2026-11-12
"""
import argparse
import datetime as dt
import gzip
import json
import time
import urllib.parse
import urllib.request

import history
from paper import IST

API = "https://api.upstox.com/v3/historical-candle"
MASTER = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
INDEX_KEYS = {"NIFTY": "NSE_INDEX|Nifty 50", "SENSEX": "BSE_INDEX|SENSEX", "INDIAVIX": "NSE_INDEX|India VIX"}
UA = {"Accept": "application/json", "User-Agent": "nse-paper-trader"}


def _get(url):
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
                raw = r.read()
            return raw
        except Exception as e:
            if attempt == 3:
                raise
            time.sleep(1.5 * (attempt + 1))   # rate limit or a hiccup: back off and retry
    return None


def candles(key, minutes, start, end):
    """All candles for one instrument, fetched a month at a time (the API's limit)."""
    out, cur = [], start
    while cur <= end:
        stop = min(end, (cur.replace(day=1) + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1))
        url = f"{API}/{urllib.parse.quote(key, safe='')}/minutes/{minutes}/{stop}/{cur}"
        data = json.loads(_get(url)).get("data") or {}
        for ts, o, h, l, c, v, *_ in data.get("candles") or []:
            out.append({"t": dt.datetime.fromisoformat(ts).astimezone(IST), "o": o, "h": h, "l": l, "c": c, "v": v or 0})
        cur = stop + dt.timedelta(days=1)
        time.sleep(0.25)
    return out


def stock_keys(symbols):
    """NSE equity instrument keys (NSE_EQ|<ISIN>) from Upstox's public instrument list."""
    rows = json.loads(gzip.decompress(_get(MASTER)))
    want = set(symbols)
    return {r["trading_symbol"]: r["instrument_key"] for r in rows
            if r.get("segment") == "NSE_EQ" and r.get("instrument_type") == "EQ" and r.get("trading_symbol") in want}


def run(start, end, stocks=False):
    for sym, key in INDEX_KEYS.items():
        for minutes, interval in ((1, "1m"), (5, "5m")):
            try:
                bars = candles(key, minutes, start, end)
            except Exception as e:
                print(f"{sym} {interval}: {e}")
                continue
            print(f"{sym} {interval}: {len(bars)} candles, +{history.merge(sym, bars, interval)} new", flush=True)
    if stocks:
        import backtest
        with open(backtest.UNIVERSE, encoding="utf-8-sig") as f:
            universe = [s.strip() for s in f if s.strip() and not s.startswith("#")]
        keys = stock_keys(universe)
        missing = sorted(set(universe) - set(keys))
        if missing:
            print("no Upstox key for:", ", ".join(missing))
        for sym in universe:
            if sym not in keys:
                continue
            try:
                bars = candles(keys[sym], 5, start, end)
            except Exception as e:
                print(f"{sym}: {e}")
                continue
            print(f"{sym} 5m: {len(bars)} candles, +{history.merge(sym, bars, '5m')} new", flush=True)
    print(json.dumps({"5m": history.info("5m"), "1m": history.info("1m")}, indent=1))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=400)
    p.add_argument("--from", dest="start")
    p.add_argument("--to", dest="end")
    p.add_argument("--stocks", action="store_true")
    a = p.parse_args()
    end = dt.date.fromisoformat(a.end) if a.end else dt.datetime.now(IST).date()
    start = dt.date.fromisoformat(a.start) if a.start else end - dt.timedelta(days=a.days)
    run(start, end, a.stocks)
