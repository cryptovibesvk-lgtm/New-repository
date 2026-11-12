"""Local dashboard for the virtual-money paper trader.

    python dashboard.py            # then open http://127.0.0.1:8765

Serves dashboard.html plus a small JSON API over paper/account.json. The Start
button launches `paper.py run` (virtual money only); nothing here talks to a broker.
"""
import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import paper
from paper import nsemkt, IST

HERE = paper.HERE
PORT = int(os.environ.get("PORT", "8765"))
LIVE_STATES = {"starting", "waiting", "scanning", "trading"}

_proc = None
_cache = {}
_lock = threading.Lock()


def chart(symbol, range_):
    """Yahoo 5-minute chart, cached (1 min for today, 30 min for history)."""
    ttl = 60 if range_ == "1d" else 1800
    key = (symbol, range_)
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    data = nsemkt.yahoo_chart(paper.yahoo_of(symbol), interval="5m", range_=range_)
    with _lock:
        _cache[key] = (time.time(), data)
    return data


def session_bars(symbol, day, range_):
    bars = chart(symbol, range_)["bars"]
    return {b["t"]: b for b in bars
            if b["t"].date() == day and dt.time(9, 15) <= b["t"].time() < dt.time(15, 30)}


def read_status():
    try:
        with open(paper.STATUS, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        return {"state": "off", "message": "Not started", "running": False}
    age = (dt.datetime.now(IST) - dt.datetime.fromisoformat(st["updated"])).total_seconds()
    st["running"] = st["state"] in LIVE_STATES and age < 120
    if st["state"] in LIVE_STATES and not st["running"]:
        st.update(state="stopped", message="Session stopped (no heartbeat)")
    return st


def watch_symbols(acct, day):
    """NIFTY and SENSEX always come first, then the day's stock picks."""
    idx = list(paper.INDEXES)
    rec = acct["days"].get(day.isoformat(), {})
    if rec.get("watchlist"):
        return idx + rec["watchlist"], "today"
    try:
        with open(os.path.join(HERE, "nse-trader", "watchlist.json"), encoding="utf-8") as f:
            wl = json.load(f)
        items = wl.get("items", wl) if isinstance(wl, dict) else wl
        return idx + [i["symbol"] for i in items][: paper.CONFIG["watchlist_size"]], "last scan"
    except (OSError, ValueError, KeyError):
        return idx, "none"


def quotes(symbols):
    out = {}
    for s in symbols:
        try:
            c = chart(s, "1d")
            bars = c["bars"]
            prev = c["meta"].get("chartPreviousClose") or c["meta"].get("previousClose")
            last = bars[-1]["c"] if bars else c["meta"].get("regularMarketPrice")
            out[s] = {"last": last, "prev": prev,
                      "chg_pct": (last / prev - 1) * 100 if last and prev else None,
                      "as_of": bars[-1]["t"].isoformat() if bars else None}
        except Exception as e:
            out[s] = {"error": str(e)}
    return out


def api_state():
    acct = paper.load_account()
    now = dt.datetime.now(IST)
    day = now.date()
    rec = acct["days"].get(day.isoformat(), {})
    symbols, source = watch_symbols(acct, day)
    all_trades = [t for r in acct["days"].values() for t in r.get("trades", [])]
    s = paper.summarise(all_trades)
    today_trades = rec.get("trades", [])
    realised = sum(t["net_pnl"] for t in today_trades if t["status"] == "CLOSED")
    unrealised = sum(t.get("unrealised", 0) for t in today_trades if t["status"] == "OPEN")
    equity, curve = acct["start_capital"], [{"date": "start", "equity": acct["start_capital"]}]
    for d, r in sorted(acct["days"].items()):
        equity += sum(t["net_pnl"] for t in r.get("trades", []) if t["status"] == "CLOSED")
        curve.append({"date": d, "equity": round(equity, 2)})
    log = []
    try:
        with open(os.path.join(paper.PAPER_DIR, "dashboard-run.log"), encoding="utf-8", errors="replace") as f:
            log = f.read().splitlines()[-40:]
    except OSError:
        pass
    return {
        "now": now.isoformat(timespec="seconds"),
        "session": nsemkt.session_state(now),
        "status": read_status(),
        "config": {k: (v.strftime("%H:%M") if isinstance(v, dt.time) else v) for k, v in paper.CONFIG.items()},
        "indexes": {s: {"lot": paper.lot_size(s)} for s in paper.INDEXES},
        "start_capital": acct["start_capital"],
        "equity": round(equity, 2),
        "summary": s,
        "today": {"date": day.isoformat(), "watchlist": symbols, "watchlist_source": source,
                  "ranges": rec.get("ranges", {}), "trades": today_trades, "notes": rec.get("notes", {}),
                  "realised": round(realised, 2), "unrealised": round(unrealised, 2),
                  "updated_at": rec.get("updated_at")},
        "quotes": quotes(symbols),
        "curve": curve,
        "log": log,
    }


def api_bars(symbol, date):
    today = dt.datetime.now(IST).date()
    day = dt.date.fromisoformat(date) if date else today
    bars = session_bars(symbol, day, "1d" if day == today else "60d")
    shown = day
    if not bars:  # before the open: show the latest session instead
        hist = chart(symbol, "60d")["bars"]
        if hist:
            shown = max(b["t"].date() for b in hist)
            bars = session_bars(symbol, shown, "60d")
    return {"symbol": symbol, "requested": day.isoformat(), "date": shown.isoformat(),
            "bars": [{"time": int(t.timestamp()), "open": b["o"], "high": b["h"], "low": b["l"],
                      "close": b["c"], "volume": b["v"]} for t, b in sorted(bars.items())]}


def api_days():
    acct = paper.load_account()
    symbols, _ = watch_symbols(acct, dt.datetime.now(IST).date())
    if not symbols:
        return {"days": []}
    today = dt.datetime.now(IST).date()
    days = sorted({b["t"].date() for b in chart(symbols[0], "60d")["bars"]} - {today}, reverse=True)
    return {"days": [d.isoformat() for d in days]}


def api_replay(date):
    acct = paper.load_account()
    day = dt.date.fromisoformat(date)
    symbols, _ = watch_symbols(acct, dt.datetime.now(IST).date())
    bars = {s: session_bars(s, day, "60d") for s in symbols}
    res = paper.simulate_day(day, bars, paper.CONFIG["start_capital"])
    quotes_ = {}
    for s, b in bars.items():
        if b:
            ts = sorted(b)
            quotes_[s] = {"last": b[ts[-1]]["c"], "prev": b[ts[0]]["o"],
                          "chg_pct": (b[ts[-1]]["c"] / b[ts[0]]["o"] - 1) * 100}
    realised = sum(t["net_pnl"] for t in res["trades"] if t["status"] == "CLOSED")
    return {"date": date, "watchlist": symbols, "quotes": quotes_, "realised": round(realised, 2), **res}


def start_session():
    global _proc
    if read_status()["running"]:
        return {"ok": True, "message": "Already running"}
    log = open(os.path.join(paper.PAPER_DIR, "dashboard-run.log"), "a", encoding="utf-8")
    log.write(f"\n=== started from dashboard {dt.datetime.now(IST):%Y-%m-%d %H:%M:%S} IST ===\n")
    log.flush()
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    _proc = subprocess.Popen([sys.executable, "-u", os.path.join(HERE, "paper.py"), "run"],
                             cwd=HERE, stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
    paper.write_status("starting", "Starting virtual session")
    return {"ok": True, "message": "Virtual trading started", "pid": _proc.pid}


def stop_session():
    global _proc
    st = read_status()
    if _proc and _proc.poll() is None:
        _proc.terminate()
    elif st.get("running") and st.get("pid"):
        subprocess.run(["taskkill", "/PID", str(st["pid"]), "/T", "/F"], capture_output=True)
    _proc = None
    paper.write_status("stopped", "Stopped from the dashboard")
    return {"ok": True, "message": "Virtual trading stopped"}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(HERE, "dashboard.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if u.path == "/api/state":
                return self._send(200, api_state())
            if u.path == "/api/bars":
                return self._send(200, api_bars(q["symbol"], q.get("date")))
            if u.path == "/api/days":
                return self._send(200, api_days())
            if u.path == "/api/replay":
                return self._send(200, api_replay(q["date"]))
            self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def do_POST(self):
        try:
            if self.path == "/api/start":
                return self._send(200, start_session())
            if self.path == "/api/stop":
                return self._send(200, stop_session())
            self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    os.makedirs(paper.PAPER_DIR, exist_ok=True)
    print(f"NSE paper-trading dashboard: http://127.0.0.1:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
