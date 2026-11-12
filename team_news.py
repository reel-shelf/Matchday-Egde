#!/usr/bin/env python3
"""
Matchday Edge: team news.

Pulls injuries, suspensions, doubts and confirmed lineups from API-Football
(api-sports.io) for games in the next 36 hours and saves them to
data/team-news.json for the app. Run by .github/workflows/team-news.yml every
10 minutes.

- Lineups are polled from 75 minutes before kick-off until they appear (clubs
  usually publish them about 60 minutes before).
- Final check: GitHub's schedule can run late, so when a game kicks off within
  the next 25 minutes this run waits until 4 minutes before kick-off and then
  re-checks the lineup and injuries one last time.
- Free plan = 100 calls a day. Every call is counted (data/team-news-state.json)
  and the script stops at API_FOOTBALL_DAILY_LIMIT (default 95). Lineups are
  checked before anything else.

Needs one secret: API_FOOTBALL_KEY (free from api-football.com), or RAPIDAPI_KEY
(the same data through rapidapi.com, free "Basic" plan for API-Football).
"""
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone

API = "https://v3.football.api-sports.io"
RAPID_HOST = "api-football-v1.p.rapidapi.com"
RAPID_API = f"https://{RAPID_HOST}/v3"
LEAGUE_IDS = {"PL": 39, "PD": 140, "SA": 135, "BL1": 78, "FL1": 61, "CL": 2}
MIN = 60
LOOK_AHEAD = 36 * 3600
LINEUP_FROM = 75 * MIN       # start polling lineups this long before kick-off
FINAL_CHECK = 6 * MIN        # a check inside this window is the final check
FINAL_AT = 4 * MIN           # when waiting for the final check, run it at kick-off - 4 min
WAIT_UP_TO = 25 * MIN        # wait for a final check at most this long
KEY_PLAYERS_MAX_AGE = 7 * 86400
OUT_DIR = os.environ.get("OUT_DIR", "data")
STATE_PATH = os.path.join(OUT_DIR, "team-news-state.json")
NEWS_PATH = os.path.join(OUT_DIR, "team-news.json")


