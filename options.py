"""Index option-buying strategies on NIFTY and SENSEX (virtual money only).

Weekly schedule (only that day's strategy trades):
    Monday, Friday     NIFTY  ORB1
    Tuesday            NIFTY  SELL_TIME (NIFTY weekly expiry day)
    Wednesday, Thursday SENSEX EMA9_21

The option is priced with Black-Scholes using India VIX as the volatility, the ATM
strike at entry and the time left to the weekly expiry, so premium, time decay and
charges are realistic estimates, not exchange quotes (free intraday option prices
are not available).

ORB1      1-minute candles. Mark the first 1-minute candle (09:15) high and low.
          A 1-minute close above the high buys an ATM CE; a close below the low buys
          an ATM PE. Stop = 20 index points (NIFTY, ~10 premium points) / 30 (SENSEX).
          Trailing (index points in our favour):
            NIFTY : +20 -> stop at -10, +30 -> break-even, +40 -> +10, then the stop
                    follows every further 10 points.
            SENSEX: +30 -> break-even, +40 -> +10, then every further 10 points.
          Quantity: NIFTY 195 then 325; SENSEX 200 then 300 (trade 2 only after a loss).
EMA9_21   5-minute candles. The 9/21 EMA cross only arms the setup; the cross candle is
          never an entry. CE: wait for a later candle whose wick comes back to the 9 EMA
          and whose body closes ABOVE it; a retest closing below is skipped. PE mirror.
          Stop = lowest low / highest high of the last 3 candles. Book at +30 points and
          move the stop to break-even; the rest exits when a candle BODY closes across
          the 9 EMA. 5 lots, then 7 lots after a loss.

SELL_TIME Tuesday only, NIFTY, same-day expiry. 09:16 sell the OTM1 CE and OTM1 PE
          (2 lots each), buy back at 14:25. 14:30 sell the ATM CE and ATM PE (2 lots
          each), buy back at 15:15. Added for capital protection: each pair is closed
          early if its combined premium rises 50% above the premium collected.

ORB1 and EMA9_21: a winning trade ends that strategy for the day, a losing trade 1
allows trade 2, two losses end it. The option book has its own capital.
"""
import datetime as dt
import math

import paper
from paper import nsemkt, IST

OPT = {
    "capital": 500000.0,              # NIFTY + SENSEX option book, separate from stocks
    "underlyings": ["NIFTY", "SENSEX"],
    "strategies": ["ORB1", "EMA9_21", "SELL_TIME"],
    # weekday -> (index, strategy): Mon/Fri NIFTY ORB1, Tue NIFTY SELL_TIME, Wed/Thu SENSEX EMA9_21
    "schedule": {0: ("NIFTY", "ORB1"), 1: ("NIFTY", "SELL_TIME"), 2: ("SENSEX", "EMA9_21"),
                 3: ("SENSEX", "EMA9_21"), 4: ("NIFTY", "ORB1")},
    "sell": {"lots": 2, "windows": [
        {"name": "OTM1 09:16-14:25", "entry": dt.time(9, 16), "exit": dt.time(14, 25), "otm": 1},
        {"name": "ATM 14:30-15:15", "entry": dt.time(14, 30), "exit": dt.time(15, 15), "otm": 0}],
        "pair_stop": 0.5},       # close a pair if its combined premium rises 50% over the credit
    "strike_step": {"NIFTY": 50, "SENSEX": 100},
    "orb1": {
        "NIFTY": {"sl": 20, "qty": [195, 325], "early_trail": (20, -10)},
        "SENSEX": {"sl": 30, "qty": [200, 300], "early_trail": None},
        "breakeven_at": 30, "step": 10,
    },
    # EMA9_21 points (SENSEX moves ~3.2x NIFTY's points, so its thresholds are scaled)
    "points": {
        "NIFTY": {"partial": 30, "retest_buffer": 5, "min_sl": 10, "max_sl": 40},
        "SENSEX": {"partial": 100, "retest_buffer": 15, "min_sl": 30, "max_sl": 130},
    },
    "ema_lots": [5, 7], "ema_book": [3, 5],
    "max_trades": 2,
    "orb_entry_from": dt.time(9, 16),
    "ema_entry_from": dt.time(9, 30),
    "entry_until": dt.time(14, 30),
    "squareoff": dt.time(15, 15),
    "day_loss_limit": 10000.0,        # all option trades together: 2% of the option capital
    "week_loss_limit": 15000.0,       # pause for the rest of the week after -3%
    "month_loss_limit": 30000.0,      # pause for the rest of the month after -6%
    "ema_fast": 9, "ema_slow": 21, "ema_sl_lookback": 3,
    "expiry_weekday": {"NIFTY": 1, "SENSEX": 3},   # Tuesday / Thursday weeklies
    "premium_slippage": 0.0025,       # 0.25% of premium against us per fill
    "rate": 0.065,
    "default_vix": 15.0,
}
M1, M5 = dt.timedelta(minutes=1), dt.timedelta(minutes=5)
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def plan_for_day(day):
    """(index, strategy) scheduled for this date, or (None, None) on weekends."""
    return OPT["schedule"].get(day.weekday(), (None, None))


