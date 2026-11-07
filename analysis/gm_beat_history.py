#!/usr/bin/env python3
"""Does a player who has been beating their prop lines carry value into the next over?

Cohort and bets as in gm_vs_lines.py (over at the closing line). Each bet is split by the player's
earlier lined games the same season: hit rate and mean yards-over-line in the last 3, and how far
season yds/game sits above the line. Also measures how much the closing line moves after a beat.

Run: .venv/bin/python analysis/gm_beat_history.py [2024 2025 2026]"""
import sys
import gm_vs_lines as G

def rep(n, z):
    w, l = sum(r["res"] == "win" for r in z), sum(r["res"] == "loss" for r in z)
    if w + l < 5:
        return
    avg = sum(r["yds"] - r["line"] for r in z) / len(z)
    print(f"  {n:34} {w:3}-{l:<3} ({w / (w + l):.0%}) flat {(w * 100 / 110 - l) / (w + l):+.0%}  yds vs line {avg:+5.1f}")

def splits(rows):
    z = [r for r in rows if len(r["prior"]) >= 3]
    for r in z:
        last = r["prior"][-3:]
        r["hit3"] = sum(v > L for _, L, v in last)
        r["m3"] = sum(v - L for _, L, v in last) / 3
        r["gap"] = r["ypg"] - r["line"]
    print(f"{len(z)} bets with 3+ earlier lined games")
    print(" last-3 hits over the line")
    for h in range(4):
        rep(f"{h}/3 over", [r for r in z if r["hit3"] == h])
    print(" last-3 mean yards over the line")
    for lo, hi in ((-99, -10), (-10, 0), (0, 10), (10, 20), (20, 99)):
        rep(f"{lo:+} to {hi:+}", [r for r in z if lo <= r["m3"] < hi])
    print(" season yds/game minus this line")
    for lo, hi in ((-99, 0), (0, 5), (5, 10), (10, 20), (20, 99)):
        rep(f"{lo:+} to {hi:+}", [r for r in z if lo <= r["gap"] < hi])
    print(" both: 3/3 over and mean beat 10+")
    rep("hot", [r for r in z if r["hit3"] == 3 and r["m3"] >= 10])
    rep("the rest", [r for r in z if not (r["hit3"] == 3 and r["m3"] >= 10)])

def line_moves(rows):
    """How much does next week's line move per yard the player beat the last one by?"""
    xs, ys = [], []
    for r in rows:
        if r["prior"] and r["prior"][-1][0] == r["wk"] - 1:
            _, L, v = r["prior"][-1]
            xs.append(v - L); ys.append(r["line"] - L)
    n = len(xs); mx, my = sum(xs) / n, sum(ys) / n
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
    print(f"\nline move week-to-week vs last beat (n={n}): slope {b:.3f} yds of line per yd over the line, mean move {my:+.1f}")
    xs2 = [r["ypg"] - r["line"] for r in rows]
    print(f"line sits {sum(xs2) / len(xs2):+.1f} below season yds/game on average (cohort), median {sorted(xs2)[len(xs2) // 2]:+.1f}")

if __name__ == "__main__":
    seasons = [int(a) for a in sys.argv[1:]] or [2024, 2025, 2026]
    rows = [r for s in seasons for r in G.season_rows(s)]
    print("=== Good Matchup bets (bottom-10 defense) ===")
    splits([r for r in rows if r["weak"]])
    print("\n=== whole cohort (any defense) ===")
    splits(rows)
    line_moves(rows)
