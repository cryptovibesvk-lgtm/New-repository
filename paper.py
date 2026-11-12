"""Virtual-money paper trader for NSE opening-range breakouts.

No broker, no real orders. It reads real NSE 5-minute bars (Yahoo, may be
delayed), applies fixed rules, and keeps a virtual account in paper/.

    python paper.py premarket            # scan Nifty 100, store today's watchlist
    python paper.py update               # simulate today from completed bars so far
    python paper.py run                  # wait for the session, then premarket + update every 5 min
    python paper.py report               # account summary + paper/journal.md
    python paper.py replay --days 20     # rules over past sessions (separate from the account)
    python paper.py tick                 # one scheduler step (used by GitHub Actions)

Rules (see CONFIG): opening range = 09:15-09:30 bars; entry = first completed
5-minute close above the range high from 09:30 to 15:00, filled at that close
plus slippage; stop = range midpoint; target = 1.5R; a bar touching both counts
as the stop; anything open is squared off at the 15:15 bar's open.
"""
import argparse
import datetime as dt
import json
import math
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# the bundled copy in vendor/ lets this run on GitHub Actions; fall back to the installed skill
_VENDOR = os.path.join(HERE, "vendor", "nse-trader-scanner", "scripts")
SCANNER_DIR = _VENDOR if os.path.isdir(_VENDOR) else os.path.join(
    os.path.expanduser("~"), ".claude", "skills", "nse-trader-scanner", "scripts")
sys.path.insert(0, SCANNER_DIR)
import nsemkt  # noqa: E402

PAPER_DIR = os.path.join(HERE, "paper")
ACCOUNT = os.path.join(PAPER_DIR, "account.json")
STATUS = os.path.join(PAPER_DIR, "status.json")
IST = nsemkt.IST

CONFIG = {
    "start_capital": 500000.0,
    "max_loss_per_trade": 10000.0,   # 2% of capital
    "daily_loss_limit": 20000.0,
    "max_position_pct": 0.25,
    "max_trades_per_day": 3,
    "watchlist_size": 5,
    "rr": 1.5,
    "slippage_pct": 0.0005,          # 0.05% against us on every fill
    "min_stop_pct": 0.0015,
    "max_stop_pct": 0.02,
    "entry_from": dt.time(9, 30),
    "entry_cutoff": dt.time(15, 0),
    "squareoff": dt.time(15, 15),
}
BAR = dt.timedelta(minutes=5)

# Index trades: the same breakout rules on the index level, sized and charged as
# index futures. Prices come from the spot index (Yahoo), so a real future's
# premium/discount is not modelled. NIFTY's lot is read from NSE's fo_mktlots;
# SENSEX (a BSE contract) uses the fallback - confirm it with your broker.
INDEXES = {
    "NIFTY": {"yahoo": "^NSEI", "fallback_lot": 65},
    "SENSEX": {"yahoo": "^BSESN", "fallback_lot": 20},
}
# The index-futures ORB is replaced by the option strategies in options.py.
INDEX_FUTURES = False
INDEX_CONFIG = {
    "lots": 1,
    "margin_pct": 0.12,          # estimated futures margin, of contract value
    "slippage_pct": 0.0002,
    "min_stop_pct": 0.0008,      # ~20 points on NIFTY
}
_lots_cache = {}


def yahoo_of(sym):
    return INDEXES[sym]["yahoo"] if sym in INDEXES else sym


def lot_size(sym):
    if sym not in _lots_cache:
        try:
            _lots_cache[sym] = int(nsemkt.fo_lots().get(sym) or INDEXES[sym]["fallback_lot"])
        except Exception:
            _lots_cache[sym] = INDEXES[sym]["fallback_lot"]
    return _lots_cache[sym]


def now_ist():
    return dt.datetime.now(IST)


def load_account():
    if os.path.exists(ACCOUNT):
        with open(ACCOUNT, encoding="utf-8") as f:
            return json.load(f)
    return {"start_capital": CONFIG["start_capital"], "created": now_ist().isoformat(timespec="seconds"),
            "days": {}}


def save_account(acct):
    os.makedirs(PAPER_DIR, exist_ok=True)
    tmp = ACCOUNT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(acct, f, indent=2, default=str)
    os.replace(tmp, ACCOUNT)


def day_trades(rec):
    """Stock trades plus option-strategy trades of one day record."""
    return rec.get("trades", []) + (rec.get("options") or {}).get("trades", [])


