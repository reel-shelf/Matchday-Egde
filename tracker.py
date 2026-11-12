#!/usr/bin/env python3
"""
Matchday Edge: parlay and team-news track record.

Every day at 12:00 UTC this locks in the parlays the app is showing (the four
tiers plus the Safe, Bold and Long Shot parlays, with team news applied) and saves them to
parlay-history.json. As games finish, each leg is graded from the real scores.

It also records, just before kick-off, each game's chances with and without the
team-news adjustment (news-eval.json), so the app can show whether team news
makes the picks more accurate.

No API calls: uses predictions.json and history.json (OUT_DIR) and the live data
in LIVE_DIR (team-news.json, odds.json, live.json).
"""
import json
import os
from datetime import datetime, timedelta, timezone

import parlay_logic as pl

OUT_DIR = os.environ.get("OUT_DIR", "data")
LIVE_DIR = os.environ.get("LIVE_DIR", OUT_DIR)
LOCK_HOUR = 12           # UTC
KEEP_DAYS = 120


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_if_changed(path, data):
    if load_json(path, None) == data:
        return False
    with open(path, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    return True


def parse(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def results(history, live):
    """match id -> final score (regular time)."""
    out = {k: v["result"]["ft"] for k, v in history.get("matches", {}).items()
           if v.get("result") and v["result"].get("ft")}
    for k, v in live.get("matches", {}).items():
        if v.get("status") in ("FINISHED", "AWARDED") and k not in out:
            out[k] = v["score"]
    return out


def book_price(odds, leg):
    o = (odds.get(str(leg["id"])) or {}).get(leg["market"])
    return o if isinstance(o, (int, float)) and o > 1 else None


def make_parlay(kind, name, legs, odds, target=None):
    priced = [book_price(odds, l) for l in legs]
    book = None
    if legs and all(priced):
        book = 1.0
        for p in priced:
            book *= p
    return {"kind": kind, "name": name, "target": target,
            "legs": [{**l, "book": b, "result": None} for l, b in zip(legs, priced)],
            "fair": round(pl.fair_odds(legs), 2), "book": round(book, 2) if book else None,
            "status": "pending"}


def lock_today(now, preds, news, odds):
    odds = {**{str(m["id"]): m["odds"] for m in preds.get("matches", []) if m.get("odds")}, **odds}
    soon = [pl.blend(pl.apply_news(m, news), odds.get(str(m["id"])))
            for m in preds.get("matches", [])
            if pl.enough(m) and now < parse(m["date"]) < now + timedelta(days=15)]
    if len(soon) < 3:
        return None
    parlays = []
    for t in pl.build_tiers(soon):
        parlays.append(make_parlay("tier", f"{t['target']:,} odds tier", t["legs"], odds, t["target"]))
    risk = pl.build_risk(soon)
    parlays.append(make_parlay("safe", "Safe parlay", risk["safe"], odds))
    parlays.append(make_parlay("bold", "Bold parlay", risk["bold"], odds))
    parlays.append(make_parlay("long", "Long Shot parlay", risk["long"], odds))
    return {"locked": now.isoformat().replace("+00:00", "Z"), "parlays": parlays}


def grade(day, res, now):
    for p in day["parlays"]:
        if p["status"] != "pending":
            continue
        for l in p["legs"]:
            if l["result"] is not None:
                continue
            ft = res.get(str(l["id"]))
            if ft:
                l["result"] = pl.leg_result(l["market"], ft)
            elif now - parse(l["date"]) > timedelta(days=3):
                l["result"] = "void"          # postponed or cancelled
        rs = [l["result"] for l in p["legs"]]
        if any(r is False for r in rs):
            p["status"] = "lost"
        elif all(r is not None for r in rs) and any(r is True for r in rs):
            p["status"] = "won"
        elif rs and all(r == "void" for r in rs):
            p["status"] = "void"


def main():
    now = datetime.now(timezone.utc)
    preds = load_json(os.path.join(OUT_DIR, "predictions.json"), {"matches": []})
    history = load_json(os.path.join(OUT_DIR, "history.json"), {"matches": {}})
    news = pl.merge_news(load_json(os.path.join(LIVE_DIR, "team-news.json"), {}).get("matches", {}),
                         load_json(os.path.join(OUT_DIR, "manual-news.json"), {}).get("matches", {}))
    odds = load_json(os.path.join(LIVE_DIR, "odds.json"), {}).get("matches", {})
    live = load_json(os.path.join(LIVE_DIR, "live.json"), {"matches": {}})
    res = results(history, live)
    os.makedirs(LIVE_DIR, exist_ok=True)

    # ---- parlays
    path = os.path.join(LIVE_DIR, "parlay-history.json")
    ph = load_json(path, {"days": {}})
    today = now.strftime("%Y-%m-%d")
    if today not in ph["days"] and now.hour >= LOCK_HOUR:
        locked = lock_today(now, preds, news, odds)
        if locked:
            ph["days"][today] = locked
            print(f"Locked today's parlays ({len(locked['parlays'])}).")
    for d, day in ph["days"].items():
        grade(day, res, now)
    cutoff = (now - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    ph["days"] = {d: v for d, v in sorted(ph["days"].items()) if d >= cutoff}
    save_if_changed(path, ph)

    # ---- team news: chances with and without the adjustment, just before kick-off
    path = os.path.join(LIVE_DIR, "news-eval.json")
    ev = load_json(path, {"matches": {}})
    for m in preds.get("matches", []):
        k = str(m["id"])
        to_kick = parse(m["date"]) - now
        if k in ev["matches"] or k not in news or not (timedelta(0) < to_kick <= timedelta(minutes=10)):
            continue
        adj = pl.apply_news(m, news)
        if "ftBase" not in adj:
            continue
        f, b = adj["ft"], adj["ftBase"]
        ev["matches"][k] = {"kickoff": m["date"], "league": m["league"], "home": m["home"]["name"],
                            "away": m["away"]["name"], "base": [b["home"], b["draw"], b["away"]],
                            "adj": [f["home"], f["draw"], f["away"]], "result": None}
    for k, e in ev["matches"].items():
        if e["result"] is None and k in res:
            e["result"] = res[k]
    cutoff = now - timedelta(days=KEEP_DAYS)
    ev["matches"] = {k: v for k, v in ev["matches"].items() if parse(v["kickoff"]) > cutoff}
    save_if_changed(path, ev)


if __name__ == "__main__":
    main()
