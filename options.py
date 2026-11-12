"""Index option-buying strategies on NIFTY and SENSEX (virtual money only).

Signals come from the index's 5-minute candles. The option itself is priced with
Black-Scholes using India VIX as the volatility, the ATM strike at entry, and the
time left to the weekly expiry, so premium, time decay and charges are realistic
estimates, not exchange quotes (free intraday option prices are not available).

ORB5      First 5-min candle (09:15-09:20) high/low. A close above the high, then a
          retest (a later candle dips back to the high and still closes above it)
          buys an ATM CE; the mirror below the low buys an ATM PE.
          Stop = middle of the first candle. Exit: book at +30, stop to
          break-even, then trail the stop 20 points per 20 points.
EMA9_21   The 9/21 EMA cross only arms the setup; the cross candle is never an entry.
          CE: after the 9 crosses above the 21, wait for a candle whose wick comes
          back to the 9 EMA and whose body closes ABOVE it - that candle is the
          entry. A retest candle that closes below the 9 EMA is skipped; keep
          waiting. PE is the mirror. Stop = lowest low (CE) / highest high (PE) of
          the last 3 candles. Exit: book at +30 and move the stop to break-even;
          the rest stays open until a candle BODY closes across the 9 EMA (a wick
          through it does not exit).

Money management (each strategy on its own): trade 1 = 5 lots, book 3 at +30.
If trade 1 makes money that strategy is done for the day; if it loses, trade 2 =
7 lots (book 5 at +30). After 2 trades, or a winning one, the strategy stops.
ORB5 and EMA9_21 run independently, each with its own 2-trade day.
All points are index points (SENSEX values are scaled to its larger point size).
The option book has its own capital (OPT["capital"]), separate from stocks.
"""
import datetime as dt
import math

import paper
from paper import nsemkt, IST

OPT = {
    "capital": 500000.0,              # NIFTY + SENSEX option book, separate from stocks
    "underlyings": ["NIFTY", "SENSEX"],
    "strategies": ["ORB5", "EMA9_21"],
    "strike_step": {"NIFTY": 50, "SENSEX": 100},
    # SENSEX moves ~3.2x NIFTY's points, so its thresholds are scaled
    "points": {
        "NIFTY": {"partial": 30, "trail": 20, "retest_buffer": 5, "min_sl": 10, "max_sl": 40},
        "SENSEX": {"partial": 100, "trail": 65, "retest_buffer": 15, "min_sl": 30, "max_sl": 130},
    },
    "lots": [5, 7],          # trade 1, trade 2 (only after a losing trade 1)
    "book": [3, 5],          # lots booked at the first +30
    "max_trades": 2,
    "orb_entry_from": dt.time(9, 20),
    "ema_entry_from": dt.time(9, 30),
    "entry_until": dt.time(14, 30),   # leave time for the trade to work
    "squareoff": dt.time(15, 15),
    "day_loss_limit": 10000.0,        # all option trades together: 2% of the option capital
    "week_loss_limit": 15000.0,       # pause for the rest of the week after -3%
    "month_loss_limit": 30000.0,      # pause for the rest of the month after -6%
    "ema_fast": 9, "ema_slow": 21, "ema_sl_lookback": 3,
    "expiry_weekday": {"NIFTY": 1, "SENSEX": 3},   # Tuesday / Thursday weeklies
    "premium_slippage": 0.0025,       # 0.25% of premium against us per fill (liquid ATM weeklies)
    "rate": 0.065,
    "default_vix": 15.0,
}
BAR = dt.timedelta(minutes=5)


# ------------------------------------------------------------ option pricing

def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_price(S, K, T, vol, kind, r=OPT["rate"]):
    if T <= 0 or vol <= 0:
        return max(0.0, S - K) if kind == "CE" else max(0.0, K - S)
    d1 = (math.log(S / K) + (r + vol * vol / 2) * T) / (vol * math.sqrt(T))
    d2 = d1 - vol * math.sqrt(T)
    if kind == "CE":
        return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


def expiry_for(und, day):
    """Next weekly expiry; on expiry day itself use the following week (avoids
    expiry-day decay). Exchange holiday shifts are not modelled."""
    wd = OPT["expiry_weekday"][und]
    ahead = (wd - day.weekday()) % 7 or 7
    return day + dt.timedelta(days=ahead)


