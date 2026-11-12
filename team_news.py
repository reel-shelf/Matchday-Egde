#!/usr/bin/env python3
"""
Matchday Edge: team news and bookmaker odds.

Pulls injuries, suspensions, doubts and confirmed lineups from API-Football
(api-sports.io) for games in the next 36 hours, and Bet365 odds for games in the
next 3 days. Saves team-news.json and odds.json in LIVE_DIR (the live-data branch).
Run every 5 minutes by .github/workflows/team-news.yml.

- Lineups are polled from 75 minutes before kick-off until they appear (clubs
  usually publish them about 60 minutes before).
- Final check: the run that lands in the last 6 minutes before kick-off
  re-checks the lineup and injuries one last time.
- Free plan = 100 calls a day. Every call is counted (team-news-state.json)
  and the script stops at API_FOOTBALL_DAILY_LIMIT (default 95). Lineups and
  injuries come first; odds only use calls while 30 or more are left.

Needs one secret: API_FOOTBALL_KEY (free from api-football.com), or RAPIDAPI_KEY.
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
LEAGUE_IDS = {"PL": 39, "PD": 140, "SA": 135, "BL1": 78, "FL1": 61, "CL": 2,
              "ELC": 40, "DED": 88, "PPL": 94}
MIN = 60
NEWS_AHEAD = 36 * 3600       # team news for games in the next 36 hours
ODDS_AHEAD = 72 * 3600       # bookmaker odds for games in the next 3 days
ODDS_RESERVE = 30            # always keep this many calls for lineups and injuries
BOOKMAKER = 8                # Bet365
LINEUP_FROM = 75 * MIN       # start polling lineups this long before kick-off
FINAL_CHECK = 6 * MIN        # a check inside this window is the final check
KEY_PLAYERS_MAX_AGE = 7 * 86400
OUT_DIR = os.environ.get("OUT_DIR", "data")            # predictions.json is read from here
LIVE_DIR = os.environ.get("LIVE_DIR", OUT_DIR)         # team news and odds are written here
STATE_PATH = os.path.join(LIVE_DIR, "team-news-state.json")
NEWS_PATH = os.path.join(LIVE_DIR, "team-news.json")
ODDS_PATH = os.path.join(LIVE_DIR, "odds.json")


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

    def status(self):
        """Account check. API-Football's /status endpoint doesn't count toward daily calls."""
        req = urllib.request.Request(self.base + "/status", headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = json.load(r)
        except (urllib.error.URLError, OSError, ValueError) as e:
            return {"ok": False, "error": f"could not reach API-Football: {e}"}
        errs = body.get("errors")
        if errs:
            return {"ok": False, "error": "; ".join(errs.values() if isinstance(errs, dict) else map(str, errs))}
        r = body.get("response") or {}
        return {"ok": True, "plan": (r.get("subscription") or {}).get("plan"),
                "callsToday": (r.get("requests") or {}).get("current"),
                "dailyLimit": (r.get("requests") or {}).get("limit_day")}

    def get(self, path, full=False):
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
        return body if full else (body.get("response") or [])


MARKETS = {1: {"Home": "1", "Draw": "X", "Away": "2"},
           12: {"Home/Draw": "1X", "Home/Away": "12", "Draw/Away": "X2"},
           5: {"Over 1.5": "O15", "Over 2.5": "O25", "Over 3.5": "O35"},
           8: {"Yes": "BTTS"}}


def parse_odds(item):
    for b in item.get("bookmakers") or []:
        out = {"book": b.get("name")}
        for bet in b.get("bets") or []:
            names = MARKETS.get(bet.get("id"), {})
            for v in bet.get("values") or []:
                k = names.get(str(v.get("value")))
                try:
                    if k:
                        out[k] = round(float(v["odd"]), 2)
                except (TypeError, ValueError, KeyError):
                    pass
        if len(out) > 1:
            return out
    return None


def season_of(date):
    d = datetime.fromisoformat(date.replace("Z", "+00:00"))
    return d.year if d.month >= 7 else d.year - 1


def check(api, state, matches, now, final_only=False):
    """One round of checks. Returns nothing; updates state in place."""
    to_kick = lambda m: parse(m["date"]) - now
    raw_of = lambda m: state["raw"].setdefault(str(m["id"]), {"kickoff": m["date"]})
    mp = state["map"]

    # 1. Find each game's API-Football fixture id (one call per day of games).
    if not final_only:
        for d in sorted({m["date"][:10] for m in matches if str(m["id"]) not in mp}):
            fetched = state["dates"].get(d)
            if fetched and now - parse(fetched) < 8 * 3600:
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
        if to_kick(m) > NEWS_AHEAD:
            continue
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

    # 4. Bookmaker odds for the next 3 days, refreshed every 12 hours per league and day.
    #    Problems here (e.g. a plan limit) are saved but don't stop the other checks.
    if not final_only:
        pairs = sorted({(m["league"], m["date"][:10]) for m in mapped if 0 < to_kick(m) < ODDS_AHEAD})
        try:
            for code, d in pairs:
                at = state["oddsAt"].get(f"{code}:{d}")
                if at and now - parse(at) < 12 * 3600:
                    continue
                if api.limit - state["calls"] < ODDS_RESERVE:
                    break
                season = season_of(d + "T12:00:00Z")
                page, total = 1, 1
                while page <= min(total, 3):
                    body = api.get(f"/odds?league={LEAGUE_IDS[code]}&season={season}&date={d}"
                                   f"&bookmaker={BOOKMAKER}&page={page}", full=True)
                    total = int((body.get("paging") or {}).get("total") or 1)
                    for item in body.get("response") or []:
                        o = parse_odds(item)
                        fid = (item.get("fixture") or {}).get("id")
                        if o and fid:
                            state["odds"][str(fid)] = {**o, "at": iso(now)}
                    page += 1
                state["oddsAt"][f"{code}:{d}"] = iso(now)
            state["oddsError"] = None
        except (RuntimeError, urllib.error.URLError, OSError) as e:
            state["oddsError"] = str(e)
            print(e)

    # 5. Key players (top scorers and assisters), one league per run, refreshed weekly.
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
        if parse(m["date"]) - now > NEWS_AHEAD:
            continue
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
    return {"updated": iso(now), "status": state.get("lastError") or "ok", "account": state.get("account"),
            "callsToday": state["calls"], "limit": limit, "matches": out}


def build_odds(state, matches, now):
    out = {}
    for m in matches:
        mp = state["map"].get(str(m["id"]))
        o = mp and state["odds"].get(str(mp["af"]))
        if o:
            out[str(m["id"])] = o
    return {"updated": iso(now), "status": state.get("oddsError") or "ok", "matches": out}


def save(state, news, odds=None):
    os.makedirs(LIVE_DIR, exist_ok=True)
    if odds is not None and load_json(ODDS_PATH, {}).get("matches") != odds["matches"] \
            or odds is not None and load_json(ODDS_PATH, {}).get("status") != odds["status"]:
        with open(ODDS_PATH, "w") as f:
            json.dump(odds, f, separators=(",", ":"))
    # Only write (and so only commit) when an API call was made or the news changed.
    old_state, old_news = load_json(STATE_PATH, None), load_json(NEWS_PATH, {})
    if old_state == state and old_news.get("matches") == news["matches"] and old_news.get("status") == news["status"]:
        return
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, separators=(",", ":"))
    with open(NEWS_PATH, "w") as f:
        json.dump(news, f, separators=(",", ":"))


def main():
    # strip stray spaces, line breaks or quotes picked up when copying the key
    key = (os.environ.get("API_FOOTBALL_KEY") or "").strip().strip('"\'').strip()
    rapid_key = (os.environ.get("RAPIDAPI_KEY") or "").strip().strip('"\'').strip()
    if not key and not rapid_key:
        print("Neither API_FOOTBALL_KEY nor RAPIDAPI_KEY is set yet, so there is no team news to fetch.")
        return
    limit = int(os.environ.get("API_FOOTBALL_DAILY_LIMIT") or 95)
    now = time.time()
    today = iso(now)[:10]

    state = {"day": today, "calls": 0, "dates": {}, "map": {}, "raw": {}, "key": {}, "odds": {}, "oddsAt": {}}
    state.update(load_json(STATE_PATH, {}))
    if state["day"] != today:
        state["day"], state["calls"] = today, 0
    state["lastError"] = None

    preds = load_json(os.path.join(OUT_DIR, "predictions.json"), {"matches": []})
    matches = [m for m in preds.get("matches", [])
               if m.get("league") in LEAGUE_IDS and -10 * MIN < parse(m["date"]) - now < ODDS_AHEAD]
    keep = {str(m["id"]) for m in matches}
    for k in ("raw", "map"):
        state[k] = {i: v for i, v in state[k].items() if i in keep}
    live_af = {str(v["af"]) for v in state["map"].values()}
    state["odds"] = {i: v for i, v in state["odds"].items() if i in live_af}
    state["oddsAt"] = {k: v for k, v in state["oddsAt"].items() if parse(k.split(":")[1] + "T23:59:59Z") > now}
    state["dates"] = {d: v for d, v in state["dates"].items() if parse(d + "T00:00:00Z") > now - 2 * 86400}

    api = Client(key or rapid_key, state, limit, rapid=not key)
    acct = state.get("account") or {}
    if acct.get("ok") and acct.get("day") == today:
        api = Client(key or rapid_key, state, limit, rapid=acct.get("via") == "rapidapi")
    else:   # check the key once a day (free call)
        check_result = {**api.status(), "via": "rapidapi" if not key else "api-football"}
        if not check_result["ok"] and key:
            # A RapidAPI key saved as API_FOOTBALL_KEY: try it through RapidAPI.
            rapid = Client(key, state, limit, rapid=True)
            r = rapid.status()
            if r["ok"]:
                api, check_result = rapid, {**r, "via": "rapidapi"}
            else:
                check_result["rapidapiError"] = r.get("error")
        state["account"] = {**check_result, "day": today}
        print("API key check:", state["account"])
        if not check_result["ok"]:
            state["lastError"] = "API key not accepted: " + str(check_result.get("error"))
            save(state, build_news(state, [], now, limit))
            return
    try:
        check(api, state, matches, now)
    except (OutOfCalls, RuntimeError, urllib.error.URLError, OSError) as e:
        state["lastError"] = str(e)
        print(e)
    save(state, build_news(state, matches, now, limit), build_odds(state, matches, now))
    print(f"Team news saved. API calls today: {state['calls']}/{limit}.")


if __name__ == "__main__":
    main()
