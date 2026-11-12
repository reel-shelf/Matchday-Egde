#!/usr/bin/env python3
"""
Matchday Edge: international games (national teams). Free sources, no API key.

1. History: the open international results dataset (github.com/martj42/international_results,
   every men's international since 1872), last 3 years, plus recent results from ESPN
   for the weeks the dataset hasn't caught up with yet.
2. Rates every national team with the same Poisson model as the clubs (predict.py),
   with older games counting for more, because national teams play less often.
3. Upcoming games for the next 14 days from ESPN's free (unofficial) scoreboard:
   Nations League, friendlies, qualifiers and tournaments, with bookmaker odds
   (DraftKings) when ESPN has them.
4. Adds them to predictions.json (league "INT") and locks/grades their picks in
   history.json, so parlays and the track record include them.

Runs after predict.py in .github/workflows/predict.yml. ESPN's feed isn't an official
API: if it changes, international games simply stop appearing; club games are unaffected.
"""
import csv
import gzip
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import predict

OUT_DIR = os.environ.get("OUT_DIR", "data")
CACHE_PATH = os.path.join(OUT_DIR, "intl-results.json")
RESULTS_CSV = "https://raw.githubusercontent.com/martj42/international_results/master/results.csv"
ESPN = "https://site.api.espn.com/apis/site/v2/sports/soccer"
SLUGS = {
    "uefa.nations": "Nations League", "fifa.friendly": "Friendly", "concacaf.nations.league": "CONCACAF Nations League",
    "fifa.worldq.uefa": "World Cup qualifier", "fifa.worldq.conmebol": "World Cup qualifier",
    "fifa.worldq.caf": "World Cup qualifier", "fifa.worldq.afc": "World Cup qualifier",
    "fifa.worldq.concacaf": "World Cup qualifier", "uefa.euroq": "Euro qualifier",
    "fifa.world": "World Cup", "uefa.euro": "Euro Championship", "conmebol.america": "Copa América",
    "caf.nations": "Africa Cup of Nations", "afc.asian.cup": "Asian Cup", "concacaf.gold": "Gold Cup",
}
# ESPN name -> name used by the results dataset
ALIASES = {"Bosnia-Herzegovina": "Bosnia and Herzegovina", "Chinese Taipei": "Taiwan", "Czechia": "Czech Republic",
           "Kyrgyz Republic": "Kyrgyzstan", "Türkiye": "Turkey", "St. Kitts and Nevis": "Saint Kitts and Nevis",
           "St. Lucia": "Saint Lucia", "St. Martin": "Saint Martin", "St. Vincent and the Grenadines":
           "Saint Vincent and the Grenadines", "USA": "United States", "Korea Republic": "South Korea",
           "Korea DPR": "North Korea", "Côte d'Ivoire": "Ivory Coast", "Cape Verde Islands": "Cape Verde",
           "Congo DR": "DR Congo", "IR Iran": "Iran", "Curacao": "Curaçao"}
HISTORY_DAYS = 3 * 365
DAYS_AHEAD = 14
HALF_LIFE_DAYS = 400       # clubs use 150; national teams play far less often
ID_OFFSET = 10 ** 10       # keeps these ids apart from football-data.org's


def get(url, raw=False):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "gzip"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return body.decode() if raw else json.loads(body)
        except (urllib.error.URLError, OSError, ValueError) as e:
            if attempt == 2:
                print("Could not load", url.split("?")[0], "-", e)
                return None
            time.sleep(3)


def canon(name):
    return ALIASES.get(name, name)


def american_to_decimal(ml):
    try:
        ml = float(ml)
    except (TypeError, ValueError):
        return None
    if ml == 0:
        return None
    return round(1 + ml / 100 if ml > 0 else 1 + 100 / abs(ml), 2)


