#!/usr/bin/env python3
"""
Matchday Edge: how good is the model, honestly?

Reads data/backtest.json (this season's games, each predicted only from earlier games)
and writes data/evaluation.json for the Track record tab:

- results: how often the most likely result happened, against simple baselines and
  against Bet365's favourite;
- probability quality (Brier score): our chances vs Bet365's (margin removed) and vs
  "the same average chances for every game";
- goals markets before and after calibration;
- a betting simulation of the app's value-bet rule at Bet365's pre-match odds:
  1 unit on every pick where our chance is 35%+ and Bet365 pays 5-30% more than fair.

Odds come from football-data.co.uk's free results files (domestic leagues only).
"""
import json
import math
import os
from collections import defaultdict

OUT_DIR = os.environ.get("OUT_DIR", "data")
VALUE_MIN, CHECK_EDGE, MIN_CHANCE = 0.05, 0.30, 35


def outcome(ft):
    return 0 if ft[0] > ft[1] else 1 if ft[0] == ft[1] else 2


def brier(ps, o):
    return sum((p - (i == o)) ** 2 for i, p in enumerate(ps))


def brier2(p, y):
    return (p - y) ** 2


def pct(x):
    return round(100 * x, 1)


def main():
    rows = [r for r in json.load(open(os.path.join(OUT_DIR, "backtest.json")))["rows"] if r.get("result")]
    n = len(rows)
    if not n:
        return
    ev = {"games": n}

    # ---- results
    outs = [outcome(r["result"]["ft"]) for r in rows]
    right = sum(r["probs"].index(max(r["probs"])) == o for r, o in zip(rows, outs))
    ev["result"] = {"model": pct(right / n), "alwaysHome": pct(sum(o == 0 for o in outs) / n)}

    base = [sum(o == k for o in outs) / n for k in range(3)]
    ev["brier"] = {"model": round(sum(brier([p / 100 for p in r["probs"]], o) for r, o in zip(rows, outs)) / n, 4),
                   "average": round(sum(brier(base, o) for o in outs) / n, 4)}

    # ---- against Bet365 (games with odds)
    with_odds = [(r, o) for r, o in zip(rows, outs) if r.get("book")]
    if with_odds:
        m = len(with_odds)
        imp = []
        for r, _ in with_odds:
            b = r["book"]
            s = 1 / b["1"] + 1 / b["X"] + 1 / b["2"]
            imp.append([1 / b["1"] / s, 1 / b["X"] / s, 1 / b["2"] / s])
        book_right = sum(ps.index(max(ps)) == o for ps, (_, o) in zip(imp, with_odds))
        model_right = sum(r["probs"].index(max(r["probs"])) == o for r, o in with_odds)
        ev["vsBook"] = {
            "games": m,
            "modelRight": pct(model_right / m), "bookRight": pct(book_right / m),
            "modelBrier": round(sum(brier([p / 100 for p in r["probs"]], o) for r, o in with_odds) / m, 4),
            "bookBrier": round(sum(brier(ps, o) for ps, (_, o) in zip(imp, with_odds)) / m, 4),
        }

    # ---- goals markets: raw model, calibrated, Bet365 (over 2.5 only)
    goals = {}
    for key, happened in (("over25", lambda f: f[0] + f[1] >= 3), ("btts", lambda f: f[0] > 0 and f[1] > 0)):
        ys = [1 if happened(r["result"]["ft"]) else 0 for r in rows]
        rate = sum(ys) / n
        g = {"rate": pct(rate), "average": round(sum(brier2(rate, y) for y in ys) / n, 4),
             "model": round(sum(brier2(r[key] / 100, y) for r, y in zip(rows, ys)) / n, 4)}
        raw = [(r["raw"][key], y) for r, y in zip(rows, ys) if r.get("raw") and r["raw"].get(key) is not None]
        if raw:
            g["raw"] = round(sum(brier2(p / 100, y) for p, y in raw) / len(raw), 4)
        if key == "over25":
            bo = [(r, y) for r, y in zip(rows, ys) if r.get("book", {}).get("O25") and r["book"].get("U25")]
            if bo:
                imp = [(1 / r["book"]["O25"]) / (1 / r["book"]["O25"] + 1 / r["book"]["U25"]) for r, _ in bo]
                g["book"] = round(sum(brier2(p, y) for p, (_, y) in zip(imp, bo)) / len(bo), 4)
                g["modelOnBookGames"] = round(sum(brier2(r[key] / 100, y) for r, y in bo) / len(bo), 4)
                g["bookGames"] = len(bo)
        goals[key] = g
    ev["goals"] = goals

    # ---- betting simulation: the app's value-bet rule at Bet365 pre-match odds
    bets = defaultdict(lambda: {"bets": 0, "won": 0, "profit": 0.0})
    for r, o in zip(rows, outs):
        b = r.get("book")
        if not b:
            continue
        ft = r["result"]["ft"]
        picks = [("1", r["probs"][0], o == 0), ("X", r["probs"][1], o == 1), ("2", r["probs"][2], o == 2)]
        if b.get("O25"):
            picks.append(("O25", r["over25"], ft[0] + ft[1] >= 3))
        for mk, p, won in picks:
            odds = b.get(mk)
            if not odds or p < MIN_CHANCE:
                continue
            edge = odds * p / 100 - 1
            if VALUE_MIN <= edge <= CHECK_EDGE:
                for k in (mk if mk == "O25" else "result", "all"):
                    bets[k]["bets"] += 1
                    bets[k]["won"] += won
                    bets[k]["profit"] += (odds - 1) if won else -1
        # baseline: 1 unit on Bet365's favourite every game
        fav = min(("1", "X", "2"), key=lambda k: b[k])
        bets["favourite"]["bets"] += 1
        won = (fav == "1" and o == 0) or (fav == "X" and o == 1) or (fav == "2" and o == 2)
        bets["favourite"]["won"] += won
        bets["favourite"]["profit"] += (b[fav] - 1) if won else -1
    ev["betting"] = {k: {**v, "profit": round(v["profit"], 2), "roi": pct(v["profit"] / v["bets"]) if v["bets"] else None}
                     for k, v in bets.items()}

    with open(os.path.join(OUT_DIR, "evaluation.json"), "w") as f:
        json.dump(ev, f, indent=1)
    print(json.dumps(ev, indent=1))


if __name__ == "__main__":
    main()