def capital_before(acct, day):
    """Start capital plus realised P&L of every earlier day."""
    cap = acct["start_capital"]
    for d, rec in sorted(acct["days"].items()):
        if d < day:
            cap += sum(t["net_pnl"] for t in day_trades(rec) if t.get("status") == "CLOSED")
    return cap


def day_bars(symbol, day, range_="5d"):
    """Regular-session 5-minute bars for one date, keyed by bar start time."""
    chart = nsemkt.yahoo_chart(yahoo_of(symbol), interval="5m", range_=range_)
    out = {}
    for b in chart["bars"]:
        t = b["t"]
        if t.date() == day and dt.time(9, 15) <= t.time() < dt.time(15, 30):
            out[t] = b
    return out


def fill_up(p, slip=None):
    return nsemkt.round_tick(p * (1 + (CONFIG["slippage_pct"] if slip is None else slip)), "up")


def fill_down(p, slip=None):
    return nsemkt.round_tick(p * (1 - (CONFIG["slippage_pct"] if slip is None else slip)), "down")


def charges(segment, qty, entry, exit_px):
    product = "NRML" if segment == "FNO" else "MIS"
    return nsemkt.round_trip_charges(segment, qty, entry, exit_px, product)["total"]


def simulate_day(day, bars_by_symbol, capital, now=None):
    """Replay one session on completed bars only. Deterministic, so it can be rerun."""
    c = CONFIG
    cutoff = now or dt.datetime.combine(day, dt.time(23, 59), IST)
    ranges, notes = {}, {}
    for sym, bars in bars_by_symbol.items():
        orb = [bars.get(dt.datetime.combine(day, dt.time(9, m), IST)) for m in (15, 20, 25)]
        if None in orb:
            notes[sym] = "opening range incomplete"
            continue
        hi, lo = max(b["h"] for b in orb), min(b["l"] for b in orb)
        ranges[sym] = {"or_high": round(hi, 2), "or_low": round(lo, 2), "or_mid": round((hi + lo) / 2, 2)}

    trades, cash = [], capital
    times = sorted({t for bars in bars_by_symbol.values() for t in bars if t + BAR <= cutoff})
    for t in times:
        # 1. manage open positions on this bar
        for tr in trades:
            if tr["status"] != "OPEN":
                continue
            b = bars_by_symbol[tr["symbol"]].get(t)
            if b is None or t <= dt.datetime.fromisoformat(tr["entry_time"]):
                continue
            slip = tr.get("slippage_pct")
            exit_px = reason = None
            if t.time() >= c["squareoff"]:
                exit_px, reason = fill_down(b["o"], slip), "SQUARE_OFF_15:15"
            elif b["l"] <= tr["stop"]:
                exit_px, reason = fill_down(min(b["o"], tr["stop"]), slip), "STOP"
            elif b["h"] >= tr["target"]:
                exit_px, reason = nsemkt.round_tick(max(b["o"], tr["target"]) if b["o"] > tr["target"]
                                                    else tr["target"]), "TARGET"
            if exit_px is not None:
                close_trade(tr, exit_px, reason, t)
                cash += tr["margin"] + tr["gross_pnl"]

        # 2. look for new entries on this bar
        if not (c["entry_from"] <= t.time() < c["entry_cutoff"]):
            continue
        realised = sum(x["net_pnl"] for x in trades if x["status"] == "CLOSED")
        open_risk = sum(x["risk_at_stop"] for x in trades if x["status"] == "OPEN")
        stock_trades = sum(1 for x in trades if x["symbol"] not in INDEXES)
        # indices first, then stocks; the stock cap does not count index trades
        for sym in sorted(ranges, key=lambda s: (s not in INDEXES, s)):
            is_index = sym in INDEXES
            if any(x["symbol"] == sym for x in trades):
                continue
            if not is_index and stock_trades >= c["max_trades_per_day"]:
                continue
            b = bars_by_symbol[sym].get(t)
            r = ranges[sym]
            if b is None or b["c"] <= r["or_high"]:
                continue
            # only the FIRST close above the range counts as the breakout
            prior = [bb for tt, bb in bars_by_symbol[sym].items()
                     if c["entry_from"] <= tt.time() and tt < t and bb["c"] > r["or_high"]]
            if prior:
                continue
            slip = INDEX_CONFIG["slippage_pct"] if is_index else c["slippage_pct"]
            min_stop = INDEX_CONFIG["min_stop_pct"] if is_index else c["min_stop_pct"]
            entry = fill_up(b["c"], slip)
            stop = nsemkt.round_tick(r["or_mid"], "down")
            risk = entry - stop
            if risk < max(2 * nsemkt.TICK, min_stop * entry):
                notes[sym] = f"{t:%H:%M} skipped: stop too tight ({risk:.2f})"
                continue
            if risk > c["max_stop_pct"] * entry:
                notes[sym] = f"{t:%H:%M} skipped: opening range too wide ({risk / entry:.1%})"
                continue
            target = nsemkt.round_tick(entry + c["rr"] * risk, "up")
            if is_index:
                segment, lot = "FNO", lot_size(sym)
                lots = INDEX_CONFIG["lots"]
                while lots > 0 and lots * lot * risk + charges(segment, lots * lot, entry, stop) > c["max_loss_per_trade"]:
                    lots -= 1
                qty = lots * lot
                margin = INDEX_CONFIG["margin_pct"] * entry * qty
                if qty < 1:
                    notes[sym] = f"{t:%H:%M} skipped: one lot risks more than the max loss"
                    continue
                if margin > cash:
                    notes[sym] = f"{t:%H:%M} skipped: not enough virtual cash for margin (Rs {margin:,.0f})"
                    continue
            else:
                segment, lot, lots = "CASH", 1, None
                per_share_chg = charges(segment, 1000, entry, stop) / 1000
                qty = math.floor(min(c["max_loss_per_trade"] / (risk + per_share_chg),
                                     c["max_position_pct"] * capital / entry,
                                     cash / entry))
                margin = qty * entry
                if qty < 1:
                    notes[sym] = f"{t:%H:%M} skipped: no cash for a position"
                    continue
            risk_at_stop = qty * risk + charges(segment, qty, entry, stop)
            if -realised + open_risk + risk_at_stop > c["daily_loss_limit"]:
                notes[sym] = f"{t:%H:%M} skipped: daily loss limit"
                continue
            trade = {"symbol": sym, "status": "OPEN", "entry_time": t.isoformat(),
                     "entry": round(entry, 2), "stop": round(stop, 2), "target": round(target, 2), "qty": qty,
                     "segment": segment, "margin": round(margin, 2), "slippage_pct": slip,
                     "risk_at_stop": round(risk_at_stop, 2), **r,
                     "gross_pnl": 0.0, "charges": 0.0, "net_pnl": 0.0}
            if is_index:
                trade.update(kind="INDEX_FUT", lot=lot, lots=lots)
            trades.append(trade)
            cash -= margin
            open_risk += risk_at_stop
            if not is_index:
                stock_trades += 1

    # mark open positions to the last completed bar; past 15:20 a missing
    # 15:15 bar must not leave a position open overnight, so close at that bar
    session_over = cutoff >= dt.datetime.combine(day, dt.time(15, 20), IST)
    for tr in trades:
        if tr["status"] == "OPEN":
            bars = bars_by_symbol[tr["symbol"]]
            done = [tt for tt in bars if tt + BAR <= cutoff]
            last_t = max(done) if done else None
            last = bars[last_t]["c"] if done else tr["entry"]
            if session_over and last_t is not None:
                close_trade(tr, fill_down(last, tr.get("slippage_pct")), "SQUARE_OFF_15:15", last_t)
                continue
            tr["last"] = last
            tr["unrealised"] = round((last - tr["entry"]) * tr["qty"], 2)
    return {"ranges": ranges, "trades": trades, "notes": notes}


