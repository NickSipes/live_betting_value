#!/usr/bin/env python3
"""Standard (non-PPR) fantasy points and positional ranks from nflverse play-by-play.

Scoring for skill players: 0.1/rushing yd, 0.1/receiving yd, 6/rushing or receiving TD,
2/two-point conversion (rusher or receiver), -2/fumble lost. Ranked within WR/TE/RB by
season total through the week before `before_week` (how fantasy sites show positional rank).

Usage: python3 analysis/fantasy.py 7   -> prints the top 10 at each position entering week 7
"""
import csv, gzip, os, sys, collections

ROOT = os.path.join(os.path.dirname(__file__), "..")

def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0

def ranks(before_week, pbp=f"{ROOT}/data/pbp_2026.csv.gz", roster=f"{ROOT}/data/roster_2026.csv", per_game=False):
    """name -> {"pos", "pts", "rank"} for WR/TE/RB, from weeks < before_week.
    per_game=True ranks by points per game played (2+ games), so missed games don't sink a player."""
    pos, name = {}, {}
    for r in csv.DictReader(open(roster)):
        if r["gsis_id"]:
            pos[r["gsis_id"]], name[r["gsis_id"]] = r["position"], r["full_name"]
    pts = collections.defaultdict(float)
    games = collections.defaultdict(set)
    with gzip.open(pbp, "rt") as f:
        for p in csv.DictReader(f):
            if int(p["week"]) >= before_week:
                continue
            rush, rec = p.get("rusher_player_id"), p.get("receiver_player_id")
            for pid in (rush, rec):
                if pid:
                    games[pid].add(p["game_id"])
            if rush:
                pts[rush] += 0.1 * num(p["rushing_yards"]) + 6 * num(p["rush_touchdown"])
            if rec:
                pts[rec] += 0.1 * num(p["receiving_yards"]) + 6 * num(p["pass_touchdown"])
            if p.get("two_point_conv_result") == "success":
                for pid in (rush, rec):
                    if pid:
                        pts[pid] += 2
            if num(p.get("fumble_lost")) and p.get("fumbled_1_player_id"):
                pts[p["fumbled_1_player_id"]] -= 2
    if per_game:
        pts = {i: v / len(games[i]) for i, v in pts.items() if len(games[i]) >= 2}
    out = {}
    for ps in ("WR", "TE", "RB"):
        ids = sorted((i for i in pts if pos.get(i) == ps), key=lambda i: -pts[i])
        for k, i in enumerate(ids):
            out[name[i]] = {"pos": ps, "pts": round(pts[i], 1), "rank": k + 1}
    return out

if __name__ == "__main__":
    wk = int(sys.argv[1]) if len(sys.argv) > 1 else 99
    R = ranks(wk)
    for ps in ("WR", "TE", "RB"):
        top = sorted((v["rank"], n, v["pts"]) for n, v in R.items() if v["pos"] == ps)[:10]
        print(ps, " · ".join(f"{r}. {n} {p}" for r, n, p in top))
