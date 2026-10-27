#!/usr/bin/env python3
"""Fit an isotonic calibration of the live model's P on archived week snapshots.

Label: did the player's yards ever pass that snapshot's line afterwards (the
live-win settlement rule). Each prop's snapshots share one outcome, so every
prop gets total weight 1 (1/n per snapshot) to stop long games dominating.

Writes live/calib.json: {"p": [...], "cal": [...]} breakpoints; the poller
interpolates linearly between them. Delete the file to run uncalibrated.

Usage: python3 analysis/fit_calib.py [weeks...]   (default: 7)
"""
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

def pav(pairs):
    """Weighted pool-adjacent-violators. pairs: sorted (p, label, weight)."""
    blocks = [[p, y * w, w] for p, y, w in pairs]   # [max_p, sum y*w, sum w]
    out = []
    for b in blocks:
        out.append(b)
        while len(out) > 1 and out[-2][1] / out[-2][2] >= out[-1][1] / out[-1][2]:
            a = out.pop()
            out[-1] = [a[0], out[-1][1] + a[1], out[-1][2] + a[2]]
    return [(b[0], b[1] / b[2]) for b in out]

def samples(weeks):
    for wk in weeks:
        path = f"{ROOT}/data/fly/docs/archive/props_w{wk}.json"
        for prop in json.load(open(path)).values():
            final = prop.get("final")
            if final is None:
                continue
            rows, seen = [], set()
            for gc, yds, ev, line, p in prop["snaps"]:
                if p is None or line is None or gc in seen:
                    continue
                seen.add(gc)
                later = max([s[1] for s in prop["snaps"] if s[0] >= gc] or [0])
                rows.append((p, 1.0 if max(later, final) > line else 0.0))
            for p, y in rows:
                yield p, y, 1.0 / len(rows)

def main(weeks):
    pairs = sorted(samples(weeks))
    print(f"{len(pairs)} snapshots from weeks {weeks}")
    curve = pav(pairs)
    # anchor the ends so interpolation covers [0, 1]
    pts = [(0.0, 0.0)] + curve + [(1.0, 1.0)]
    out = {"weeks": weeks, "p": [round(p, 4) for p, _ in pts],
           "cal": [round(c, 4) for _, c in pts]}
    json.dump(out, open(f"{ROOT}/live/calib.json", "w"), indent=1)
    for raw in (0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9):
        import bisect
        i = bisect.bisect_right(out["p"], raw) - 1
        p0, p1 = out["p"][i], out["p"][min(i + 1, len(out["p"]) - 1)]
        c0, c1 = out["cal"][i], out["cal"][min(i + 1, len(out["p"]) - 1)]
        c = c0 if p1 == p0 else c0 + (c1 - c0) * (raw - p0) / (p1 - p0)
        print(f"  raw {raw:.2f} -> calibrated {c:.2f}")
    print(f"wrote live/calib.json ({len(pts)} breakpoints)")

if __name__ == "__main__":
    main([int(w) for w in sys.argv[1:]] or [7])