def close_trade(tr, exit_px, reason, t):
    exit_px = round(exit_px, 2)
    tr.update(status="CLOSED", exit=exit_px, exit_time=t.isoformat(), exit_reason=reason)
    tr["gross_pnl"] = round((exit_px - tr["entry"]) * tr["qty"], 2)
    tr["charges"] = charges(tr.get("segment", "CASH"), tr["qty"], tr["entry"], exit_px)
    tr["net_pnl"] = round(tr["gross_pnl"] - tr["charges"], 2)
    tr["r_multiple"] = round(tr["net_pnl"] / tr["risk_at_stop"], 2) if tr["risk_at_stop"] else None


# ---------------------------------------------------------------- commands


def cmd_premarket(args):
    day = now_ist().date()
    acct = load_account()
    subprocess.run([sys.executable, os.path.join(SCANNER_DIR, "scan.py"),
                    "--mode", "daily", "--universe", "nifty100", "--top", str(CONFIG["watchlist_size"])],
                   cwd=HERE, check=False)
    wl_path = os.path.join(HERE, "nse-trader", "watchlist.json")
    with open(wl_path, encoding="utf-8") as f:
        wl = json.load(f)
    items = wl.get("items", wl) if isinstance(wl, dict) else wl
    symbols = [i["symbol"] for i in items][: CONFIG["watchlist_size"]]
    rec = acct["days"].setdefault(day.isoformat(), {})
    rec.update(watchlist=symbols, scanned_at=now_ist().isoformat(timespec="seconds"))
    save_account(acct)
    print(f"watchlist {day}: {', '.join(symbols) or '(none)'}")