def years_to(expiry, t):
    end = dt.datetime.combine(expiry, dt.time(15, 30), IST)
    return max((end - t).total_seconds(), 60) / (365 * 24 * 3600)


def premium(und, kind, strike, expiry, S, t, vix):
    return max(0.05, bs_price(S, strike, years_to(expiry, t), vix / 100.0, kind))


def option_charges(qty, buy_px, sells):
    """One buy order for qty, one sell order per exit leg [(qty, px), ...]."""
    total = nsemkt.broker_charges("FNO", "BUY", qty, buy_px, "OPT")["total"]
    for q, px in sells:
        total += nsemkt.broker_charges("FNO", "SELL", q, px, "OPT")["total"]
    return round(total, 2)


# ------------------------------------------------------------ indicators

def ema_series(closes, n):
    k, out, e = 2 / (n + 1), [], None
    for c in closes:
        e = c if e is None else (c - e) * k + e
        out.append(e)
    return out


# ------------------------------------------------------------ the simulation

def circuit_breaker(day, past):
    """past: [(date, net P&L)] of earlier option days. Returns why trading is paused, or None."""
    y, w, _ = day.isocalendar()
    week = sum(n for d, n in past if d < day and d.isocalendar()[:2] == (y, w))
    month = sum(n for d, n in past if d < day and (d.year, d.month) == (day.year, day.month))
    if week <= -OPT["week_loss_limit"]:
        return f"paused for the rest of the week: week loss Rs {week:,.0f} (limit Rs {OPT['week_loss_limit']:,.0f})"
    if month <= -OPT["month_loss_limit"]:
        return f"paused for the rest of the month: month loss Rs {month:,.0f} (limit Rs {OPT['month_loss_limit']:,.0f})"
    return None


def paused_day(reason):
    status = {f"{s}:{u}": {"trades": 0, "done": True, "note": reason, "net": 0.0}
              for s in OPT["strategies"] for u in OPT["underlyings"]}
    return {"levels": {}, "trades": [], "notes": {}, "status": status, "ema": {}, "paused": reason}


class Book:
    """Per (strategy, underlying) day state."""

    def __init__(self, strategy, und):
        self.strategy, self.und = strategy, und
        self.trades, self.done, self.note = [], False, ""

    def open_trade(self):
        return next((t for t in self.trades if t["status"] == "OPEN"), None)

    def next_size(self):
        """(lots, book_lots) for the next trade, or None when the day is over."""
        if self.done or self.open_trade() or len(self.trades) >= OPT["max_trades"]:
            return None
        if not self.trades:
            return OPT["lots"][0], OPT["book"][0]
        if self.trades[-1]["net_pnl"] > 0:
            return None
        return OPT["lots"][1], OPT["book"][1]


