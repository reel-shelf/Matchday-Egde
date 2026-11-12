#!/usr/bin/env python3
"""
Matchday Edge: live scores.

While any predicted game is on (from 5 minutes before kick-off to 2.5 hours after),
asks football-data.org for today's scores and saves live.json in LIVE_DIR.
One call per run (every 5 minutes); the free plan allows 10 a minute.
Uses the same FOOTBALL_DATA_TOKEN secret as predict.py.
"""
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.football-data.org/v4"
OUT_DIR = os.environ.get("OUT_DIR", "data")
LIVE_DIR = os.environ.get("LIVE_DIR", OUT_DIR)
LIVE_PATH = os.path.join(LIVE_DIR, "live.json")
KEEP_HOURS = 30          # keep finished scores this long (parlays and alerts use them)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def parse(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def main():
    token = os.environ.get("FOOTBALL_DATA_TOKEN")
    if not token:
        print("FOOTBALL_DATA_TOKEN is not set, so no live scores.")
        return
    now = datetime.now(timezone.utc)
    live = load_json(LIVE_PATH, {"updated": None, "matches": {}})
    preds = load_json(os.path.join(OUT_DIR, "predictions.json"), {"matches": []})
    known = {str(m["id"]): m for m in preds.get("matches", [])}
    for k, v in live["matches"].items():   # games already being followed
        known.setdefault(k, {"date": v["kickoff"]})

    on = [m for m in known.values()
          if parse(m["date"]) - timedelta(minutes=5) <= now <= parse(m["date"]) + timedelta(minutes=150)]
    unfinished = [k for k, v in live["matches"].items() if v["status"] not in ("FINISHED", "AWARDED")
                  and now - parse(v["kickoff"]) < timedelta(hours=4)]
    if not on and not unfinished:
        return

    day = lambda d: d.strftime("%Y-%m-%d")
    req = urllib.request.Request(
        f"{API}/matches?dateFrom={day(now - timedelta(days=1))}&dateTo={day(now)}",
        headers={"X-Auth-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.load(r)
    except (urllib.error.URLError, OSError, ValueError) as e:
        print("Live scores:", e)
        return

    out = {}
    for m in body.get("matches") or []:
        key = str(m.get("id"))
        if key not in known:
            continue
        sc = m.get("score") or {}
        ft, ht = sc.get("fullTime") or {}, sc.get("halfTime") or {}
        if m.get("status") not in ("IN_PLAY", "PAUSED", "FINISHED", "AWARDED"):
            continue
        # regular time only, like the predictions (extra time and penalties don't count)
        rt = sc.get("regularTime") or ft
        out[key] = {"kickoff": m.get("utcDate"), "status": m.get("status"),
                    "score": [rt.get("home") or 0, rt.get("away") or 0],
                    "ht": [ht.get("home"), ht.get("away")], "minute": m.get("minute")}
    # keep recent finished games from earlier runs
    for k, v in live["matches"].items():
        if k not in out and now - parse(v["kickoff"]) < timedelta(hours=KEEP_HOURS):
            out[k] = v
    if out == live["matches"]:
        return
    os.makedirs(LIVE_DIR, exist_ok=True)
    with open(LIVE_PATH, "w") as f:
        json.dump({"updated": now.isoformat().replace("+00:00", "Z"), "matches": out}, f, separators=(",", ":"))
    print(f"Live scores saved: {sum(1 for v in out.values() if v['status'] in ('IN_PLAY', 'PAUSED'))} in play.")


if __name__ == "__main__":
    main()