def cmd_update(args):
    now = now_ist()
    day = now.date()
    acct = load_account()
    rec = acct["days"].setdefault(day.isoformat(), {})
    if rec.get("watchlist"):
        bars = {}
        for sym in (list(INDEXES) if INDEX_FUTURES else []) + rec["watchlist"]:
            try:
                bars[sym] = day_bars(sym, day, "1d")
            except Exception as e:  # keep going on one bad symbol
                print(f"  {sym}: data error {e}")
        rec.update(simulate_day(day, bars, capital_before(acct, day.isoformat()), now=now))
    else:
        print("no stock watchlist for today - run: python paper.py premarket")
    try:
        import options
        und, vix = options.fetch("5d")
        rec["options"] = options.simulate_options_day(
            day, und, [b for b in vix if b["t"].date() == day], now=now)
    except Exception as e:
        print(f"  options: data error {e}")
    rec.update(updated_at=now.isoformat(timespec="seconds"), final=now.time() >= dt.time(15, 35))
    save_account(acct)
    print_day(day.isoformat(), rec)


def print_day(day, rec):
    print(f"PAPER {day}  indices {', '.join(INDEXES)}  watchlist {', '.join(rec.get('watchlist', []))}")
    for sym, r in rec.get("ranges", {}).items():
        print(f"  {sym:<12} OR {r['or_low']:.2f}-{r['or_high']:.2f}")
    for t in rec.get("trades", []):
        size = f"{t['lots']} lot ({t['qty']})" if t.get("kind") == "INDEX_FUT" else str(t["qty"])
        line = (f"  {t['status']:<6} {t['symbol']:<12} BUY {size} @ {t['entry']:.2f} "
                f"({t['entry_time'][11:16]})  stop {t['stop']:.2f}  target {t['target']:.2f}")
        if t["status"] == "CLOSED":
            line += f"  -> {t['exit_reason']} {t['exit']:.2f} ({t['exit_time'][11:16]})  net Rs {t['net_pnl']:,.2f}"
        else:
            line += f"  last {t.get('last', 0):.2f}  unrealised Rs {t.get('unrealised', 0):,.2f}"
        print(line)
    for sym, n in rec.get("notes", {}).items():
        print(f"  note {sym}: {n}")
    if not rec.get("trades"):
        print("  no stock trades yet")
    opt = rec.get("options") or {}
    for und, lv in opt.get("levels", {}).items():
        print(f"  {und:<12} first 5-min candle {lv['low']:.2f}-{lv['high']:.2f} (mid {lv['mid']:.2f})")
    for t in opt.get("trades", []):
        legs = " | ".join(f"{l['lots']}L {l['reason']} @ {l['index']:.2f} (prem {l['premium']:.2f})" for l in t["legs"])
        tail = f"net Rs {t['net_pnl']:,.2f}" if t["status"] == "CLOSED" else f"open, unrealised Rs {t.get('unrealised', 0):,.2f}"
        print(f"  {t['strategy']:<8} #{t['trade_no']} BUY {t['lots']} lots {t['symbol']} @ prem {t['entry_premium']:.2f} "
              f"(index {t['entry_index']:.2f}, {t['entry_time'][11:16]})  {legs}  {tail}")
    for k, s in opt.get("status", {}).items():
        if s.get("note"):
            print(f"  {k}: {s['note']}")


def summarise(trades):
    closed = [t for t in trades if t.get("status") == "CLOSED"]
    wins = [t for t in closed if t["net_pnl"] > 0]
    net = sum(t["net_pnl"] for t in closed)
    return {"trades": len(closed), "wins": len(wins),
            "win_rate": len(wins) / len(closed) if closed else 0.0,
            "net": net, "charges": sum(t["charges"] for t in closed),
            "avg_r": sum(t["r_multiple"] or 0 for t in closed) / len(closed) if closed else 0.0,
            "by_reason": {k: sum(1 for t in closed if t["exit_reason"] == k)
                          for k in ("TARGET", "STOP", "SQUARE_OFF_15:15")}}