def index_for_day(day):
    return plan_for_day(day)[0]


def schedule_text():
    return {DAY_NAMES[d]: f"{u} {s}" for d, (u, s) in sorted(OPT["schedule"].items())}


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


def expiry_for(und, day, same_day=False):
    """Next weekly expiry. Buying strategies skip to the following week on expiry day
    (avoids expiry-day decay); the Tuesday sell strategy uses same_day=True to sell the
    expiring weekly. Exchange holiday shifts are not modelled."""
    wd = OPT["expiry_weekday"][und]
    ahead = (wd - day.weekday()) % 7
    if ahead == 0 and not same_day:
        ahead = 7
    return day + dt.timedelta(days=ahead)


SESSION_MIN = 375          # 09:15-15:30
TRADING_YEAR_MIN = 252 * SESSION_MIN


def years_to(expiry, t):
    """Time to expiry in trading time: market minutes left (today's remaining session
    plus a full session for every weekday up to and including expiry) over a 252-day
    year. Calendar time badly understates intraday movement, especially on expiry day."""
    close = dt.datetime.combine(t.date(), dt.time(15, 30), IST)
    left = max(0.0, min(SESSION_MIN, (close - t).total_seconds() / 60))
    d = t.date() + dt.timedelta(days=1)
    while d <= expiry:
        if d.weekday() < 5:
            left += SESSION_MIN
        d += dt.timedelta(days=1)
    return max(left, 1.0) / TRADING_YEAR_MIN


def premium(und, kind, strike, expiry, S, t, vix):
    return max(0.05, bs_price(S, strike, years_to(expiry, t), vix / 100.0, kind))


def option_charges(qty, open_px, closes, action="BUY"):
    """One opening order for qty, one closing order per exit leg [(qty, px), ...].
    action BUY = bought then sold; SELL = sold (written) then bought back."""
    close_side = "SELL" if action == "BUY" else "BUY"
    total = nsemkt.broker_charges("FNO", action, qty, open_px, "OPT")["total"]
    for q, px in closes:
        total += nsemkt.broker_charges("FNO", close_side, q, px, "OPT")["total"]
    return round(total, 2)


def ema_series(closes, n):
    k, out, e = 2 / (n + 1), [], None
    for c in closes:
        e = c if e is None else (c - e) * k + e
        out.append(e)
    return out


# ------------------------------------------------------------ capital guards

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


def paused_day(reason, day=None):
    status = {f"{s}:{u}": {"trades": 0, "done": True, "note": reason, "net": 0.0}
              for s in OPT["strategies"] for u in OPT["underlyings"]}
    u, s = plan_for_day(day) if day else (None, None)
    return {"levels": {}, "trades": [], "notes": {}, "status": status, "ema": {}, "paused": reason,
            "index_today": u, "strategy_today": s, "schedule": schedule_text()}


class Book:
    """Per (strategy, underlying) day state."""

    def __init__(self, strategy, und):
        self.strategy, self.und = strategy, und
        self.trades, self.done, self.note = [], False, ""

    def open_trade(self):
        return next((t for t in self.trades if t["status"] == "OPEN"), None)

    def trade_no(self):
        """1 or 2 for the next trade, or None when the day is over for this strategy."""
        if self.done or self.open_trade() or len(self.trades) >= OPT["max_trades"]:
            return None
        if not self.trades:
            return 1
        return None if self.trades[-1]["net_pnl"] > 0 else 2


