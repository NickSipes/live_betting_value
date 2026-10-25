#!/usr/bin/env python3
"""v2: price-aware over model. P(final > quoted line) from game state + price.

Rows: every fresh quote (FD preferred, DK fallback) on a player with priors,
10 game-min in, up to 85% of the game. Features = v1 state features computed at
the quote's game-clock + three price features:
    needed_rem  = line - cum                (yards the over still needs)
    price_ratio = needed_rem / baseline_rem (baseline = season ypg * rem_frac)
    discount    = line / pregame line
Label: player's final stat > line.

Train: quotes in week 4-5 games. Test: week 6-7 games (selected by v1 flags, so
still a biased sample - stated, not hidden). Flag protocol: first quote in a
player-game with P >= threshold -> bet the over.

Run from analysis/: ../.venv/bin/python v2_model.py
"""
import csv, os, bisect, collections, datetime
import numpy as np

exec(open(os.path.join(os.path.dirname(__file__), "burst_model.py")).read())   # v1 data + model

MARKET = {"rec": "player_reception_yds", "rush": "player_rush_yds"}
gsis_by_name = {v: k for k, v in name_of.items()}

def tsp(x):
    return datetime.datetime.fromisoformat(x.replace("Z", "+00:00")).timestamp()

have = {fn[:fn.index("_player")] for fn in os.listdir(f"{ROOT}/data/props_series")}
wallm = collections.defaultdict(list)
with gzip.open(f"{ROOT}/data/pbp_2026.csv.gz", "rt") as f:
    for p in csv.DictReader(f):
        if p["game_id"] in have and p.get("time_of_day") \
                and p["qtr"] not in ("", "NA") and p.get("quarter_seconds_remaining") not in ("", "NA", None):
            wallm[p["game_id"]].append((tsp(p["time_of_day"]),
                                        elapsed(int(float(p["qtr"])), float(p["quarter_seconds_remaining"]))))
for g in wallm:
    wallm[g].sort()

def to_gc(gid, t):
    pts = wallm[gid]
    i = bisect.bisect_right(pts, (t, float("inf"))) - 1
    return 0 if i < 0 else min(pts[i][1], game_len[gid])

quotes = collections.defaultdict(dict)   # (kind,gid,pid) -> {gc: line}
l0 = {}                                  # pregame line
for fn in os.listdir(f"{ROOT}/data/props_series"):
    gid, market = fn[:fn.index("_player")], fn[fn.index("player"):-4]
    kind = "rec" if market == MARKET["rec"] else "rush"
    for x in sorted(csv.DictReader(open(f"{ROOT}/data/props_series/{fn}")), key=lambda r: r["snapshot"]):
        if x["side"] != "Over" or not x["point"]:
            continue
        pid = gsis_by_name.get(x["player"])
        if not pid:
            continue
        k = (kind, gid, pid)
        L = float(x["point"])
        if k not in l0 and x["book"] == "fanduel":
            l0[k] = L
        l0.setdefault(k, L)
        gc = to_gc(gid, tsp(x["snapshot"]))
        if gc <= 0:
            continue
        if x["book"] == "fanduel" or gc not in quotes[k]:
            quotes[k][gc] = L

finals = {k: sum(e[5] for e in v if e[6]) for k, v in pgames.items()}

V2_FEATS = FEATS + ["needed_rem", "price_ratio", "discount"]

