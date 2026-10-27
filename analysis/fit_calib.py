#!/usr/bin/env python3
"""Fit per-market isotonic calibrations of the live model's P.

rec:  on archived week snapshots (the archive P values came from the deployed
      rec scorer, so they match what the poller will produce). Label: yards
      ever passed that snapshot's line afterwards (the live-win rule).
rush: on the rush model's out-of-sample week-7 predictions
      (data/week7/oos_preds.json from analysis/retrain_markets.py; the archive
      snapshots were scored by the old pooled model, which no longer scores rush).
      Label there is final > line — slightly stricter than the live-win rule.

Each prop/player-game gets total weight 1 (1/n per snapshot) so long games
don't dominate. Writes live/calib.json: {"rec": {p, cal}, "rush": {p, cal}};
the poller interpolates linearly. Delete the file to run uncalibrated.

Usage: python3 analysis/fit_calib.py [weeks...]   (default: 7)
"""
import json, os, sys, bisect
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

def pav(pairs):
    """Weighted pool-adjacent-violators. pairs: sorted (p, label, weight)."""
    out = []
    for p, y, w in pairs:
        out.append([p, y * w, w])
        while len(out) > 1 and out[-2][1] / out[-2][2] >= out[-1][1] / out[-1][2]:
            a = out.pop()
            out[-1] = [a[0], out[-1][1] + a[1], out[-1][2] + a[2]]
    return [(b[0], b[1] / b[2]) for b in out]

def rec_samples(weeks):
    for wk in weeks:
        path = f"{ROOT}/data/fly/docs/archive/props_w{wk}.json"
        for prop in json.load(open(path)).values():
            final = prop.get("final")
            if prop["market"] != "rec" or final is None:
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

def rush_samples():
    d = json.load(open(f"{ROOT}/data/week7/oos_preds.json"))["rush"]
    n = defaultdict(int)
    for m in d["meta"]:
        n[(m[1], m[2])] += 1
    for p, y, m in zip(d["p"], d["y"], d["meta"]):
        yield p, y, 1.0 / n[(m[1], m[2])]

def fit(samples, label):
    pairs = sorted(samples)
    curve = pav(pairs)
    pts = [(0.0, 0.0)] + curve + [(1.0, 1.0)]
    out = {"p": [round(p, 4) for p, _ in pts], "cal": [round(c, 4) for _, c in pts]}
    print(f"{label}: {len(pairs)} samples, {len(pts)} breakpoints")
    for raw in (0.5, 0.6, 0.7, 0.8, 0.9):
        i = bisect.bisect_right(out["p"], raw) - 1
        j = min(i + 1, len(out["p"]) - 1)
        p0, p1, c0, c1 = out["p"][i], out["p"][j], out["cal"][i], out["cal"][j]
        c = c0 if p1 == p0 else c0 + (c1 - c0) * (raw - p0) / (p1 - p0)
        print(f"  raw {raw:.2f} -> calibrated {c:.2f}")
    return out

def main(weeks):
    out = {"weeks": weeks, "rec": fit(rec_samples(weeks), "rec"),
           "rush": fit(rush_samples(), "rush (w7 out-of-sample)")}
    json.dump(out, open(f"{ROOT}/live/calib.json", "w"), indent=1)
    print("wrote live/calib.json")

if __name__ == "__main__":
    main([int(w) for w in sys.argv[1:]] or [7])
