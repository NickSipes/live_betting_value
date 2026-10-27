#!/usr/bin/env python3
"""Per-market retrain with week-7 live-archive quotes folded into the pipeline.

The deployed pooled model scored AUC ~0.4-0.5 on week 7 and its is_rush
penalty (~-1.1 logits) made live rush calls impossible. This script:
  1. execs v2_model.py (pbp structures + props_series quotes, weeks 2-6ish),
  2. injects every archived live snapshot (data/fly/docs/archive/props_w*.json)
     as quotes, mapping ESPN games/players to nflverse ids,
  3. evaluates per-market models out-of-sample (train w2-6 -> test w7),
  4. refits each market on ALL weeks and writes live/model_weights.json as
     {"feats": [...], "rec": {mu,sd,w,b}, "rush": {mu,sd,w,b}}.

Run from analysis/: ../.venv/bin/python retrain_markets.py
"""
import json, os

exec(open(os.path.join(os.path.dirname(__file__), "v2_model.py")).read())

ESPN2NFLV = {"LAR": "LA", "WSH": "WAS"}   # inverse of the poller's ALIAS

def inject_archive_weeks():
    added = dropped = 0
    byweek = {}
    for m in game_meta.values():
        byweek.setdefault(m["week"], {})
    gid_by_teams = {(m["week"], m["away"], m["home"]): gid for gid, m in game_meta.items()}
    arch_dir = f"{ROOT}/data/fly/docs/archive"
    for fn in sorted(os.listdir(arch_dir)):
        if not fn.startswith("props_w"):
            continue
        wk = int(fn[7:-5])
        for key, prop in json.load(open(f"{arch_dir}/{fn}")).items():
            away, home = [ESPN2NFLV.get(t, t) for t in prop["game"].split(" @ ")]
            gid = gid_by_teams.get((wk, away, home))
            pid = gsis_by_name.get(prop["player"])
            if not gid or not pid:
                dropped += 1
                continue
            k = (prop["market"], gid, pid)
            if prop.get("l0") is not None:
                l0.setdefault(k, prop["l0"])
            q = quotes[k]
            for gc, cum, nev, L, P in prop["snaps"]:
                if L is not None and gc > 0:
                    q.setdefault(gc, L)   # props_series data (if any) wins
            added += 1
    print(f"injected {added} archived props ({dropped} unmatched)")

inject_archive_weeks()

def market_rows(X, y, meta, kind):
    idx = [i for i, m in enumerate(meta) if m[0] == kind]
    return X[idx], y[idx], [meta[i] for i in idx]

Xtr, ytr, mtr = v2_rows(set(range(2, 7)))
Xte, yte, mte = v2_rows({7})

MODELS = {}
print("\n======== per-market, train w2-6 -> test w7 (out-of-sample) ========")
for kind in ("rec", "rush"):
    Xk, yk, mk = market_rows(Xtr, ytr, mtr, kind)
    Xv, yv, mv = market_rows(Xte, yte, mte, kind)
    mu, sd = Xk.mean(0), Xk.std(0) + 1e-9
    w, b = fit_logistic((Xk - mu) / sd, yk, l2=3e-3)
    pv = 1 / (1 + np.exp(-((Xv - mu) / sd @ w + b)))
    print(f"\n{kind}: train {len(yk)} quotes, test {len(yv)} quotes "
          f"({len({(m[1], m[2]) for m in mv})} player-games), "
          f"test over-rate {yv.mean():.1%}, test AUC {auc(yv, pv):.3f}")
    for thr in (0.55, 0.60, 0.65, 0.70):
        firsts = {}
        for i in sorted(range(len(mv)), key=lambda i: mv[i][4]):
            if pv[i] >= thr:
                firsts.setdefault((mv[i][1], mv[i][2]), i)
        n = len(firsts)
        wins = sum(yv[i] for i in firsts.values())
        print(f"  flag first P>={thr:.2f}: {wins:.0f}-{n-wins:.0f}" + (f" ({wins/n:.0%})" if n else ""))
    MODELS[kind] = dict(test_p=pv.tolist(), test_y=yv.tolist(), test_meta=mv, test_x=Xv)

# deployment: rush refit on everything; rec stays the deployed pooled v2 model
# (retrained rec tested WORSE than random on w7 — AUC .396 — so there is nothing
# better to ship for rec yet; the pooled weights keep rec numerically unchanged)
Xall, yall, mall = v2_rows(set(range(2, 99)))
out = {"feats": V2_FEATS,
       "rec": {k: v for k, v in json.load(open(f"{ROOT}/live/model_weights_pooled_v2.json")).items()
               if k in ("mu", "sd", "w", "b")}}
Xk, yk, _ = market_rows(Xall, yall, mall, "rush")
mu, sd = Xk.mean(0), Xk.std(0) + 1e-9
w, b = fit_logistic((Xk - mu) / sd, yk, l2=3e-3)
out["rush"] = {"mu": mu.tolist(), "sd": sd.tolist(), "w": w.tolist(), "b": float(b)}
print(f"\nrush all-data fit: {len(yk)} quotes; top coefficients:")
for f, c in sorted(zip(V2_FEATS, w), key=lambda x: -abs(x[1]))[:6]:
    print(f"  {f:12s} {c:+.3f}")
json.dump(out, open(f"{ROOT}/live/model_weights.json", "w"), indent=1)
print("wrote live/model_weights.json (rec: pooled v2, rush: per-market)")

# out-of-sample w7 predictions for the rush calibration fit (analysis/fit_calib.py)
# and for gating in backtests: score_diff is x[9], ev_sofar x[4]
json.dump({k: {"p": MODELS[k]["test_p"], "y": MODELS[k]["test_y"],
               "meta": MODELS[k]["test_meta"],
               "sd9": MODELS[k]["test_x"][:, 9].tolist(),
               "ev4": MODELS[k]["test_x"][:, 4].tolist()} for k in MODELS},
          open(f"{ROOT}/data/week7/oos_preds.json", "w"))
print("wrote data/week7/oos_preds.json (w7 out-of-sample preds for calibration)")
