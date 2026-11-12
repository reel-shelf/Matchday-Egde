"""
Matchday Edge: shared parlay and team-news maths (Python copy of the logic in index.html).

Used by tracker.py to lock in each day's parlays and grade them, and to measure
whether the team-news adjustment makes the picks better.

Markets: "1" home win, "2" away win, "X" draw, "1X"/"X2" team or draw, "12" no draw,
"O15"/"O25"/"O35" over 1.5/2.5/3.5 goals, "BTTS" both teams score.
"""
import math

TIERS = (10, 20, 100, 1000)
MAX_LEGS = 30
MIN_GAMES = 8          # both teams need this many recent games (predict.py sets m["enough"])
BLEND_UNTIL = 15       # below this many games, our chance is blended with the bookmaker's
CHECK_EDGE = 0.30      # an edge bigger than this is flagged "check this"


def enough(m):
    return m.get("enough", True)


def merge_news(api_news, manual):
    """Team news from the API plus players you marked yourself (data/manual-news.json)."""
    out = dict(api_news or {})
    for k, man in (manual or {}).items():
        base = out.get(k) or {"home": {"out": [], "benched": []}, "away": {"out": [], "benched": []}}
        merged = dict(base)
        for side in ("home", "away"):
            b = dict(base.get(side) or {"out": [], "benched": []})
            names = {p.get("name") for p in b.get("out") or []}
            extra = [{"name": p["name"], "status": "out", "key": bool(p.get("key")),
                      "reason": "rested" if p.get("type") == "rested" else "injured", "manual": True}
                     for p in man.get(side) or [] if p.get("name") not in names]
            b["out"] = list(b.get("out") or []) + extra
            b.setdefault("benched", [])
            merged[side] = b
        out[k] = merged
    return out


# ----------------------------------------------------------------- team news
def impact(side):
    """Missing players lower a team's expected goals and slightly raise the opponent's."""
    att = dfn = 0.0
    for p in side.get("out") or []:
        w = 0.5 if p.get("status") == "doubt" else 1.0
        att += (0.07 if p.get("key") else 0.02) * w
        dfn += 0.015 * w
    att += 0.05 * len(side.get("benched") or [])
    return min(att, 0.25), min(dfn, 0.12)


def full_time(lh, la, old):
    n, rho = 10, -0.06
    pois = lambda l, k: math.exp(-l) * l ** k / math.factorial(k)
    g = [[pois(lh, i) * pois(la, j) for j in range(n + 1)] for i in range(n + 1)]
    g[0][0] *= 1 - lh * la * rho
    g[0][1] *= 1 + lh * rho
    g[1][0] *= 1 + la * rho
    g[1][1] *= 1 - rho
    tot = sum(map(sum, g))
    H = D = A = bt = o15 = o25 = o35 = 0.0
    for i, row in enumerate(g):
        for j, x in enumerate(row):
            x /= tot
            if i > j:
                H += x
            elif i == j:
                D += x
            else:
                A += x
            if i and j:
                bt += x
            o15 += x if i + j > 1.5 else 0
            o25 += x if i + j > 2.5 else 0
            o35 += x if i + j > 3.5 else 0
    pc = lambda x: round(1000 * x) / 10
    out = dict(old)
    out.update({"home": pc(H), "draw": pc(D), "away": pc(A),
                "dc": {"1X": pc(H + D), "X2": pc(D + A), "12": pc(H + A)},
                "xg": [round(lh, 2), round(la, 2)],
                "over15": pc(o15), "over25": pc(o25), "over35": pc(o35), "btts": pc(bt)})
    return out


def apply_news(m, news):
    """Returns the match with team-news-adjusted probabilities (or unchanged)."""
    n = (news or {}).get(str(m["id"]))
    if not n:
        return m
    ah, dh = impact(n["home"])
    aa, da = impact(n["away"])
    if not (ah or dh or aa or da):
        return m
    xh, xa = m["ft"]["xg"]
    out = dict(m)
    out["ftBase"] = m["ft"]
    out["ft"] = full_time(xh * (1 - ah) * (1 + da), xa * (1 - aa) * (1 + dh), m["ft"])
    return out


