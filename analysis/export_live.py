#!/usr/bin/env python3
"""Train v2 on everything pulled so far and export what the live poller needs:
live/model_weights.json, live/priors.json (entering next week), live/def_priors.json.
Run from analysis/: ../.venv/bin/python export_live.py
"""
import json, os, collections

exec(open(os.path.join(os.path.dirname(__file__), "v2_model.py")).read())

# deployment weights: refit on ALL pulled data (train + test splits combined)
Xall = np.vstack([Xtr2, Xte2])
yall = np.concatenate([ytr2, yte2])
mua, sda = Xall.mean(0), Xall.std(0) + 1e-9
wa, ba = fit_logistic((Xall - mua) / sda, yall, l2=3e-3)

LIVE = f"{ROOT}/live"
json.dump({"feats": V2_FEATS, "mu": mua.tolist(), "sd": sda.tolist(),
           "w": wa.tolist(), "b": float(ba)}, open(f"{LIVE}/model_weights.json", "w"), indent=1)

# priors entering NEXT week (all data to date)
w_next = max_week + 1
pri, dpri = {}, {}
for kind, evs in events.items():
    tot, gms, nev = collections.defaultdict(float), collections.defaultdict(set), collections.defaultdict(int)
    dy, dg = collections.defaultdict(float), collections.defaultdict(set)
    for e in evs:
        gms[e[4]].add(e[1]); nev[e[4]] += 1
        if e[6]:
            tot[e[4]] += e[5]
            dy[(e[2], pos_of[e[4]])] += e[5]
        dg[e[2]].add(e[1])
    for p in gms:
        g = len(gms[p])
        ypg = tot[p] / g
        if g < 2 or ypg < 20:
            continue
        pri.setdefault(name_of[p], {})[kind] = {
            "ypg": round(ypg, 1), "ype": round(tot[p] / nev[p], 2) if nev[p] else 0,
            "epg": round(nev[p] / g, 1), "pos": pos_of[p]}
    for (dt, pos), v in dy.items():
        dpri[f"{kind}|{dt}|{pos}"] = round(v / len(dg[dt]), 1)

json.dump(pri, open(f"{LIVE}/priors.json", "w"), indent=1)
json.dump(dpri, open(f"{LIVE}/def_priors.json", "w"), indent=1)
print(f"exported weights + priors for {len(pri)} players, {len(dpri)} defense cells (entering week {w_next})")
