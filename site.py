"""Export the dashboard as a static site in docs/ (for GitHub Pages).

    python site.py

Writes docs/index.html (a copy of dashboard.html) and docs/data/*.json with the
same shapes the local dashboard API returns, so the page works without a server.
"""
import datetime as dt
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
    shutil.copyfile(os.path.join(HERE, "dashboard.html"), os.path.join(DOCS, "index.html"))
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
    state.update(status=st, log=tail_log(), static=True,
                 exported_at=dt.datetime.now(IST).isoformat(timespec="seconds"),
                 actions_url=f"https://github.com/{repo}/actions" if repo else "")
    dump(os.path.join(DATA, "state.json"), state)

    for sym in state["today"]["watchlist"]:
        try:
            dump(os.path.join(DATA, "bars", f"{sym}.json"), dashboard.api_bars(sym, None))
        except Exception as e:
            print(f"bars {sym}: {e}")

    # replays only change when the watchlist does, so build them once per watchlist
    marker = os.path.join(DATA, "replay", "_watchlist.json")
    wl = state["today"]["watchlist"]
    try:
        with open(marker, encoding="utf-8") as f:
            same = json.load(f) == wl
    except (OSError, ValueError):
        same = False
    if wl and not same:
        shutil.rmtree(os.path.join(DATA, "replay"), ignore_errors=True)
        days = dashboard.api_days()["days"][:REPLAY_DAYS]
        for d in days:
            rep = dashboard.api_replay(d)
            rep["bars"] = {s: dashboard.api_bars(s, d)["bars"] for s in wl}
            dump(os.path.join(DATA, "replay", f"{d}.json"), rep)
        dump(os.path.join(DATA, "days.json"), {"days": days})
        dump(marker, wl)
    print(f"site exported: {DOCS}")


if __name__ == "__main__":
    export()
