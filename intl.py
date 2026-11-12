#!/usr/bin/env python3
"""
Matchday Edge: international games (national teams).

football-data.org's free plan has no internationals, so this uses API-Football:
1. Keeps a results file (data/intl-results.json) with the last 3 years of national
   team games: Nations League, friendlies, World Cup and Euro qualifiers, and the
   big tournaments. Past seasons are downloaded once; current ones are refreshed
   each run. The first fill is spread over a few runs to stay inside the free
   plan's 100 calls a day.
2. Rates every national team with the same Poisson model as the clubs (predict.py),
   but older games count for more, because national teams play less often.
3. Adds the next 14 days of international games to predictions.json (league "INT")
   and locks their picks into history.json, so parlays, team news, odds and the
   track record include them too.

Runs after predict.py in .github/workflows/predict.yml. Needs API_FOOTBALL_KEY.
"""
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import predict
import team_news

OUT_DIR = os.environ.get("OUT_DIR", "data")
CACHE_PATH = os.path.join(OUT_DIR, "intl-results.json")
COMPS = {
    5: "Nations League", 10: "Friendly", 32: "World Cup qualifier", 960: "Euro qualifier",
    1: "World Cup", 4: "Euro Championship", 9: "Copa América", 6: "Africa Cup of Nations",
    7: "Asian Cup", 22: "Gold Cup", 536: "CONCACAF Nations League",
    34: "World Cup qualifier", 29: "World Cup qualifier", 30: "World Cup qualifier", 31: "World Cup qualifier",
}
HISTORY_DAYS = 3 * 365
DAYS_AHEAD = 14
HALF_LIFE_DAYS = 400       # clubs use 150; national teams play far less often
RESERVE = 40               # always leave this many calls a day for team news and odds
MAX_CALLS = 40             # most calls one run may use
ID_OFFSET = 10 ** 9        # keeps these ids apart from football-data.org's
FINISHED = {"FT", "AET", "PEN", "AWD", "WO"}
DROPPED = {"PST", "CANC", "ABD"}


def now_utc():
    return datetime.now(timezone.utc)


def iso(d):
    return d.isoformat().replace("+00:00", "Z")


def fixture_row(f):
    fx, lg, tm, sc = f["fixture"], f["league"], f["teams"], f.get("score") or {}
    ft, ht = sc.get("fulltime") or {}, sc.get("halftime") or {}   # fulltime = after 90 minutes
    pair = lambda s: [s["home"], s["away"]] if s.get("home") is not None and s.get("away") is not None else None
    return {"id": fx["id"], "league": lg["id"], "season": lg.get("season"), "round": lg.get("round"),
            "date": fx["date"], "status": (fx.get("status") or {}).get("short"),
            "home": {"id": tm["home"]["id"], "name": tm["home"]["name"], "logo": tm["home"].get("logo")},
            "away": {"id": tm["away"]["id"], "name": tm["away"]["name"], "logo": tm["away"].get("logo")},
            "ft": pair(ft), "ht": pair(ht)}


def update_cache(api, cache, now):
    """Download what's missing or current, newest first, within this run's call budget."""
    if not cache.get("leaguesAt") or now - predict.parse_time(cache["leaguesAt"]) > timedelta(days=7):
        seasons = {}
        for lg in api.get("/leagues"):
            lid = (lg.get("league") or {}).get("id")
            if lid in COMPS:
                seasons[str(lid)] = [{"year": s["year"], "start": s.get("start"), "end": s.get("end"),
                                      "current": s.get("current")} for s in lg.get("seasons") or []]
        cache["seasons"], cache["leaguesAt"] = seasons, iso(now)
    cutoff = (now - timedelta(days=HISTORY_DAYS)).date().isoformat()
    today = now.date().isoformat()
    todo = []
    for lid, seasons in cache["seasons"].items():
        for s in seasons:
            if (s.get("end") or "9999") < cutoff:
                continue
            key = f"{lid}:{s['year']}"
            current = s.get("current") or (s.get("end") or "9999") >= (now - timedelta(days=3)).date().isoformat()
            fetched = cache["fetched"].get(key)
            if fetched and not current:
                continue                     # past season, already downloaded
            if fetched and current and now - predict.parse_time(fetched) < timedelta(hours=10):
                continue
            starts_soon = (s.get("start") or "9999") <= (now + timedelta(days=DAYS_AHEAD)).date().isoformat()
            if current and not starts_soon:
                continue
            todo.append((0 if current else 1, -(int(s["year"])), lid, s["year"], key))
    todo.sort()
    for _, _, lid, year, key in todo:
        if api.state["calls"] >= api.limit:
            print("Call budget for this run used; the rest next run.")
            break
        for f in api.get(f"/fixtures?league={lid}&season={year}"):
            r = fixture_row(f)
            cache["games"][str(r["id"])] = r
        cache["fetched"][key] = iso(now)
    # drop games older than we need
    cache["games"] = {k: g for k, g in cache["games"].items() if g["date"][:10] >= cutoff}


