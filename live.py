#!/usr/bin/env python3
"""
Matchday Edge: live scores.

While any predicted game is on (from 5 minutes before kick-off to 2.5 hours after),
gets the scores and saves live.json in LIVE_DIR: club games from football-data.org
(FOOTBALL_DATA_TOKEN, one call per run), international games from ESPN's free feed.
"""
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.football-data.org/v4"
ESPN = "https://site.api.espn.com/apis/site/v2/sports/soccer"
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


def football_data(token, now, known):
    """Club games: today's and yesterday's scores from football-data.org."""
    day = lambda d: d.strftime("%Y-%m-%d")
    req = urllib.request.Request(
        f"{API}/matches?dateFrom={day(now - timedelta(days=1))}&dateTo={day(now)}",
        headers={"X-Auth-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.load(r)
    except (urllib.error.URLError, OSError, ValueError) as e:
        print("Live scores:", e)
        return {}
    out = {}
    for m in body.get("matches") or []:
        key = str(m.get("id"))
        if key not in known or m.get("status") not in ("IN_PLAY", "PAUSED", "FINISHED", "AWARDED"):
            continue
        sc = m.get("score") or {}
        ft, ht = sc.get("fullTime") or {}, sc.get("halfTime") or {}
        rt = sc.get("regularTime") or ft      # regular time only, like the predictions
        out[key] = {"kickoff": m.get("utcDate"), "status": m.get("status"),
                    "score": [rt.get("home") or 0, rt.get("away") or 0],
                    "ht": [ht.get("home"), ht.get("away")], "minute": m.get("minute")}
    return out


def espn(games, now):
    """International games: scores from ESPN's scoreboard (one request per competition and day)."""
    out = {}
    for slug, day in {(m["espnSlug"], parse(m["date"]).strftime("%Y%m%d")) for m in games}:
        req = urllib.request.Request(f"{ESPN}/{slug}/scoreboard?dates={day}", headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                events = json.load(r).get("events") or []
        except (urllib.error.URLError, OSError, ValueError) as e:
            print("ESPN live scores:", e)
            continue
        for e in events:
            key = str(10 ** 10 + int(e["id"]))
            if key not in {str(m["id"]) for m in games}:
                continue
            st = e["status"]["type"]
            if st.get("state") == "pre":
                continue
            side = {x.get("homeAway"): x for x in e["competitions"][0]["competitors"]}
            score = lambda x: int(x["score"]) if str(x.get("score", "")).isdigit() else 0
            clock = "".join(ch for ch in str(e["status"].get("displayClock", "")) if ch.isdigit())
            status = "FINISHED" if st.get("completed") else "PAUSED" if "HALF" in str(st.get("name")) else "IN_PLAY"
            out[key] = {"kickoff": e["date"], "status": status, "score": [score(side["home"]), score(side["away"])],
                        "ht": [None, None], "minute": int(clock) if clock and status == "IN_PLAY" else None}
    return out


def main():
    now = datetime.now(timezone.utc)
    live = load_json(LIVE_PATH, {"updated": None, "matches": {}})
    preds = load_json(os.path.join(OUT_DIR, "predictions.json"), {"matches": []})
    known = {str(m["id"]): m for m in preds.get("matches", [])}
    for k, v in live["matches"].items():   # games already being followed
        known.setdefault(k, {"id": k, "date": v["kickoff"]})

    unfinished = {k for k, v in live["matches"].items() if v["status"] not in ("FINISHED", "AWARDED")
                  and now - parse(v["kickoff"]) < timedelta(hours=4)}
    on = [m for k, m in known.items() if k in unfinished
          or parse(m["date"]) - timedelta(minutes=5) <= now <= parse(m["date"]) + timedelta(minutes=150)]
    if not on:
        return
    out = {}
    token = os.environ.get("FOOTBALL_DATA_TOKEN")
    if token and any(not m.get("espnSlug") for m in on):
        out.update(football_data(token, now, known))
    intl = [m for m in on if m.get("espnSlug")]
    if intl:
        out.update(espn(intl, now))
    for k, v in live["matches"].items():   # keep recent finished games from earlier runs
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