def espn_odds(comp):
    """Bookmaker odds from ESPN's scoreboard, in decimal, keyed by the app's markets.
    Layout: moneyline.{home,away,draw}.close.odds and total.over.close.{line,odds} (American odds)."""
    o = (comp.get("odds") or [None])[0]
    if not o:
        return None
    price = lambda node: american_to_decimal(((node or {}).get("close") or (node or {}).get("open") or {}).get("odds"))
    ml = o.get("moneyline") or {}
    out = {"book": (o.get("provider") or {}).get("name") or "Bookmaker",
           "1": price(ml.get("home")) or american_to_decimal((o.get("homeTeamOdds") or {}).get("moneyLine")),
           "2": price(ml.get("away")) or american_to_decimal((o.get("awayTeamOdds") or {}).get("moneyLine")),
           "X": price(ml.get("draw")) or american_to_decimal((o.get("drawOdds") or {}).get("moneyLine"))}
    over = (o.get("total") or {}).get("over") or {}
    line = str(((over.get("close") or over.get("open") or {}).get("line")) or "").lstrip("o")
    key = {"1.5": "O15", "2.5": "O25", "3.5": "O35"}.get(line)
    if key:
        out[key] = price(over)
    elif o.get("overUnder") in (1.5, 2.5, 3.5):
        out[{1.5: "O15", 2.5: "O25", 3.5: "O35"}[o["overUnder"]]] = american_to_decimal(o.get("overOdds"))
    out = {k: v for k, v in out.items() if v}
    return out if len(out) > 1 else None


def espn_day(slug, day):
    sb = get(f"{ESPN}/{slug}/scoreboard?dates={day.strftime('%Y%m%d')}")
    time.sleep(0.25)
    return (sb or {}).get("events") or []


def parse_event(slug, e):
    c = e["competitions"][0]
    side = {x.get("homeAway"): x for x in c["competitors"]}
    if "home" not in side or "away" not in side:
        return None
    st = e["status"]["type"]
    score = lambda x: int(x["score"]) if str(x.get("score", "")).isdigit() else None
    return {"espn": e["id"], "slug": slug, "comp": SLUGS[slug], "date": e["date"],
            "state": st.get("state"), "completed": bool(st.get("completed")),
            "neutral": bool(c.get("neutralSite")),
            "home": {"name": canon(side["home"]["team"]["displayName"]), "logo": side["home"]["team"].get("logo")},
            "away": {"name": canon(side["away"]["team"]["displayName"]), "logo": side["away"]["team"].get("logo")},
            "ft": [score(side["home"]), score(side["away"])], "odds": espn_odds(c)}


