#!/usr/bin/env python3
"""For each instance: pregame line, decayed line at the end of the blank window
(or dry trigger), and whether betting the over there would have cashed."""
import json, os
import blanked_report as br   # reuse its series loading + gc mapping

inst = json.load(open(f"{br.ROOT}/data/blanked_instances.json"))

print(f"{'player':22s} {'kind':4s} {'book':3s} {'pregame':>7s} {'@window':>7s} {'decay':>6s} {'final':>5s} {'over?':>5s}")
wins = total = 0
for r in inst:
    series = br.load_series(r)
    # entry point: just before the window ends (blank) or at the dry trigger
    entry_gc = r.get("gap_end") or r.get("dry_gc")
    if "dry" in r["tags"] and "blanked" in r["tags"]:
        entry_gc = min(r["gap_end"], r["dry_gc"])   # earliest signal
    for b, tag in (("draftkings", "DK"), ("fanduel", "FD")):
        pts = series.get(b)
        if not pts:
            continue
        pre = pts[0][1]   # first quote (pre-kick collapses to gc 0)
        before = [v for gc, v in pts if gc < entry_gc]
        if not before:
            continue
        entry = before[-1]
        over = r["final_yds"] > entry
        wins += over
        total += 1
        print(f"{r['player']:22s} {r['kind']:4s} {tag:3s} {pre:7.1f} {entry:7.1f} {pre-entry:6.1f} "
              f"{r['final_yds']:5d} {'WIN' if over else 'loss':>5s}")
print(f"\nover at the decayed line: {wins}/{total} = {wins/total:.0%} (breakeven ~52.4% at -110)")