def cmd_report(args):
    acct = load_account()
    all_trades = [t for rec in acct["days"].values() for t in day_trades(rec)]
    s = summarise(all_trades)
    equity = acct["start_capital"] + s["net"]
    lines = [
        "# Paper trading journal (virtual money)", "",
        f"Start capital: Rs {acct['start_capital']:,.2f}  ",
        f"Current equity: **Rs {equity:,.2f}** ({(equity / acct['start_capital'] - 1):+.2%})  ",
        f"Closed trades: {s['trades']}  |  wins {s['wins']} ({s['win_rate']:.0%})  |  "
        f"avg R {s['avg_r']:+.2f}  |  charges Rs {s['charges']:,.2f}  ",
        f"Exits: target {s['by_reason']['TARGET']}, stop {s['by_reason']['STOP']}, "
        f"15:15 square-off {s['by_reason']['SQUARE_OFF_15:15']}", "",
        "| Date | Stock | Qty | Entry | Exit | Reason | Net P&L | R |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for d, rec in sorted(acct["days"].items()):
        for t in day_trades(rec):
            if t.get("strategy"):  # option strategy: show the contract, lots and every exit leg
                legs = "; ".join(f"{l['lots']}L {l['reason']} {l['premium']:.2f}" for l in t["legs"]) or "open"
                net = f"{t['net_pnl']:,.2f}" if t["status"] == "CLOSED" else "-"
                lines.append(f"| {d} | {t['strategy']} {t['symbol']} | {t['lots']} lots | "
                             f"{t['entry_premium']:.2f} @ {t['entry_time'][11:16]} | {legs} | "
                             f"{t.get('exit_reason', 'open')} | {net} | {t.get('r_multiple') or 0:+.2f} |")
            elif t["status"] == "CLOSED":
                lines.append(f"| {d} | {t['symbol']} | {t['qty']} | {t['entry']:.2f} @ {t['entry_time'][11:16]} | "
                             f"{t['exit']:.2f} @ {t['exit_time'][11:16]} | {t['exit_reason']} | "
                             f"{t['net_pnl']:,.2f} | {t['r_multiple']:+.2f} |")
            else:
                lines.append(f"| {d} | {t['symbol']} | {t['qty']} | {t['entry']:.2f} | open | - | - | - |")
    lines += ["", "Virtual fills on delayed Yahoo 5-minute bars with 0.05% slippage per side. "
              "Not financial advice."]
    os.makedirs(PAPER_DIR, exist_ok=True)
    with open(os.path.join(PAPER_DIR, "journal.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines[2:6]))
    print(f"journal: {os.path.join(PAPER_DIR, 'journal.md')}")


def write_status(state, message=""):
    """Heartbeat for the dashboard: what the session is doing right now."""
    os.makedirs(PAPER_DIR, exist_ok=True)
    tmp = STATUS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"state": state, "message": message, "pid": os.getpid(),
                   "updated": now_ist().isoformat(timespec="seconds")}, f)
    os.replace(tmp, STATUS)


def nap(seconds, state, message):
    """Sleep in short chunks so the heartbeat stays fresh."""
    end = time.time() + seconds
    while True:
        write_status(state, message)
        left = end - time.time()
        if left <= 0:
            return
        time.sleep(min(30, left))


def cmd_run(args):
    """One trading session: wait, scan before the open, update every 5 minutes, report."""
    def until(hh, mm):
        target = dt.datetime.combine(now_ist().date(), dt.time(hh, mm), IST)
        return (target - now_ist()).total_seconds()

    try:
        day = now_ist().date()
        if not nsemkt.is_trading_day(day):
            print(f"{day} is not an NSE trading day - nothing to do")
            write_status("idle", f"{day:%a %d %b} is not an NSE trading day")
            return
        acct = load_account()
        if not acct["days"].get(day.isoformat(), {}).get("watchlist"):
            print("waiting until 08:45 IST ...", flush=True)
            nap(until(8, 45), "waiting", "Waiting until 08:45 IST to scan Nifty 100")
            write_status("scanning", "Scanning Nifty 100 for today's watchlist")
            cmd_premarket(args)
        nap(until(9, 31), "waiting", "Watchlist ready. Building the opening range (09:15-09:30)")
        while True:
            write_status("trading", "Checking prices")
            try:
                cmd_update(args)
            except Exception as e:
                print(f"update failed: {e}", flush=True)
            sys.stdout.flush()
            if now_ist().time() >= dt.time(15, 36):
                break
            nxt = (now_ist() + dt.timedelta(minutes=5)).replace(second=40, microsecond=0)
            nap(max(30, (nxt - now_ist()).total_seconds()), "trading",
                f"Watching for breakouts. Next check {nxt:%H:%M} IST")
        cmd_report(args)
        write_status("finished", "Session finished. Journal written.")
    except Exception as e:
        write_status("error", str(e))
        raise


def cmd_tick(args):
    """One short step for a scheduler (GitHub Actions every 5 min). Safe to run at any time:
    each update rebuilds the day from completed bars, so a late or skipped run changes nothing."""
    now = now_ist()
    day = now.date()
    print(f"--- tick {now:%Y-%m-%d %H:%M} IST")
    if not nsemkt.is_trading_day(day):
        write_status("idle", f"{day:%a %d %b} is not an NSE trading day")
        print("not a trading day")
        return
    rec = load_account()["days"].get(day.isoformat(), {})
    if now.time() < dt.time(8, 40):
        write_status("waiting", "Waiting until 08:45 IST to scan Nifty 100")
        return
    if not rec.get("watchlist"):
        write_status("scanning", "Scanning Nifty 100 for today's watchlist")
        cmd_premarket(args)
    if now.time() < dt.time(9, 20):
        write_status("waiting", "Watchlist ready. Building the opening range (09:15-09:30)")
        return
    cmd_update(args)
    if now.time() >= dt.time(15, 36):
        cmd_report(args)
        write_status("finished", "Session finished. Journal written.")
    else:
        write_status("trading", "Watching for breakouts")


def cmd_replay(args):
    """Same rules on past sessions, for a fixed ticker list. Never touches the account."""
    symbols = args.tickers.split(",") if args.tickers else None
    if not symbols:
        with open(os.path.join(HERE, "nse-trader", "watchlist.json"), encoding="utf-8") as f:
            wl = json.load(f)
        symbols = list(INDEXES) + [i["symbol"] for i in (wl.get("items", wl) if isinstance(wl, dict) else wl)][:10]
    charts = {s: nsemkt.yahoo_chart(yahoo_of(s), interval="5m", range_="60d")["bars"] for s in symbols}
    days = sorted({b["t"].date() for bars in charts.values() for b in bars})
    today = now_ist().date()
    days = [d for d in days if d < today][-args.days:]
    capital, all_trades = CONFIG["start_capital"], []
    for d in days:
        bars = {s: {b["t"]: b for b in charts[s]
                    if b["t"].date() == d and dt.time(9, 15) <= b["t"].time() < dt.time(15, 30)}
                for s in symbols}
        res = simulate_day(d, bars, capital)
        net = sum(t["net_pnl"] for t in res["trades"])
        capital += net
        all_trades += res["trades"]
        for t in res["trades"]:
            print(f"{d} {t['symbol']:<12} {t['qty']:>5} @ {t['entry']:>9.2f} -> "
                  f"{t.get('exit', 0):>9.2f} {t.get('exit_reason', 'OPEN'):<16} net Rs {t['net_pnl']:>10,.2f}")
    s = summarise(all_trades)
    print(f"\nREPLAY {len(days)} sessions, {len(symbols)} symbols: {s['trades']} trades, "
          f"win rate {s['win_rate']:.0%}, avg R {s['avg_r']:+.2f}, net Rs {s['net']:,.2f} "
          f"(charges Rs {s['charges']:,.2f}), exits {s['by_reason']}")
    print(f"virtual equity Rs {CONFIG['start_capital']:,.0f} -> Rs {capital:,.2f}")
    print("Caveat: fixed ticker list chosen today (hindsight bias); delayed bars; not a forecast.")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("premarket")
    sub.add_parser("update")
    sub.add_parser("run")
    sub.add_parser("report")
    sub.add_parser("tick")
    rp = sub.add_parser("replay")
    rp.add_argument("--days", type=int, default=20)
    rp.add_argument("--tickers", default="")
    args = p.parse_args()
    {"premarket": cmd_premarket, "update": cmd_update, "run": cmd_run, "report": cmd_report,
     "tick": cmd_tick, "replay": cmd_replay}[args.cmd](args)


if __name__ == "__main__":
    main()