def simulate_options_day(day, und_bars, vix_bars, now=None):
    """und_bars: {und: [bars across recent days, sorted]} so the EMAs are warm at 09:15.
    Deterministic and rebuilt from completed bars, like paper.simulate_day."""
    cutoff = now or dt.datetime.combine(day, dt.time(23, 59), IST)
    vix_at = _vix_lookup(vix_bars)
    levels, emas, prev_t, books, notes, today = {}, {}, {}, {}, {}, {}
    for und, bars in und_bars.items():
        done = [b for b in bars if b["t"] + BAR <= cutoff]
        today[und] = [b for b in done if b["t"].date() == day
                      and dt.time(9, 15) <= b["t"].time() < dt.time(15, 30)]
        first = next((b for b in today[und] if b["t"].time() == dt.time(9, 15)), None)
        if first:
            levels[und] = {"high": round(first["h"], 2), "low": round(first["l"], 2),
                           "mid": round((first["h"] + first["l"]) / 2, 2)}
        closes = [b["c"] for b in done]
        f, s = ema_series(closes, OPT["ema_fast"]), ema_series(closes, OPT["ema_slow"])
        emas[und] = {b["t"]: (f[i], s[i]) for i, b in enumerate(done)}
        prev_t[und] = {done[i]["t"]: done[i - 1]["t"] for i in range(1, len(done))}
        for strat in OPT["strategies"]:
            books[(strat, und)] = Book(strat, und)
    orb_state = {und: {"prev_close": None, "up_broken": False, "dn_broken": False} for und in und_bars}
    ema_state = {und: {"setup": None} for und in und_bars}

    times = sorted({b["t"] for bars in today.values() for b in bars})
    for t in times:
        realised = sum(tr["net_pnl"] for bk in books.values() for tr in bk.trades if tr["status"] == "CLOSED")
        for und in und_bars:
            bar = next((b for b in today[und] if b["t"] == t), None)
            if bar is None:
                continue
            vix = vix_at(t)
            pts = OPT["points"][und]
            e_now = emas[und].get(t)
            e_prev = emas[und].get(prev_t[und].get(t))
            ema_sig = None
            if e_now and e_prev:  # track the EMA setup on every bar, trade open or not
                recent = [b for b in today[und] if b["t"] <= t][-OPT["ema_sl_lookback"]:]
                ema_sig = _ema_signal(ema_state[und], e_prev, e_now, bar, recent, pts)
            for strat in OPT["strategies"]:
                bk = books[(strat, und)]
                tr = bk.open_trade()
                if tr:
                    _manage(tr, bar, t, vix, e_now[0] if e_now else None)
                    if tr["status"] == "CLOSED":
                        realised += tr["net_pnl"]
                        if tr["net_pnl"] > 0:
                            bk.done, bk.note = True, "done for the day: trade made money"
                        elif len(bk.trades) >= OPT["max_trades"]:
                            bk.done, bk.note = True, "done for the day: 2 losing trades"
                    continue
                if t.time() >= OPT["entry_until"]:
                    continue
                size = bk.next_size()
                if size is None:
                    continue
                if realised <= -OPT["day_loss_limit"]:
                    bk.note = f"no new trades: day loss limit Rs {OPT['day_loss_limit']:,.0f} reached"
                    continue
                signal = None
                if strat == "ORB5" and und in levels and t.time() >= OPT["orb_entry_from"]:
                    signal = _orb_signal(orb_state[und], levels[und], bar, pts)
                elif strat == "EMA9_21" and t.time() >= OPT["ema_entry_from"]:
                    signal = ema_sig
                if not signal:
                    continue
                kind, stop, why = signal
                dist = abs(bar["c"] - stop)
                if dist > pts["max_sl"]:
                    notes[f"{strat}:{und}:{t:%H%M}"] = f"{t:%H:%M} {strat} {und} {kind} skipped: stop {dist:.0f} pts > max {pts['max_sl']}"
                    continue
                if dist < pts["min_sl"]:
                    stop = bar["c"] - pts["min_sl"] if kind == "CE" else bar["c"] + pts["min_sl"]
                trade = _enter(strat, und, kind, bar, t, stop, size, vix, why, len(bk.trades) + 1)
                # capital first: realised loss + what open trades can still lose + this trade's
                # risk must stay inside the day limit (a trade past break-even risks ~0)
                open_risk = sum(o["risk_at_stop"] for b2 in books.values() for o in b2.trades
                                if o["status"] == "OPEN" and not o["partial_done"])
                if -realised + open_risk + trade["risk_at_stop"] > OPT["day_loss_limit"]:
                    notes[f"{strat}:{und}:{t:%H%M}"] = (f"{t:%H:%M} {strat} {und} {kind} skipped: would risk more than the "
                                                        f"Rs {OPT['day_loss_limit']:,.0f} day limit")
                    continue
                bk.trades.append(trade)
            if und in levels:
                orb_state[und]["prev_close"] = bar["c"]

    # mark or close what is still open
    session_over = cutoff >= dt.datetime.combine(day, dt.time(15, 20), IST)
    for bk in books.values():
        tr = bk.open_trade()
        if not tr:
            continue
        bars = today[tr["underlying"]]
        last = bars[-1] if bars else None
        if not last:
            continue
        vix = vix_at(last["t"])
        if session_over:
            _exit(tr, tr["lots_open"], last["c"], last["t"], "SQUARE_OFF_15:15", vix)
            _close(tr)
        else:
            px = premium(tr["underlying"], tr["side"], tr["strike"], dt.date.fromisoformat(tr["expiry"]),
                         last["c"], last["t"], vix)
            tr["last_index"], tr["last_premium"] = round(last["c"], 2), round(px, 2)
            booked = sum((l["premium"] - tr["entry_premium"]) * l["qty"] for l in tr["legs"])
            tr["unrealised"] = round(booked + (px - tr["entry_premium"]) * tr["lots_open"] * tr["lot"], 2)

    trades = [tr for bk in books.values() for tr in bk.trades]
    trades.sort(key=lambda x: x["entry_time"])
    status = {f"{s}:{u}": {"trades": len(books[(s, u)].trades), "done": books[(s, u)].done,
                           "note": books[(s, u)].note,
                           "net": round(sum(t["net_pnl"] for t in books[(s, u)].trades), 2)}
              for (s, u) in books}
    ema_now = {}
    for u, e in emas.items():
        day_ts = [t for t in e if t.date() == day]
        if day_ts:
            last = e[max(day_ts)]
            ema_now[u] = {"ema9": round(last[0], 2), "ema21": round(last[1], 2),
                          "setup": ema_state[u]["setup"]}
    return {"levels": levels, "trades": trades, "notes": notes, "status": status, "ema": ema_now}