# ----------------------------------------------------------------- bookmaker blend
def main_pick(H, D, A):
    """Same headline-pick rules as predict.py (H, D, A as fractions)."""
    if max(H, A) >= 0.45:
        side, p = ("1", H) if H >= A else ("2", A)
        return side, p, "High" if p >= 0.55 else "Medium"
    if abs(H - A) < 0.06 and D >= 0.28:
        return "X", D, "Low"
    side, p = ("1X", H + D) if H >= A else ("X2", A + D)
    return side, p, "High" if p >= 0.78 else ("Medium" if p >= 0.68 else "Low")


def implied(o):
    """Bookmaker chances (percent) with the bookmaker's margin taken out."""
    if not o:
        return {}
    r = {}
    if all(o.get(k) for k in ("1", "X", "2")):
        s = sum(1 / o[k] for k in ("1", "X", "2"))
        for k in ("1", "X", "2"):
            r[k] = 100 / o[k] / s
    for k in ("O15", "O25", "O35", "BTTS"):
        if o.get(k):
            r[k] = min(99.0, 100 / o[k] / 1.05)
    return r


def blend(m, o):
    """Teams with fewer than BLEND_UNTIL recent games: mix our chance with the bookmaker's.
    8 games -> half and half; 15 or more -> ours only."""
    n = min(m.get("n") or [99, 99])
    imp = implied(o)
    if n >= BLEND_UNTIL or not imp:
        return m
    w = 0.5 + 0.5 * max(0, n - MIN_GAMES) / (BLEND_UNTIL - MIN_GAMES)
    f = dict(m["ft"])
    mix = lambda a, b: round(w * a + (1 - w) * b, 1)
    if "1" in imp:
        f["home"], f["draw"], f["away"] = mix(f["home"], imp["1"]), mix(f["draw"], imp["X"]), mix(f["away"], imp["2"])
        f["dc"] = {"1X": round(f["home"] + f["draw"], 1), "X2": round(f["draw"] + f["away"], 1),
                   "12": round(f["home"] + f["away"], 1)}
        pick, pp, conf = main_pick(f["home"] / 100, f["draw"] / 100, f["away"] / 100)
        f.update(pick=pick, pickPct=round(100 * pp, 1), confidence=conf)
    for k, key in (("O15", "over15"), ("O25", "over25"), ("O35", "over35"), ("BTTS", "btts")):
        if k in imp:
            f[key] = mix(f[key], imp[k])
    out = dict(m)
    out["ft"], out["blend"] = f, round(w, 2)
    return out


# ----------------------------------------------------------------- parlays
def favourite(m):
    f = m["ft"]
    home = f["home"] >= f["away"]
    return {"name": m["home"]["name"] if home else m["away"]["name"],
            "win": f["home"] if home else f["away"], "dc": f["dc"]["1X"] if home else f["dc"]["X2"],
            "winMarket": "1" if home else "2", "dcMarket": "1X" if home else "X2"}


def leg(m, market, label, p):
    return {"id": m["id"], "market": market, "label": label, "p": p,
            "home": m["home"]["name"], "away": m["away"]["name"], "date": m["date"], "league": m["league"]}


def safe_leg(m):
    fav = favourite(m)
    if fav["win"] >= 65:
        return leg(m, fav["winMarket"], f"{fav['name']} to win", fav["win"])
    return leg(m, fav["dcMarket"], f"{fav['name']} or draw", fav["dc"])


def win_leg(m):
    fav = favourite(m)
    return leg(m, fav["winMarket"], f"{fav['name']} to win", fav["win"])


# Pick types, so every parlay mixes them instead of piling up straight wins.
FAMILY = {"1": "win", "2": "win", "1X": "dc", "X2": "dc", "O15": "goals", "O25": "goals",
          "BTTS": "btts", "H1_1": "half", "H1_2": "half"}


