"""5-minute and 1-minute bar archive for NIFTY, SENSEX and India VIX (for backtests).

Free Yahoo data only reaches back ~60 days (5-minute) and ~30 days (1-minute), so
every run saves what it sees into data/bars/<SYMBOL>/<YYYY-MM>.json (5m) and
data/bars1m/<SYMBOL>/<YYYY-MM>.json (1m), and the history grows by itself.
Older data (e.g. a year of 5-minute candles exported from Zerodha Kite) can be
imported from CSV.

    python history.py update [--range 60d]       # fetch and merge the latest bars
    python history.py seed1m                     # back-fill ~30 days of 1-minute bars
    python history.py import FILE --symbol NIFTY [--interval 1m]
                                                 # CSV: datetime,open,high,low,close[,volume]
    python history.py info                       # what the archive holds
"""
import argparse
import csv
import datetime as dt
import json
import os
import urllib.parse

import paper
from paper import nsemkt, IST

ARCHIVES = {"5m": os.path.join(paper.HERE, "data", "bars"), "1m": os.path.join(paper.HERE, "data", "bars1m")}
SYMBOLS = {"NIFTY": "^NSEI", "SENSEX": "^BSESN", "INDIAVIX": "^INDIAVIX"}
SESSION = (dt.time(9, 15), dt.time(15, 30))


def _month_file(sym, ym, interval="5m"):
    return os.path.join(ARCHIVES[interval], sym, f"{ym}.json")


def _in_session(t):
    return t.weekday() < 5 and SESSION[0] <= t.time() < SESSION[1]


def load(sym, start=None, end=None, interval="5m"):
    """Bars as dicts {t,o,h,l,c,v}, sorted, optionally limited to [start, end] dates."""
    folder = os.path.join(ARCHIVES[interval], sym)
    if not os.path.isdir(folder):
        return []
    out = []
    for name in sorted(os.listdir(folder)):
        ym = name[:7]
        if start and ym < start.strftime("%Y-%m"):
            continue
        if end and ym > end.strftime("%Y-%m"):
            continue
        with open(os.path.join(folder, name), encoding="utf-8") as f:
            for ts, o, h, l, c, v in json.load(f):
                t = dt.datetime.fromtimestamp(ts, IST)
                if (start and t.date() < start) or (end and t.date() > end):
                    continue
                out.append({"t": t, "o": o, "h": h, "l": l, "c": c, "v": v})
    return out


def merge(sym, bars, interval="5m"):
    """Add bars to the archive (regular session only); returns how many were new."""
    by_month = {}
    for b in bars:
        if _in_session(b["t"]):
            by_month.setdefault(b["t"].strftime("%Y-%m"), []).append(b)
    added = 0
    for ym, new in by_month.items():
        path = _month_file(sym, ym, interval)
        rows = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                rows = {r[0]: r for r in json.load(f)}
        before = len(rows)
        for b in new:
            ts = int(b["t"].timestamp())
            rows[ts] = [ts, round(b["o"], 2), round(b["h"], 2), round(b["l"], 2), round(b["c"], 2), int(b.get("v") or 0)]
        added += len(rows) - before
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump([rows[k] for k in sorted(rows)], f, separators=(",", ":"))
    return added


def merged(sym, fresh, days=10, interval="5m"):
    """The archive's last `days` calendar days plus freshly fetched bars (fresh wins)."""
    start = (dt.datetime.now(IST) - dt.timedelta(days=days)).date()
    by_t = {b["t"]: b for b in load(sym, start=start, interval=interval)}
    for b in fresh:
        if _in_session(b["t"]):
            by_t[b["t"]] = b
    return [by_t[t] for t in sorted(by_t)]


def update(range_="5d"):
    """Fetch the latest 5m and 1m bars and keep only completed ones (a forming bar is skipped)."""
    now = dt.datetime.now(IST)
    for interval, rng, mins in (("5m", range_, 5), ("1m", "5d", 1)):
        for sym, ysym in SYMBOLS.items():
            try:
                bars = nsemkt.yahoo_chart(ysym, interval=interval, range_=rng)["bars"]
            except Exception as e:
                print(f"history {sym} {interval}: {e}")
                continue
            done = [b for b in bars if b["t"] + dt.timedelta(minutes=mins) <= now]
            print(f"history {sym} {interval}: +{merge(sym, done, interval)} bars")


def _yahoo_period(ysym, interval, p1, p2):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ysym)}"
           f"?interval={interval}&period1={p1}&period2={p2}&includePrePost=false")
    res = (nsemkt.http_json(url).get("chart") or {}).get("result") or []
    if not res:
        return []
    r = res[0]
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    vols = q.get("volume") or []
    out = []
    for i, ts in enumerate(r.get("timestamp") or []):
        try:
            o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        except (KeyError, IndexError):
            continue
        if None in (o, h, l, c):
            continue
        out.append({"t": dt.datetime.fromtimestamp(ts, IST), "o": o, "h": h, "l": l, "c": c,
                    "v": (vols[i] if i < len(vols) else 0) or 0})
    return out


def seed1m(days=30):
    """Yahoo serves 1-minute bars for ~30 days, at most 7 days per request."""
    now = int(dt.datetime.now(IST).timestamp())
    for sym, ysym in SYMBOLS.items():
        total = 0
        for back in range(days, 0, -7):
            p1 = now - back * 86400
            try:
                total += merge(sym, _yahoo_period(ysym, "1m", p1, min(p1 + 7 * 86400, now)), "1m")
            except Exception as e:
                print(f"seed1m {sym}: {e}")
        print(f"seed1m {sym}: +{total} bars")


def import_csv(path, sym, interval="5m"):
    """CSV with a date/time column and open, high, low, close (volume optional).
    Times are taken as IST. Accepts 'date' + 'time' columns or one 'datetime' column."""
    bars = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            r = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            stamp = r.get("datetime") or r.get("timestamp") or r.get("date", "")
            if r.get("time") and "date" in r and not r.get("datetime"):
                stamp = f"{r['date']} {r['time']}"
            stamp = stamp.replace("T", " ").split("+")[0].strip()
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M", "%d-%m-%Y %H:%M:%S"):
                try:
                    t = dt.datetime.strptime(stamp, fmt).replace(tzinfo=IST)
                    break
                except ValueError:
                    t = None
            if t is None:
                continue
            bars.append({"t": t, "o": float(r["open"]), "h": float(r["high"]), "l": float(r["low"]),
                         "c": float(r["close"]), "v": float(r.get("volume") or 0)})
    print(f"import {sym} {interval}: {len(bars)} rows read, +{merge(sym, bars, interval)} new bars")


def info(interval="5m"):
    out = {}
    for sym in SYMBOLS:
        bars = load(sym, interval=interval)
        days = sorted({b["t"].date() for b in bars})
        out[sym] = {"bars": len(bars), "sessions": len(days),
                    "first": days[0].isoformat() if days else None,
                    "last": days[-1].isoformat() if days else None}
    return out


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("update")
    u.add_argument("--range", default="5d")
    i = sub.add_parser("import")
    i.add_argument("file")
    i.add_argument("--symbol", required=True, choices=list(SYMBOLS))
    i.add_argument("--interval", default="5m", choices=list(ARCHIVES))
    sub.add_parser("info")
    sub.add_parser("seed1m")
    a = p.parse_args()
    if a.cmd == "update":
        update(a.range)
    elif a.cmd == "import":
        import_csv(a.file, a.symbol, a.interval)
    elif a.cmd == "seed1m":
        seed1m()
    else:
        print(json.dumps({"5m": info("5m"), "1m": info("1m")}, indent=2))
