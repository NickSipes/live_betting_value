#!/usr/bin/env python3
"""The RB-under search, run for WR receiving, TE receiving and QB rushing, overs and unders.
Found on 2024 only; every top rule is then tested on 2025 and 2026 (weeks 4-8) unchanged.
Run: .venv/bin/python analysis/prop_search_pos.py"""
import itertools, math
import numpy as np
import prop_search as S

GROUPS = [("WR rec", lambda r: r["pos"] == "WR" and r["kind"] == "rec"),
          ("TE rec", lambda r: r["pos"] == "TE" and r["kind"] == "rec"),
          ("QB rush", lambda r: r["pos"] == "QB" and r["kind"] == "rush")]

def stats(rows, side):
    g = [S.grade(r, side) for r in rows]
    return len(g), (np.mean(g) if g else float("nan")), sum(x > 0 for x in g), sum(x < 0 for x in g)

def main():
    data = {s: S.build(s) for s in (2024, 2025, 2026)}
    tr = data[2024]
    conds = S.conditions(tr); cf = dict(conds)
    for gname, gf in GROUPS:
        print(f"\n=== {gname} ===")
        for s in (2024, 2025, 2026):
            z = [r for r in data[s] if gf(r)]
            print(f"  {s} baseline: n {len(z)}; every over {stats(z, 'over')[1]:+.1%}, every under {stats(z, 'under')[1]:+.1%}")
        base = [r for r in tr if gf(r)]
        masks = [(n, [r for r in base if f(r)]) for n, f in conds]
        found = []
        for (a, ra), (b, rb) in [((n, rs), (None, None)) for n, rs in masks] + list(itertools.combinations(masks, 2)):
            if b and a.split("<")[0] == b.split("<")[0]:
                continue
            rows = ra if not b else [r for r in ra if cf[b](r)]
            if len(rows) < 40:
                continue
            for side in ("over", "under"):
                g = np.array([S.grade(r, side) for r in rows])
                found.append((np.mean(g) / (g.std() / math.sqrt(len(g)) + 1e-9), side, a if not b else f"{a} & {b}"))
        found.sort(reverse=True)
        held = {2025: 0, 2026: 0}
        print(f"  rules searched on 2024 (n>=40): {len(found)}; top 12 tested forward:")
        for z, side, name in found[:12]:
            fs = [cf[c] for c in name.split(" & ")]
            sel = lambda s: [r for r in data[s] if gf(r) and all(f(r) for f in fs)]
            n4, r4, w4, l4 = stats(sel(2024), side); n5, r5, w5, l5 = stats(sel(2025), side); n6, r6, w6, l6 = stats(sel(2026), side)
            held[2025] += r5 > 0; held[2026] += (r6 > 0) if n6 else 0
            print(f"  {side:5} {name[:52]:52} 2024 {w4}-{l4} {r4:+.0%} | 2025 {w5}-{l5} {r5:+.0%} | 2026 {w6}-{l6} {r6:+.0%}")
        print(f"  profitable going forward: 2025 {held[2025]}/12, 2026 {held[2026]}/12 (about 6 by chance)")

if __name__ == "__main__":
    main()