def _vix_lookup(vix_bars):
    pts = sorted((b["t"], b["c"]) for b in vix_bars)

    def at(t):
        best = None
        for tt, v in pts:
            if tt > t:
                break
            best = v
        return best or OPT["default_vix"]
    return at


def _orb_signal(st, lv, bar, pts):
    """Breakout (close crosses the level) then retest (dips back to it, closes beyond)."""
    hi, lo, buf = lv["high"], lv["low"], pts["retest_buffer"]
    prev = st["prev_close"]
    sig = None
    if st["up_broken"] and bar["l"] <= hi + buf and bar["c"] > hi:
        sig = ("CE", lv["mid"], f"retest of ORB high {hi:.2f}")
        st["up_broken"] = False
    elif st["dn_broken"] and bar["h"] >= lo - buf and bar["c"] < lo:
        sig = ("PE", lv["mid"], f"retest of ORB low {lo:.2f}")
        st["dn_broken"] = False
    if sig:
        return sig
    # a close back through the middle cancels a pending breakout
    if st["up_broken"] and bar["c"] < lv["mid"]:
        st["up_broken"] = False
    if st["dn_broken"] and bar["c"] > lv["mid"]:
        st["dn_broken"] = False
    if prev is not None and prev <= hi < bar["c"]:
        st["up_broken"], st["dn_broken"] = True, False
    elif prev is not None and prev >= lo > bar["c"]:
        st["dn_broken"], st["up_broken"] = True, False
    return None


def _ema_signal(st, e_prev, e_now, bar, recent, pts):
    """The cross arms a setup; the entry is a later retest candle whose wick reaches the
    9 EMA and whose body closes on the trend side of it."""
    (f0, s0), (f1, s1) = e_prev, e_now
    if f0 <= s0 and f1 > s1:
        st["setup"] = "CE"
        return None          # the cross candle itself is never an entry
    if f0 >= s0 and f1 < s1:
        st["setup"] = "PE"
        return None
    buf = pts["retest_buffer"]
    if st["setup"] == "CE" and f1 > s1 and bar["l"] <= f1 + buf and bar["c"] > f1:
        return "CE", min(b["l"] for b in recent), f"retest of 9 EMA {f1:.1f} after bullish cross, closed above"
    if st["setup"] == "PE" and f1 < s1 and bar["h"] >= f1 - buf and bar["c"] < f1:
        return "PE", max(b["h"] for b in recent), f"retest of 9 EMA {f1:.1f} after bearish cross, closed below"
    return None


def _enter(strat, und, kind, bar, t, stop, size, vix, why, n):
    lots, book_lots = size
    lot = paper.lot_size(und)
    step = OPT["strike_step"][und]
    strike = int(round(bar["c"] / step) * step)
    expiry = expiry_for(und, t.date())
    entry_prem = premium(und, kind, strike, expiry, bar["c"], t, vix) * (1 + OPT["premium_slippage"])
    pts = OPT["points"][und]
    stop_prem = premium(und, kind, strike, expiry, stop, t, vix)
    risk = (entry_prem - stop_prem) * lots * lot
    return {
        "strategy": strat, "underlying": und, "side": kind, "strike": strike,
        "expiry": expiry.isoformat(), "symbol": f"{und} {strike} {kind}", "trade_no": n,
        "status": "OPEN", "why": why, "entry_time": t.isoformat(),
        "entry_index": round(bar["c"], 2), "entry_premium": round(entry_prem, 2), "vix": round(vix, 2),
        "lot": lot, "lots": lots, "qty": lots * lot, "book_lots": book_lots, "lots_open": lots,
        "stop_index": round(stop, 2), "initial_stop": round(stop, 2),
        "trail_mode": "ema9_close" if strat == "EMA9_21" else "points",
        "partial_index": round(bar["c"] + (pts["partial"] if kind == "CE" else -pts["partial"]), 2),
        "partial_done": False, "next_trail": pts["partial"] + pts["trail"],
        "risk_at_stop": round(risk, 2), "legs": [],
        "gross_pnl": 0.0, "charges": 0.0, "net_pnl": 0.0,
    }


