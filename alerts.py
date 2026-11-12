#!/usr/bin/env python3
"""
Matchday Edge: phone alerts through ntfy (https://ntfy.sh, free, no account).

Subscribe once on your phone: open https://ntfy.sh/<topic> (or the ntfy app)
and allow notifications. Alerts for games in today's locked parlays:
  - lineups confirmed (with any key players missing)
  - a key player ruled out or not starting
  - full-time score and whether the leg won
  - a parlay won or lost

Only alerts that haven't been sent before go out (alerts-state.json). The first
run marks everything already there as sent, so you don't get a flood.
"""
import json
import os
import urllib.request

from parlay_logic import leg_result

OUT_DIR = os.environ.get("OUT_DIR", "data")
LIVE_DIR = os.environ.get("LIVE_DIR", OUT_DIR)
TOPIC = os.environ.get("NTFY_TOPIC") or "matchday-edge-tugoa54ey5"
SERVER = "https://ntfy.sh"
STATE_PATH = os.path.join(LIVE_DIR, "alerts-state.json")


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def send(title, message, tags):
    body = json.dumps({"topic": TOPIC, "title": title, "message": message, "tags": tags}).encode()
    req = urllib.request.Request(SERVER, data=body, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except OSError as e:
        print("Alert not sent:", e)
        return False


def events():
    """(key, title, message, tags) for everything worth an alert right now."""
    ph = load_json(os.path.join(LIVE_DIR, "parlay-history.json"), {"days": {}})
    news = load_json(os.path.join(LIVE_DIR, "team-news.json"), {}).get("matches", {})
    live = load_json(os.path.join(LIVE_DIR, "live.json"), {}).get("matches", {})
    out, games = [], {}
    for day, d in ph["days"].items():
        for p in d["parlays"]:
            if p["status"] != "pending":
                key = f"parlay:{day}:{p['kind']}:{p.get('target')}"
                won = p["status"] == "won"
                if p["status"] in ("won", "lost"):
                    odds = f"{p['book']:.2f} at Bet365" if p.get("book") else f"fair odds {p['fair']:.2f}"
                    out.append((key, f"{p['name']} {'WON' if won else 'lost'}",
                                f"Locked {day}, {len(p['legs'])} picks, {odds}.",
                                ["white_check_mark"] if won else ["x"]))
                continue
            for l in p["legs"]:
                games.setdefault(str(l["id"]), l)
    for k, l in games.items():
        game = f"{l['home']} v {l['away']}"
        n = news.get(k)
        if n:
            key_out = [(p["name"], t, p.get("reason") or "out") for t, side in (("home", n["home"]), ("away", n["away"]))
                       for p in side["out"] if p.get("key") and p.get("status") == "out"]
            key_out += [(p["name"], t, "not starting") for t, side in (("home", n["home"]), ("away", n["away"]))
                        for p in side["benched"]]
            if n.get("lineups"):
                extra = "; ".join(f"{name} {why}" for name, _, why in key_out)
                out.append((f"lineups:{k}", f"Lineups out: {game}",
                            f"In your parlays: {l['label']}." + (f" Key players missing: {extra}." if extra else ""),
                            ["clipboard"]))
            for name, t, why in key_out:
                team = l["home"] if t == "home" else l["away"]
                out.append((f"keyout:{k}:{name}", f"{name} ({team}): {why}",
                            f"{game} is in your parlays ({l['label']}). Odds have been adjusted.", ["warning"]))
        s = live.get(k)
        if s and s.get("status") in ("FINISHED", "AWARDED"):
            won = leg_result(l["market"], s["score"])
            out.append((f"ft:{k}", f"FT: {l['home']} {s['score'][0]}-{s['score'][1]} {l['away']}",
                        f"Parlay pick {l['label']}: {'won' if won else 'lost'}.",
                        ["white_check_mark" if won else "x"]))
    return out


def main():
    state = load_json(STATE_PATH, None)
    evs = events()
    first = state is None
    state = state or {"sent": []}
    sent = set(state["sent"])
    for key, title, msg, tags in evs:
        if key in sent:
            continue
        if first or send(title, msg, tags):
            sent.add(key)
    # forget keys that no longer matter (keep the list small)
    current = {e[0] for e in evs}
    state["sent"] = sorted(k for k in sent if k in current or k.startswith("parlay:"))[-2000:]
    os.makedirs(LIVE_DIR, exist_ok=True)
    if load_json(STATE_PATH, None) != state:
        with open(STATE_PATH, "w") as f:
            json.dump(state, f, separators=(",", ":"))


if __name__ == "__main__":
    main()
