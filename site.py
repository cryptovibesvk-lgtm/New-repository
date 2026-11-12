"""Export the dashboard as a static site in docs/ (for GitHub Pages).

    python site.py

Writes docs/index.html (a copy of dashboard.html) and docs/data/*.json with the
same shapes the local dashboard API returns, so the page works without a server.
"""
import datetime as dt
import hashlib
import json
import os
import shutil

import dashboard
import paper
from paper import IST

HERE = paper.HERE
DOCS = os.path.join(HERE, "docs")
DATA = os.path.join(DOCS, "data")
CLOUD_LOG = os.path.join(paper.PAPER_DIR, "cloud.log")
REPLAY_DAYS = 20
REPLAY_VERSION = 5  # 5 = weekly schedule incl. Tuesday SELL_TIME, trading-time pricing


def dump(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, default=str, separators=(",", ":"))


def tail_log(n=300):
    try:
        with open(CLOUD_LOG, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    with open(CLOUD_LOG, "w", encoding="utf-8") as f:  # keep the file small
        f.write("\n".join(lines[-n:]) + "\n")
    return lines[-40:]


def export():
    os.makedirs(DOCS, exist_ok=True)
    # stamp the page with a version so an old cached copy notices and reloads itself
    with open(os.path.join(HERE, "dashboard.html"), encoding="utf-8") as f:
        page = f.read()
    version = hashlib.md5(page.encode("utf-8")).hexdigest()[:10]
    with open(os.path.join(DOCS, "index.html"), "w", encoding="utf-8") as f:
        f.write(page.replace("__APP_VERSION__", version))
    open(os.path.join(DOCS, ".nojekyll"), "w").close()

    state = dashboard.api_state()
    st = dict(state["status"])
    try:
        with open(paper.STATUS, encoding="utf-8") as f:
            raw = json.load(f)
        st.update(state=raw["state"], message=raw["message"], updated=raw["updated"])
    except (OSError, ValueError, KeyError):
        pass
    st["running"] = st.get("state") in dashboard.LIVE_STATES
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    state.update(status=st, log=tail_log(), static=True, app_version=version,
                 exported_at=dt.datetime.now(IST).isoformat(timespec="seconds"),
                 actions_url=f"https://github.com/{repo}/actions" if repo else "")
    dump(os.path.join(DATA, "state.json"), state)

    for sym in state["today"]["watchlist"]:
        try:
            dump(os.path.join(DATA, "bars", f"{sym}.json"), dashboard.api_bars(sym, None))
            if sym in paper.INDEXES:
                dump(os.path.join(DATA, "bars", f"{sym}-1m.json"), dashboard.api_bars(sym, None, "1m"))
        except Exception as e:
            print(f"bars {sym}: {e}")

    # replays only change when the watchlist or the rules do, so build them once per
    # watchlist and REPLAY_VERSION (bump it whenever strategy rules change)
    marker = os.path.join(DATA, "replay", "_watchlist.json")
    wl = state["today"]["watchlist"]
    try:
        with open(marker, encoding="utf-8") as f:
            same = json.load(f) == {"version": REPLAY_VERSION, "watchlist": wl}
    except (OSError, ValueError):
        same = False
    if wl and not same:
        shutil.rmtree(os.path.join(DATA, "replay"), ignore_errors=True)
        days = dashboard.api_days()["days"][:REPLAY_DAYS]
        for d in days:
            rep = dashboard.api_replay(d)
            rep["bars"] = {s: dashboard.api_bars(s, d)["bars"] for s in wl}
            rep["bars1m"] = {s: dashboard.api_bars(s, d, "1m")["bars"] for s in paper.INDEXES}
            dump(os.path.join(DATA, "replay", f"{d}.json"), rep)
        dump(os.path.join(DATA, "days.json"), {"days": days})
        dump(marker, {"version": REPLAY_VERSION, "watchlist": wl})
    import backtest
    path = os.path.join(DATA, "backtest.json")
    bt = backtest.export(path, backtest.run(), dashboard.site_stocks(path))
    print(f"backtest: {len(bt['index']['days'])} index sessions, "
          f"{len((bt.get('stocks') or {}).get('days') or [])} stock sessions")
    print(f"site exported: {DOCS}")


if __name__ == "__main__":
    export()
