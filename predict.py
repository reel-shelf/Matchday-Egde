#!/usr/bin/env python3
"""
Matchday Edge: football predictions.

1. Downloads fixtures, results and tables from football-data.org (free plan)
   for the Premier League, La Liga, Serie A, Bundesliga, Ligue 1, the
   Championship, Eredivisie, Primeira Liga and the Champions League.
2. Rates every team's attack and defence with a Poisson model, counting
   recent games more. All competitions are rated together, so Champions
   League games show how strong the leagues are compared with each other.
   This is done three times: full time, first half and second half.
3. Predicts upcoming matches: result, double chance, goals, both teams to
   score, likely score, and each half.
4. Back-test: replays this season week by week, predicting every game using
   only results from before it, to show how accurate the model would have been.
5. Saves everything in the data folder for the website. data/history.json
   locks in each live prediction before kick-off so it can be graded later.

Needs one secret, passed in as FOOTBALL_DATA_TOKEN (free from football-data.org).
"""
import csv
import io
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

API = "https://api.football-data.org/v4"
COMPS = {
    "PL": "Premier League",
    "PD": "La Liga",
    "SA": "Serie A",
    "BL1": "Bundesliga",
    "FL1": "Ligue 1",
    "CL": "Champions League",
    "ELC": "Championship",
    "DED": "Eredivisie",
    "PPL": "Primeira Liga",
}
DOMESTIC = {"PL", "PD", "SA", "BL1", "FL1", "ELC", "DED", "PPL"}
PAST_SEASONS = {"CL": 1}   # how many earlier seasons to download (default 2)

# free corners data (football-data.co.uk spreadsheets, no key needed)
CORNER_FILES = {"PL": "E0", "PD": "SP1", "SA": "I1", "BL1": "D1", "FL1": "F1",
                "ELC": "E1", "DED": "N1", "PPL": "P1"}
CORNER_URL = "https://www.football-data.co.uk/mmz4281/{season}/{file}.csv"
CORNER_LINES = (6.5, 7.5, 8.5, 9.5, 10.5, 11.5)

DAYS_AHEAD = 14        # how far ahead to predict
HALF_LIFE_DAYS = 150   # a game this old counts half as much as one today
PRIOR = 4.0            # pulls teams with few games towards their starting rating
NEWCOMER = (0.85, 1.15)  # promoted teams / unknown teams start weaker (attack, defence)
RHO = -0.06            # small correction so 0-0 / 1-1 draws aren't undercounted
MAX_GOALS = 10
BEST_PICKS = 6
MIN_GAMES = 8          # both teams need this many recent games for a prediction
RECENT_DAYS = 730      # "recent" = the last 2 years


def recent_counts(games, now):
    """team -> number of finished games in the last RECENT_DAYS."""
    n = defaultdict(int)
    since = now - timedelta(days=RECENT_DAYS)
    for g in games:
        if g["date"] >= since:
            n[g["home"]] += 1
            n[g["away"]] += 1
    return n
OUT_DIR = os.environ.get("OUT_DIR", "data")
CALL_GAP = 6.5         # seconds between API calls (free plan = 10 per minute)

_last_call = 0.0


# ----------------------------------------------------------------- download