def v2_rows(weeks):
    X, y, meta = [], [], []
    for key, q in quotes.items():
        kind, gid, pid = key
        wk = game_meta[gid]["week"]
        if wk not in weeks:
            continue
        pr = priors.get((kind, wk), {}).get(pid)
        if not pr or pr[2] < 2 or pr[0] < 20:
            continue
        ypg, ype, _ = pr
        evs = pgames.get(key)
        if not evs:
            continue   # quoted but never targeted/carried (inactive)
        dv = def_ypg.get((kind, wk), {}).get((evs[0][2], pos_of[pid]), 0)
        team = evs[0][3]
        home = game_meta[gid]["home"] == team
        glen = game_len[gid]
        fin = finals[key]
        for gc, L in sorted(q.items()):
            if gc < 600 or gc > glen * 0.85:
                continue
            if L < 15:
                continue   # micro/junk markets
            cum = nev = y10 = e10 = 0
            last_prog = 0
            for e in evs:
                if e[7] > gc:
                    break
                nev += 1
                if e[7] > gc - 600:
                    e10 += 1
                if e[6]:
                    cum += e[5]
                    last_prog = e[7]
                    if e[7] > gc - 600:
                        y10 += e[5]
            tg = min(GRID, key=lambda t: abs(t - gc))
            st, hs, as_ = team_state[(gid, tg)]
            ts_ = st.get(team, [0, 0, 0.0, 0.0])
            diff = (hs - as_) if home else (as_ - hs)
            ef = gc / glen
            rem_frac = 1 - ef
            needed = L - cum
            if needed < 3:
                continue   # stale quote / line already beaten: a fake free win
            baseline_rem = ypg * rem_frac
            X.append([ef, ypg, ype, dv, nev, cum, (gc - last_prog) / 60,
                      e10, y10, diff, diff * ef, ts_[1] / ts_[0] if ts_[0] else 0.5,
                      ts_[2] / ts_[1] if ts_[1] else 0, ts_[3] / (ts_[0] - ts_[1]) if ts_[0] > ts_[1] else 0,
                      ts_[0] / (gc / 60), 1.0 if kind == "rush" else 0.0,
                      needed, needed / (baseline_rem + 1), L / l0[key]])
            y.append(1.0 if fin > L else 0.0)
            meta.append((kind, gid, name_of[pid], wk, gc, round(cum), L, round(fin)))
    return np.array(X), np.array(y), meta

Xtr2, ytr2, mtr2 = v2_rows(set(range(2, 6)))
Xte2, yte2, mte2 = v2_rows(set(range(6, 99)))
mu2, sd2 = Xtr2.mean(0), Xtr2.std(0) + 1e-9
Ztr2, Zte2 = (Xtr2 - mu2) / sd2, (Xte2 - mu2) / sd2
w2, b2 = fit_logistic(Ztr2, ytr2, l2=3e-3)
pte2 = 1 / (1 + np.exp(-(Zte2 @ w2 + b2)))
ptr2 = 1 / (1 + np.exp(-(Ztr2 @ w2 + b2)))

print("\n================ v2 (price-aware) ================")
print(f"train: {len(ytr2)} quotes ({len({(m[0],m[1],m[2]) for m in mtr2})} player-games), over rate {ytr2.mean():.1%}")
print(f"test:  {len(yte2)} quotes ({len({(m[0],m[1],m[2]) for m in mte2})} player-games), over rate {yte2.mean():.1%}")
print(f"test AUC: {auc(yte2, pte2):.3f}")
print("\ncoefficients:")
for f, c in sorted(zip(V2_FEATS, w2), key=lambda x: -abs(x[1])):
    print(f"  {f:12s} {c:+.3f}")

for thr in (0.55, 0.60, 0.65, 0.70):
    firsts = {}
    for i in sorted(range(len(mte2)), key=lambda i: mte2[i][4]):
        if pte2[i] < thr:
            continue
        k = (mte2[i][0], mte2[i][1], mte2[i][2])
        firsts.setdefault(k, i)
    wins = sum(yte2[i] for i in firsts.values())
    n = len(firsts)
    print(f"\nflag protocol P>={thr}: {wins:.0f}/{n}" + (f" = {wins/n:.0%}" if n else ""))
    if thr == 0.60:
        for k, i in sorted(firsts.items(), key=lambda kv: -pte2[kv[1]]):
            kind, gid, player, wk, gc, cum, L, fin = mte2[i]
            print(f"  wk{wk} {player:22s} {kind:4s} @{gc//60}min cum {cum:3d} line {L:5.1f} "
                  f"P={pte2[i]:.2f} final {fin} {'WIN' if yte2[i] else 'loss'}")
