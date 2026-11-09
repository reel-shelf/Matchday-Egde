#!/usr/bin/env python3
"""
Football predictions for the Premier League, La Liga and Serie A.

1. Downloads fixtures and results from football-data.org (free plan).
2. Rates every team's attack and defence with a Poisson model, weighting
   recent games more. This is done three times: full time, first half and
   second half (second half = full-time score minus half-time score).
3. Turns those ratings into probabilities for upcoming matches.
4. Saves data/predictions.json for the website, and data/history.json,
   which locks in each prediction before kick-off so it can be graded
   against the real result afterwards (the "track record").

Needs one secret: FOOTBALL_DATA_TOKEN (free from football-data.org).
"""
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.football-data.org/v4"
LEAGUES = {"PL": "Premier League", "PD": "La Liga", "SA": "Serie A"}

DAYS_AHEAD = 10        # how far ahead to predict
HALF_LIFE_DAYS = 120   # a game this old counts half as much as one today
PRIOR = 4.0            # pulls teams with few games towards average
NEWCOMER = (0.85, 1.15)  # promoted teams start a bit weaker (attack, defence)
RHO = -0.06            # small correction so 0-0 / 1-1 draws aren't undercounted
MAX_GOALS = 10
OUT_DIR = os.environ.get("OUT_DIR", "data")
CALL_GAP = 7           # seconds between API calls (free plan = 10 per minute)

_last_call = 0.0


def fetch(path):
    """GET from football-data.org. Returns parsed JSON, or None if not allowed."""
    global _last_call
    token = os.environ.get("FOOTBALL_DATA_TOKEN")
    if not token:
        sys.exit("Missing FOOTBALL_DATA_TOKEN secret.")
    wait = CALL_GAP - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(API + path, headers={"X-Auth-Token": token})
    for _ in range(3):
        _last_call = time.time()
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:          # rate limited: wait a minute and retry
                time.sleep(65)
                continue
            if e.code in (403, 404):   # not on the free plan / not found
                print(f"  skipped {path} (HTTP {e.code})")
                return None
            raise
    return None


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def finished_games(matches, now):
    """Turn API matches into rows for the model: (home, away, ft, ht, weight)."""
    rows = []
    for m in matches:
        if m.get("status") != "FINISHED":
            continue
        ft = (m.get("score") or {}).get("fullTime") or {}
        ht = (m.get("score") or {}).get("halfTime") or {}
        if ft.get("home") is None or ft.get("away") is None:
            continue
        days = max(0.0, (now - parse_time(m["utcDate"])).total_seconds() / 86400)
        w = 0.5 ** (days / HALF_LIFE_DAYS)
        half = None
        if ht.get("home") is not None and ht.get("away") is not None:
            half = (ht["home"], ht["away"])
        rows.append((m["homeTeam"]["id"], m["awayTeam"]["id"],
                     (ft["home"], ft["away"]), half, w))
    return rows


def fit(games, teams, centre):
    """
    Rate each team. Expected goals:
      home side = base_home * attack[home] * defence[away]
      away side = base_away * attack[away] * defence[home]
    attack > 1 scores more than average; defence > 1 concedes more.
    games: list of (home, away, home_goals, away_goals, weight)
    centre: team -> (attack, defence) the rating is pulled towards.
    """
    att = {t: centre[t][0] for t in teams}
    dfn = {t: centre[t][1] for t in teams}
    bh, ba = 1.4, 1.1
    if not games:
        return bh, ba, att, dfn
    for _ in range(80):
        num_h = sum(w * hg for h, a, hg, ag, w in games)
        num_a = sum(w * ag for h, a, hg, ag, w in games)
        den_h = sum(w * att[h] * dfn[a] for h, a, hg, ag, w in games)
        den_a = sum(w * att[a] * dfn[h] for h, a, hg, ag, w in games)
        bh = num_h / den_h if den_h else bh
        ba = num_a / den_a if den_a else ba

        scored = {t: PRIOR * centre[t][0] for t in teams}
        exp_s = {t: PRIOR for t in teams}
        for h, a, hg, ag, w in games:
            scored[h] += w * hg
            exp_s[h] += w * bh * dfn[a]
            scored[a] += w * ag
            exp_s[a] += w * ba * dfn[h]
        att = {t: scored[t] / exp_s[t] for t in teams}

        conc = {t: PRIOR * centre[t][1] for t in teams}
        exp_c = {t: PRIOR for t in teams}
        for h, a, hg, ag, w in games:
            conc[a] += w * hg
            exp_c[a] += w * bh * att[h]
            conc[h] += w * ag
            exp_c[h] += w * ba * att[a]
        dfn = {t: conc[t] / exp_c[t] for t in teams}
    return bh, ba, att, dfn