# ------------------------------------------------------------ the simulation

def simulate_options_day(day, und5, und1, vix_bars, now=None):
    """und5 / und1: {und: [5-minute / 1-minute bars across recent days, sorted]}.
    Deterministic and rebuilt from completed bars, like paper.simulate_day."""
    cutoff = now or dt.datetime.combine(day, dt.time(23, 59), IST)
    vix_at = _vix_lookup(vix_bars)
    active, active_strat = plan_for_day(day)
    levels, emas, prev_t, books, notes, today5, today1 = {}, {}, {}, {}, {}, {}, {}
    in_day = lambda b: b["t"].date() == day and dt.time(9, 15) <= b["t"].time() < dt.time(15, 30)
    for und in OPT["underlyings"]:
        done5 = [b for b in und5.get(und, []) if b["t"] + M5 <= cutoff]
        today5[und] = [b for b in done5 if in_day(b)]
        today1[und] = [b for b in und1.get(und, []) if b["t"] + M1 <= cutoff and in_day(b)]
        first = next((b for b in today1[und] if b["t"].time() == dt.time(9, 15)), None)
        if first:
            levels[und] = {"high": round(first["h"], 2), "low": round(first["l"], 2)}
        closes = [b["c"] for b in done5]
        f, s = ema_series(closes, OPT["ema_fast"]), ema_series(closes, OPT["ema_slow"])
        emas[und] = {b["t"]: (f[i], s[i]) for i, b in enumerate(done5)}
        prev_t[und] = {done5[i]["t"]: done5[i - 1]["t"] for i in range(1, len(done5))}
        for strat in OPT["strategies"]:
            books[(strat, und)] = Book(strat, und)
            if (und, strat) != (active, active_strat):
                days = [DAY_NAMES[d] for d, (u2, s2) in sorted(OPT["schedule"].items()) if (u2, s2) == (und, strat)]
                books[(strat, und)].done = True
                books[(strat, und)].note = (f"{und} {strat} runs on {'/'.join(days)} only - today "
                                            f"({DAY_NAMES[day.weekday()]}) is {active or '-'} {active_strat or 'no trading'}"
                                            if days else f"{und} {strat} is not in the schedule")
    orb_state = {u: {"prev_close": None} for u in OPT["underlyings"]}
    ema_state = {u: {"setup": None} for u in OPT["underlyings"]}

    # events in completion order: every 1-minute bar (ORB1), every 5-minute bar (EMA9_21)
    events = []
    if active_strat == "ORB1":
        events += [(b["t"] + M1, 0, "1m", active, b) for b in today1[active]]
    elif active_strat == "EMA9_21":
        events += [(b["t"] + M5, 1, "5m", active, b) for b in today5[active]]
    events.sort(key=lambda e: (e[0], e[1]))
    if active_strat == "SELL_TIME":
        bars = today1[active] or today5[active]   # 1-minute when available, else 5-minute
        step = M1 if today1[active] else M5
        books[("SELL_TIME", active)].trades = _sell_time(active, day, bars, step, vix_at, notes, cutoff)
        books[("SELL_TIME", active)].done = all(t["status"] == "CLOSED" for t in books[("SELL_TIME", active)].trades) \
            and cutoff >= dt.datetime.combine(day, dt.time(15, 16), IST)

    for done_at, _, res, und, bar in events:
        t = bar["t"]
        realised = sum(tr["net_pnl"] for bk in books.values() for tr in bk.trades if tr["status"] == "CLOSED")
        vix = vix_at(t)
        strat = "ORB1" if res == "1m" else "EMA9_21"
        bk = books[(strat, und)]
        e_now = emas[und].get(t) if res == "5m" else None
        signal = None
        if res == "5m":
            e_prev = emas[und].get(prev_t[und].get(t))
            if e_now and e_prev:  # track the EMA setup on every bar, trade open or not
                recent = [b for b in today5[und] if b["t"] <= t][-OPT["ema_sl_lookback"]:]
                signal = _ema_signal(ema_state[und], e_prev, e_now, bar, recent, OPT["points"][und])
        elif und in levels and t.time() >= OPT["orb_entry_from"]:
            signal = _orb1_signal(orb_state[und], levels[und], bar, und)
        if res == "1m":
            orb_state[und]["prev_close"] = bar["c"]

        tr = bk.open_trade()
        if tr:
            _manage(tr, bar, t, done_at, vix, e_now[0] if e_now else None)
            if tr["status"] == "CLOSED":
                if tr["net_pnl"] > 0:
                    bk.done, bk.note = True, "done for the day: trade made money"
                elif len(bk.trades) >= OPT["max_trades"]:
                    bk.done, bk.note = True, "done for the day: 2 losing trades"
            continue
        if not signal or t.time() >= OPT["entry_until"]:
            continue
        n = bk.trade_no()
        if n is None:
            continue
        if realised <= -OPT["day_loss_limit"]:
            bk.note = f"no new trades: day loss limit Rs {OPT['day_loss_limit']:,.0f} reached"
            continue
        kind, stop, why = signal
        if strat == "EMA9_21":
            pts = OPT["points"][und]
            dist = abs(bar["c"] - stop)
            if dist > pts["max_sl"]:
                notes[f"{strat}:{und}:{t:%H%M}"] = f"{t:%H:%M} {strat} {und} {kind} skipped: stop {dist:.0f} pts > max {pts['max_sl']}"
                continue
            if dist < pts["min_sl"]:
                stop = bar["c"] - pts["min_sl"] if kind == "CE" else bar["c"] + pts["min_sl"]
        trade = _enter(strat, und, kind, bar, done_at, stop, n, vix, why)
        # capital first: realised loss + what open trades can still lose + this trade's
        # risk must stay inside the day limit (a trade at break-even or better risks ~0)
        open_risk = sum(o["risk_at_stop"] for b2 in books.values() for o in b2.trades
                        if o["status"] == "OPEN" and not o["protected"])
        if -realised + open_risk + trade["risk_at_stop"] > OPT["day_loss_limit"]:
            notes[f"{strat}:{und}:{t:%H%M}"] = (f"{t:%H:%M} {strat} {und} {kind} skipped: would risk more than the "
                                                f"Rs {OPT['day_loss_limit']:,.0f} day limit")
            continue
        bk.trades.append(trade)

    # mark or close what is still open
    session_over = cutoff >= dt.datetime.combine(day, dt.time(15, 20), IST)
    for bk in books.values():
        tr = bk.open_trade()
        if not tr:
            continue
        if tr["strategy"] == "SELL_TIME":
            continue          # its own exits are fixed times
        bars = today1[tr["underlying"]] if tr["strategy"] == "ORB1" else today5[tr["underlying"]]
        if not bars:
            continue
        last = bars[-1]
        vix = vix_at(last["t"])
        if session_over:
            _exit(tr, tr["qty_open"], last["c"], last["t"], "SQUARE_OFF_15:15", vix)
            _close(tr)
        else:
            px = premium(tr["underlying"], tr["side"], tr["strike"], dt.date.fromisoformat(tr["expiry"]),
                         last["c"], last["t"], vix)
            tr["last_index"], tr["last_premium"] = round(last["c"], 2), round(px, 2)
            booked = sum((l["premium"] - tr["entry_premium"]) * l["qty"] for l in tr["legs"])
            tr["unrealised"] = round(booked + (px - tr["entry_premium"]) * tr["qty_open"], 2)

    trades = sorted((tr for bk in books.values() for tr in bk.trades), key=lambda x: x["entry_time"])
    status = {f"{s}:{u}": {"trades": len(books[(s, u)].trades), "done": books[(s, u)].done,
                           "note": books[(s, u)].note, "active": u == active,
                           "net": round(sum(t["net_pnl"] for t in books[(s, u)].trades), 2)}
              for (s, u) in books}
    ema_now = {}
    for u, e in emas.items():
        day_ts = [t for t in e if t.date() == day]
        if day_ts:
            last = e[max(day_ts)]
            ema_now[u] = {"ema9": round(last[0], 2), "ema21": round(last[1], 2), "setup": ema_state[u]["setup"]}
    return {"levels": levels, "trades": trades, "notes": notes, "status": status, "ema": ema_now,
            "index_today": active, "strategy_today": active_strat, "schedule": schedule_text()}


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


