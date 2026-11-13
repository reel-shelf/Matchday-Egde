#!/usr/bin/env python3
"""Leak test for predict.py's back-test. Run: python3 tests/leak_test.py (offline, ~1 minute).

Runs predict.main() twice on the same synthetic league:
  run A: the real data
  run B: every result from CUTOFF on is scrambled, and the league table is replaced with nonsense
A back-test prediction for a game before CUTOFF must be identical in both runs; if it changes,
it was using information from after the game (or from the table).
"""
import json, os, random, sys, copy, shutil
from datetime import datetime, timedelta, timezone

import tempfile
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
work = tempfile.mkdtemp(prefix="leak-test-")
sys.path.insert(0, root)
import predict

CUTOFF = "2026-10-01"
now = datetime.now(timezone.utc)
random.seed(7)
teams = [{"id": 100 + i, "name": f"Team {chr(65 + i)}", "shortName": f"T{i}", "crest": None} for i in range(20)]
power = {t["id"]: random.uniform(0.6, 1.8) for t in teams}


def season(start_year, mid):
    out, d0 = [], datetime(start_year, 8, 15, 15, tzinfo=timezone.utc)
    ids = [t["id"] for t in teams]
    for rnd in range(38):
        random.shuffle(ids)
        when = d0 + timedelta(days=7 * rnd)
        for k in range(10):
            h, a = ids[2 * k], ids[2 * k + 1]
            done = when < now
            hg = min(9, int(random.expovariate(1 / (1.4 * power[h] / power[a]))))
            ag = min(9, int(random.expovariate(1 / (1.1 * power[a] / power[h]))))
            mid[0] += 1
            out.append({"id": mid[0], "utcDate": when.isoformat().replace("+00:00", "Z"),
                        "status": "FINISHED" if done else "TIMED", "matchday": rnd + 1, "stage": "REGULAR_SEASON",
                        "season": {"startDate": f"{start_year}-08-15"},
                        "homeTeam": next(t for t in teams if t["id"] == h), "awayTeam": next(t for t in teams if t["id"] == a),
                        "score": {"duration": "REGULAR",
                                  "fullTime": {"home": hg if done else None, "away": ag if done else None},
                                  "halfTime": {"home": hg // 2 if done else None, "away": ag // 2 if done else None}}})
    return out


mid = [1000]
DATA = {2026: season(2026, mid), 2025: season(2025, mid), 2024: season(2024, mid)}
TABLE = {"standings": [{"type": "TOTAL", "table": [
    {"position": i + 1, "team": t, "playedGames": 12, "won": 6, "draw": 3, "lost": 3, "goalDifference": 5, "points": 21}
    for i, t in enumerate(teams)]}]}


def csv_rows(data, url):
    """football-data.co.uk style rows (with Bet365 odds and corners) for the Premier League file."""
    if "/E0.csv" not in url:
        return []
    yy = int(url.split("/mmz4281/")[1][:2])
    rows = []
    for m in data.get(2000 + yy, []):
        if m["status"] != "FINISHED":
            continue
        d = datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00"))
        rows.append({"Date": d.strftime("%d/%m/%Y"), "HomeTeam": m["homeTeam"]["name"], "AwayTeam": m["awayTeam"]["name"],
                     "FTHG": str(m["score"]["fullTime"]["home"]), "FTAG": str(m["score"]["fullTime"]["away"]),
                     "HC": "5", "AC": "4", "B365H": "2.1", "B365D": "3.4", "B365A": "3.6", "B365>2.5": "1.9", "B365<2.5": "1.9"})
    return rows


def run(data, table, out):
    os.makedirs(out, exist_ok=True)
    def fake_fetch(path):
        if not path.startswith("/competitions/PL/"):
            return None
        if path.endswith("/standings"):
            return table
        year = int(path.split("season=")[1]) if "season=" in path else 2026
        return {"competition": {"emblem": None}, "matches": data.get(year, [])}
    predict.fetch = fake_fetch
    predict.fetch_csv = lambda url: csv_rows(data, url)
    predict.CALIB_MIN_GAMES = 40     # so the goals calibration switches on in this small league
    predict.OUT_DIR = out
    predict.main()
    return json.load(open(os.path.join(out, "backtest.json")))["rows"]


a = run(DATA, TABLE, os.path.join(work, "A"))

scrambled = copy.deepcopy(DATA)
changed = 0
for m in scrambled[2026]:
    if m["utcDate"] >= CUTOFF and m["status"] == "FINISHED":
        m["score"]["fullTime"] = {"home": random.randint(0, 6), "away": random.randint(0, 6)}
        m["score"]["halfTime"] = {"home": 0, "away": 0}
        changed += 1
bad_table = copy.deepcopy(TABLE)
random.shuffle(bad_table["standings"][0]["table"])
for i, r in enumerate(bad_table["standings"][0]["table"]):
    r.update(position=i + 1, points=random.randint(0, 40), goalDifference=random.randint(-20, 20))
b = run(scrambled, bad_table, os.path.join(work, "B"))

key = lambda r: (r["date"], r["home"], r["away"])
A, B = {key(r): r for r in a}, {key(r): r for r in b}
before = [k for k in A if k[0] < CUTOFF]
after = [k for k in A if k[0] >= CUTOFF]
same_before = sum(1 for k in before if {**A[k], "result": None} == {**B[k], "result": None})
diff_after = sum(1 for k in after if A[k]["probs"] != B[k]["probs"])
print(f"\nscrambled {changed} results from {CUTOFF} on, and the whole league table")
print(f"games before {CUTOFF}: {same_before}/{len(before)} back-test predictions unchanged")
print(f"games from {CUTOFF} on: {diff_after}/{len(after)} predictions changed (expected: their own earlier results changed)")
cal = sum(1 for r in a if r.get("raw") and r["raw"]["over25"] != r["over25"])
print(f"(goals calibration active on {cal} back-test games; Bet365 odds on {sum(1 for r in a if r.get('book'))})")
print("LEAK" if same_before != len(before) else "NO LEAK: predictions only use results from before each game")
sys.exit(1 if same_before != len(before) else 0)