def pois(lam, k):
    return math.exp(-lam) * lam ** k / math.factorial(k)


def score_grid(lh, la, rho=0.0):
    g = [[pois(lh, i) * pois(la, j) for j in range(MAX_GOALS + 1)]
         for i in range(MAX_GOALS + 1)]
    if rho:
        g[0][0] *= 1 - lh * la * rho
        g[0][1] *= 1 + lh * rho
        g[1][0] *= 1 + la * rho
        g[1][1] *= 1 - rho
    total = sum(map(sum, g))
    return [[x / total for x in row] for row in g]


def summarise(grid, lines):
    home = draw = away = btts = 0.0
    over = {l: 0.0 for l in lines}
    best = (0, 0, -1.0)
    for i, row in enumerate(grid):
        for j, p in enumerate(row):
            if i > j:
                home += p
            elif i == j:
                draw += p
            else:
                away += p
            for l in lines:
                if i + j > l:
                    over[l] += p
            if i and j:
                btts += p
            if p > best[2]:
                best = (i, j, p)
    return home, draw, away, over, btts, best


def total_goals_dist(lam):
    return [pois(lam, k) for k in range(MAX_GOALS * 2 + 1)]


def pct(x):
    return round(100 * x, 1)


def best_score_for(grid, side):
    """Most likely scoreline that agrees with the pick (1 = home, X = draw, 2 = away)."""
    best = (0, 0, -1.0)
    for i, row in enumerate(grid):
        for j, p in enumerate(row):
            ok = (i > j) if side == "1" else (i < j) if side == "2" else (i == j)
            if ok and p > best[2]:
                best = (i, j, p)
    return best


def pick_of(h, d, a):
    p = max(h, d, a)
    side = "1" if p == h else ("X" if p == d else "2")
    conf = "High" if p >= 0.55 else ("Medium" if p >= 0.45 else "Low")
    return side, conf


