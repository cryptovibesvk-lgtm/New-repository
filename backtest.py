"""Backtests with full trade details, for the dashboard's Backtest tab.

    python backtest.py                         # index options over the whole archive
    python backtest.py --stocks                # stock ORB over Nifty 50 (last ~60 days)
    python backtest.py --from 2026-10-01 --to 2026-10-31 --group week
    python backtest.py --export docs/data/backtest.json

Index options: the same code as the live book (options.py) replayed on every session in
the bar archive, with the weekly schedule (Mon/Fri NIFTY ORB1, Tue NIFTY SELL_TIME,
Wed/Thu SENSEX EMA9_21) and the loss stops. ORB1 needs 1-minute candles: free data keeps
~30 days, `python kite.py history --days 365` imports a year from Zerodha.

Stocks: the stock ORB (paper.simulate_day) on every Nifty 50 stock each day, taking the
first 3 breakouts - a fixed universe, so no hindsight from today's scan.
"""
import argparse
import collections
import datetime as dt
import json
import os

import history
import options
import paper

WARMUP_DAYS = 6  # calendar days of earlier 5-minute bars so the 9/21 EMAs are settled at 09:15
UNIVERSE = os.path.join(paper.SCANNER_DIR, "..", "assets", "nifty50.txt")


def run(start=None, end=None):
    und5 = {u: history.load(u) for u in options.OPT["underlyings"]}
    und1 = {u: history.load(u, interval="1m") for u in options.OPT["underlyings"]}
    by_day = lambda bars: _group(bars)
    one_min = {u: by_day(b) for u, b in und1.items()}
    vix_by_day = by_day(history.load("INDIAVIX"))
    days = sorted({b["t"].date() for b in und5["NIFTY"]} | {b["t"].date() for b in und1["NIFTY"]})
    today = paper.now_ist().date()
    days = [d for d in days if d < today and d.weekday() < 5
            and (not start or d >= start) and (not end or d <= end)]
    out, past = [], []
    for d in days:
        idx = options.index_for_day(d)
        rec = {"date": d.isoformat(), "dow": options.DAY_NAMES[d.weekday()], "index": idx, "trades": []}
        paused = options.circuit_breaker(d, past)
        if paused:
            rec["paused"] = paused
            out.append(rec)
            continue
        lo = d - dt.timedelta(days=WARMUP_DAYS)
        h5 = {u: [b for b in bars if lo <= b["t"].date() <= d] for u, bars in und5.items()}
        h1 = {u: one_min[u].get(d, []) for u in und1}
        if not h1.get(idx):
            rec["no1m"] = True
        res = options.simulate_options_day(d, h5, h1, vix_by_day.get(d, []))
        rec["trades"] = [detail(t) for t in res["trades"]]
        out.append(rec)
        past.append((d, sum(t["net_pnl"] for t in res["trades"])))
    return out


def _group(bars):
    g = collections.defaultdict(list)
    for b in bars:
        g[b["t"].date()].append(b)
    return g


def detail(t):
    """Everything about one option trade, for the trade log and CSV."""
    return {
        "s": t["strategy"], "u": t["underlying"], "side": t["side"], "n": t["trade_no"],
        "act": t.get("action", "BUY"), "win": t.get("window", ""),
        "contract": t["symbol"], "strike": t["strike"], "expiry": t["expiry"],
        "qty": t["qty"], "lots": t["lots"],
        "in": t["entry_time"][11:16], "in_idx": t["entry_index"], "in_prem": t["entry_premium"],
        "sl_idx": t["initial_stop"], "sl_prem": t["sl_premium"], "final_sl": t["stop_index"],
        "best": t.get("best_points", 0),
        "out": t.get("exit_time", "")[11:16], "out_idx": t.get("exit_index"), "out_prem": t.get("exit"),
        "why": t.get("exit_reason", ""), "reason": t.get("why", ""),
        "legs": [[l["time"][11:16], l["qty"], l["index"], l["premium"], l["reason"]] for l in t["legs"]],
        "moves": [[m["time"][11:16], m["stop"], m["why"]] for m in t.get("stop_moves", [])],
        "gross": t["gross_pnl"], "chg": t["charges"], "net": t["net_pnl"],
    }