class OutOfCalls(Exception):
    pass


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def parse(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


STOP = {"fc", "cf", "ac", "afc", "sc", "ssc", "as", "us", "rc", "rcd", "ca", "cd", "ud", "sd", "club",
        "de", "del", "la", "le", "calcio", "1", "tsg", "vfb", "vfl", "sv", "bv", "fk", "sk", "if", "bk",
        "and", "the", "hotspur"}


def tokens(name):
    s = unicodedata.normalize("NFD", name)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return {w for w in re.sub(r"[^a-z0-9 ]", " ", s).split() if w not in STOP}


def similar(a, b):
    x, y = tokens(a), tokens(b)
    n = sum(1 for w in x if w in y or any(len(v) > 3 and len(w) > 3 and (v.startswith(w) or w.startswith(v)) for v in y))
    return n / max(1, min(len(x), len(y)))


def match_fixture(m, fixtures):
    """Same competition, kick-off within 3 hours, best team-name match."""
    t = parse(m["date"])
    best, best_score = None, 0
    for f in fixtures:
        if (f.get("league") or {}).get("id") != LEAGUE_IDS[m["league"]]:
            continue
        ts = f["fixture"]["timestamp"]
        if abs(ts - t) > 3 * 3600:
            continue
        score = (similar(m["home"]["name"], f["teams"]["home"]["name"])
                 + similar(m["away"]["name"], f["teams"]["away"]["name"])
                 + (0.5 if ts == t else 0))
        if score > best_score:
            best, best_score = f, score
    return best if best_score >= 1 else None


def chunks(xs, n):
    return [xs[i:i + n] for i in range(0, len(xs), n)]


class Client:
    def __init__(self, key, state, limit, rapid=False):
        self.state, self.limit = state, limit
        self.base = RAPID_API if rapid else API
        self.headers = ({"x-rapidapi-key": key, "x-rapidapi-host": RAPID_HOST} if rapid
                        else {"x-apisports-key": key})

    def get(self, path):
        if self.state["calls"] >= self.limit:
            raise OutOfCalls(f"Daily limit of {self.limit} API-Football calls reached")
        self.state["calls"] += 1
        req = urllib.request.Request(self.base + path, headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = json.load(r)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"API-Football {path}: HTTP {e.code}")
        errs = body.get("errors")
        if errs:
            msg = "; ".join(errs.values() if isinstance(errs, dict) else map(str, errs))
            raise RuntimeError(f"API-Football {path}: {msg}")
        return body.get("response") or []


def check(api, state, matches, now, final_only=False):
    """One round of checks. Returns nothing; updates state in place."""
    to_kick = lambda m: parse(m["date"]) - now
    raw_of = lambda m: state["raw"].setdefault(str(m["id"]), {"kickoff": m["date"]})
    mp = state["map"]

    # 1. Find each game's API-Football fixture id (one call per day of games).
    if not final_only:
        for d in sorted({m["date"][:10] for m in matches if str(m["id"]) not in mp}):
            fetched = state["dates"].get(d)
            if fetched and now - parse(fetched) < 6 * 3600:
                continue
            fixtures = api.get(f"/fixtures?date={d}")
            state["dates"][d] = iso(now)
            for m in matches:
                if m["date"][:10] == d and str(m["id"]) not in mp:
                    f = match_fixture(m, fixtures)
                    if f:
                        mp[str(m["id"])] = {"af": f["fixture"]["id"], "homeId": f["teams"]["home"]["id"],
                                            "awayId": f["teams"]["away"]["id"]}
    mapped = [m for m in matches if str(m["id"]) in mp]
    af = lambda m: mp[str(m["id"])]["af"]

    # 2. Lineups: poll from 75 min out until they appear, then once more in the last few minutes.
    due = []
    for m in mapped:
        t, r = to_kick(m), raw_of(m)
        if -2 * MIN <= t <= LINEUP_FROM and (not r.get("lineups") or (t <= FINAL_CHECK and not r.get("finalAt"))):
            if not final_only or t <= FINAL_CHECK:
                due.append(m)
    for batch in chunks(due, 20):
        res = api.get("/fixtures?ids=" + "-".join(str(af(m)) for m in batch))
        for m in batch:
            fx = next((f for f in res if (f.get("fixture") or {}).get("id") == af(m)), None)
            r = raw_of(m)
            r["lineupsAt"] = iso(now)
            if to_kick(m) <= FINAL_CHECK:
                r["finalAt"] = r["lineupsAt"]
            if fx and fx.get("lineups"):
                r["lineups"] = [{"teamId": (l.get("team") or {}).get("id"), "formation": l.get("formation") or "",
                                 "xi": [{"id": (p.get("player") or {}).get("id"), "name": (p.get("player") or {}).get("name")}
                                        for p in l.get("startXI") or []]} for l in fx["lineups"]]

    # 3. Injuries & suspensions: every 4 hours, every 30 min in the last 3 hours, and at the final check.
    due = []
    for m in mapped:
        t, r = to_kick(m), raw_of(m)
        if t < -2 * MIN:
            continue
        at = r.get("injuriesAt")
        if final_only:
            if r.get("finalAt") and (not at or parse(at) < parse(r["finalAt"])):
                due.append(m)
        elif not at or (r.get("finalAt") and parse(at) < parse(r["finalAt"])) \
                or now - parse(at) > (30 * MIN if t <= 3 * 3600 else 4 * 3600):
            due.append(m)
    for batch in chunks(due, 20):
        res = api.get("/injuries?ids=" + "-".join(str(af(m)) for m in batch))
        for m in batch:
            r = raw_of(m)
            r["injuriesAt"] = iso(now)
            r["injuries"] = [{"teamId": (x.get("team") or {}).get("id"), "type": (x.get("player") or {}).get("type") or "",
                              "player": {"id": x["player"].get("id"), "name": x["player"].get("name"),
                                         "reason": x["player"].get("reason") or ""}}
                             for x in res if (x.get("fixture") or {}).get("id") == af(m) and x.get("player")]

    # 4. Key players (top scorers and assisters), one league per run, refreshed weekly.
    if not final_only and api.limit - state["calls"] >= 20:
        for code in sorted({m["league"] for m in matches}):
            k = state["key"].get(code)
            if k and now - parse(k["at"]) < KEY_PLAYERS_MAX_AGE:
                continue
            kick = datetime.fromisoformat(next(m for m in matches if m["league"] == code)["date"].replace("Z", "+00:00"))
            season = kick.year if kick.month >= 7 else kick.year - 1
            players = []
            for kind in ("topscorers", "topassists"):
                for x in api.get(f"/players/{kind}?league={LEAGUE_IDS[code]}&season={season}"):
                    pid = (x.get("player") or {}).get("id")
                    team = (((x.get("statistics") or [{}])[0]).get("team") or {}).get("id")
                    if pid and team and not any(p["id"] == pid for p in players):
                        players.append({"id": pid, "name": x["player"].get("name"), "teamId": team})
            state["key"][code] = {"at": iso(now), "players": players}
            break


def build_news(state, matches, now, limit):
    out = {}
    for m in matches:
        mp, r = state["map"].get(str(m["id"])), state["raw"].get(str(m["id"]))
        if not mp or not r or ("injuries" not in r and "lineups" not in r):
            continue
        keys = (state["key"].get(m["league"]) or {}).get("players") or []
        key_ids = {p["id"] for p in keys}

        def side(team_id):
            lineup = next((l for l in r.get("lineups") or [] if l["teamId"] == team_id), None)
            in_xi = {p["id"] for p in (lineup or {}).get("xi") or []}
            outs = []
            for i in r.get("injuries") or []:
                if i["teamId"] != team_id or i["player"]["id"] in in_xi:
                    continue  # other team, or a doubt who starts
                doubt = "questionable" in i["type"].lower() and not lineup
                outs.append({**i["player"], "status": "doubt" if doubt else "out", "key": i["player"]["id"] in key_ids})
            benched = [{"id": p["id"], "name": p["name"]} for p in keys
                       if lineup and p["teamId"] == team_id and p["id"] not in in_xi
                       and not any(o["id"] == p["id"] for o in outs)]
            return {"out": outs, "benched": benched, "formation": (lineup or {}).get("formation"),
                    "xi": [p["name"] for p in lineup["xi"]] if lineup else None}

        out[str(m["id"])] = {
            "kickoff": m["date"], "home": side(mp["homeId"]), "away": side(mp["awayId"]),
            "lineups": bool(r.get("lineups")), "finalCheck": r.get("finalAt"),
            "injuriesChecked": r.get("injuriesAt"), "lineupsChecked": r.get("lineupsAt"),
        }
    return {"updated": iso(now), "status": state.get("lastError") or "ok",
            "callsToday": state["calls"], "limit": limit, "matches": out}


def save(state, news):
    # Only write (and so only commit) when an API call was made or the news changed.
    old_state, old_news = load_json(STATE_PATH, None), load_json(NEWS_PATH, {})
    if old_state == state and old_news.get("matches") == news["matches"] and old_news.get("status") == news["status"]:
        return
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, separators=(",", ":"))
    with open(NEWS_PATH, "w") as f:
        json.dump(news, f, separators=(",", ":"))