def candidates(m):
    """Every pick we'd consider for a game: favourite to win, favourite or draw, over 1.5 / 2.5
    goals, both teams score, favourite winning the 1st half (clubs only: needs half-time scores)."""
    fav, f = favourite(m), m["ft"]
    opts = [leg(m, fav["winMarket"], f"{fav['name']} to win", fav["win"]),
            leg(m, fav["dcMarket"], f"{fav['name']} or draw", fav["dc"]),
            leg(m, "O15", "Over 1.5 goals", f["over15"]),
            leg(m, "O25", "Over 2.5 goals", f["over25"]),
            leg(m, "BTTS", "Both teams score", f["btts"])]
    if m.get("league") != "INT" and m.get("h1"):
        home = fav["winMarket"] == "1"
        opts.append(leg(m, "H1_1" if home else "H1_2", f"{fav['name']} to win 1st half",
                        m["h1"]["home"] if home else m["h1"]["away"]))
    return [l for l in opts if 1 < l["p"] < 99]


leg_options = candidates


def pick_next(pool, used, counts):
    """Most likely pick from the pick type used least so far (one pick per game)."""
    avail = [l for l in pool if l["id"] not in used]
    if not avail:
        return None
    least = min(counts.get(FAMILY[l["market"]], 0) for l in avail)
    return next(l for l in avail if counts.get(FAMILY[l["market"]], 0) == least)


def mixed_pool(games, lo, hi):
    return sorted((l for m in games for l in candidates(m) if lo <= l["p"] <= hi), key=lambda l: -l["p"])


def fair_odds(legs):
    o = 1.0
    for l in legs:
        o *= 100 / l["p"]
    return o


def build_tiers(games):
    pool = mixed_pool(games, 55, 97)
    used, legs, out, counts = set(), [], [], {}
    for target in TIERS:
        while len(legs) < MAX_LEGS:
            odds = fair_odds(legs)
            if odds >= target:
                break
            # Can one more pick finish this tier? Take the one landing closest above the target.
            finish = None
            for m in games:
                if m["id"] in used:
                    continue
                for l in candidates(m):
                    o = odds * 100 / l["p"]
                    if target <= o <= target * 1.25 and (finish is None or o < finish[1]):
                        finish = (l, o)
            nxt = finish[0] if finish else pick_next(pool, used, counts)
            if not nxt:
                break
            used.add(nxt["id"])
            counts[FAMILY[nxt["market"]]] = counts.get(FAMILY[nxt["market"]], 0) + 1
            legs.append(nxt)
        out.append({"target": target, "legs": list(legs), "odds": fair_odds(legs)})
    return out


def chance(legs):
    c = 1.0
    for l in legs:
        c *= l["p"] / 100
    return c


def build_risk(games):
    """Safe (about 50%+ to win), Bold (about 20-30%) and Long Shot (about 5%) parlays,
    each mixing pick types: wins, win-or-draw, goals, both teams score, 1st half."""
    def grow(lo, hi, floor, min_legs, max_legs):
        pool, used, counts, legs = mixed_pool(games, lo, hi), set(), {}, []
        while len(legs) < max_legs:
            nxt = pick_next(pool, used, counts)
            if not nxt or (len(legs) >= min_legs and chance(legs + [nxt]) < floor):
                break
            used.add(nxt["id"])
            counts[FAMILY[nxt["market"]]] = counts.get(FAMILY[nxt["market"]], 0) + 1
            legs.append(nxt)
        return legs
    return {"safe": grow(68, 98, 0.50, 2, 4), "bold": grow(55, 90, 0.20, 3, 8),
            "long": grow(45, 85, 0.04, 5, 14)}


def leg_result(market, ft, ht=None):
    """True (won) / False (lost), or None if it can't be told (1st-half pick without a half-time score)."""
    if market.startswith("H1_"):
        if not ht or ht[0] is None or ht[1] is None:
            return None
        return ht[0] > ht[1] if market == "H1_1" else ht[1] > ht[0]
    h, a = ft
    return {"1": h > a, "2": a > h, "X": h == a, "1X": h >= a, "X2": a >= h, "12": h != a,
            "O15": h + a >= 2, "O25": h + a >= 3, "O35": h + a >= 4, "BTTS": h > 0 and a > 0}[market]


def market_prob(f, market):
    return {"1": f["home"], "2": f["away"], "X": f["draw"], "1X": f["dc"]["1X"], "X2": f["dc"]["X2"],
            "12": f["dc"]["12"], "O15": f["over15"], "O25": f["over25"], "O35": f.get("over35"),
            "BTTS": f["btts"]}.get(market)