def _orb1_signal(st, lv, bar, und):
    """A 1-minute close that crosses the first 1-minute candle's high (CE) or low (PE)."""
    prev, sl = st["prev_close"], OPT["orb1"][und]["sl"]
    if prev is None:
        return None
    if prev <= lv["high"] < bar["c"]:
        return "CE", bar["c"] - sl, f"1-min close {bar['c']:.2f} above first-minute high {lv['high']:.2f}"
    if prev >= lv["low"] > bar["c"]:
        return "PE", bar["c"] + sl, f"1-min close {bar['c']:.2f} below first-minute low {lv['low']:.2f}"
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


def _enter(strat, und, kind, bar, at, stop, n, vix, why):
    lot = paper.lot_size(und)
    if strat == "ORB1":
        qty = OPT["orb1"][und]["qty"][n - 1]
        book_qty = 0
    else:
        qty = OPT["ema_lots"][n - 1] * lot
        book_qty = OPT["ema_book"][n - 1] * lot
    step = OPT["strike_step"][und]
    strike = int(round(bar["c"] / step) * step)
    expiry = expiry_for(und, at.date())
    entry_prem = premium(und, kind, strike, expiry, bar["c"], at, vix) * (1 + OPT["premium_slippage"])
    stop_prem = premium(und, kind, strike, expiry, stop, at, vix)
    pts = OPT["points"][und]
    return {
        "strategy": strat, "underlying": und, "side": kind, "strike": strike, "action": "BUY",
        "expiry": expiry.isoformat(), "symbol": f"{und} {strike} {kind}", "trade_no": n,
        "status": "OPEN", "why": why, "entry_time": at.isoformat(),
        "entry_index": round(bar["c"], 2), "entry_premium": round(entry_prem, 2), "vix": round(vix, 2),
        "lot": lot, "lots": round(qty / lot, 2), "qty": qty, "qty_open": qty, "book_qty": book_qty,
        "stop_index": round(stop, 2), "initial_stop": round(stop, 2), "sl_premium": round(stop_prem, 2),
        "partial_index": round(bar["c"] + (pts["partial"] if kind == "CE" else -pts["partial"]), 2),
        "partial_done": False, "protected": False, "best_points": 0.0, "stop_moves": [],
        "risk_at_stop": round(max(entry_prem - stop_prem, 0) * qty, 2), "legs": [],
        "gross_pnl": 0.0, "charges": 0.0, "net_pnl": 0.0,
    }


