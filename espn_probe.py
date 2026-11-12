#!/usr/bin/env python3
"""One-off check of what ESPN's free (unofficial) soccer feed covers. Prints a summary."""
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
SLUGS = ["uefa.nations", "fifa.friendly", "fifa.worldq.uefa", "fifa.worldq.conmebol", "fifa.worldq.caf",
         "fifa.worldq.afc", "fifa.worldq.concacaf", "uefa.euroq", "concacaf.nations.league", "eng.1", "ned.1"]


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"_error": f"{e.code}: {e.read()[:300]!r}"}
    except Exception as e:  # noqa: BLE001 - this is a probe
        return {"_error": str(e)}


# Which request formats work at all?
_now = datetime.now(timezone.utc)
for url in [f"{BASE}/eng.1/scoreboard",
            f"{BASE}/eng.1/scoreboard?dates={_now.strftime('%Y%m%d')}",
            f"{BASE}/eng.1/scoreboard?dates={_now.strftime('%Y%m%d')}-{(_now + timedelta(days=14)).strftime('%Y%m%d')}",
            f"{BASE}/eng.1/scoreboard?dates=20240101-20240131&limit=100",
            f"{BASE}/uefa.nations/scoreboard",
            f"{BASE}/fifa.friendly/scoreboard",
            "https://site.api.espn.com/apis/site/v2/sports/soccer/all/scoreboard",
            "https://site.web.api.espn.com/apis/site/v2/sports/soccer/uefa.nations/scoreboard",
            "https://site.api.espn.com/apis/v2/sports/soccer/eng.1/standings"]:
    d = get(url)
    print("FORMAT", url.split("soccer/")[-1], "->", d.get("_error") or f"OK, {len(d.get('events') or [])} events, keys {sorted(d)[:8]}")


def keys(d, depth=0):
    return sorted(d.keys()) if isinstance(d, dict) else type(d).__name__


now = datetime.now(timezone.utc)
fmt = lambda d: d.strftime("%Y%m%d")
out = {}
for slug in SLUGS:
    res = {}
    up = get(f"{BASE}/{slug}/scoreboard?dates={fmt(now - timedelta(days=2))}-{fmt(now + timedelta(days=14))}&limit=500")
    if "_error" in up:
        res["error"] = up["_error"]; out[slug] = res; continue
    evs = up.get("events") or []
    res["events_next14d"] = len(evs)
    res["statuses"] = sorted({e["status"]["type"]["name"] for e in evs})
    if evs:
        e = evs[0]
        c = e["competitions"][0]
        res["sample"] = {"id": e["id"], "date": e["date"], "name": e.get("name"),
                         "competitor_keys": keys(c["competitors"][0]), "team_keys": keys(c["competitors"][0].get("team")),
                         "has_odds": bool(c.get("odds")), "odds": c.get("odds", [None])[0] if c.get("odds") else None,
                         "neutral": c.get("neutralSite"), "score": [x.get("score") for x in c["competitors"]]}
        # one finished and one upcoming summary
        for label, ev in (("finished", next((x for x in evs if x["status"]["type"]["completed"]), None)),
                          ("upcoming", next((x for x in evs if x["status"]["type"]["state"] == "pre"), None))):
            if not ev:
                continue
            sm = get(f"{BASE}/{slug}/summary?event={ev['id']}")
            info = {"keys": keys(sm)}
            ros = sm.get("rosters") or []
            if ros:
                r0 = ros[0]
                info["roster_keys"] = keys(r0)
                pl = r0.get("roster") or []
                info["roster_size"] = len(pl)
                info["starters"] = sum(1 for p in pl if p.get("starter"))
                info["player_sample"] = {k: pl[0].get(k) for k in ("starter", "jersey", "position", "subbedIn")} if pl else None
                info["player_name"] = (pl[0].get("athlete") or {}).get("displayName") if pl else None
            for k in ("injuries", "odds", "pickcenter", "headToHeadGames", "keyEvents"):
                if k in sm:
                    v = sm[k]
                    info[k] = (len(v) if isinstance(v, list) else keys(v))
            if sm.get("pickcenter"):
                info["pickcenter_sample"] = sm["pickcenter"][0]
            if sm.get("injuries"):
                info["injuries_sample"] = sm["injuries"][0] if isinstance(sm["injuries"], list) else None
            res[label] = info
    # history: one year in one call?
    hist = get(f"{BASE}/{slug}/scoreboard?dates=20240101-20241231&limit=1000")
    res["events_2024"] = len(hist.get("events") or []) if "_error" not in hist else hist["_error"]
    out[slug] = res
    time.sleep(1)
print(json.dumps(out, indent=1, default=str)[:60000])