def main():
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=HISTORY_DAYS)).date().isoformat()
    cache = predict.load_json(CACHE_PATH, {})
    if "games" in cache:                       # old API-Football cache: start fresh
        cache = {}
    recent = cache.get("recent", {})           # ESPN results newer than the dataset

    # ---- history: the open results dataset
    text = get(RESULTS_CSV, raw=True)
    if not text:
        print("Results dataset unavailable; skipping internationals this run.")
        return
    games, logos, latest = [], cache.get("logos", {}), ""
    for r in csv.DictReader(io.StringIO(text)):
        if r["date"] < cutoff or not r["home_score"].isdigit() or not r["away_score"].isdigit():
            continue
        latest = max(latest, r["date"])
        games.append({"home": r["home_team"], "away": r["away_team"],
                      "ft": (int(r["home_score"]), int(r["away_score"])),
                      "date": predict.parse_time(r["date"] + "T18:00:00Z"),
                      "key": f"{r['date']}|{r['home_team']}|{r['away_team']}"})

    # ---- ESPN: results since the dataset's last game, and the next 14 days
    start = max(predict.parse_time((latest or now.date().isoformat()) + "T00:00:00Z"), now - timedelta(days=60))
    days = [start + timedelta(days=i) for i in range((now - start).days + DAYS_AHEAD + 1)]
    # every few days check all competitions; in between only the ones with games
    check_all = not cache.get("slugsAt") or now - predict.parse_time(cache["slugsAt"]) > timedelta(days=3)
    slugs = list(SLUGS) if check_all else cache.get("activeSlugs") or list(SLUGS)
    upcoming, seen = [], set()
    jobs = [(slug, day) for slug in slugs for day in days]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda j: (j[0], espn_day(*j)), jobs))
    for slug, events in results:
        for e in events:
            if True:
                ev = parse_event(slug, e)
                if not ev:
                    continue
                seen.add(slug)
                for s in ("home", "away"):
                    if ev[s]["logo"]:
                        logos[ev[s]["name"]] = ev[s]["logo"]
                if ev["completed"] and None not in ev["ft"]:
                    recent[ev["espn"]] = ev
                elif ev["state"] == "pre":
                    upcoming.append(ev)
    if check_all:
        cache["activeSlugs"], cache["slugsAt"] = sorted(seen), now.isoformat()
    have = {g["key"] for g in games}
    for ev in recent.values():
        d = ev["date"][:10]
        if d >= cutoff and f"{d}|{ev['home']['name']}|{ev['away']['name']}" not in have:
            games.append({"home": ev["home"]["name"], "away": ev["away"]["name"], "ft": tuple(ev["ft"]),
                          "date": predict.parse_time(ev["date"]), "key": ev["espn"]})
    cache["recent"] = {k: v for k, v in recent.items() if v["date"][:10] >= (latest or "0")
                       or now - predict.parse_time(v["date"]) < timedelta(days=10)}
    cache["logos"] = logos
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, separators=(",", ":"))

    # ---- rate national teams (no half-time scores in the dataset, so halves use averages)
    games.sort(key=lambda g: g["date"])
    for g in games:
        g["ht"] = None
    teams = {g["home"] for g in games} | {g["away"] for g in games}
    teams |= {ev[s]["name"] for ev in upcoming for s in ("home", "away")}
    predict.HALF_LIFE_DAYS = HALF_LIFE_DAYS
    centre = {t: (1.0, 1.0) for t in teams}
    models = predict.fit_all(games, teams, centre, now, iters=80)

    by_team, pairs = defaultdict(list), defaultdict(list)
    for g in games:
        by_team[g["home"]].append(g)
        by_team[g["away"]].append(g)
        pairs[frozenset((g["home"], g["away"]))].append(g)
    form = lambda t: [predict.result_letter(g, t) for g in by_team[t][-5:]][::-1]

    preds = predict.load_json(os.path.join(OUT_DIR, "predictions.json"), {"matches": [], "leagues": {}})
    history = predict.load_json(os.path.join(OUT_DIR, "history.json"), {"matches": {}})
    preds["matches"] = [m for m in preds.get("matches", []) if m.get("league") != "INT"]
    counts = predict.recent_counts(games, now)
    added = 0
    for ev in upcoming:
        when = predict.parse_time(ev["date"])
        if not (now < when <= now + timedelta(days=DAYS_AHEAD)):
            continue
        h, a = ev["home"]["name"], ev["away"]["name"]
        p = predict.predict(h, a, models, None)
        h2h = sorted(pairs[frozenset((h, a))], key=lambda x: x["date"], reverse=True)[:5]
        mid = ID_OFFSET + int(ev["espn"])
        p.update({
            "id": mid, "league": "INT", "compName": ev["comp"], "espn": ev["espn"], "espnSlug": ev["slug"],
            "neutral": ev["neutral"], "date": ev["date"], "matchday": None,
            "home": {"name": h, "crest": ev["home"]["logo"] or logos.get(h), "form": form(h)},
            "away": {"name": a, "crest": ev["away"]["logo"] or logos.get(a), "form": form(a)},
            "h2h": [{"date": x["date"].date().isoformat(), "league": "INT", "home": x["home"], "away": x["away"],
                     "score": list(x["ft"])} for x in h2h],
        })
        if ev["odds"]:
            p["odds"] = ev["odds"]
        p["n"] = [counts[h], counts[a]]
        p["enough"] = min(counts[h], counts[a]) >= predict.MIN_GAMES
        preds["matches"].append(p)
        if p["enough"]:      # no prediction is made (or graded) without enough data
            history["matches"][str(mid)] = predict.record("INT", ev["date"], h, a, p)
        else:
            history["matches"].pop(str(mid), None)
        added += 1
    preds["matches"].sort(key=lambda m: m["date"])
    preds.setdefault("leagues", {})["INT"] = {"name": "Internationals", "emblem": None}

    # ---- grade finished international picks
    graded = 0
    for k, rec in list(history["matches"].items()):
        if rec.get("league") != "INT" or rec.get("result") is not None:
            continue
        ev = recent.get(str(int(k) - ID_OFFSET))
        if ev and ev["completed"]:
            rec["result"] = {"ft": ev["ft"], "ht": [None, None], "corners": None}
            graded += 1
        elif predict.parse_time(rec["date"]) < now - timedelta(days=7):
            del history["matches"][k]          # never played (postponed or cancelled)

    with open(os.path.join(OUT_DIR, "predictions.json"), "w") as f:
        json.dump(preds, f, separators=(",", ":"))
    with open(os.path.join(OUT_DIR, "history.json"), "w") as f:
        json.dump(history, f, separators=(",", ":"))
    print(f"Internationals: {len(games)} results ({len(cache['recent'])} recent from ESPN), {len(teams)} teams, "
          f"{added} upcoming games predicted, {graded} graded.")


if __name__ == "__main__":
    sys.exit(main())