def main():
    key = os.environ.get("API_FOOTBALL_KEY")
    rapid_key = os.environ.get("RAPIDAPI_KEY")
    if not key and not rapid_key:
        print("Neither API_FOOTBALL_KEY nor RAPIDAPI_KEY is set yet, so there is no team news to fetch.")
        return
    limit = int(os.environ.get("API_FOOTBALL_DAILY_LIMIT") or 95)
    now = time.time()
    today = iso(now)[:10]

    state = {"day": today, "calls": 0, "dates": {}, "map": {}, "raw": {}, "key": {}}
    state.update(load_json(STATE_PATH, {}))
    if state["day"] != today:
        state["day"], state["calls"] = today, 0
    state["lastError"] = None

    preds = load_json(os.path.join(OUT_DIR, "predictions.json"), {"matches": []})
    matches = [m for m in preds.get("matches", [])
               if m.get("league") in LEAGUE_IDS and -10 * MIN < parse(m["date"]) - now < LOOK_AHEAD]
    keep = {str(m["id"]) for m in matches}
    for k in ("raw", "map"):
        state[k] = {i: v for i, v in state[k].items() if i in keep}
    state["dates"] = {d: v for d, v in state["dates"].items() if parse(d + "T00:00:00Z") > now - 2 * 86400}

    api = Client(key or rapid_key, state, limit, rapid=not key)
    try:
        check(api, state, matches, now)
    except (OutOfCalls, RuntimeError, urllib.error.URLError, OSError) as e:
        state["lastError"] = str(e)
        print(e)
    save(state, build_news(state, matches, now, limit))

    # Final check: wait for any game kicking off soon that hasn't had one yet.
    soon = [m for m in matches if str(m["id"]) in state["map"]
            and not (state["raw"].get(str(m["id"])) or {}).get("finalAt")
            and FINAL_AT < parse(m["date"]) - now <= WAIT_UP_TO]
    if soon and not state["lastError"]:
        at = min(parse(m["date"]) for m in soon) - FINAL_AT
        print(f"Waiting {round((at - time.time()) / 60)} min for the final check before kick-off...")
        time.sleep(max(0, at - time.time()))
        now = time.time()
        try:
            check(api, state, matches, now, final_only=True)
        except (OutOfCalls, RuntimeError, urllib.error.URLError, OSError) as e:
            state["lastError"] = str(e)
            print(e)
        save(state, build_news(state, matches, now, limit))
    print(f"Team news saved. API calls today: {state['calls']}/{limit}.")


if __name__ == "__main__":
    main()