def _move_stop(tr, new_stop, at, why):
    d = 1 if tr["side"] == "CE" else -1
    if (new_stop - tr["stop_index"]) * d > 0.001:
        tr["stop_index"] = round(new_stop, 2)
        tr["stop_moves"].append({"time": at.isoformat(), "stop": tr["stop_index"], "why": why})
        tr["protected"] = (tr["stop_index"] - tr["entry_index"]) * d >= -0.001


def _manage(tr, bar, t, at, vix, ema9):
    """A wick through the stop fills it first; then the trail moves (ORB1 points
    ladder / EMA9_21 booking + break-even); EMA9_21 also exits on a body close
    across the 9 EMA."""
    if at <= dt.datetime.fromisoformat(tr["entry_time"]):
        return
    d = 1 if tr["side"] == "CE" else -1
    e = tr["entry_index"]
    if t.time() >= OPT["squareoff"]:
        _exit(tr, tr["qty_open"], bar["o"], at, "SQUARE_OFF_15:15", vix)
        return _close(tr)
    adverse = bar["l"] if d == 1 else bar["h"]
    if (adverse - tr["stop_index"]) * d <= 0:
        level = bar["o"] if (bar["o"] - tr["stop_index"]) * d < 0 else tr["stop_index"]  # gap through the stop
        off = (tr["stop_index"] - e) * d
        reason = "STOP" if off < -0.01 and not tr["stop_moves"] else (
            "BREAKEVEN_STOP" if abs(off) < 0.01 else "TRAIL_STOP")
        _exit(tr, tr["qty_open"], level, at, reason, vix)
        return _close(tr)
    fav = ((bar["h"] if d == 1 else bar["l"]) - e) * d
    tr["best_points"] = round(max(tr["best_points"], fav), 2)
    if tr["strategy"] == "ORB1":
        cfg, und_cfg = OPT["orb1"], OPT["orb1"][tr["underlying"]]
        k = math.floor(fav / cfg["step"]) * cfg["step"]
        if k >= cfg["breakeven_at"]:
            off = k - cfg["breakeven_at"]
            _move_stop(tr, e + d * off, at, f"+{k:.0f} pts -> stop {'break-even' if off == 0 else f'+{off:.0f}'}")
        elif und_cfg["early_trail"] and fav >= und_cfg["early_trail"][0]:
            trig, off = und_cfg["early_trail"]
            _move_stop(tr, e + d * off, at, f"+{trig} pts -> stop {off}")
        return
    pts = OPT["points"][tr["underlying"]]
    if not tr["partial_done"] and fav >= pts["partial"]:
        _exit(tr, tr["book_qty"], e + d * pts["partial"], at, "PARTIAL_+%d" % pts["partial"], vix)
        tr["partial_done"] = True
        _move_stop(tr, e, at, f"+{pts['partial']} pts booked -> stop break-even")
    if ema9 is not None and (bar["c"] - ema9) * d < 0:
        # only a candle BODY closing across the 9 EMA exits; a wick through it does not
        _exit(tr, tr["qty_open"], bar["c"], at, "EMA9_CLOSE", vix)
        return _close(tr)


