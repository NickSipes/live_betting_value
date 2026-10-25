#!/usr/bin/env python3
"""Evaluate burst-model flags against real quoted lines.

1. For each flagged player-game (drought>=10 & top-decile score, first flagged
   snapshot = entry): freshest quote within 6 game-min at entry; bet the over;
   win = player's final stat > line.
2. Context curve: ALL test-week quoted snapshots joined to model scores,
   over-win rate by score tercile.

Run with: ../.venv/bin/python flags_vs_lines.py   (from analysis/)
"""
import csv, os, bisect, collections, datetime
import numpy as np

exec(open(os.path.join(os.path.dirname(__file__), "burst_model.py")).read())

MARKET = {"rec": "player_reception_yds", "rush": "player_rush_yds"}
gsis_by_name = {v: k for k, v in name_of.items()}

def tsp(x):
    return datetime.datetime.fromisoformat(x.replace("Z", "+00:00")).timestamp()

# wall->gc maps for games with pulled lines
have = {fn[:fn.index("_player")] for fn in os.listdir(f"{ROOT}/data/props_series")}
wall = collections.defaultdict(list)
with gzip.open(f"{ROOT}/data/pbp_2026.csv.gz", "rt") as f:
    for p in csv.DictReader(f):
        if p["game_id"] in have and p.get("time_of_day") \
                and p["qtr"] not in ("", "NA") and p.get("quarter_seconds_remaining") not in ("", "NA", None):
            wall[p["game_id"]].append((tsp(p["time_of_day"]),
                                       elapsed(int(float(p["qtr"])), float(p["quarter_seconds_remaining"]))))
for g in wall:
    wall[g].sort()

def to_gc(gid, t):
    pts = wall[gid]
    i = bisect.bisect_right(pts, (t, float("inf"))) - 1
    return 0 if i < 0 else min(pts[i][1], game_len[gid])

# quotes: (kind,gid,pid) -> {gc: line}, FD preferred then DK
quotes = collections.defaultdict(dict)
for fn in os.listdir(f"{ROOT}/data/props_series"):
    gid, market = fn[:fn.index("_player")], fn[fn.index("player"):-4]
    kind = "rec" if market == MARKET["rec"] else "rush"
    for x in csv.DictReader(open(f"{ROOT}/data/props_series/{fn}")):
        if x["side"] != "Over" or not x["point"]:
            continue
        pid = gsis_by_name.get(x["player"])
        if not pid:
            continue
        gc = to_gc(gid, tsp(x["snapshot"]))
        if gc <= 0:
            continue
        k = (kind, gid, pid)
        if x["book"] == "fanduel" or gc not in quotes[k]:
            quotes[k][gc] = float(x["point"])

def quote_at(k, t):
    q = quotes.get(k, {})
    fresh = [g for g in q if t - 360 <= g <= t]
    return q[max(fresh)] if fresh else None

finals = {k: sum(e[5] for e in v if e[6]) for k, v in pgames.items()}
pid_by = {}
for (kind, gid, pid) in pgames:
    pid_by[(kind, gid, name_of[pid])] = pid

# ---- 1. the 13 flags at real prices ----------------------------------------
print("\n=== flagged entries vs real lines (bet the over at first flag) ===")
first = {}
for i in sorted(np.where(dry & hot)[0], key=lambda i: mte[i][4]):
    kind, gid, player, wk, t, cum, nev, nxt = mte[i]
    k = (kind, gid, player)
    if k not in first:
        first[k] = i
wins = tot = 0
for (kind, gid, player), i in sorted(first.items(), key=lambda kv: mte[kv[1]][3]):
    _, _, _, wk, t, cum, nev, nxt = mte[i]
    pid = pid_by.get((kind, gid, player))
    key = (kind, gid, pid)
    L = quote_at(key, t)
    fin = finals.get(key, 0)
    if L is None:
        print(f"wk{wk} {player:22s} {kind:4s} @{t//60}min  cum {cum:3d}  -- no fresh quote --  final {fin:.0f}")
        continue
    win = fin > L
    wins += win; tot += 1
    print(f"wk{wk} {player:22s} {kind:4s} @{t//60}min  cum {cum:3d}  line {L:5.1f}  final {fin:3.0f}  {'WIN' if win else 'loss'}")
print(f"==> {wins}/{tot} = {wins/tot:.0%} (breakeven ~53.5% at -115)" if tot else "==> none priced")

# ---- 2. all test-week quoted snapshots by model-score tercile ---------------
print("\n=== all test-week snapshots with fresh quotes, over result by score tercile ===")
scored = []
for i in range(len(mte)):
    kind, gid, player, wk, t, cum, nev, nxt = mte[i]
    pid = pid_by.get((kind, gid, player))
    key = (kind, gid, pid)
    L = quote_at(key, t)
    if L is None:
        continue
    scored.append((pte[i], finals[key] > L))
if scored:
    scored.sort()
    n = len(scored)
    for name, chunk in (("low", scored[:n//3]), ("mid", scored[n//3:2*n//3]), ("high", scored[2*n//3:])):
        w = sum(1 for _, win in chunk if win)
        print(f"  {name:4s} score tercile: over wins {w}/{len(chunk)} = {w/len(chunk):.0%}")
    print(f"  (n={n} snapshot-quotes)")
