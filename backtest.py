"""Backtest the option strategies over every session in the bar archive.

    python backtest.py                      # summary over the whole archive
    python backtest.py --from 2026-09-01 --to 2026-10-31 --group month
    python backtest.py --export docs/data/backtest.json

The export holds one compact record per trade; the dashboard's Backtest tab
groups it by day / week / month / year and filters any date range in the browser.
"""
import argparse
import collections
import datetime as dt
import json
import os

import history
import options
import paper

WARMUP_DAYS = 6  # calendar days of earlier bars so the 9/21 EMAs are settled at 09:15


def run(start=None, end=None):
    und_all = {u: history.load(u) for u in options.OPT["underlyings"]}
    vix_all = history.load("INDIAVIX")
    vix_by_day = collections.defaultdict(list)
    for b in vix_all:
        vix_by_day[b["t"].date()].append(b)
    days = sorted({b["t"].date() for b in und_all["NIFTY"]})
    today = paper.now_ist().date()
    days = [d for d in days if d < today and (not start or d >= start) and (not end or d <= end)]
    out, past = [], []
    for d in days:
        paused = options.circuit_breaker(d, past)
        if paused:
            out.append({"date": d.isoformat(), "trades": [], "paused": paused})
            continue
        lo = d - dt.timedelta(days=WARMUP_DAYS)
        hist = {u: [b for b in bars if lo <= b["t"].date() <= d] for u, bars in und_all.items()}
        res = options.simulate_options_day(d, hist, vix_by_day.get(d, []))
        out.append({"date": d.isoformat(), "trades": [compact(t) for t in res["trades"]]})
        past.append((d, sum(t["net_pnl"] for t in res["trades"])))
    return out


def compact(t):
    return {"s": t["strategy"], "u": t["underlying"], "side": t["side"], "n": t["trade_no"],
            "lots": t["lots"], "in": t["entry_time"][11:16], "prem": t["entry_premium"],
            "out": t.get("exit_time", "")[11:16], "why": t.get("exit_reason", ""),
            "legs": [[l["lots"], l["reason"]] for l in t["legs"]],
            "net": t["net_pnl"], "chg": t["charges"]}


def period_key(date_s, group):
    d = dt.date.fromisoformat(date_s)
    if group == "week":
        y, w, _ = d.isocalendar()
        return f"{y}-W{w:02d}"
    return {"day": date_s, "month": date_s[:7], "year": date_s[:4]}[group]


def summarise(days, group="month"):
    rows = collections.OrderedDict()
    for day in days:
        k = period_key(day["date"], group)
        r = rows.setdefault(k, {"trades": 0, "wins": 0, "net": 0.0, "charges": 0.0})
        for t in day["trades"]:
            r["trades"] += 1
            r["wins"] += t["net"] > 0
            r["net"] += t["net"]
            r["charges"] += t["chg"]
    return rows


def export(path, days):
    info = history.info()
    payload = {
        "generated": paper.now_ist().isoformat(timespec="seconds"),
        "capital": options.OPT["capital"],
        "archive": info,
        "rules": {"lots": options.OPT["lots"], "book": options.OPT["book"],
                  "day_loss_limit": options.OPT["day_loss_limit"], "points": options.OPT["points"]},
        "days": days,
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    return payload


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from", dest="start")
    p.add_argument("--to", dest="end")
    p.add_argument("--group", default="month", choices=["day", "week", "month", "year"])
    p.add_argument("--export")
    a = p.parse_args()
    days = run(dt.date.fromisoformat(a.start) if a.start else None, dt.date.fromisoformat(a.end) if a.end else None)
    if a.export:
        export(a.export, days)
        print(f"exported {len(days)} sessions -> {a.export}")
    total = collections.Counter()
    by_strat = collections.defaultdict(lambda: [0, 0, 0.0])
    for day in days:
        for t in day["trades"]:
            s = by_strat[(t["s"], t["u"])]
            s[0] += 1
            s[1] += t["net"] > 0
            s[2] += t["net"]
    eq = peak = options.OPT["capital"]
    dd = 0.0
    for day in days:
        eq += sum(t["net"] for t in day["trades"])
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    for k, r in summarise(days, a.group).items():
        print(f"{k:<10} trades {r['trades']:>3}  wins {r['wins']:>3}  net Rs {r['net']:>10,.0f}  charges Rs {r['charges']:>8,.0f}")
    print()
    for (s, u), (n, w, net) in sorted(by_strat.items()):
        print(f"{s:<8} {u:<7} trades {n:>3}  win {w / n if n else 0:>4.0%}  net Rs {net:>10,.0f}")
    print(f"\n{len(days)} sessions: equity Rs {options.OPT['capital']:,.0f} -> Rs {eq:,.0f}, max drawdown Rs {dd:,.0f}")
