#!/usr/bin/env python3
"""Scan NSE equities for repeatable daily or opening-range setups.

Data comes from Yahoo's chart endpoint; symbol universes come from bundled NSE
index constituent files or the official NSE equity master.  The scanner is
advisory only and writes ./nse-trader/watchlist.json for the news and entry
skills.  It never places an order.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import defaultdict

import nsemkt


HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.normpath(os.path.join(HERE, "..", "assets"))


def load_symbols(spec: str) -> list[str]:
    """Resolve an index, the full EQ list, or file:PATH to unique symbols."""
    spec = spec.strip()
    if spec.startswith("file:"):
        path = spec[5:]
        if not os.path.isfile(path):
            raise ValueError(f"universe file not found: {path}")
        with open(path, encoding="utf-8-sig") as f:
            raw = [line.strip().split(",")[0] for line in f]
    elif spec in ("nifty50", "nifty100", "nifty500"):
        path = os.path.join(ASSETS, f"{spec}.txt")
        if not os.path.isfile(path):
            raise ValueError(f"bundled universe missing: {path}")
        with open(path, encoding="utf-8-sig") as f:
            raw = [line.strip() for line in f]
    elif spec == "eq":
        raw = [r["symbol"] for r in nsemkt.nse_symbols() if r.get("series") == "EQ"]
    else:
        raise ValueError("universe must be nifty50, nifty100, nifty500, eq, or file:PATH")

    seen: set[str] = set()
    out: list[str] = []
    for symbol in raw:
        symbol = symbol.strip().upper()
        if symbol and symbol not in seen and not symbol.startswith("#"):
            seen.add(symbol)
            out.append(symbol)
    return out


def average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def atr_from_bars(bars: list[dict], period: int = 14) -> float:
    if len(bars) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        trs.append(max(
            bars[i]["h"] - bars[i]["l"],
            abs(bars[i]["h"] - bars[i - 1]["c"]),
            abs(bars[i]["l"] - bars[i - 1]["c"]),
        ))
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def daily_candidate(symbol: str, min_price: float, min_turnover_cr: float) -> dict | None:
    """Score one symbol from daily bars; return None when no setup survives."""
    # One year is needed for a true 52-week-high test. Other setup windows use
    # at most six months.
    data = nsemkt.yahoo_chart(symbol, "1d", "1y")
    bars = data["bars"]
    if len(bars) < 35:
        return None

    current = bars[-1]
    previous = bars[-2]
    close = current["c"]
    if close < min_price:
        return None

    avg_vol20 = average([b["v"] for b in bars[-21:-1]])
    turnover_cr = close * avg_vol20 / 10_000_000.0
    if turnover_cr < min_turnover_cr:
        return None

    closes = [b["c"] for b in bars]
    vols = [b["v"] for b in bars]
    sma20 = nsemkt.sma(closes, 20) or close
    prev_sma20 = nsemkt.sma(closes[:-1], 20) or previous["c"]
    sma50 = nsemkt.sma(closes, 50) or sma20
    rsi14 = nsemkt.rsi(closes, 14)
    prev_rsi = nsemkt.rsi(closes[:-1], 14)
    atr = atr_from_bars(bars)
    prior20_high = max(b["h"] for b in bars[-21:-1])
    prior20_close = max(b["c"] for b in bars[-21:-1])
    high52 = max(b["h"] for b in bars[-252:])
    vol_ratio = current["v"] / avg_vol20 if avg_vol20 else 0.0
    day_pct = (close / previous["c"] - 1.0) * 100.0 if previous["c"] else 0.0

    setups: list[str] = []
    reasons: list[str] = []
    score = 0

    if close > prior20_high or (close > prior20_close and current["h"] > prior20_high):
        setups.append("20D_BREAKOUT")
        score += 34
        reasons.append(f"close above prior 20-day pivot {prior20_high:.2f}")
        if vol_ratio >= 1.5:
            score += 8

    # A practical flag: strong 15-day impulse, shallow 3-7 day pullback, price
    # still above the 20-day average, and today's close turns upward.
    impulse_start = bars[-16]["c"]
    impulse_high = max(b["h"] for b in bars[-10:-2])
    impulse_pct = (impulse_high / impulse_start - 1.0) * 100.0 if impulse_start else 0.0
    pullback_pct = (impulse_high - previous["c"]) / impulse_high * 100.0 if impulse_high else 0.0
    if impulse_pct >= 7 and 0 <= pullback_pct <= 7 and close > sma20 and close > previous["c"]:
        setups.append("BULL_FLAG")
        score += 28
        reasons.append(f"{impulse_pct:.1f}% impulse, {pullback_pct:.1f}% pullback")

    if previous["c"] <= prev_sma20 and close > sma20:
        setups.append("RECLAIM_20MA")
        score += 24
        reasons.append(f"reclaimed 20DMA {sma20:.2f}")

    if rsi14 is not None and rsi14 <= 35:
        setups.append("OVERSOLD")
        score += 18
        reasons.append(f"RSI14 {rsi14:.1f}")

    if prev_rsi is not None and prev_rsi <= 35 and rsi14 is not None and rsi14 > prev_rsi and day_pct > 0.75:
        setups.append("OVERSOLD_BOUNCE")
        score += 26
        reasons.append(f"RSI turned up {prev_rsi:.1f}->{rsi14:.1f}")

    proximity = close / high52 if high52 else 0.0
    if proximity >= 0.97:
        setups.append("52W_HIGH_PROX")
        score += 16
        reasons.append(f"{(1-proximity)*100:.1f}% below 52-week high")

    if not setups:
        return None

    # Quality modifiers: liquid volume, trend alignment, and today’s demand.
    if close > sma20 > sma50:
        score += 10
    elif close > sma20:
        score += 5
    if vol_ratio >= 1.2:
        score += min(10, int((vol_ratio - 1.0) * 10))
    if turnover_cr >= 50:
        score += 4

    pivot = prior20_high
    stop_hint = max(0.0, close - (atr if atr else close * 0.008))
    return {
        "symbol": symbol,
        "ltp": round(close, 2),
        "chg_pct": round(day_pct, 2),
        "setup": ",".join(setups),
        "score": min(100, score),
        "note": "; ".join(reasons),
        "turnover_cr": round(turnover_cr, 2),
        "rsi14": round(rsi14, 1) if rsi14 is not None else None,
        "volume_ratio": round(vol_ratio, 2),
        "levels": {
            "or_high": None,
            "or_low": None,
            "pivot": round(pivot, 2),
            "stop_hint": round(nsemkt.round_tick(stop_hint, "down"), 2),
        },
    }


def session_groups(bars: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for bar in bars:
        t = bar["t"]
        if t.weekday() < 5 and nsemkt.dt.time(9, 15) <= t.time() <= nsemkt.dt.time(15, 30):
            groups[t.date().isoformat()].append(bar)
    return dict(groups)


def intraday_candidate(symbol: str) -> dict | None:
    data = nsemkt.yahoo_chart(symbol, "5m", "5d")
    groups = session_groups(data["bars"])
    if not groups:
        return None
    day = sorted(groups)[-1]
    bars = sorted(groups[day], key=lambda b: b["t"])
    opening = [b for b in bars if nsemkt.dt.time(9, 15) <= b["t"].time() < nsemkt.dt.time(9, 30)]
    after = [b for b in bars if b["t"].time() >= nsemkt.dt.time(9, 30)]
    if len(opening) < 2 or not after:
        return None
    or_high = max(b["h"] for b in opening)
    or_low = min(b["l"] for b in opening)
    or_mid = (or_high + or_low) / 2.0
    breakout = next((b for b in after if b["c"] > or_high), None)
    breakdown = next((b for b in after if b["c"] < or_low), None)
    last = bars[-1]
    prev_close = float(data["meta"].get("chartPreviousClose") or opening[0]["o"])
    chg_pct = (last["c"] / prev_close - 1) * 100 if prev_close else 0.0
    setups: list[str] = []
    notes: list[str] = []
    score = 0
    if breakout:
        setups.append("OR_BREAKOUT")
        score += 60
        notes.append(f"first close above OR at {breakout['t'].strftime('%H:%M')} IST")
    if breakdown:
        setups.append("OR_LOW_BREAK")
        score -= 25
        notes.append(f"OR low broke at {breakdown['t'].strftime('%H:%M')} IST")
    if not setups:
        return None
    return {
        "symbol": symbol,
        "ltp": round(last["c"], 2),
        "chg_pct": round(chg_pct, 2),
        "setup": ",".join(setups),
        "score": max(0, min(100, score)),
        "note": f"session {day}; " + "; ".join(notes),
        "levels": {
            "or_high": round(or_high, 2),
            "or_low": round(or_low, 2),
            "pivot": round(or_high, 2),
            "stop_hint": round(nsemkt.round_tick(or_mid, "down"), 2),
        },
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Scan NSE charts for daily or opening-range setups.")
    p.add_argument("--mode", required=True, choices=["daily", "intraday"])
    p.add_argument("--universe", default="nifty100",
                   help="nifty50|nifty100|nifty500|eq|file:PATH")
    p.add_argument("--limit", type=int, default=None,
                   help="scan only the first N symbols (useful for a smoke test)")
    p.add_argument("--top", type=int, default=10, help="keep the top N matches")
    p.add_argument("--out", default=None)
    p.add_argument("--min-price", type=float, default=50.0)
    p.add_argument("--min-turnover", type=float, default=5.0,
                   help="minimum average daily turnover in crore rupees")
    args = p.parse_args()

    try:
        symbols = load_symbols(args.universe)
    except (ValueError, RuntimeError) as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        sys.exit(2)
    if args.limit is not None:
        if args.limit < 1:
            print("REFUSED: --limit must be positive", file=sys.stderr)
            sys.exit(2)
        symbols = symbols[:args.limit]

    matches: list[dict] = []
    errors: list[str] = []
    print(f"Scanning {len(symbols)} NSE symbols ({args.mode}) ...")
    for i, symbol in enumerate(symbols, 1):
        try:
            if args.mode == "daily":
                item = daily_candidate(symbol, args.min_price, args.min_turnover)
            else:
                item = intraday_candidate(symbol)
            if item:
                matches.append(item)
        except Exception as e:  # one bad symbol must not kill the universe scan
            errors.append(f"{symbol}: {e}")
        if i < len(symbols):
            time.sleep(0.12)

    matches.sort(key=lambda x: (x["score"], x.get("turnover_cr", 0)), reverse=True)
    matches = matches[: max(0, args.top)]
    payload = {
        "generated_ist": nsemkt.now_ist().isoformat(timespec="seconds"),
        "mode": args.mode,
        "universe": args.universe,
        "scanned": len(symbols),
        "matched": len(matches),
        "items": matches,
        "errors": errors[:20],
        "data_note": "Yahoo chart data may be delayed; confirm prices in your broker before acting.",
    }
    out_dir = args.out or nsemkt.STATE_DIR
    path = nsemkt.save_json(os.path.join(out_dir, "watchlist.json"), payload)

    print(f"MATCHES {len(matches)} (showing top {args.top})")
    for item in matches:
        print(f"  {item['symbol']:<14} {item['ltp']:>10.2f}  {item['chg_pct']:>+6.2f}%  "
              f"score {item['score']:>3}  {item['setup']}")
    if errors:
        print(f"WARNING: {len(errors)} symbols failed; first errors recorded in JSON")
    print(f"saved: {path}")


if __name__ == "__main__":
    main()
