#!/usr/bin/env python3
"""Burst classifier: P(player gains 25+ rec yds / 20+ rush yds in the NEXT 10
game-minutes), from live-computable state. All 2026 season data.

Features per (player-game, t) snapshot:
  player in-game: events/yards so far, last-10-min usage and yards, drought
  player priors:  season ypg, yards/event (entering that week)
  defense:        yds/game allowed to the player's position (entering week)
  situation:      elapsed frac, score diff, score diff x elapsed (late-game
                  script), team pass share, team yds/pass and yds/rush so far
                  ("what's working"), team plays/min (pace)

Logistic regression (numpy), trained on weeks 3-5, tested on weeks 6-7.
Run with the project venv: .venv/bin/python analysis/burst_model.py
"""
import csv, gzip, collections, os
import numpy as np

ROOT = os.path.join(os.path.dirname(__file__), "..")
GRID = range(600, 3001, 300)          # snapshot times, 10..50 game-min
HORIZON = 600                          # "burst" window: next 10 game-min
BURST = {"rec": 25, "rush": 20}
TRAIN_WEEKS = range(3, 6)
TEST_WEEKS = range(6, 99)

def elapsed(qtr, qsr):
    full = min(qtr - 1, 4) * 900 + max(qtr - 5, 0) * 600
    return full + ((900 if qtr <= 4 else 600) - qsr)

pos_of = {}
name_of = {}
for r in csv.DictReader(open(f"{ROOT}/data/roster_2026.csv")):
    if r["gsis_id"]:
        pos_of[r["gsis_id"]] = r["position"]
        name_of[r["gsis_id"]] = r["full_name"]

events = {"rec": [], "rush": []}   # (week,gid,defteam,posteam,pid,yds,counts,gc)
plays = collections.defaultdict(list)   # gid -> (gc, posteam, is_pass, yards)
scores = collections.defaultdict(list)  # gid -> (gc, home_score, away_score)
game_meta, game_len = {}, collections.defaultdict(int)
with gzip.open(f"{ROOT}/data/pbp_2026.csv.gz", "rt") as f:
    for p in csv.DictReader(f):
        gid, wk = p["game_id"], int(p["week"])
        if p["qtr"] in ("", "NA") or p.get("quarter_seconds_remaining") in ("", "NA", None):
            continue
        gc = elapsed(int(float(p["qtr"])), float(p["quarter_seconds_remaining"]))
        game_len[gid] = max(game_len[gid], gc)
        game_meta.setdefault(gid, {"week": wk, "home": p["home_team"], "away": p["away_team"]})
        try:
            scores[gid].append((gc, int(float(p["total_home_score"])), int(float(p["total_away_score"]))))
        except ValueError:
            pass
        if p.get("posteam") and (p.get("pass") == "1" or p.get("rush") == "1"):
            yg = p.get("yards_gained")
            yg = float(yg) if yg not in ("", "NA") else 0.0
            plays[gid].append((gc, p["posteam"], p.get("pass") == "1", yg))
        rid = p.get("receiver_player_id")
        if rid and p.get("passer_player_name") and pos_of.get(rid) in ("WR", "TE"):
            y = p.get("receiving_yards")
            y = float(y) if y not in ("", "NA") else 0.0
            events["rec"].append((wk, gid, p["defteam"], p["posteam"], rid, y, p.get("complete_pass") == "1", gc))
        uid = p.get("rusher_player_id")
        if uid and p.get("rush_attempt") == "1" and pos_of.get(uid) == "RB":
            y = p.get("rushing_yards")
            y = float(y) if y not in ("", "NA") else 0.0
            events["rush"].append((wk, gid, p["defteam"], p["posteam"], uid, y, True, gc))
for gid in plays:
    plays[gid].sort()
    scores[gid].sort()
max_week = max(m["week"] for m in game_meta.values())

# priors entering each week: player (ypg, ype, games) and defense ypg allowed/pos
priors, def_ypg = {}, {}
for kind, evs in events.items():
    for w in range(2, max_week + 1):
        tot, gms, nev = collections.defaultdict(float), collections.defaultdict(set), collections.defaultdict(int)
        dy, dg = collections.defaultdict(float), collections.defaultdict(set)
        for e in evs:
            if e[0] < w:
                gms[e[4]].add(e[1]); nev[e[4]] += 1
                if e[6]:
                    tot[e[4]] += e[5]
                    dy[(e[2], pos_of[e[4]])] += e[5]
                dg[e[2]].add(e[1])
        priors[(kind, w)] = {p: (tot[p] / len(gms[p]), tot[p] / nev[p] if nev[p] else 0, len(gms[p]))
                             for p in gms}
        def_ypg[(kind, w)] = {k: v / len(dg[k[0]]) for k, v in dy.items() if len(dg[k[0]]) >= 1}

# player-game event lists
pgames = collections.defaultdict(list)
for kind, evs in events.items():
    for e in evs:
        pgames[(kind, e[1], e[4])].append(e)
for v in pgames.values():
    v.sort(key=lambda e: e[7])

# team state at grid times, precomputed per game
team_state = {}   # (gid, t) -> {team: (plays, pass_plays, pass_yds, rush_yds)}, score (hs, as)
for gid, pl in plays.items():
    for t in GRID:
        st = collections.defaultdict(lambda: [0, 0, 0.0, 0.0])
        for gc, team, is_pass, yg in pl:
            if gc > t:
                break
            s = st[team]
            s[0] += 1
            if is_pass:
                s[1] += 1; s[2] += yg
            else:
                s[3] += yg
        hs = as_ = 0
        for gc, h, a_ in scores[gid]:
            if gc > t:
                break
            hs, as_ = h, a_
        team_state[(gid, t)] = (dict(st), hs, as_)