def main():
    key = (os.environ.get("API_FOOTBALL_KEY") or "").strip().strip('"\'').strip()
    if not key:
        print("API_FOOTBALL_KEY is not set, so no international games.")
        return
    now = now_utc()
    cache = predict.load_json(CACHE_PATH, {})
    cache.setdefault("seasons", {}); cache.setdefault("fetched", {}); cache.setdefault("games", {})
    budget = cache.setdefault("budget", {"day": "", "calls": 0})
    if budget["day"] != now.date().isoformat():
        budget.update(day=now.date().isoformat(), calls=0)

    api = team_news.Client(key, budget, limit=0)
    acct = api.status()                        # free call: how many calls are left today
    if not acct.get("ok"):
        print("API-Football:", acct.get("error"))
        return
    left = (acct.get("dailyLimit") or 100) - (acct.get("callsToday") or 0) - RESERVE
    api.limit = budget["calls"] + max(0, min(MAX_CALLS, left))
    try:
        update_cache(api, cache, now)
    except (team_news.OutOfCalls, RuntimeError, OSError) as e:
        print("International results:", e)
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, separators=(",", ":"))

    # ---- rate national teams
    games, teams_info = [], {}
    for g in cache["games"].values():
        for side in ("home", "away"):
            teams_info[g[side]["id"]] = {"name": g[side]["name"], "crest": g[side]["logo"]}
        if g["status"] in FINISHED and g["ft"]:
            games.append({"id": g["id"], "home": g["home"]["id"], "away": g["away"]["id"],
                          "ft": tuple(g["ft"]), "ht": tuple(g["ht"]) if g["ht"] else None,
                          "date": predict.parse_time(g["date"]), "comp": g["league"]})
    games.sort(key=lambda g: g["date"])
    if len(games) < 50:
        print(f"Only {len(games)} international results so far; predictions start once there are more.")
        return
    predict.HALF_LIFE_DAYS = HALF_LIFE_DAYS
    teams = set(teams_info)
    centre = {t: (1.0, 1.0) for t in teams}
    models = predict.fit_all(games, teams, centre, now, iters=80)

    by_team, pairs = defaultdict(list), defaultdict(list)
    for g in games:
        by_team[g["home"]].append(g); by_team[g["away"]].append(g)
        pairs[frozenset((g["home"], g["away"]))].append(g)
    form = lambda t: [predict.result_letter(g, t) for g in by_team[t][-5:]][::-1]

    # ---- predictions for the next 14 days
    preds = predict.load_json(os.path.join(OUT_DIR, "predictions.json"), {"matches": [], "leagues": {}})
    history = predict.load_json(os.path.join(OUT_DIR, "history.json"), {"matches": {}})
    preds["matches"] = [m for m in preds.get("matches", []) if m.get("league") != "INT"]
    added = 0
    for g in cache["games"].values():
        when = predict.parse_time(g["date"])
        if g["status"] not in ("NS", "TBD") or not (now < when <= now + timedelta(days=DAYS_AHEAD)):
            continue
        hid, aid = g["home"]["id"], g["away"]["id"]
        p = predict.predict(hid, aid, models, None)
        h2h = sorted(pairs[frozenset((hid, aid))], key=lambda x: x["date"], reverse=True)[:5]
        mid = ID_OFFSET + g["id"]
        date = iso(when)
        p.update({
            "id": mid, "league": "INT", "compName": COMPS.get(g["league"], "International"),
            "af": g["id"], "afLeague": g["league"], "season": g["season"], "homeAf": hid, "awayAf": aid,
            "date": date, "matchday": None,
            "home": {"name": g["home"]["name"], "crest": g["home"]["logo"], "form": form(hid)},
            "away": {"name": g["away"]["name"], "crest": g["away"]["logo"], "form": form(aid)},
            "h2h": [{"date": x["date"].date().isoformat(), "league": "INT",
                     "home": teams_info[x["home"]]["name"], "away": teams_info[x["away"]]["name"],
                     "score": list(x["ft"])} for x in h2h],
        })
        preds["matches"].append(p)
        history["matches"][str(mid)] = predict.record("INT", date, g["home"]["name"], g["away"]["name"], p)
        added += 1
    preds["matches"].sort(key=lambda m: m["date"])
    preds.setdefault("leagues", {})["INT"] = {"name": "Internationals", "emblem": None}

    # ---- grade finished international picks
    graded = 0
    for k, rec in list(history["matches"].items()):
        if rec.get("league") != "INT" or rec.get("result") is not None:
            continue
        g = cache["games"].get(str(int(k) - ID_OFFSET))
        if not g:
            continue
        if g["status"] in FINISHED and g["ft"]:
            rec["result"] = {"ft": g["ft"], "ht": g["ht"] or [None, None], "corners": None}
            graded += 1
        elif g["status"] in DROPPED:
            del history["matches"][k]

    with open(os.path.join(OUT_DIR, "predictions.json"), "w") as f:
        json.dump(preds, f, separators=(",", ":"))
    with open(os.path.join(OUT_DIR, "history.json"), "w") as f:
        json.dump(history, f, separators=(",", ":"))
    print(f"Internationals: {len(games)} results, {len(teams)} teams, {added} upcoming games predicted, "
          f"{graded} graded. API calls this run: {budget['calls']}.")


if __name__ == "__main__":
    sys.exit(main())