def _manage(tr, bar, t, vix, ema9):
    """Stop first (a wick through a stop fills it), then booking at +30, then the trail:
    points steps for ORB5, a candle body closing across the 9 EMA for EMA9_21."""
    if t <= dt.datetime.fromisoformat(tr["entry_time"]):
        return
    d = 1 if tr["side"] == "CE" else -1
    e, pts = tr["entry_index"], OPT["points"][tr["underlying"]]
    if t.time() >= OPT["squareoff"]:
        _exit(tr, tr["lots_open"], bar["o"], t, "SQUARE_OFF_15:15", vix)
        return _close(tr)
    adverse = bar["l"] if d == 1 else bar["h"]
    if (adverse - tr["stop_index"]) * d <= 0:
        level = bar["o"] if (bar["o"] - tr["stop_index"]) * d < 0 else tr["stop_index"]  # gap through the stop
        reason = "STOP" if not tr["partial_done"] else (
            "BREAKEVEN_STOP" if abs(tr["stop_index"] - e) < 0.01 else "TRAIL_STOP")
        _exit(tr, tr["lots_open"], level, t, reason, vix)
        return _close(tr)
    fav = ((bar["h"] if d == 1 else bar["l"]) - e) * d
    if not tr["partial_done"] and fav >= pts["partial"]:
        _exit(tr, tr["book_lots"], e + d * pts["partial"], t, "PARTIAL_+%d" % pts["partial"], vix)
        tr["partial_done"], tr["stop_index"] = True, e
    if tr["trail_mode"] == "points":
        while tr["partial_done"] and fav >= tr["next_trail"]:
            tr["stop_index"] = round(e + d * (tr["next_trail"] - pts["partial"]), 2)
            tr["next_trail"] += pts["trail"]
    elif ema9 is not None and (bar["c"] - ema9) * d < 0:
        # only a candle BODY closing across the 9 EMA exits; a wick through it does not
        _exit(tr, tr["lots_open"], bar["c"], t, "EMA9_CLOSE", vix)
        return _close(tr)


def _exit(tr, lots, index_level, t, reason, vix):
    if lots <= 0:
        return
    px = premium(tr["underlying"], tr["side"], tr["strike"], dt.date.fromisoformat(tr["expiry"]),
                 index_level, t, vix) * (1 - OPT["premium_slippage"])
    tr["legs"].append({"time": t.isoformat(), "lots": lots, "qty": lots * tr["lot"],
                       "index": round(index_level, 2), "premium": round(px, 2), "reason": reason})
    tr["lots_open"] -= lots


def _close(tr):
    tr["status"] = "CLOSED"
    last = tr["legs"][-1]
    tr.update(exit_time=last["time"], exit_reason=last["reason"], exit_index=last["index"],
              exit=last["premium"])
    tr["gross_pnl"] = round(sum((l["premium"] - tr["entry_premium"]) * l["qty"] for l in tr["legs"]), 2)
    tr["charges"] = option_charges(tr["qty"], tr["entry_premium"], [(l["qty"], l["premium"]) for l in tr["legs"]])
    tr["net_pnl"] = round(tr["gross_pnl"] - tr["charges"], 2)
    tr["r_multiple"] = round(tr["net_pnl"] / tr["risk_at_stop"], 2) if tr["risk_at_stop"] > 0 else None
    tr["entry"] = tr["entry_premium"]
    tr.pop("unrealised", None)


# ------------------------------------------------------------ data helpers

def fetch(range_="5d"):
    """Recent bars: the local archive (history.py) topped up with a fresh Yahoo fetch."""
    import history
    out = {}
    for u in OPT["underlyings"]:
        out[u] = history.merged(u, nsemkt.yahoo_chart(paper.yahoo_of(u), interval="5m", range_=range_)["bars"])
    try:
        vix = history.merged("INDIAVIX", nsemkt.yahoo_chart("^INDIAVIX", interval="5m", range_=range_)["bars"])
    except Exception:
        vix = history.load("INDIAVIX")
    return out, vix
