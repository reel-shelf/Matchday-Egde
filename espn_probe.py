#!/usr/bin/env python3
"""One-off check of ESPN's free soccer feed for internationals, plus the open international results dataset."""
import csv
import gzip
import io
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
SLUGS = ["uefa.nations", "fifa.friendly", "fifa.worldq.uefa", "fifa.worldq.conmebol", "fifa.worldq.caf",
         "fifa.worldq.afc", "fifa.worldq.concacaf", "uefa.euroq", "concacaf.nations.league"]
RESULTS = "https://raw.githubusercontent.com/martj42/international_results/master/results.csv"


def get(url, raw=False):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            return body.decode() if raw else json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            body = gzip.decompress(body)
        except OSError:
            pass
        return {"_error": f"{e.code}: {body[:200]!r}"}
    except Exception as e:  # noqa: BLE001
        return {"_error": str(e)}


now = datetime.now(timezone.utc)
espn_teams, upcoming, finished = set(), [], []
for slug in SLUGS:
    count = 0
    for d in range(-3, 15):
        day = (now + timedelta(days=d)).strftime("%Y%m%d")
        sb = get(f"{BASE}/{slug}/scoreboard?dates={day}")
        for e in sb.get("events") or []:
            count += 1
            c = e["competitions"][0]
            names = [x["team"]["displayName"] for x in c["competitors"]]
            espn_teams.update(names)
            st = e["status"]["type"]
            (finished if st["completed"] else upcoming).append((slug, e["id"], e["date"], names, bool(c.get("odds"))))
        time.sleep(0.2)
    print(f"{slug}: {count} games from 3 days ago to 14 days ahead")
print(f"\nupcoming: {len(upcoming)}, finished: {len(finished)}, with odds on scoreboard: {sum(1 for u in upcoming if u[4])}")
for u in upcoming[:6]:
    print("  ", u)
if upcoming:
    c0 = get(f"{BASE}/{upcoming[0][0]}/scoreboard?dates={upcoming[0][2][:10].replace('-', '')}")
    ev = next(e for e in c0["events"] if e["id"] == upcoming[0][1])
    comp = ev["competitions"][0]
    print("\nscoreboard competition keys:", sorted(comp))
    print("competitor keys:", sorted(comp["competitors"][0]), "| homeAway:", comp["competitors"][0].get("homeAway"),
          "| neutralSite:", comp.get("neutralSite"))
    if comp.get("odds"):
        print("odds sample:", json.dumps(comp["odds"][0])[:600])
for label, lst in (("UPCOMING", upcoming), ("FINISHED", finished)):
    if not lst:
        continue
    slug, eid = lst[0][0], lst[0][1]
    sm = get(f"{BASE}/{slug}/summary?event={eid}")
    print(f"\n{label} summary keys:", sorted(sm) if isinstance(sm, dict) else sm)
    for r in sm.get("rosters") or []:
        pl = r.get("roster") or []
        print(f"  roster {r.get('team', {}).get('displayName')}: {len(pl)} players, {sum(1 for p in pl if p.get('starter'))} starters,"
              f" sample {[(p.get('athlete') or {}).get('displayName') for p in pl[:3]]}")
    for k in ("pickcenter", "odds", "injuries", "gameInfo", "keyEvents"):
        if sm.get(k):
            print(f"  {k}:", json.dumps(sm[k])[:500])
    # lineups for finished games come from rosters; check a few more finished games for rosters
if finished:
    have = 0
    for slug, eid, *_ in finished[:8]:
        sm = get(f"{BASE}/{slug}/summary?event={eid}")
        have += any(any(p.get("starter") for p in (r.get("roster") or [])) for r in sm.get("rosters") or [])
    print(f"\nfinished games with starting lineups: {have}/{min(8, len(finished))}")

csv_text = get(RESULTS, raw=True)
if isinstance(csv_text, dict):
    print("\nresults dataset:", csv_text)
else:
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    print(f"\nresults dataset: {len(rows)} games, columns {list(rows[0])}, latest {rows[-1]}")
    recent = [r for r in rows if r["date"] >= "2023-01-01"]
    names = {r["home_team"] for r in recent} | {r["away_team"] for r in recent}
    print(f"since 2023: {len(recent)} games, {len(names)} teams")
    missing = sorted(t for t in espn_teams if t not in names)
    print(f"ESPN team names not in the dataset ({len(missing)} of {len(espn_teams)}):", missing)