def fetch(path):
    """GET from football-data.org. Returns parsed JSON, or None if not allowed."""
    global _last_call
    token = os.environ.get("FOOTBALL_DATA_TOKEN")
    if not token:
        sys.exit("Missing FOOTBALL_DATA_TOKEN secret.")
    req = urllib.request.Request(API + path, headers={"X-Auth-Token": token})
    for _ in range(3):
        wait = CALL_GAP - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
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
        except urllib.error.URLError:
            time.sleep(10)
    return None


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def clean_name(team):
    name = team.get("name") or team.get("shortName") or "?"
    for suffix in (" FC", " AFC", " CF", " SC"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    if name.startswith("FC "):
        name = name[3:]
    return name


def regular_time_score(m):
    """Score after 90 minutes (ignores extra time and penalties)."""
    s = m.get("score") or {}
    if m.get("score", {}).get("duration", "REGULAR") != "REGULAR":
        rt = s.get("regularTime") or {}
        if rt.get("home") is None:
            return None
        return (rt["home"], rt["away"])
    ft = s.get("fullTime") or {}
    if ft.get("home") is None or ft.get("away") is None:
        return None
    return (ft["home"], ft["away"])


def half_time_score(m):
    ht = (m.get("score") or {}).get("halfTime") or {}
    if ht.get("home") is None or ht.get("away") is None:
        return None
    return (ht["home"], ht["away"])


def fetch_csv(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Matchday Edge)"})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
    except Exception as e:
        print(f"  corners: skipped {url} ({e})")
        return []
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig", errors="replace"))))


def parse_csv_date(s):
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime((s or "").strip(), fmt).date()
        except ValueError:
            pass
    return None


def load_corners(games, season_start):
    """
    Corner counts and Bet365 pre-match odds for finished league games, keyed by
    football-data.org match id. Returns (corners, odds).
    The spreadsheets spell team names differently, so names are matched
    automatically: games on the same day with the same score are paired up.
    """
    rows = []
    for code, file in CORNER_FILES.items():
        start = season_start.get(code, "")
        if not start[:4].isdigit():
            continue
        y = int(start[:4])
        for back in range(0, PAST_SEASONS.get(code, 2) + 1):
            yy = y - back
            season = f"{yy % 100:02d}{(yy + 1) % 100:02d}"
            for r in fetch_csv(CORNER_URL.format(season=season, file=file)):
                try:
                    hc = int(r["HC"]) if (r.get("HC") or "").strip().isdigit() else None
                    ac = int(r["AC"]) if (r.get("AC") or "").strip().isdigit() else None
                    rows.append((code, parse_csv_date(r.get("Date")), r["HomeTeam"].strip(),
                                 r["AwayTeam"].strip(), int(r["FTHG"]), int(r["FTAG"]), hc, ac, csv_odds(r)))
                except (KeyError, ValueError, TypeError, AttributeError):
                    continue
    rows = [r for r in rows if r[1]]
    by_day = defaultdict(list)
    for g in games:
        by_day[(g["comp"], g["date"].date())].append(g)

    def near(code, d):
        out = []
        for dd in (d, d - timedelta(days=1), d + timedelta(days=1)):
            out += by_day.get((code, dd), [])
        return out

    votes = defaultdict(Counter)
    for code, d, home, away, hg, ag, hc, ac, _ in rows:
        cands = [g for g in near(code, d) if g["ft"] == (hg, ag)]
        if len(cands) == 1:
            votes[(code, home)][cands[0]["home"]] += 1
            votes[(code, away)][cands[0]["away"]] += 1
    name_to_id = {k: c.most_common(1)[0][0] for k, c in votes.items()}

    corners, odds = {}, {}
    for code, d, home, away, hg, ag, hc, ac, o in rows:
        hid, aid = name_to_id.get((code, home)), name_to_id.get((code, away))
        if hid is None or aid is None:
            continue
        for g in near(code, d):
            if g["home"] == hid and g["away"] == aid:
                if hc is not None and ac is not None:
                    corners[g["id"]] = (hc, ac)
                if o:
                    odds[g["id"]] = o
                break
    return corners, odds


def csv_odds(r):
    """Bet365 pre-match odds from a football-data.co.uk row (decimal)."""
    out = {}
    for key, col in (("1", "B365H"), ("X", "B365D"), ("2", "B365A"), ("O25", "B365>2.5"), ("U25", "B365<2.5")):
        try:
            v = float(r.get(col) or "")
            if v > 1:
                out[key] = v
        except ValueError:
            pass
    return out if all(k in out for k in ("1", "X", "2")) else None


# ----------------------------------------------------------------- calibration
# The goals model is simple, so its over/under and both-teams-score chances drift (this
# season it under-rated goals). Each is corrected with a 2-number logistic fit
# (p' = 1 / (1 + exp(-(a + b * logit(p))))) on past back-test games with known results.
CALIB_MARKETS = {"O15": ("over15", lambda f: f[0] + f[1] >= 2), "O25": ("over25", lambda f: f[0] + f[1] >= 3),
                 "O35": ("over35", lambda f: f[0] + f[1] >= 4), "BTTS": ("btts", lambda f: f[0] > 0 and f[1] > 0)}
CALIB_MIN_GAMES = 150


def logit(p):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def fit_platt(pairs):
    """pairs: (model chance 0-1, happened 0/1). Returns (a, b); (0, 1) means no change."""
    if len(pairs) < CALIB_MIN_GAMES:
        return 0.0, 1.0
    a, b = 0.0, 1.0
    for _ in range(50):              # Newton steps with a little ridge towards (0, 1)
        ga = gb = haa = hab = hbb = 0.0
        for p, y in pairs:
            x = logit(p)
            q = 1 / (1 + math.exp(-(a + b * x)))
            ga += q - y
            gb += (q - y) * x
            w = q * (1 - q)
            haa += w; hab += w * x; hbb += w * x * x
        ga += a; gb += b - 1; haa += 1; hbb += 1
        det = haa * hbb - hab * hab
        if det <= 0:
            break
        da, db = (hbb * ga - hab * gb) / det, (haa * gb - hab * ga) / det
        a, b = a - da, b - db
        if abs(da) + abs(db) < 1e-6:
            break
    return round(a, 4), round(b, 4)


def fit_calibration(rows):
    """rows: back-test records with results. -> {market: (a, b)}"""
    out = {}
    for mk, (key, happened) in CALIB_MARKETS.items():
        pairs = [(r["raw"][key] / 100, 1 if happened(r["result"]["ft"]) else 0)
                 for r in rows if r.get("result") and r.get("raw", {}).get(key) is not None]
        out[mk] = fit_platt(pairs)
    return out


def calibrate(p, calib):
    """Apply the corrections to a prediction's full-time goals chances (keeps the raw ones)."""
    f = p["ft"]
    f["raw"] = {key: f[key] for key, _ in CALIB_MARKETS.values()}
    for mk, (key, _) in CALIB_MARKETS.items():
        a, b = calib.get(mk, (0.0, 1.0))
        q = 1 / (1 + math.exp(-(a + b * logit(f[key] / 100))))
        f[key] = round(100 * q, 1)
    return p


# ----------------------------------------------------------------- model

def fit(rows, teams, centre, as_of, init=None, iters=80):
    """
    Rate each team. Expected goals:
      home side = base_home * attack[home] * defence[away]
      away side = base_away * attack[away] * defence[home]
    attack > 1 scores more than average; defence > 1 concedes more.
    rows: list of (home, away, home_goals, away_goals, kickoff)
    Only games before as_of are used; older games count less.
    """
    games = []
    for h, a, hg, ag, t in rows:
        if t < as_of:
            days = (as_of - t).total_seconds() / 86400
            games.append((h, a, hg, ag, 0.5 ** (days / HALF_LIFE_DAYS)))
    if init:
        bh, ba, att, dfn = init
        att = {t: att.get(t, centre[t][0]) for t in teams}
        dfn = {t: dfn.get(t, centre[t][1]) for t in teams}
    else:
        bh, ba = 1.4, 1.1
        att = {t: centre[t][0] for t in teams}
        dfn = {t: centre[t][1] for t in teams}
    if not games:
        return bh, ba, att, dfn
    num_h = sum(w * hg for h, a, hg, ag, w in games)
    num_a = sum(w * ag for h, a, hg, ag, w in games)
    for _ in range(iters):
        den_h = sum(w * att[h] * dfn[a] for h, a, hg, ag, w in games)
        den_a = sum(w * att[a] * dfn[h] for h, a, hg, ag, w in games)
        bh = num_h / den_h if den_h else bh
        ba = num_a / den_a if den_a else ba

        scored = {t: PRIOR * centre[t][0] for t in teams}
        exp_s = dict.fromkeys(teams, PRIOR)
        for h, a, hg, ag, w in games:
            scored[h] += w * hg
            exp_s[h] += w * bh * dfn[a]
            scored[a] += w * ag
            exp_s[a] += w * ba * dfn[h]
        att = {t: scored[t] / exp_s[t] for t in teams}

        conc = {t: PRIOR * centre[t][1] for t in teams}
        exp_c = dict.fromkeys(teams, PRIOR)
        for h, a, hg, ag, w in games:
            conc[a] += w * hg
            exp_c[a] += w * bh * att[h]
            conc[h] += w * ag
            exp_c[h] += w * ba * att[a]
        dfn = {t: conc[t] / exp_c[t] for t in teams}
    return bh, ba, att, dfn


def fit_all(games, teams, centre, as_of, init=None, iters=80):
    ft = [(g["home"], g["away"], g["ft"][0], g["ft"][1], g["date"]) for g in games]
    h1 = [(g["home"], g["away"], g["ht"][0], g["ht"][1], g["date"]) for g in games if g["ht"]]
    h2 = [(g["home"], g["away"], g["ft"][0] - g["ht"][0], g["ft"][1] - g["ht"][1], g["date"])
          for g in games if g["ht"]]
    init = init or (None, None, None)
    return (fit(ft, teams, centre, as_of, init[0], iters),
            fit(h1, teams, centre, as_of, init[1], iters),
            fit(h2, teams, centre, as_of, init[2], iters))


def fit_corners(games, corners, teams, centre, as_of, init=None, iters=80):
    """Corner ratings (same method as goals) plus how spread out corner totals are."""
    rows = [(g["home"], g["away"], corners[g["id"]][0], corners[g["id"]][1], g["date"])
            for g in games if g["id"] in corners]
    if not any(t < as_of for *_, t in rows):
        return None
    model = fit(rows, teams, centre, as_of, init[0] if init else None, iters)
    bh, ba, att, dfn = model
    spread, n = 0.0, 0
    for h, a, hc, ac, t in rows:
        if t < as_of:
            mu = bh * att[h] * dfn[a] + ba * att[a] * dfn[h]
            spread += (hc + ac - mu) ** 2 / mu
            n += 1
    return model, max(1.0, spread / n) if n else 1.0


def total_dist(mu, spread, top=30):
    """Chance of each total (0..top) with mean mu; spread > 1 means more variable than Poisson."""
    if spread <= 1.02:
        return [pois(mu, k) for k in range(top + 1)]
    n = mu / (spread - 1)
    p = n / (n + mu)
    return [math.exp(math.lgamma(k + n) - math.lgamma(n) - math.lgamma(k + 1)
                     + n * math.log(p) + k * math.log(1 - p)) for k in range(top + 1)]


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
    return home, draw, away, over, btts


def best_score_for(grid, side):
    """Most likely scoreline that agrees with the pick."""
    best = (0, 0, -1.0)
    for i, row in enumerate(grid):
        for j, p in enumerate(row):
            ok = {"1": i > j, "2": i < j, "X": i == j,
                  "1X": i >= j, "X2": i <= j, "12": i != j}[side]
            if ok and p > best[2]:
                best = (i, j, p)
    return best


def straight_pick(h, d, a):
    p = max(h, d, a)
    return "1" if p == h else ("X" if p == d else "2")


def main_pick(h, d, a):
    """
    The headline pick:
    - a clear favourite (45%+) -> that team to win
    - a very close game where a draw is quite likely -> draw
    - otherwise -> double chance: the favourite or a draw
    Returns (pick, chance of the pick, confidence).
    """
    if max(h, a) >= 0.45:
        side = "1" if h >= a else "2"
        p = max(h, a)
        return side, p, "High" if p >= 0.55 else "Medium"
    if abs(h - a) < 0.06 and d >= 0.28:
        return "X", d, "Low"
    side, p = ("1X", h + d) if h >= a else ("X2", a + d)
    return side, p, "High" if p >= 0.78 else ("Medium" if p >= 0.68 else "Low")


def pct(x):
    return round(100 * x, 1)


def predict(hid, aid, models, corner_model=None):
    ft, m1, m2 = models

    def lams(model):
        bh, ba, att, dfn = model
        return (bh * att.get(hid, 1) * dfn.get(aid, 1),
                ba * att.get(aid, 1) * dfn.get(hid, 1))

    lh, la = lams(ft)
    grid = score_grid(lh, la, RHO)
    H, D, A, over, btts = summarise(grid, (1.5, 2.5, 3.5))
    pick, pick_p, conf = main_pick(H, D, A)
    # predicted score: most likely scoreline for the single most likely result
    best = best_score_for(grid, "X" if pick == "X" else straight_pick(H, D, A))

    halves, totals = {}, []
    for key, model in (("h1", m1), ("h2", m2)):
        xh, xa = lams(model)
        hgrid = score_grid(xh, xa)
        hh, hd, ha, hover, _ = summarise(hgrid, (0.5, 1.5))
        hside = straight_pick(hh, hd, ha)
        hbest = best_score_for(hgrid, hside)
        halves[key] = {
            "home": pct(hh), "draw": pct(hd), "away": pct(ha), "pick": hside,
            "xg": [round(xh, 2), round(xa, 2)],
            "over05": pct(hover[0.5]), "over15": pct(hover[1.5]),
            "score": [hbest[0], hbest[1]],
        }
        totals.append([pois(xh + xa, k) for k in range(MAX_GOALS * 2 + 1)])

    first = second = equal = 0.0
    for i, p in enumerate(totals[0]):
        for j, q in enumerate(totals[1]):
            if i > j:
                first += p * q
            elif j > i:
                second += p * q
            else:
                equal += p * q

    corners = None
    if corner_model:
        (cbh, cba, catt, cdfn), spread = corner_model
        ch = cbh * catt.get(hid, 1) * cdfn.get(aid, 1)
        ca = cba * catt.get(aid, 1) * cdfn.get(hid, 1)
        dist = total_dist(ch + ca, spread)
        corners = {"exp": [round(ch, 1), round(ca, 1)], "total": round(ch + ca, 1),
                   "over": {str(l): pct(sum(dist[int(l) + 1:])) for l in CORNER_LINES}}

    return {
        "corners": corners,
        "ft": {
            "home": pct(H), "draw": pct(D), "away": pct(A),
            "pick": pick, "pickPct": pct(pick_p), "confidence": conf,
            "dc": {"1X": pct(H + D), "X2": pct(D + A), "12": pct(H + A)},
            "xg": [round(lh, 2), round(la, 2)],
            "score": [best[0], best[1]],
            "over15": pct(over[1.5]), "over25": pct(over[2.5]),
            "over35": pct(over[3.5]), "btts": pct(btts),
        },
        "h1": halves["h1"],
        "h2": halves["h2"],
        "busierHalf": {"first": pct(first), "second": pct(second), "equal": pct(equal)},
    }


def record(code, date, home, away, p):
    """Compact copy of a prediction, graded later against the real score."""
    f = p["ft"]
    return {
        "league": code, "date": date, "home": home, "away": away,
        "pick": f["pick"], "confidence": f["confidence"],
        "probs": [f["home"], f["draw"], f["away"]],
        "dcPick": "1X" if f["home"] >= f["away"] else "X2",
        "score": f["score"], "over15": f["over15"], "over25": f["over25"], "over35": f["over35"],
        "btts": f["btts"], "raw": f.get("raw"),
        "h1pick": p["h1"]["pick"], "h2pick": p["h2"]["pick"],
        "cornersOver95": p["corners"]["over"]["9.5"] if p.get("corners") else None,
        "result": None,
    }


# ----------------------------------------------------------------- helpers for the site

def result_letter(g, team):
    hg, ag = g["ft"]
    mine, theirs = (hg, ag) if g["home"] == team else (ag, hg)
    return "W" if mine > theirs else ("L" if mine < theirs else "D")


def table_from_games(games, teams_info):
    t = defaultdict(lambda: {"p": 0, "w": 0, "d": 0, "l": 0, "gf": 0, "ga": 0})
    for g in games:
        hg, ag = g["ft"]
        for team, f, a in ((g["home"], hg, ag), (g["away"], ag, hg)):
            r = t[team]
            r["p"] += 1
            r["gf"] += f
            r["ga"] += a
            r["w" if f > a else ("l" if f < a else "d")] += 1
    rows = []
    for team, r in t.items():
        rows.append({"id": team, "name": teams_info[team]["name"], "crest": teams_info[team]["crest"],
                     "p": r["p"], "w": r["w"], "d": r["d"], "l": r["l"],
                     "gd": r["gf"] - r["ga"], "pts": 3 * r["w"] + r["d"]})
    rows.sort(key=lambda r: (-r["pts"], -r["gd"], r["name"]))
    for i, r in enumerate(rows, 1):
        r["pos"] = i
    return rows


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


# ----------------------------------------------------------------- main

def main():
    now = datetime.now(timezone.utc)
    os.makedirs(OUT_DIR, exist_ok=True)
    hist_path = os.path.join(OUT_DIR, "history.json")
    history = load_json(hist_path, {"matches": {}})

    teams_info = {}
    games = []            # every finished game, all competitions and seasons
    fixtures = []         # upcoming games
    current = {}          # comp -> list of this season's API matches
    standings = {}
    emblems = {}
    season_start = {}
    seen_ids = set()
    league_members = defaultdict(lambda: defaultdict(set))  # comp -> season -> teams

    for code, name in COMPS.items():
        print(f"{name}...")
        cur = fetch(f"/competitions/{code}/matches")
        if not cur or not cur.get("matches"):
            print("  no data, skipping")
            continue
        emblems[code] = (cur.get("competition") or {}).get("emblem")
        current[code] = cur["matches"]
        start = (cur["matches"][0].get("season") or {}).get("startDate", "")
        season_start[code] = start
        batches = [(0, cur["matches"])]
        if start[:4].isdigit():
            for back in range(1, PAST_SEASONS.get(code, 2) + 1):
                old = fetch(f"/competitions/{code}/matches?season={int(start[:4]) - back}")
                if old and old.get("matches"):
                    batches.append((back, old["matches"]))
        st = fetch(f"/competitions/{code}/standings")
        if st:
            standings[code] = st

        for back, matches in batches:
            for m in matches:
                for side in ("homeTeam", "awayTeam"):
                    t = m.get(side) or {}
                    if t.get("id") and t["id"] not in teams_info:
                        teams_info[t["id"]] = {"name": clean_name(t), "crest": t.get("crest")}
                if not (m.get("homeTeam") or {}).get("id") or not (m.get("awayTeam") or {}).get("id"):
                    continue
                league_members[code][back].update({m["homeTeam"]["id"], m["awayTeam"]["id"]})
                if m.get("status") == "FINISHED" and m["id"] not in seen_ids:
                    ft = regular_time_score(m)
                    if ft is None:
                        continue
                    seen_ids.add(m["id"])
                    games.append({"id": m["id"], "comp": code, "date": parse_time(m["utcDate"]),
                                  "home": m["homeTeam"]["id"], "away": m["awayTeam"]["id"],
                                  "ft": ft, "ht": half_time_score(m), "season": back,
                                  "stage": m.get("stage")})
                elif back == 0 and m.get("status") in ("SCHEDULED", "TIMED"):
                    fixtures.append((code, m))
    games.sort(key=lambda g: g["date"])
    print("Corners...")
    corners, book_odds = load_corners(games, season_start)
    print(f"  Bet365 odds found for {len(book_odds)} games")
    print(f"  corners found for {len(corners)} games")

    # starting ratings: promoted teams and teams only seen in Europe start weaker
    teams = set(teams_info)
    domestic_teams = set()
    for code in DOMESTIC:
        for members in league_members[code].values():
            domestic_teams |= members
    centre = {}
    for t in teams:
        centre[t] = (1.0, 1.0) if t in domestic_teams else NEWCOMER
    for code in DOMESTIC:
        now_in, before = league_members[code].get(0, set()), league_members[code].get(1, set())
        if before:
            for t in now_in - before:
                centre[t] = NEWCOMER

    # ------------------------------------------------------------- back-test
    print("Back-testing this season...")
    cur_games = [g for g in games if g["season"] == 0]
    weeks = defaultdict(list)
    for g in cur_games:
        monday = (g["date"] - timedelta(days=g["date"].weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
        weeks[monday].append(g)
    backtest, models, cmodel = [], None, None
    for monday in sorted(weeks):
        # goals calibration fitted only on earlier weeks' games, so the back-test stays honest
        calib = fit_calibration(backtest)
        models = fit_all(games, teams, centre, monday, models, iters=80 if models is None else 25)
        cmodel = fit_corners(games, corners, teams, centre, monday, cmodel,
                             iters=80 if cmodel is None else 25) if corners else None
        for g in weeks[monday]:
            p = calibrate(predict(g["home"], g["away"], models,
                                  cmodel if g["comp"] in CORNER_FILES else None), calib)
            r = record(g["comp"], g["date"].isoformat(), teams_info[g["home"]]["name"],
                       teams_info[g["away"]]["name"], p)
            if g["id"] in book_odds:
                r["book"] = book_odds[g["id"]]
            r["result"] = {"ft": list(g["ft"]), "ht": list(g["ht"]) if g["ht"] else [None, None],
                           "corners": sum(corners[g["id"]]) if g["id"] in corners else None}
            backtest.append(r)

    # ------------------------------------------------------------- live predictions
    models = fit_all(games, teams, centre, now, models, iters=80)
    cmodel = fit_corners(games, corners, teams, centre, now, cmodel, iters=80) if corners else None

    by_team = defaultdict(list)
    for g in cur_games:
        by_team[g["home"]].append(g)
        by_team[g["away"]].append(g)

    def form(team, comp=None, n=5):
        gs = [g for g in by_team[team] if comp is None or g["comp"] == comp]
        return [result_letter(g, team) for g in gs[-n:]][::-1]   # newest first

    pair_games = defaultdict(list)
    for g in games:
        pair_games[frozenset((g["home"], g["away"]))].append(g)

    out = {"updated": now.isoformat(), "leagues": {}, "matches": [], "tables": {}, "best": []}
    for code in current:
        out["leagues"][code] = {"name": COMPS[code], "emblem": emblems.get(code)}

    horizon = now + timedelta(days=DAYS_AHEAD)
    counts = recent_counts(games, now)
    calib = fit_calibration(backtest)
    out["calib"] = {k: list(v) for k, v in calib.items()}
    print("Goals calibration:", out["calib"])
    for code, m in fixtures:
        when = parse_time(m["utcDate"])
        if not (now < when <= horizon):
            continue
        hid, aid = m["homeTeam"]["id"], m["awayTeam"]["id"]
        p = calibrate(predict(hid, aid, models, cmodel if code in CORNER_FILES else None), calib)
        h2h = sorted(pair_games[frozenset((hid, aid))], key=lambda g: g["date"], reverse=True)[:5]
        p.update({
            "id": m["id"], "league": code, "date": m["utcDate"], "matchday": m.get("matchday"),
            "home": {"name": teams_info[hid]["name"], "crest": teams_info[hid]["crest"],
                     "form": form(hid)},
            "away": {"name": teams_info[aid]["name"], "crest": teams_info[aid]["crest"],
                     "form": form(aid)},
            "h2h": [{"date": g["date"].date().isoformat(), "league": g["comp"],
                     "home": teams_info[g["home"]]["name"], "away": teams_info[g["away"]]["name"],
                     "score": list(g["ft"])} for g in h2h],
            "n": [counts[hid], counts[aid]],
            "enough": min(counts[hid], counts[aid]) >= MIN_GAMES,
        })
        out["matches"].append(p)
        if p["enough"]:      # no prediction is made (or graded) without enough data
            history["matches"][str(m["id"])] = record(code, m["utcDate"], p["home"]["name"],
                                                      p["away"]["name"], p)
        else:
            history["matches"].pop(str(m["id"]), None)

    # grade finished live predictions; drop postponed ones
    for code, matches in current.items():
        for m in matches:
            key = str(m["id"])
            rec = history["matches"].get(key)
            if not rec:
                continue
            if rec.get("result") is not None:
                if rec["result"].get("corners") is None and m["id"] in corners:
                    rec["result"]["corners"] = sum(corners[m["id"]])
                continue
            if m.get("status") == "FINISHED":
                ft = regular_time_score(m)
                if ft:
                    ht = half_time_score(m)
                    rec["result"] = {"ft": list(ft), "ht": list(ht) if ht else [None, None],
                                     "corners": sum(corners[m["id"]]) if m["id"] in corners else None}
            elif m.get("status") in ("POSTPONED", "CANCELLED"):
                del history["matches"][key]

    out["matches"].sort(key=lambda p: p["date"])

    # best picks: the most likely main picks in the next 7 days
    week = now + timedelta(days=7)
    ok = [p for p in out["matches"] if p["enough"]]
    soon = [p for p in ok if parse_time(p["date"]) <= week] or ok
    soon.sort(key=lambda p: p["ft"]["pickPct"], reverse=True)
    out["best"] = [p["id"] for p in soon[:BEST_PICKS]]

    # league tables (official ones when available, otherwise worked out from results)
    for code in current:
        rows = []
        st = standings.get(code) or {}
        total = next((s for s in st.get("standings", []) if s.get("type") == "TOTAL"), None)
        if total and total.get("table"):
            for r in total["table"]:
                t = r["team"]
                rows.append({"id": t["id"], "pos": r["position"],
                             "name": teams_info.get(t["id"], {}).get("name") or clean_name(t),
                             "crest": t.get("crest"), "p": r["playedGames"], "w": r["won"],
                             "d": r["draw"], "l": r["lost"], "gd": r["goalDifference"],
                             "pts": r["points"]})
        else:
            league_games = [g for g in cur_games if g["comp"] == code
                            and g["stage"] in (None, "REGULAR_SEASON", "LEAGUE_STAGE")]
            rows = table_from_games(league_games, teams_info)
        for r in rows:
            r["form"] = form(r["id"], code)
        out["tables"][code] = rows

    with open(os.path.join(OUT_DIR, "predictions.json"), "w") as f:
        json.dump(out, f, separators=(",", ":"))
    with open(os.path.join(OUT_DIR, "history.json"), "w") as f:
        json.dump(history, f, separators=(",", ":"))
    with open(os.path.join(OUT_DIR, "backtest.json"), "w") as f:
        json.dump({"updated": now.isoformat(), "rows": backtest}, f, separators=(",", ":"))
    graded = sum(1 for r in history["matches"].values() if r.get("result"))
    print(f"Done: {len(out['matches'])} upcoming predictions, {graded} graded so far, "
          f"{len(backtest)} past games back-tested.")


if __name__ == "__main__":
    main()
