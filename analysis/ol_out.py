#!/usr/bin/env python3
"""RB rushing props when a regular OL starter is out, and the Good Matchups over, at real closing prices 2024-26.

OL regular = 85%+ offense snaps in 75%+ of the team's earlier games (3+ games). 'Out' = a regular under 50% or absent.
A long absence drops the player out of 'regular', so this catches the first few games without him (the window where
season averages still include him). Good Matchups = the live watchlist rule: top-N season ypg (WR 20, TE 10, RB 15)
vs a bottom-10 defense at the position, over at the closing line.

Needs nflverse snap_counts_{season}.csv.gz in SNAPS (default: data/).
Run: .venv/bin/python analysis/ol_out.py [snaps_dir]"""
import collections, csv, gzip, os, sys
import numpy as np
sys.argv, SNAPS = sys.argv[:1], sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "data")
import prop_search as PS

TOP_N = {"WR": 20, "TE": 10, "RB": 15}

def ol_out(season):
    """(team, week) -> number of OL regulars out that game."""
    snaps = collections.defaultdict(dict)   # (team, week) -> {player: pct}
    for r in csv.DictReader(gzip.open(f"{SNAPS}/snap_counts_{season}.csv.gz", "rt")):
        if r["game_type"] == "REG" and r["position"] in ("T", "G", "C", "OL"):
            snaps[(r["team"], int(r["week"]))][r["pfr_player_id"]] = float(r["offense_pct"])
    out = {}
    for (t, w), cur in snaps.items():
        prior = [snaps[(t, x)] for x in range(1, w) if (t, x) in snaps]
        if len(prior) < 3:
            continue
        regs = {p for g in prior for p in g if sum(h.get(p, 0) >= .85 for h in prior) >= .75 * len(prior)}
        out[(t, w)] = sum(cur.get(p, 0) < .5 for p in regs)
    return out

def show(label, rows, side):
    g = [PS.grade(r, side) for r in rows]
    g = [x for x in g if x]
    if not g:
        return print(f"  {label:44s} n=0")
    w = sum(x > 0 for x in g)
    print(f"  {label:44s} {side:5s} {w:3d}-{len(g) - w:<3d} {w / len(g):5.1%}  roi {np.mean(g):+6.1%}  "
          f"line/ypg {np.mean([r['line_vs_ypg'] for r in rows]):.2f}")

def gm_rows(rows):
    """Rows the Good Matchups rule would have bet (ranked within each week's quoted props)."""
    pick = []
    for wk in {(r["season"], r["week"]) for r in rows}:
        wr = [r for r in rows if (r["season"], r["week"]) == wk]
        for kind in ("rec", "rush"):
            for pos, n in TOP_N.items():
                cand = sorted((r for r in wr if r["kind"] == kind and r["pos"] == pos), key=lambda r: -r["ypg"])[:n]
                # defense rank: yards allowed to this position per game, 1 = worst, among this week's opponents
                d = sorted({(r["opp_allow"], r["gid"], r["team"]) for r in wr if r["kind"] == kind and r["pos"] == pos and r["opp_allow"] == r["opp_allow"]}, reverse=True)
                bad = {x[0] for x in d}
                cut = sorted(bad, reverse=True)[:10]
                pick += [r for r in cand if r["opp_allow"] in cut]
    return pick

if __name__ == "__main__":
    allr = []
    for s in (2024, 2025, 2026):
        rows = PS.build(s)
        oo = ol_out(s)
        for r in rows:
            r["ol_out"] = oo.get((r["team"], r["week"]))
        allr += rows
        rb = [r for r in rows if r["kind"] == "rush" and r["pos"] == "RB" and r["ol_out"] is not None]
        print(f"{s}: {len(rb)} RB rush props with snap data")
    rb = [r for r in allr if r["kind"] == "rush" and r["pos"] == "RB" and r["ol_out"] is not None]
    print("\nRB rushing yards vs closing line, by OL regulars out")
    for lab, f in (("0 out", lambda r: r["ol_out"] == 0), ("1 out", lambda r: r["ol_out"] == 1), ("2+ out", lambda r: r["ol_out"] >= 2)):
        sub = [r for r in rb if f(r)]
        show(lab, sub, "under")
    for s in (2024, 2025, 2026):
        show(f"{s} 1+ out", [r for r in rb if r["season"] == s and r["ol_out"] >= 1], "under")
    print("\nRB yards-per-carry-free check: final rush yds minus line")
    for lab, f in (("0 out", lambda r: r["ol_out"] == 0), ("1+ out", lambda r: r["ol_out"] >= 1)):
        sub = [r for r in rb if f(r)]
        print(f"  {lab:8s} n={len(sub):4d}  mean(final-line) {np.mean([r['yds'] - r['line'] for r in sub]):+5.1f}  "
              f"mean(line-ypg) {np.mean([r['line'] - r['ypg'] for r in sub]):+5.1f}")
    print("\nGood Matchups over (the live watchlist rule) at closing prices")
    gm = gm_rows(allr)
    for s in (2024, 2025, 2026):
        show(f"{s}", [r for r in gm if r["season"] == s], "over")
    show("all", gm, "over")
    show("all, recent form down (l3 < 0.8 ypg)", [r for r in gm if r["l3_vs_ypg"] < .8], "over")
    show("all, recent form holding (l3 >= 0.8 ypg)", [r for r in gm if r["l3_vs_ypg"] >= .8], "over")
    show("RB rush only", [r for r in gm if r["kind"] == "rush"], "over")
    show("rec only", [r for r in gm if r["kind"] == "rec"], "over")