def run_stocks(start=None, end=None):
    with open(UNIVERSE, encoding="utf-8-sig") as f:
        universe = [s.strip() for s in f if s.strip() and not s.startswith("#")]
    charts = {}
    for s in universe:
        # the archive (e.g. a year imported from Kite) topped up with Yahoo's last ~60 days
        by_t = {b["t"]: b for b in history.load(s)}
        try:
            for b in paper.nsemkt.yahoo_chart(s, interval="5m", range_="60d")["bars"]:
                by_t[b["t"]] = b
        except Exception as e:
            print(f"stocks {s}: {e}")
        if by_t:
            charts[s] = _group([by_t[t] for t in sorted(by_t)])
    days = sorted({d for g in charts.values() for d in g})
    today = paper.now_ist().date()
    days = [d for d in days if d < today and (not start or d >= start) and (not end or d <= end)]
    capital, out = paper.CONFIG["start_capital"], []
    for d in days:
        bars = {s: {b["t"]: b for b in g.get(d, []) if dt.time(9, 15) <= b["t"].time() < dt.time(15, 30)}
                for s, g in charts.items()}
        res = paper.simulate_day(d, {s: b for s, b in bars.items() if b}, capital)
        trades = [{"sym": t["symbol"], "qty": t["qty"], "in": t["entry_time"][11:16], "in_px": t["entry"],
                   "sl": t["stop"], "tgt": t["target"], "or_hi": t["or_high"], "or_lo": t["or_low"],
                   "out": t.get("exit_time", "")[11:16], "out_px": t.get("exit"), "why": t.get("exit_reason", ""),
                   "gross": t["gross_pnl"], "chg": t["charges"], "net": t["net_pnl"]}
                  for t in res["trades"]]
        capital += sum(t["net"] for t in trades)
        out.append({"date": d.isoformat(), "dow": options.DAY_NAMES[d.weekday()], "trades": trades})
    return out, len(charts)


def period_key(date_s, group):
    d = dt.date.fromisoformat(date_s)
    if group == "week":
        y, w, _ = d.isocalendar()
        return f"{y}-W{w:02d}"
    return {"day": date_s, "month": date_s[:7], "year": date_s[:4]}[group]


def export(path, index_days, stocks=None):
    """stocks: (days, universe_size) or None to keep what the file already has."""
    old = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                old = json.load(f)
        except (OSError, ValueError):
            old = {}
    payload = {
        "generated": paper.now_ist().isoformat(timespec="seconds"),
        "index": {"capital": options.OPT["capital"], "archive5": history.info("5m"),
                  "archive1": history.info("1m"), "days": index_days,
                  "rules": {"orb1": options.OPT["orb1"], "schedule": options.schedule_text(),
                            "sell": {"lots": options.OPT["sell"]["lots"], "pair_stop": options.OPT["sell"]["pair_stop"]},
                            "day_loss_limit": options.OPT["day_loss_limit"],
                            "week_loss_limit": options.OPT["week_loss_limit"],
                            "month_loss_limit": options.OPT["month_loss_limit"]}},
        "stocks": old.get("stocks"),
    }
    if stocks is not None:
        days, n = stocks
        first = days[0]["date"] if days else None
        payload["stocks"] = {"capital": paper.CONFIG["start_capital"], "universe": f"Nifty 50 ({n} stocks)", "first": first,
                             "generated_for": paper.now_ist().date().isoformat(), "days": days}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    return payload


def _print(days, group, capital, label):
    rows = collections.OrderedDict()
    eq = peak = capital
    dd = 0.0
    for day in days:
        r = rows.setdefault(period_key(day["date"], group), [0, 0, 0.0])
        for t in day["trades"]:
            r[0] += 1
            r[1] += t["net"] > 0
            r[2] += t["net"]
        eq += sum(t["net"] for t in day["trades"])
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    for k, (n, w, net) in rows.items():
        print(f"{k:<10} trades {n:>3}  wins {w:>3}  net Rs {net:>10,.0f}")
    print(f"{label}: {len(days)} sessions, Rs {capital:,.0f} -> Rs {eq:,.0f}, max drawdown Rs {dd:,.0f}\n")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from", dest="start")
    p.add_argument("--to", dest="end")
    p.add_argument("--group", default="month", choices=["day", "week", "month", "year"])
    p.add_argument("--stocks", action="store_true")
    p.add_argument("--export")
    a = p.parse_args()
    s = dt.date.fromisoformat(a.start) if a.start else None
    e = dt.date.fromisoformat(a.end) if a.end else None
    idx_days = run(s, e)
    by = collections.defaultdict(lambda: [0, 0, 0.0])
    for day in idx_days:
        for t in day["trades"]:
            r = by[(t["s"], t["u"])]
            r[0] += 1
            r[1] += t["net"] > 0
            r[2] += t["net"]
    for (st, u), (n, w, net) in sorted(by.items()):
        print(f"{st:<8} {u:<7} trades {n:>3}  win {w / n if n else 0:>4.0%}  net Rs {net:>10,.0f}")
    _print(idx_days, a.group, options.OPT["capital"], "Index options")
    stocks = run_stocks(s, e) if a.stocks else None
    if stocks:
        _print(stocks[0], a.group, paper.CONFIG["start_capital"], "Stocks")
    if a.export:
        export(a.export, idx_days, stocks)
        print(f"exported -> {a.export}")
