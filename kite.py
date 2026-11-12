"""Import a year (or more) of candles from Zerodha Kite Connect into the bar archive.

Free Yahoo data only reaches back ~60 days (5-minute) and ~30 days (1-minute). Kite
Connect's historical API serves years of minute candles, so the backtest can cover a
full year. This only READS market data; it never places orders.

You need a Kite Connect app (developers.kite.trade) with historical data access.
Keep the keys out of chat and out of git: set them in your own terminal.

    PowerShell:  $env:KITE_API_KEY = "your_api_key"; $env:KITE_API_SECRET = "your_api_secret"
    then:        python kite.py login                # opens the Kite login, you sign in
                 python kite.py history --days 365   # NIFTY, SENSEX, INDIA VIX: 1m + 5m
                 python kite.py history --days 365 --stocks   # also the Nifty 50 stocks (5m)

The access token is saved outside the project (~/.nse_kite_token.json) and expires
every morning, so run `login` again on another day.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import time
import urllib.parse
import urllib.request
import webbrowser

import history
import paper
from paper import IST

API = "https://api.kite.trade"
TOKEN_FILE = os.path.join(os.path.expanduser("~"), ".nse_kite_token.json")
# Kite instrument tokens for the indices (looked up from the instruments list when possible)
INDEX_TOKENS = {"NIFTY": ("NSE", "NIFTY 50", 256265), "SENSEX": ("BSE", "SENSEX", 265),
                "INDIAVIX": ("NSE", "INDIA VIX", 264969)}
MAX_DAYS = {"minute": 60, "5minute": 100}   # Kite's per-request limits
OUR_INTERVAL = {"minute": "1m", "5minute": "5m"}


def _key():
    k = os.environ.get("KITE_API_KEY")
    if not k:
        raise SystemExit("Set KITE_API_KEY in this terminal first (see python kite.py --help).")
    return k


def _request(path, params=None, data=None, token=None):
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    headers = {"X-Kite-Version": "3"}
    if token:
        headers["Authorization"] = f"token {_key()}:{token}"
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode("utf-8")
    return raw if path == "/instruments" else json.loads(raw)


def login():
    key, secret = _key(), os.environ.get("KITE_API_SECRET")
    if not secret:
        raise SystemExit("Set KITE_API_SECRET in this terminal first.")
    url = f"https://kite.zerodha.com/connect/login?v=3&api_key={key}"
    print("Opening the Kite login page. Sign in there yourself (password and 2FA stay with you).")
    print(url)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    got = input("After signing in you land on your app's redirect URL. Paste that full URL (or just the "
                "request_token) here: ").strip()
    rt = urllib.parse.parse_qs(urllib.parse.urlparse(got).query).get("request_token", [got])[0]
    checksum = hashlib.sha256((key + rt + secret).encode()).hexdigest()
    res = _request("/session/token", data={"api_key": key, "request_token": rt, "checksum": checksum})
    token = res["data"]["access_token"]
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump({"access_token": token, "date": dt.date.today().isoformat()}, f)
    print(f"Logged in. Token saved to {TOKEN_FILE} (valid until tomorrow morning).")


def _token():
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return json.load(f)["access_token"]
    except (OSError, ValueError, KeyError):
        raise SystemExit("No Kite session. Run: python kite.py login")


def _instrument_tokens(token, stocks):
    """{our_symbol: instrument_token} for the indices (+ Nifty 50 stocks)."""
    out = {s: t for s, (_, _, t) in INDEX_TOKENS.items()}
    wanted_idx = {(ex, name): s for s, (ex, name, _) in INDEX_TOKENS.items()}
    wanted_stocks = set(stocks)
    try:
        csv_text = _request("/instruments", token=token)
    except Exception as e:
        print(f"instrument list unavailable ({e}); using known index tokens")
        return out
    lines = csv_text.splitlines()
    head = lines[0].split(",")
    i_tok, i_sym, i_ex, i_type, i_seg = (head.index(c) for c in
                                         ("instrument_token", "tradingsymbol", "exchange", "instrument_type", "segment"))
    for line in lines[1:]:
        f = line.split(",")
        if len(f) < len(head):
            continue
        key = (f[i_ex], f[i_sym].strip('"'))
        if key in wanted_idx:
            out[wanted_idx[key]] = int(f[i_tok])
        elif f[i_ex] == "NSE" and f[i_type] == "EQ" and f[i_seg] == "NSE" and f[i_sym] in wanted_stocks:
            out[f[i_sym]] = int(f[i_tok])
    return out


def _fetch(token, inst, interval, start, end):
    bars, cur = [], start
    while cur <= end:
        stop = min(end, cur + dt.timedelta(days=MAX_DAYS[interval] - 1))
        res = _request(f"/instruments/historical/{inst}/{interval}",
                       {"from": f"{cur} 09:00:00", "to": f"{stop} 15:45:00"}, token=token)
        for ts, o, h, l, c, *v in res["data"]["candles"]:
            t = dt.datetime.fromisoformat(ts.replace("+0530", "+05:30")).astimezone(IST)
            bars.append({"t": t, "o": o, "h": h, "l": l, "c": c, "v": (v[0] if v else 0) or 0})
        cur = stop + dt.timedelta(days=1)
        time.sleep(0.4)   # Kite allows ~3 historical requests a second
    return bars


def history_import(days, stocks):
    token = _token()
    end = dt.date.today()
    start = end - dt.timedelta(days=days)
    universe = []
    if stocks:
        import backtest
        with open(backtest.UNIVERSE, encoding="utf-8-sig") as f:
            universe = [s.strip() for s in f if s.strip() and not s.startswith("#")]
    tokens = _instrument_tokens(token, universe)
    for sym in list(INDEX_TOKENS) + universe:
        inst = tokens.get(sym)
        if not inst:
            print(f"{sym}: no instrument token, skipped")
            continue
        for interval in (("minute", "5minute") if sym in INDEX_TOKENS else ("5minute",)):
            try:
                bars = _fetch(token, inst, interval, start, end)
            except Exception as e:
                print(f"{sym} {interval}: {e}")
                continue
            added = history.merge(sym, bars, OUR_INTERVAL[interval])
            print(f"{sym} {OUR_INTERVAL[interval]}: {len(bars)} candles, +{added} new")
    print(json.dumps({"5m": history.info("5m"), "1m": history.info("1m")}, indent=1))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login")
    h = sub.add_parser("history")
    h.add_argument("--days", type=int, default=365)
    h.add_argument("--stocks", action="store_true", help="also import 5-minute candles for the Nifty 50 stocks")
    a = p.parse_args()
    login() if a.cmd == "login" else history_import(a.days, a.stocks)