def _exit(tr, qty, index_level, at, reason, vix):
    if qty <= 0:
        return
    px = premium(tr["underlying"], tr["side"], tr["strike"], dt.date.fromisoformat(tr["expiry"]),
                 index_level, at, vix) * (1 - OPT["premium_slippage"])
    tr["legs"].append({"time": at.isoformat(), "qty": qty, "lots": round(qty / tr["lot"], 2),
                       "index": round(index_level, 2), "premium": round(px, 2), "reason": reason})
    tr["qty_open"] -= qty


def _close(tr):
    tr["status"] = "CLOSED"
    last = tr["legs"][-1]
    tr.update(exit_time=last["time"], exit_reason=last["reason"], exit_index=last["index"],
              exit=last["premium"])
    sign = -1 if tr.get("action") == "SELL" else 1
    tr["gross_pnl"] = round(sign * sum((l["premium"] - tr["entry_premium"]) * l["qty"] for l in tr["legs"]), 2)
    tr["charges"] = option_charges(tr["qty"], tr["entry_premium"], [(l["qty"], l["premium"]) for l in tr["legs"]],
                                   tr.get("action", "BUY"))
    tr["net_pnl"] = round(tr["gross_pnl"] - tr["charges"], 2)
    tr["r_multiple"] = round(tr["net_pnl"] / tr["risk_at_stop"], 2) if tr["risk_at_stop"] > 0 else None
    tr["entry"] = tr["entry_premium"]
    tr.pop("unrealised", None)