def predict_match(m, models):
    ft, h1, h2 = models
    hid, aid = m["homeTeam"]["id"], m["awayTeam"]["id"]

    def lams(model):
        bh, ba, att, dfn = model
        return (bh * att.get(hid, 1) * dfn.get(aid, 1),
                ba * att.get(aid, 1) * dfn.get(hid, 1))

    lh, la = lams(ft)
    grid = score_grid(lh, la, RHO)
    H, D, A, over, btts, _ = summarise(grid, (1.5, 2.5, 3.5))
    side, conf = pick_of(H, D, A)
    best = best_score_for(grid, side)

    halves = {}
    totals = []
    for key, model in (("h1", h1), ("h2", h2)):
        xh, xa = lams(model)
        hgrid = score_grid(xh, xa)
        hh, hd, ha, hover, _, _ = summarise(hgrid, (0.5, 1.5))
        hside, _ = pick_of(hh, hd, ha)
        hbest = best_score_for(hgrid, hside)
        halves[key] = {
            "home": pct(hh), "draw": pct(hd), "away": pct(ha), "pick": hside,
            "xg": [round(xh, 2), round(xa, 2)],
            "over05": pct(hover[0.5]), "over15": pct(hover[1.5]),
            "score": [hbest[0], hbest[1]],
        }
        totals.append(total_goals_dist(xh + xa))

    first_more = second_more = equal = 0.0
    for i, p in enumerate(totals[0]):
        for j, q in enumerate(totals[1]):
            if i > j:
                first_more += p * q
            elif j > i:
                second_more += p * q
            else:
                equal += p * q

    return {
        "id": m["id"],
        "date": m["utcDate"],
        "matchday": m.get("matchday"),
        "home": {"name": m["homeTeam"].get("shortName") or m["homeTeam"]["name"],
                 "crest": m["homeTeam"].get("crest")},
        "away": {"name": m["awayTeam"].get("shortName") or m["awayTeam"]["name"],
                 "crest": m["awayTeam"].get("crest")},
        "ft": {
            "home": pct(H), "draw": pct(D), "away": pct(A),
            "pick": side, "confidence": conf,
            "xg": [round(lh, 2), round(la, 2)],
            "score": [best[0], best[1]], "scorePct": pct(best[2]),
            "over15": pct(over[1.5]), "over25": pct(over[2.5]),
            "over35": pct(over[3.5]), "btts": pct(btts),
        },
        "h1": halves["h1"],
        "h2": halves["h2"],
        "busierHalf": {"first": pct(first_more), "second": pct(second_more),
                       "equal": pct(equal)},
    }


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def main():
    now = datetime.now(timezone.utc)
    os.makedirs(OUT_DIR, exist_ok=True)
    hist_path = os.path.join(OUT_DIR, "history.json")
    history = load_json(hist_path, {"matches": {}})
    out = {"updated": now.isoformat(), "leagues": {}, "matches": []}

    for code, name in LEAGUES.items():
        print(f"{name}...")
        cur = fetch(f"/competitions/{code}/matches")
        if not cur or not cur.get("matches"):
            print("  no data, skipping")
            continue
        matches = cur["matches"]
        comp = cur.get("competition") or {}
        out["leagues"][code] = {"name": name, "emblem": comp.get("emblem")}

        season_start = (matches[0].get("season") or {}).get("startDate", "")
        prev_matches = []
        if season_start[:4].isdigit():
            prev = fetch(f"/competitions/{code}/matches?season={int(season_start[:4]) - 1}")
            prev_matches = (prev or {}).get("matches") or []

        cur_games = finished_games(matches, now)
        prev_games = finished_games(prev_matches, now)
        games = cur_games + prev_games

        teams = set()
        for m in matches + prev_matches:
            teams.add(m["homeTeam"]["id"])
            teams.add(m["awayTeam"]["id"])
        prev_teams = {g[0] for g in prev_games} | {g[1] for g in prev_games}
        centre = {t: (NEWCOMER if prev_games and t not in prev_teams else (1.0, 1.0))
                  for t in teams}

        ft_rows = [(h, a, s[0], s[1], w) for h, a, s, _, w in games]
        h1_rows = [(h, a, ht[0], ht[1], w) for h, a, _, ht, w in games if ht]
        h2_rows = [(h, a, s[0] - ht[0], s[1] - ht[1], w)
                   for h, a, s, ht, w in games if ht]
        models = (fit(ft_rows, teams, centre),
                  fit(h1_rows, teams, centre),
                  fit(h2_rows, teams, centre))

        horizon = now + timedelta(days=DAYS_AHEAD)
        for m in matches:
            when = parse_time(m["utcDate"])
            key = str(m["id"])
            if m.get("status") in ("SCHEDULED", "TIMED") and now < when <= horizon:
                p = predict_match(m, models)
                p["league"] = code
                out["matches"].append(p)
                # lock in the latest pre-kick-off prediction for grading later
                history["matches"][key] = {
                    "league": code, "date": p["date"],
                    "home": p["home"]["name"], "away": p["away"]["name"],
                    "pick": p["ft"]["pick"], "confidence": p["ft"]["confidence"],
                    "probs": [p["ft"]["home"], p["ft"]["draw"], p["ft"]["away"]],
                    "score": p["ft"]["score"], "over25": p["ft"]["over25"],
                    "btts": p["ft"]["btts"], "h1pick": p["h1"]["pick"],
                    "result": None,
                }
            elif m.get("status") == "FINISHED" and key in history["matches"]:
                rec = history["matches"][key]
                if rec.get("result") is None:
                    s = m["score"]
                    rec["result"] = {
                        "ft": [s["fullTime"]["home"], s["fullTime"]["away"]],
                        "ht": [(s.get("halfTime") or {}).get("home"),
                               (s.get("halfTime") or {}).get("away")],
                    }
            elif m.get("status") in ("POSTPONED", "CANCELLED") and key in history["matches"]:
                if history["matches"][key].get("result") is None:
                    del history["matches"][key]

    out["matches"].sort(key=lambda p: p["date"])
    with open(os.path.join(OUT_DIR, "predictions.json"), "w") as f:
        json.dump(out, f, separators=(",", ":"))
    with open(hist_path, "w") as f:
        json.dump(history, f, separators=(",", ":"))
    graded = sum(1 for r in history["matches"].values() if r.get("result"))
    print(f"Done: {len(out['matches'])} upcoming predictions, {graded} graded so far.")


if __name__ == "__main__":
    main()