FEATS = ["elapsed", "ypg", "ype", "def_ypg", "ev_sofar", "yds_sofar", "drought",
         "ev_last10", "yds_last10", "score_diff", "diff_x_late", "pass_share",
         "team_ypp", "team_ypr", "pace", "is_rush"]

def rows_for(weeks):
    X, y, meta = [], [], []
    for (kind, gid, pid), evs in pgames.items():
        wk = game_meta[gid]["week"]
        if wk not in weeks:
            continue
        pr = priors.get((kind, wk), {}).get(pid)
        if not pr or pr[2] < 2 or pr[0] < 20:    # 2+ prior games, 20+ ypg: featured
            continue
        ypg, ype, _ = pr
        dv = def_ypg.get((kind, wk), {}).get((evs[0][2], pos_of[pid]), 0)
        team = evs[0][3]
        home = game_meta[gid]["home"] == team
        glen = game_len[gid]
        for t in GRID:
            if t + HORIZON > glen:
                break
            cum = nev = y10 = e10 = 0
            last_prog = 0
            for e in evs:
                if e[7] > t:
                    break
                nev += 1
                if e[7] > t - 600:
                    e10 += 1
                if e[6]:
                    cum += e[5]
                    last_prog = e[7]
                    if e[7] > t - 600:
                        y10 += e[5]
            if nev == 0 and cum == 0 and t > 1800 and not any(e[7] > t for e in evs):
                break   # player done for the day; nothing live to price
            nxt = sum(e[5] for e in evs if e[6] and t < e[7] <= t + HORIZON)
            st, hs, as_ = team_state[(gid, t)]
            ts = st.get(team, [0, 0, 0.0, 0.0])
            opp_pts = as_ if home else hs
            own_pts = hs if home else as_
            diff = own_pts - opp_pts
            ef = t / glen
            X.append([ef, ypg, ype, dv, nev, cum, (t - last_prog) / 60,
                      e10, y10, diff, diff * ef, ts[1] / ts[0] if ts[0] else 0.5,
                      ts[2] / ts[1] if ts[1] else 0, ts[3] / (ts[0] - ts[1]) if ts[0] > ts[1] else 0,
                      ts[0] / (t / 60), 1.0 if kind == "rush" else 0.0])
            y.append(1.0 if nxt >= BURST[kind] else 0.0)
            meta.append((kind, gid, name_of[pid], wk, t, round(cum), nev, round(nxt)))
    return np.array(X), np.array(y), meta

Xtr, ytr, _ = rows_for(set(TRAIN_WEEKS))
Xte, yte, mte = rows_for(set(TEST_WEEKS))
mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
Ztr, Zte = (Xtr - mu) / sd, (Xte - mu) / sd

def fit_logistic(Z, y, l2=1e-3, iters=3000, lr=0.1):
    n, d = Z.shape
    w, b = np.zeros(d), 0.0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-(Z @ w + b)))
        g = Z.T @ (p - y) / n + l2 * w
        w -= lr * g
        b -= lr * (p - y).mean()
    return w, b

w, b = fit_logistic(Ztr, ytr)
pte = 1 / (1 + np.exp(-(Zte @ w + b)))

def auc(y, p):
    order = np.argsort(p)
    r = np.empty(len(p)); r[order] = np.arange(1, len(p) + 1)
    pos = y == 1
    return (r[pos].sum() - pos.sum() * (pos.sum() + 1) / 2) / (pos.sum() * (~pos).sum())

print(f"train: {len(ytr)} rows, burst rate {ytr.mean():.1%} | test: {len(yte)} rows, burst rate {yte.mean():.1%}")
print(f"test AUC: {auc(yte, pte):.3f}\n")
print("standardized coefficients (what predicts a burst):")
for f, c in sorted(zip(FEATS, w), key=lambda x: -abs(x[1])):
    print(f"  {f:12s} {c:+.3f}")

# lift by decile
print("\ntest burst rate by model-score decile:")
qs = np.quantile(pte, np.linspace(0, 1, 11))
for i in range(10):
    m = (pte >= qs[i]) & (pte <= qs[i + 1])
    if m.sum():
        print(f"  d{i+1}: {yte[m].mean():.1%}  (n={m.sum()})")

# the betting-relevant slice: in a drought (decayed line) AND model says burst
drought = Zte[:, FEATS.index("drought")] * sd[FEATS.index("drought")] + mu[FEATS.index("drought")]
hot = pte >= np.quantile(pte, 0.9)
dry = drought >= 10
for lbl, m in (("drought>=10min alone", dry), ("top-decile score alone", hot),
               ("drought AND top-decile", dry & hot)):
    if m.sum():
        print(f"\n{lbl}: burst rate {yte[m].mean():.1%} (n={m.sum()}, base {yte.mean():.1%})")

print("\ntop 20 test flags (weeks 6+), drought >= 10 game-min:")
idx = np.argsort(-pte)
shown = 0
for i in idx:
    if drought[i] < 10:
        continue
    kind, gid, player, wk, t, cum, nev, nxt = mte[i]
    hit = "BURST" if yte[i] else "no"
    print(f"  wk{wk} {player:22s} {kind:4s} @{t//60}min  P={pte[i]:.2f}  cum {cum:3d} ({nev} ev, drought {drought[i]:.0f}m)  next10 {nxt:3d} -> {hit}")
    shown += 1
    if shown == 20:
        break
