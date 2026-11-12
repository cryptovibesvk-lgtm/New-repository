"""5-minute bar archive for NIFTY, SENSEX and India VIX (for backtests).

Free Yahoo 5-minute data only reaches back ~60 days, so every run saves what it
sees into data/bars/<SYMBOL>/<YYYY-MM>.json and the history grows by itself.
Older data (e.g. a year of 5-minute candles exported from Zerodha Kite) can be
imported from CSV.

    python history.py update [--range 60d]       # fetch and merge the latest bars
    python history.py import FILE --symbol NIFTY # CSV: datetime,open,high,low,close[,volume]
    python history.py info                       # what the archive holds
"""
import argparse
import csv
import datetime as dt
import json
import os

import paper
from paper import nsemkt, IST

ARCHIVE = os.path.join(paper.HERE, "data", "bars")
SYMBOLS = {"NIFTY": "^NSEI", "SENSEX": "^BSESN", "INDIAVIX": "^INDIAVIX"}
SESSION = (dt.time(9, 15), dt.time(15, 30))


def _month_file(sym, ym):
    return os.path.join(ARCHIVE, sym, f"{ym}.json")


def _in_session(t):
    return t.weekday() < 5 and SESSION[0] <= t.time() < SESSION[1]


def load(sym, start=None, end=None):
    """Bars as dicts {t,o,h,l,c,v}, sorted, optionally limited to [start, end] dates."""
    folder = os.path.join(ARCHIVE, sym)
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


def merge(sym, bars):
    """Add bars to the archive (regular session only); returns how many were new."""
    by_month = {}
    for b in bars:
        if _in_session(b["t"]):
            by_month.setdefault(b["t"].strftime("%Y-%m"), []).append(b)
    added = 0
    for ym, new in by_month.items():
        path = _month_file(sym, ym)
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


def merged(sym, fresh, days=10):
    """The archive's last `days` calendar days plus freshly fetched bars (fresh wins)."""
    start = (dt.datetime.now(IST) - dt.timedelta(days=days)).date()
    by_t = {b["t"]: b for b in load(sym, start=start)}
    for b in fresh:
        if _in_session(b["t"]):
            by_t[b["t"]] = b
    return [by_t[t] for t in sorted(by_t)]


def update(range_="5d"):
    """Fetch the latest bars and keep only completed ones (a forming bar is skipped)."""
    now = dt.datetime.now(IST)
    for sym, ysym in SYMBOLS.items():
        try:
            bars = nsemkt.yahoo_chart(ysym, interval="5m", range_=range_)["bars"]
        except Exception as e:
            print(f"history {sym}: {e}")
            continue
        done = [b for b in bars if b["t"] + dt.timedelta(minutes=5) <= now]
        print(f"history {sym}: +{merge(sym, done)} bars")


def import_csv(path, sym):
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
    print(f"import {sym}: {len(bars)} rows read, +{merge(sym, bars)} new bars")


def info():
    out = {}
    for sym in SYMBOLS:
        bars = load(sym)
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
    sub.add_parser("info")
    a = p.parse_args()
    if a.cmd == "update":
        update(a.range)
    elif a.cmd == "import":
        import_csv(a.file, a.symbol)
    else:
        print(json.dumps(info(), indent=2))