def _sell_time(und, day, bars, step, vix_at, notes, cutoff):
    """Tuesday NIFTY: sell a CE + PE pair per window at a fixed time, buy back at a fixed
    time. Strikes from the index at entry: OTM1 = one strike out of the money each side."""
    cfg, lot = OPT["sell"], paper.lot_size(und)
    qty, k_step = cfg["lots"] * lot, OPT["strike_step"][und]
    expiry = expiry_for(und, day, same_day=True)
    slip = OPT["premium_slippage"]
    done_at = lambda b: b["t"] + step
    trades = []
    for w in cfg["windows"]:
        entry_bar = next((b for b in bars if done_at(b).time() >= w["entry"]), None)
        if entry_bar is None:
            continue
        at = done_at(entry_bar)
        if at.time() >= w["exit"]:
            notes[f"SELL:{w['name']}"] = f"{w['name']}: no candle at {w['entry']:%H:%M}"
            continue
        S, vix = entry_bar["c"], vix_at(entry_bar["t"])
        atm = int(round(S / k_step) * k_step)
        strikes = {"CE": atm + w["otm"] * k_step, "PE": atm - w["otm"] * k_step}
        pair = []
        for kind, k in strikes.items():
            prem = premium(und, kind, k, expiry, S, at, vix) * (1 - slip)   # selling: we receive a little less
            pair.append({
                "strategy": "SELL_TIME", "underlying": und, "side": kind, "action": "SELL", "strike": k,
                "expiry": expiry.isoformat(), "symbol": f"{und} {k} {kind}", "trade_no": len(trades) // 2 + 1,
                "window": w["name"], "status": "OPEN",
                "why": f"{w['name']}: sell {'OTM1' if w['otm'] else 'ATM'} {kind} at {at:%H:%M}, index {S:.2f}",
                "entry_time": at.isoformat(), "entry_index": round(S, 2), "entry_premium": round(prem, 2),
                "vix": round(vix, 2), "lot": lot, "lots": cfg["lots"], "qty": qty, "qty_open": qty, "book_qty": 0,
                "stop_index": None, "initial_stop": None, "sl_premium": round(prem * (1 + cfg["pair_stop"]), 2),
                "partial_done": False, "protected": False, "best_points": 0.0, "stop_moves": [],
                "risk_at_stop": round(prem * cfg["pair_stop"] * qty, 2), "legs": [],
                "gross_pnl": 0.0, "charges": 0.0, "net_pnl": 0.0,
            })
        credit = sum(t["entry_premium"] for t in pair)
        stop_level = credit * (1 + cfg["pair_stop"])
        exit_at, reason = None, None
        for b in bars:
            t_done = done_at(b)
            if t_done <= at:
                continue
            if t_done.time() > w["exit"]:
                break
            # worst case inside the candle: the CE is dearest at the high, the PE at the low
            worst = max(sum(premium(und, x["side"], x["strike"], expiry, px, t_done, vix_at(b["t"])) for x in pair)
                        for px in (b["h"], b["l"]))
            if worst >= stop_level:
                px = b["h"] if (sum(premium(und, x["side"], x["strike"], expiry, b["h"], t_done, vix_at(b["t"])) for x in pair)
                                >= sum(premium(und, x["side"], x["strike"], expiry, b["l"], t_done, vix_at(b["t"])) for x in pair)) else b["l"]
                exit_at, reason, exit_px = t_done, "PAIR_STOP_+50%", px
                break
            if t_done.time() >= w["exit"]:
                exit_at, reason, exit_px = t_done, f"TIME_EXIT_{w['exit']:%H:%M}", b["c"]
                break
        for x in pair:
            if exit_at is None:   # window still running: mark to the last completed candle
                last = [b for b in bars if done_at(b) <= cutoff and done_at(b) > at]
                if last:
                    px = premium(und, x["side"], x["strike"], expiry, last[-1]["c"], done_at(last[-1]), vix_at(last[-1]["t"]))
                    x["last_index"], x["last_premium"] = round(last[-1]["c"], 2), round(px, 2)
                    x["unrealised"] = round((x["entry_premium"] - px) * qty, 2)
                continue
            buy_back = premium(und, x["side"], x["strike"], expiry, exit_px, exit_at, vix_at(exit_at)) * (1 + slip)
            x["legs"].append({"time": exit_at.isoformat(), "qty": qty, "lots": cfg["lots"],
                              "index": round(exit_px, 2), "premium": round(buy_back, 2), "reason": reason})
            x["qty_open"] = 0
            _close(x)
        trades += pair
    return trades


# ------------------------------------------------------------ data helpers

def fetch():
    """Recent 5m + 1m bars and VIX: the local archive topped up with a fresh Yahoo fetch."""
    import history
    und5, und1 = {}, {}
    for u in OPT["underlyings"]:
        ys = paper.yahoo_of(u)
        und5[u] = history.merged(u, nsemkt.yahoo_chart(ys, interval="5m", range_="5d")["bars"])
        try:
            und1[u] = history.merged(u, nsemkt.yahoo_chart(ys, interval="1m", range_="1d")["bars"], days=2, interval="1m")
        except Exception:
            und1[u] = history.load(u, start=paper.now_ist().date(), interval="1m")
    try:
        vix = history.merged("INDIAVIX", nsemkt.yahoo_chart("^INDIAVIX", interval="5m", range_="5d")["bars"])
    except Exception:
        vix = history.load("INDIAVIX")
    return und5, und1, vix
