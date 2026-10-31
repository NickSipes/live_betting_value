#!/usr/bin/env python3
"""Research: a two-stage live model for RB rushing-yards props, and a direct test
of the "early yards-per-carry above the player's season average = leaky defense"
hypothesis. Nothing here writes to live/, docs/ or data/.

Stages (run all, or name one):
  ../.venv/bin/python rush_model.py [load|hyp|carries|quotes|eval|all]

  load     parse pbp 2024-26 into a per-game play log (cached in SCRATCH)
  hyp      hypothesis test: excess YPC so far vs remaining YPC / carries, several
           checkpoints, player- and team-level, with controls
  carries  stage 1: remaining-carries regression on the whole play log
           (train 2024-25 + 2026 w1-3, test 2026 w4-7), + dispersion
  quotes   build the live-quote rows (weeks 4-6 raw odds snapshots; w7 + w8 archive)
  eval     score every quote with the two-stage model, the baselines, and the
           week-7 held-out logistic; records / units / AUC / Brier / bootstrap

Outputs land in SCRATCH (see below). ESPN play logs are fetched only for the two
games pbp_2026.csv.gz does not cover (DAL @ PHI w7, CAR @ GB w8) and cached.
"""
import csv, gzip, json, os, sys, math, pickle, collections, random, urllib.request
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRATCH = ("/private/tmp/claude-501/-Users-nicksipes-live-betting-value/"
           "2c8c1736-84c4-4ff6-b523-8746c90fe146/scratchpad/rush_agent")
os.makedirs(SCRATCH, exist_ok=True)
rng = np.random.default_rng(7)

def elapsed(qtr, qsr):
    full = min(qtr - 1, 4) * 900 + max(qtr - 5, 0) * 600
    return full + ((900 if qtr <= 4 else 600) - qsr)

def fnum(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d

# ----------------------------------------------------------------------------- load
def load_pbp():
    """-> games: gid -> dict(season, week, home, away, spread (home margin), total, glen,
    plays: [(gc, posteam, defteam, kind, yards, rusher, hs, as)]) kind in pass/rush/other.
    Rushes by QBs and kneels are kind 'qbrush' (count as team plays, not RB carries)."""
    cache = f"{SCRATCH}/pbp.pkl"
    if os.path.exists(cache):
        return pickle.load(open(cache, "rb"))
    roster_pos, roster_name = {}, {}
    for r in csv.DictReader(open(f"{ROOT}/data/roster_2026.csv")):
        if r["gsis_id"]:
            roster_pos[r["gsis_id"]] = r["position"]
            roster_name[r["gsis_id"]] = r["full_name"]
    games, names = {}, dict(roster_name)
    raw = collections.defaultdict(list)
    passers = collections.defaultdict(collections.Counter)   # season -> passer id -> att
    for yr in (2024, 2025, 2026):
        with gzip.open(f"{ROOT}/data/pbp_{yr}.csv.gz", "rt") as f:
            for p in csv.DictReader(f):
                if p["season_type"] != "REG" or p["qtr"] in ("", "NA") \
                        or p.get("quarter_seconds_remaining") in ("", "NA", None):
                    continue
                gid = p["game_id"]
                g = games.setdefault(gid, dict(season=yr, week=int(p["week"]), home=p["home_team"],
                                               away=p["away_team"], spread=fnum(p.get("spread_line")),
                                               total=fnum(p.get("total_line")), glen=0, roof=p.get("roof")))
                gc = elapsed(int(float(p["qtr"])), float(p["quarter_seconds_remaining"]))
                g["glen"] = max(g["glen"], gc)
                if p.get("time_of_day"):
                    g.setdefault("wall", []).append((p["time_of_day"], gc))
                hs, as_ = int(fnum(p["total_home_score"])), int(fnum(p["total_away_score"]))
                if p.get("pass") == "1" and p.get("passer_player_id"):
                    passers[yr][p["passer_player_id"]] += 1
                if p.get("rusher_player_id") and p.get("rusher_player_name"):
                    names.setdefault(p["rusher_player_id"], p["rusher_player_name"])
                is_pass, is_rush = p.get("pass") == "1", p.get("rush") == "1"
                if not (is_pass or is_rush) or not p.get("posteam"):
                    raw[gid].append((gc, p.get("posteam") or "", p.get("defteam") or "", "other", 0.0, None, hs, as_, 0))
                    continue
                yg = fnum(p.get("yards_gained"))
                rusher = p.get("rusher_player_id") or None
                ry = fnum(p.get("rushing_yards"))
                kind = "pass" if is_pass else "rush"
                if kind == "rush" and (p.get("qb_kneel") == "1" or p.get("qb_scramble") == "1"
                                       or p.get("rush_attempt") != "1" or not rusher):
                    kind = "qbrush"
                raw[gid].append((gc, p["posteam"], p["defteam"], kind, yg, rusher, hs, as_, ry))
    qb = {yr: {pid for pid, n in c.items() if n >= 20} for yr, c in passers.items()}
    for gid, g in games.items():
        pl = []
        for gc, pos, deft, kind, yg, rusher, hs, as_, ry in sorted(raw[gid], key=lambda r: r[0]):
            if kind == "rush" and (rusher in qb[g["season"]] or roster_pos.get(rusher) == "QB"):
                kind = "qbrush"
            pl.append((gc, pos, deft, kind, ry if kind == "rush" else yg, rusher, hs, as_))
        g["plays"] = pl
        g["gid"] = gid
        g["wall"] = sorted(set(g.get("wall", [])))
    out = dict(games=games, names=names, pos=roster_pos)
    pickle.dump(out, open(cache, "wb"))
    return out

def rb_game_lines(D):
    """Per (gid, rusher): sorted [(gc, yards)] of RB carries."""
    out = collections.defaultdict(list)
    for gid, g in D["games"].items():
        for gc, pos, deft, kind, y, rusher, hs, as_ in g["plays"]:
            if kind == "rush":
                out[(gid, rusher)].append((gc, y, pos, deft))
    return out

def point_in_time_priors(D):
    """Season-to-date stats entering each (season, week): player carries/yards/games,
    defense RB carries/yards allowed/games, team rush share, league ypc.
    Entering week 1 the previous season's totals stand in (shrunk via K_PRIOR)."""
    cache = f"{SCRATCH}/priors.pkl"
    if os.path.exists(cache):
        return pickle.load(open(cache, "rb"))
    lines = rb_game_lines(D)
    by_season_week = collections.defaultdict(list)
    for gid, g in D["games"].items():
        by_season_week[(g["season"], g["week"])].append(gid)
    seasons = sorted({g["season"] for g in D["games"].values()})
    pri = {}
    for yr in seasons:
        weeks = sorted({g["week"] for g in D["games"].values() if g["season"] == yr})
        prev = pri.get((yr - 1, 99)) if yr > seasons[0] else None   # end of last season
        for w in weeks + [99]:
            pl = collections.defaultdict(lambda: [0, 0.0, set()])    # player -> carries, yds, games
            df = collections.defaultdict(lambda: [0, 0.0, set()])    # defteam -> RB carries, yds, games
            tm = collections.defaultdict(lambda: [0, 0, set(), 0.0])  # team -> rush plays, plays, games, rb yds
            lg = [0, 0.0]
            for (gid, rusher), ev in lines.items():
                g = D["games"][gid]
                if g["season"] != yr or g["week"] >= w:
                    continue
                for gc, y, pos, deft in ev:
                    pl[rusher][0] += 1; pl[rusher][1] += y; pl[rusher][2].add(gid)
                    df[deft][0] += 1; df[deft][1] += y; df[deft][2].add(gid)
                    lg[0] += 1; lg[1] += y
            for gid, g in D["games"].items():
                if g["season"] != yr or g["week"] >= w:
                    continue
                for gc, pos, deft, kind, y, rusher, hs, as_ in g["plays"]:
                    if kind in ("pass", "rush", "qbrush"):
                        tm[pos][1] += 1; tm[pos][2].add(gid)
                        if kind == "rush":
                            tm[pos][0] += 1; tm[pos][3] += y
                for t in (g["home"], g["away"]):
                    tm[t][2].add(gid); df[t][2].add(gid)
            pri[(yr, w)] = dict(player={k: (v[0], v[1], len(v[2])) for k, v in pl.items()},
                                defense={k: (v[0], v[1], len(v[2])) for k, v in df.items()},
                                team={k: (v[0], v[1], len(v[2]), v[3]) for k, v in tm.items()},
                                league=tuple(lg), prev=prev)
    pickle.dump(pri, open(cache, "wb"))
    return pri

K_PRIOR = 60       # carries of prior-season evidence that a season-to-date YPC is worth at season start
LG_YPC = 4.3

def player_prior(pri, yr, w, pid):
    """-> (ypc_prior, carries_per_game, carries_seen) blending last season into this one."""
    P = pri[(yr, w)]
    c, y, g = P["player"].get(pid, (0, 0.0, 0))
    pc, py, pg = (P["prev"]["player"].get(pid, (0, 0.0, 0)) if P["prev"] else (0, 0.0, 0))
    # shrink: season-to-date carries + up to K_PRIOR carries of last season at its own ypc + 20 league carries
    wprev = min(pc, K_PRIOR)
    num = y + (py / pc * wprev if pc else 0) + 20 * LG_YPC
    den = c + wprev + 20
    ypc = num / den
    cpg_now = c / g if g else None
    cpg_prev = pc / pg if pg else None
    if cpg_now is None and cpg_prev is None:
        cpg = 0.0
    elif cpg_now is None:
        cpg = cpg_prev * 0.8
    elif cpg_prev is None:
        cpg = cpg_now
    else:
        cpg = (cpg_now * g + cpg_prev * 2) / (g + 2)   # two games of last season's role
    return ypc, cpg, c

def def_prior(pri, yr, w, team):
    """Opponent RB yards-per-carry allowed, shrunk to the league (40 carries prior)."""
    P = pri[(yr, w)]
    c, y, g = P["defense"].get(team, (0, 0.0, 0))
    pc, py, pg = (P["prev"]["defense"].get(team, (0, 0.0, 0)) if P["prev"] else (0, 0.0, 0))
    lg = P["league"][1] / P["league"][0] if P["league"][0] else LG_YPC
    wprev = min(pc, 80)
    return (y + (py / pc * wprev if pc else 0) + 40 * lg) / (c + wprev + 40), lg

def team_prior(pri, yr, w, team):
    """-> (rush plays per game, plays per game, RB ypc) entering the week, with last season as a 2-game prior."""
    P = pri[(yr, w)]
    r, n, g, ry = P["team"].get(team, (0, 0, 0, 0.0))
    pr_, pn, pg, pry = (P["prev"]["team"].get(team, (0, 0, 0, 0.0)) if P["prev"] else (0, 0, 0, 0.0))
    k = 2
    rpg = ((r + (pr_ / pg * k if pg else 26 * k)) / (g + k))
    ppg = ((n + (pn / pg * k if pg else 63 * k)) / (g + k))
    ypc = (ry + (pry / pr_ * 40 if pr_ else LG_YPC * 40)) / (r + 40)
    return rpg, ppg, ypc

# -------------------------------------------------------------------------- game state
def state_at(g, gc, team, pid):
    """Everything live-computable at game-clock gc for player pid on team."""
    car = yds = tcar = tyds = oc = oy = tplays = 0
    rb_car_others = 0
    hs = as_ = 0
    last_car_gc = 0
    big = 0
    for pgc, pos, deft, kind, y, rusher, h, a in g["plays"]:
        if pgc > gc:
            break
        hs, as_ = h, a
        if pos == team and kind in ("pass", "rush", "qbrush"):
            tplays += 1
            if kind == "rush":
                tcar += 1; tyds += y
                if rusher == pid:
                    car += 1; yds += y; last_car_gc = pgc
                    big += y >= 15
                else:
                    rb_car_others += 1
        elif pos != team and kind == "rush":
            oc += 1; oy += y
    home = g["home"] == team
    diff = (hs - as_) if home else (as_ - hs)
    return dict(car=car, yds=yds, tcar=tcar, tyds=tyds, tplays=tplays, others=rb_car_others,
                oc=oc, oy=oy, diff=diff, drought=(gc - last_car_gc) / 60, big=big)

def rest_of_game(g, gc, team, pid):
    rc = ry = 0
    mx = 0
    run = 0
    for pgc, pos, deft, kind, y, rusher, h, a in g["plays"]:
        if kind == "rush" and rusher == pid:
            if pgc > gc:
                rc += 1; ry += y
                run += y; mx = max(mx, run)
    return rc, ry, mx

def shrunk_excess(car, yds, ypc_prior, K):
    """Posterior-mean YPC minus the prior, K = prior strength in carries."""
    if car == 0:
        return 0.0
    return (K * ypc_prior + yds) / (K + car) - ypc_prior

# ------------------------------------------------------------------------ hypothesis
def stage_hyp(D, pri):
    print("\n==== Hypothesis: early YPC above the player's own season average ====")
    lines = rb_game_lines(D)
    checkpoints = [("5 carries", "car", 5), ("8 carries", "car", 8), ("10 carries", "car", 10),
                   ("end Q1", "gc", 900), ("halftime", "gc", 1800)]
    rows_all = {}
    for label, mode, val in checkpoints:
        rows = []
        for (gid, pid), ev in lines.items():
            g = D["games"][gid]
            yr, w = g["season"], g["week"]
            if yr == 2024 and w <= 4:
                continue   # no prior-season evidence loaded for 2024; skip its first weeks
            ypc0, cpg0, seen = player_prior(pri, yr, w, pid)
            if seen < 30 and not (yr > 2024 and pri[(yr, w)]["prev"] and pid in pri[(yr, w)]["prev"]["player"]):
                continue   # need a real season average to be "above"
            if cpg0 < 8:
                continue   # featured backs only (the prop-market population)
            team = ev[0][2]
            if mode == "car":
                if len(ev) < val + 1:
                    continue
                gc = ev[val - 1][0]
            else:
                gc = val
            st = state_at(g, gc, team, pid)
            if st["car"] < 3:
                continue
            rc, ry, _ = rest_of_game(g, gc, team, pid)
            if mode == "gc" and gc >= g["glen"]:
                continue
            dypc, lg = def_prior(pri, yr, w, ev[0][3])
            tr, tp, typc = team_prior(pri, yr, w, team)
            team_others_c = st["tcar"] - st["car"]
            team_others_y = st["tyds"] - st["yds"]
            rows.append(dict(gid=gid, pid=pid, yr=yr, w=w, gc=gc, car=st["car"], yds=st["yds"],
                             ypc0=ypc0, cpg0=cpg0, dypc=dypc - lg, diff=st["diff"],
                             ex_raw=st["yds"] / st["car"] - ypc0,
                             ex5=shrunk_excess(st["car"], st["yds"], ypc0, 5),
                             ex10=shrunk_excess(st["car"], st["yds"], ypc0, 10),
                             ex20=shrunk_excess(st["car"], st["yds"], ypc0, 20),
                             tex=((team_others_y / team_others_c) - typc) if team_others_c >= 3 else 0.0,
                             tex_all=((st["tyds"] / st["tcar"]) - typc) if st["tcar"] else 0.0,
                             rc=rc, ry=ry, rypc=(ry / rc if rc else np.nan),
                             spread=(g["spread"] if g["home"] == team else -g["spread"])))
        rows_all[label] = rows
        R = {k: np.array([r[k] for r in rows], dtype=float) for k in rows[0] if k not in ("gid", "pid")}
        n = len(rows)
        ngames = len({r["gid"] for r in rows})
        has = ~np.isnan(R["rypc"])
        print(f"\n-- {label}: {n} player-games, {ngames} games; mean carries so far {R['car'].mean():.1f}, "
              f"remaining carries {R['rc'].mean():.1f}, remaining ypc {np.nanmean(R['rypc']):.2f}")
        def corr(a, b):
            m = ~np.isnan(a) & ~np.isnan(b)
            return np.corrcoef(a[m], b[m])[0, 1], m.sum()
        def boot_corr(a, b, gids, B=400):
            m = ~np.isnan(a) & ~np.isnan(b)
            a, b, gids = a[m], b[m], gids[m]
            ug = np.array(sorted(set(gids)))
            idx_by = {g: np.where(gids == g)[0] for g in ug}
            out = []
            for _ in range(B):
                pick = rng.choice(ug, len(ug))
                ii = np.concatenate([idx_by[g] for g in pick])
                out.append(np.corrcoef(a[ii], b[ii])[0, 1])
            return np.percentile(out, [2.5, 97.5])
        gids = np.array([r["gid"] for r in rows])
        print("   correlation with  remaining YPC        remaining carries      remaining yards")
        for k, nm in (("ex_raw", "excess ypc raw"), ("ex5", "excess ypc K=5"), ("ex10", "excess ypc K=10"),
                      ("ex20", "excess ypc K=20"), ("tex", "team-others excess ypc"),
                      ("tex_all", "team excess ypc (all)"), ("ypc0", "season ypc prior"), ("gc", "game clock"),
                      ("dypc", "opp def ypc allowed"), ("diff", "score diff"), ("yds", "yards so far"),
                      ("car", "carries so far")):
            c1, n1 = corr(R[k], R["rypc"]); lo1, hi1 = boot_corr(R[k], R["rypc"], gids)
            c2, _ = corr(R[k], R["rc"]); lo2, hi2 = boot_corr(R[k], R["rc"], gids)
            c3, _ = corr(R[k], R["ry"]); lo3, hi3 = boot_corr(R[k], R["ry"], gids)
            print(f"   {nm:24s} r={c1:+.3f} [{lo1:+.2f},{hi1:+.2f}]   r={c2:+.3f} [{lo2:+.2f},{hi2:+.2f}]   "
                  f"r={c3:+.3f} [{lo3:+.2f},{hi3:+.2f}]")
        # partial effect: OLS of remaining ypc / carries / yards on controls +/- excess
        def ols(Xcols, yk, add):
            m = has if yk == "rypc" else np.ones(n, bool)
            cols = [c for c in Xcols + add if R[c].std() > 1e-9]
            add = [c for c in add if c in cols]
            Xcols = [c for c in cols if c not in add]
            X = np.column_stack([np.ones(m.sum())] + [R[c][m] for c in Xcols + add])
            yv = R[yk][m]
            beta, *_ = np.linalg.lstsq(X, yv, rcond=None)
            res = yv - X @ beta
            # cluster-robust SE by game
            XtX_inv = np.linalg.inv(X.T @ X)
            G = gids[m]
            meat = np.zeros((X.shape[1], X.shape[1]))
            for gg in set(G):
                sel = G == gg
                u = X[sel].T @ res[sel]
                meat += np.outer(u, u)
            V = XtX_inv @ meat @ XtX_inv
            r2 = 1 - res.var() / yv.var()
            se = np.sqrt(np.diag(V))
            names = ["const"] + Xcols + add
            return {nm_: (beta[i], se[i]) for i, nm_ in enumerate(names)}, r2
        ctrl = ["ypc0", "dypc", "diff", "spread", "car", "gc", "cpg0"]
        for yk, nm in (("rypc", "remaining YPC"), ("rc", "remaining carries"), ("ry", "remaining yards")):
            _, r20 = ols(ctrl, yk, [])
            for add in (["ex10"], ["tex"], ["ex10", "tex"]):
                co, r2 = ols(ctrl, yk, add)
                coefs = ", ".join(f"{a}={co[a][0]:+.3f} (se {co[a][1]:.3f}, t={co[a][0]/co[a][1]:+.1f})" for a in add)
                print(f"   OLS {nm:18s} controls R2 {r20:.3f} -> +{'+'.join(add):9s} R2 {r2:.3f}: {coefs}")
        # quintile table for the shrunk excess (K=10)
        q = np.quantile(R["ex10"], [0, .2, .4, .6, .8, 1])
        print("   excess-ypc(K=10) quintile:  n   mean excess  rem carries  rem ypc  rem yds")
        for i in range(5):
            m = (R["ex10"] >= q[i]) & (R["ex10"] <= q[i + 1]) if i == 4 else (R["ex10"] >= q[i]) & (R["ex10"] < q[i + 1])
            print(f"     q{i+1}: {m.sum():4d}  {R['ex10'][m].mean():+.2f}        {R['rc'][m].mean():5.1f}      "
                  f"{np.nanmean(R['rypc'][m]):.2f}    {R['ry'][m].mean():5.1f}")
    pickle.dump(rows_all, open(f"{SCRATCH}/hyp_rows.pkl", "wb"))

# -------------------------------------------------------------------- stage 1: carries
TRAIN = lambda g: g["season"] < 2026 or g["week"] <= 3      # the carries/ypc fits never see w4+ of 2026
GRID = range(300, 3301, 150)

def feats(g, gc, team, pid, pri, st=None):
    """Live-computable feature dict for the two-stage model."""
    yr, w = g["season"], g["week"]
    st = st or state_at(g, gc, team, pid)
    ypc0, cpg0, seen = player_prior(pri, yr, w, pid)
    dypc, lg = def_prior(pri, yr, w, g["away"] if g["home"] == team else g["home"])
    trpg, tppg, typc = team_prior(pri, yr, w, team)
    rem = max(3600 - gc, 60) / 3600
    mins = max(gc, 60) / 60
    share0 = cpg0 / max(trpg, 10)
    spread = g["spread"] if g["home"] == team else -g["spread"]
    d = st["diff"]
    return dict(lrem=math.log(rem), rem=rem, cpg0=cpg0, share0=share0, car=st["car"], yds=st["yds"],
                car_rate=st["car"] / mins, share_s=(st["car"] + 3 * share0) / (st["tcar"] + 3),
                tcar_rate=st["tcar"] / mins, pace=st["tplays"] / mins,
                trush=(st["tcar"] + 0.42 * 10) / (st["tplays"] + 10),
                diff=d, diff_rem=d * rem, trail8=float(d <= -8), trail47=float(-8 < d <= -4),
                lead47=float(4 <= d < 8), lead8=float(d >= 8), spread=spread, total=g["total"],
                drought=min(st["drought"], 30), others=st["others"] / (st["tcar"] + 1),
                ypc0=ypc0, dex=dypc - lg, ex10=shrunk_excess(st["car"], st["yds"], ypc0, 10),
                early=float(st["car"] < 8), big=st["big"], cum=st["yds"], seen=seen)

CAR_FEATS = ["lrem", "cpg0", "share0", "car", "car_rate", "share_s", "tcar_rate", "pace", "trush",
             "diff", "diff_rem", "trail8", "trail47", "lead47", "lead8", "spread", "total", "drought",
             "others", "ypc0", "ex10"]
YPC_FEATS = ["ypc0", "dex", "diff", "ex10", "car"]

def poisson_fit(X, y, l2=1e-3, iters=40):
    """Ridge Poisson regression (log link) by Newton's method; X already has a 1 column."""
    w = np.zeros(X.shape[1]); w[0] = math.log(max(y.mean(), 1e-3))
    for _ in range(iters):
        mu = np.exp(np.clip(X @ w, -10, 5))
        g = X.T @ (mu - y) / len(y) + l2 * w
        H = (X * mu[:, None]).T @ X / len(y) + l2 * np.eye(len(w))
        w -= np.linalg.solve(H, g)
    return w

def build_carry_rows(D, pri, which):
    """Checkpoint rows from the play log for players with a featured role (cpg0 >= 6)."""
    lines = rb_game_lines(D)
    rows = []
    for (gid, pid), ev in lines.items():
        g = D["games"][gid]
        if not which(g) or (g["season"] == 2024 and g["week"] <= 4):
            continue
        team = ev[0][2]
        ypc0, cpg0, seen = player_prior(pri, g["season"], g["week"], pid)
        if cpg0 < 6:
            continue
        for gc in GRID:
            if gc >= g["glen"]:
                break
            st = state_at(g, gc, team, pid)
            if st["car"] == 0 and gc > 900:
                continue   # not in the game (hurt / inactive): no quote would be live either
            rc, ry, _ = rest_of_game(g, gc, team, pid)
            f = feats(g, gc, team, pid, pri, st)
            f.update(gid=gid, pid=pid, rc=rc, ry=ry, gc=gc)
            rows.append(f)
    return rows

def stage_carries(D, pri):
    print("\n==== Stage 1: remaining carries (Poisson, log link) and per-carry yards ====")
    tr = build_carry_rows(D, pri, TRAIN)
    te = build_carry_rows(D, pri, lambda g: not TRAIN(g))
    def mat(rows, names):
        return np.column_stack([np.ones(len(rows))] + [np.array([r[k] for r in rows], float) for k in names])
    Xtr, Xte = mat(tr, CAR_FEATS), mat(te, CAR_FEATS)
    ytr, yte = np.array([r["rc"] for r in tr], float), np.array([r["rc"] for r in te], float)
    mu_, sd_ = Xtr[:, 1:].mean(0), Xtr[:, 1:].std(0) + 1e-9
    Ztr = np.column_stack([np.ones(len(tr)), (Xtr[:, 1:] - mu_) / sd_])
    Zte = np.column_stack([np.ones(len(te)), (Xte[:, 1:] - mu_) / sd_])
    w = poisson_fit(Ztr, ytr, l2=1e-3)
    mtr, mte = np.exp(Ztr @ w), np.exp(Zte @ w)
    naive_te = np.array([r["cpg0"] * r["rem"] for r in te])
    def r2(y, m):
        return 1 - ((y - m) ** 2).mean() / y.var()
    print(f"train rows {len(tr)} ({len({(r['gid'], r['pid']) for r in tr})} player-games, "
          f"{len({r['gid'] for r in tr})} games); test rows {len(te)} "
          f"({len({(r['gid'], r['pid']) for r in te})} player-games, {len({r['gid'] for r in te})} games, 2026 w4-7)")
    print(f"remaining carries: test R2 model {r2(yte, mte):.3f} vs naive cpg0*rem {r2(yte, naive_te):.3f}; "
          f"train R2 {r2(ytr, mtr):.3f}")
    # NB dispersion on train residuals: var = mu + a mu^2
    res2 = (ytr - mtr) ** 2
    a = max(0.0, float(((res2 - mtr) * mtr ** 2).sum() / (mtr ** 4).sum()))
    print(f"dispersion alpha {a:.3f} (var = mu + alpha mu^2); test check: "
          f"mean (y-mu)^2 {((yte - mte) ** 2).mean():.2f} vs predicted {(mte + a * mte ** 2).mean():.2f}")
    print("standardized coefficients:")
    for f_, c in sorted(zip(CAR_FEATS, w[1:]), key=lambda x: -abs(x[1])):
        print(f"  {f_:10s} {c:+.3f}")
    # per-carry yards: weighted OLS of remaining ypc on YPC_FEATS, weight = remaining carries
    def ypc_fit(rows):
        R = [r for r in rows if r["rc"] > 0]
        X = mat(R, YPC_FEATS)
        y = np.array([r["ry"] / r["rc"] for r in R]); wt = np.array([r["rc"] for r in R], float)
        sw = np.sqrt(wt)
        beta, *_ = np.linalg.lstsq(X * sw[:, None], y * sw, rcond=None)
        return beta
    beta = ypc_fit(tr)
    print("per-carry yards (weighted OLS): " + ", ".join(f"{n}={b:+.3f}" for n, b in zip(["const"] + YPC_FEATS, beta)))
    Rte = [r for r in te if r["rc"] > 0]
    yv = np.array([r["ry"] / r["rc"] for r in Rte]); wt = np.array([r["rc"] for r in Rte], float)
    pv = mat(Rte, YPC_FEATS) @ beta
    base = np.array([r["ypc0"] for r in Rte])
    def wr2(y, m):
        return 1 - (wt * (y - m) ** 2).sum() / (wt * (y - np.average(y, weights=wt)) ** 2).sum()
    print(f"remaining ypc test (carry-weighted) R2: model {wr2(yv, pv):.4f}, season prior alone {wr2(yv, base):.4f}")
    # per-carry residual distribution (empirical, from the train seasons)
    carries = np.array([y for (gid, pid), ev in rb_game_lines(D).items() if TRAIN(D["games"][gid]) for _, y, _, _ in ev])
    print(f"per-carry yards (train): n {len(carries)}, mean {carries.mean():.2f}, sd {carries.std():.2f}, "
          f"P(20+) {(carries >= 20).mean():.3f}, P(<=0) {(carries <= 0).mean():.3f}")
    # does total remaining yards come out right? test: mean/var of ry vs model
    mu_y = mte * (mat(te, YPC_FEATS) @ beta)
    ry = np.array([r["ry"] for r in te])
    var_pred = mte * carries.var() + (mte + a * mte ** 2) * (mat(te, YPC_FEATS) @ beta) ** 2
    print(f"remaining yards test: R2 {r2(ry, mu_y):.3f}; mean sq err {((ry - mu_y) ** 2).mean():.0f} vs predicted var {var_pred.mean():.0f}")
    # game-level ypc effect: the leftover variance on TRAIN beyond carry noise + carry-count noise,
    # as var += (m * tau)^2 (a random per-game shift of the player's true ypc)
    yp_tr = mat(tr, YPC_FEATS) @ beta
    ry_tr = np.array([r["ry"] for r in tr])
    base_var = mtr * carries.var() + (mtr + a * mtr ** 2) * yp_tr ** 2
    tau2 = max(0.0, float((((ry_tr - mtr * yp_tr) ** 2 - base_var) * mtr ** 2).sum() / (mtr ** 4).sum()))
    print(f"game-level ypc sd tau {math.sqrt(tau2):.2f} yards/carry (from train residuals)")
    # tail check: standardized residuals, on TRAIN rows too (the normal-vs-simulation choice is made there)
    for lbl, rows_, mm in (("train", tr, mtr), ("test", te, mte)):
        yp = mat(rows_, YPC_FEATS) @ beta
        mu_y_ = mm * yp
        ry_ = np.array([r["ry"] for r in rows_])
        vp = mm * carries.var() + (mm + a * mm ** 2) * yp ** 2 + tau2 * mm ** 2
        z = (ry_ - mu_y_) / np.sqrt(vp)
        print(f"{lbl}: standardized residual quantiles (normal: -1.64,-0.67,0,0.67,1.64): "
              + ", ".join(f"{q:+.2f}" for q in np.quantile(z, [.05, .25, .5, .75, .95]))
              + f"; P(z>1.64) {(z > 1.64).mean():.3f}, P(z<-1.64) {(z < -1.64).mean():.3f}; "
              f"mean sq err {((ry_ - mu_y_) ** 2).mean():.0f} vs predicted var {vp.mean():.0f}")
    M = dict(w=w.tolist(), mu=mu_.tolist(), sd=sd_.tolist(), alpha=a, beta=beta.tolist(), tau=math.sqrt(tau2),
             carry_var=float(carries.var()), carry_dist=np.bincount(np.clip(carries, -15, 60).astype(int) + 15).tolist(),
             car_feats=CAR_FEATS, ypc_feats=YPC_FEATS)
    json.dump(M, open(f"{SCRATCH}/rush_two_stage.json", "w"), indent=1)
    return M

# ----------------------------------------------------------------------- quotes
TEAM_CODE = {"Arizona Cardinals": "ARI", "Atlanta Falcons": "ATL", "Baltimore Ravens": "BAL", "Buffalo Bills": "BUF",
             "Carolina Panthers": "CAR", "Chicago Bears": "CHI", "Cincinnati Bengals": "CIN", "Cleveland Browns": "CLE",
             "Dallas Cowboys": "DAL", "Denver Broncos": "DEN", "Detroit Lions": "DET", "Green Bay Packers": "GB",
             "Houston Texans": "HOU", "Indianapolis Colts": "IND", "Jacksonville Jaguars": "JAX", "Kansas City Chiefs": "KC",
             "Las Vegas Raiders": "LV", "Los Angeles Chargers": "LAC", "Los Angeles Rams": "LA", "Miami Dolphins": "MIA",
             "Minnesota Vikings": "MIN", "New England Patriots": "NE", "New Orleans Saints": "NO", "New York Giants": "NYG",
             "New York Jets": "NYJ", "Philadelphia Eagles": "PHI", "Pittsburgh Steelers": "PIT", "San Francisco 49ers": "SF",
             "Seattle Seahawks": "SEA", "Tampa Bay Buccaneers": "TB", "Tennessee Titans": "TEN", "Washington Commanders": "WAS"}
ESPN2NFLV = {"LAR": "LA", "WSH": "WAS"}

def name_index(D):
    import re
    import unicodedata
    def norm(n):
        n = unicodedata.normalize("NFKD", n).encode("ascii", "ignore").decode()
        n = re.sub(r"[.'’]", "", n).lower()
        return " ".join(w for w in n.split() if w not in ("jr", "sr", "ii", "iii", "iv", "v"))
    idx = collections.defaultdict(list)
    for pid, nm in D["names"].items():
        if " " in nm:
            idx[norm(nm)].append(pid)
    def lookup(name, team=None):
        c = idx.get(norm(name), [])
        if len(c) > 1 and team:
            c = [p for p in c if D["pos"].get(p)] or c
        return c[0] if len(c) == 1 else (c[0] if c else None)
    return lookup

def gc_of_wall(g, iso):
    import bisect, datetime
    t = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    pts = [(datetime.datetime.fromisoformat(a.replace("Z", "+00:00")).timestamp(), b) for a, b in g["wall"]]
    i = bisect.bisect_right(pts, (t, float("inf"))) - 1
    return 0 if i < 0 else min(pts[i][1], g["glen"])

def espn_game(espn_id, label, week, D):
    """Build a play log like load_pbp's for a game pbp_2026 lacks, from ESPN (cached)."""
    cache = f"{SCRATCH}/espn_{espn_id}.json"
    if os.path.exists(cache):
        tl = json.load(open(cache))
    else:
        sys.path.insert(0, f"{ROOT}/live")
        import poller
        pl, team = poller.espn_timeline(espn_id)
        tl = {"players": pl, "team": team}
        json.dump(tl, open(cache, "w"))
    away, home = [ESPN2NFLV.get(t, t) for t in label.split(" @ ")]
    gid = f"2026_{week:02d}_{away}_{home}"
    sys.path.insert(0, f"{ROOT}/live")
    import poller
    roster = {pid: nm for pid, nm in D["names"].items()}
    by_team = {home: [p for p, nm in roster.items()], away: None}
    rushers = {}
    for ts, gc, kind, short, yds, ok in tl["players"]:
        if kind == "rush" and short not in rushers:
            m = [pid for pid, nm in roster.items() if poller.same_player(short, nm) and D["pos"].get(pid)]
            rushers[short] = m[0] if len(m) == 1 else (next((p for p in m if D["pos"].get(p) in ("RB", "FB")), m[0]) if m else None)
    prow = {(gc, yds): short for ts, gc, kind, short, yds, ok in tl["players"] if kind == "rush"}
    plays = []
    for ts, gc, off, kind, yds, completed, hs, as_ in tl["team"]:
        team = TEAM_CODE.get(off)
        deft = away if team == home else home
        if kind == "rush":
            short = prow.get((gc, yds))
            pid = rushers.get(short)
            if pid is None or D["pos"].get(pid) == "QB":
                plays.append((gc, team, deft, "qbrush", yds, None, hs, as_))
            else:
                plays.append((gc, team, deft, "rush", yds, pid, hs, as_))
        else:
            plays.append((gc, team, deft, "pass", yds, None, hs, as_))
    plays.sort(key=lambda r: r[0])
    sp = {}
    for r in csv.DictReader(open(f"{ROOT}/data/games.csv")):
        if r["season"] == "2026" and r["home_team"] == home and r["away_team"] == away:
            sp = r
    g = dict(season=2026, week=week, home=home, away=away, spread=fnum(sp.get("spread_line")),
             total=fnum(sp.get("total_line")), glen=max(p[0] for p in plays), plays=plays, gid=gid, wall=[], espn=True)
    return gid, g

def stage_quotes(D, pri):
    """-> rows: dict(src, gid, pid, name, gc, L, price, team, cum_src, final)"""
    print("\n==== Quotes ====")
    lookup = name_index(D)
    rows, unmatched = [], collections.Counter()
    by_teams = {(g["home"], g["away"]): gid for gid, g in D["games"].items() if g["season"] == 2026}
    # weeks 4-6: raw odds snapshots
    for ev in sorted(os.listdir(f"{ROOT}/data/props")):
        fs = sorted(f for f in os.listdir(f"{ROOT}/data/props/{ev}") if f.startswith("player_rush_yds"))
        if not fs:
            continue
        first = json.load(open(f"{ROOT}/data/props/{ev}/{fs[0]}"))["data"]
        home, away = TEAM_CODE[first["home_team"]], TEAM_CODE[first["away_team"]]
        gid = by_teams.get((home, away))
        if not gid:
            print(f"  no pbp game for {away} @ {home}"); continue
        g = D["games"][gid]
        if g["week"] >= 7:
            continue
        for fn in fs:
            j = json.load(open(f"{ROOT}/data/props/{ev}/{fn}"))
            gc = gc_of_wall(g, j["timestamp"])
            if gc <= 0:
                continue
            seen = {}
            for bk in sorted(j["data"].get("bookmakers", []), key=lambda b: b["key"] != "fanduel"):
                for mk in bk.get("markets", []):
                    if mk["key"] != "player_rush_yds":
                        continue
                    for o in mk["outcomes"]:
                        if o["name"] == "Over" and o["description"] not in seen:
                            seen[o["description"]] = (o["point"], o["price"], bk["key"])
            for nm, (L, price, bk) in seen.items():
                pid = lookup(nm)
                if not pid:
                    unmatched[nm] += 1; continue
                rows.append(dict(src=f"w{g['week']}", wk=g["week"], gid=gid, pid=pid, name=nm, gc=gc, L=L,
                                 price=price, book=bk, cum_src=None, final=None, espn_id=ev))
    # week 7 and 8 archives
    for wk, fn in ((7, "props_w7.json"), (8, "props_w8.json")):
        arch = json.load(open(f"{ROOT}/data/fly/docs/archive/{fn}"))
        for key, prop in arch.items():
            if prop["market"] != "rush":
                continue
            espn_id = key.split("|")[0]
            away, home = [ESPN2NFLV.get(t, t) for t in prop["game"].split(" @ ")]
            gid = by_teams.get((home, away))
            if not gid:
                try:
                    gid, g = espn_game(espn_id, prop["game"], wk, D)
                except Exception as e:
                    print(f"  ESPN rebuild failed for {prop['game']}: {e!r}"); continue
                D["games"][gid] = g
                by_teams[(home, away)] = gid
            pid = lookup(prop["player"])
            if not pid:
                unmatched[prop["player"]] += 1; continue
            seen = set()
            for gc, cum, nev, L, P in prop["snaps"]:
                if L is None or gc <= 0 or gc in seen:
                    continue
                seen.add(gc)
                rows.append(dict(src=f"w{wk}", wk=wk, gid=gid, pid=pid, name=prop["player"], gc=gc, L=L, price=-110,
                                 book="archive", cum_src=cum, nev_src=nev, final=prop["final"], p_arch=P, l0=prop.get("l0"),
                                 espn_id=espn_id))
    # finals from the play log (all sources), team, and a consistency check vs the archive
    lines = rb_game_lines(D)
    out = []
    for r in rows:
        g = D["games"][r["gid"]]
        ev = lines.get((r["gid"], r["pid"]))
        if not ev:
            unmatched[f"no carries: {r['name']} {r['gid']}"] += 1; continue
        r["team"] = ev[0][2]
        fin_pbp = sum(y for _, y, _, _ in ev)
        if r["final"] is None:
            r["final"] = fin_pbp
        r["final_pbp"] = fin_pbp
        r["max_after"] = None
        out.append(r)
    print(f"{len(out)} quotes; unmatched: {dict(unmatched)}")
    for src in sorted({r["src"] for r in out}):
        R = [r for r in out if r["src"] == src]
        pg = {(r["gid"], r["pid"]) for r in R}
        print(f"  {src}: {len(R)} quotes, {len(pg)} player-games, {len({r['gid'] for r in R})} games")
    mism = [r for r in out if r["final"] is not None and abs(r["final"] - r["final_pbp"]) > 0.5]
    if mism:
        print("  final mismatch archive vs pbp:", sorted({(r["name"], r["final"], r["final_pbp"]) for r in mism}))
    pickle.dump(out, open(f"{SCRATCH}/quotes.pkl", "wb"))
    pickle.dump({gid: g for gid, g in D["games"].items() if g.get("espn")}, open(f"{SCRATCH}/espn_games.pkl", "wb"))
    return out

# ------------------------------------------------------------------------- scoring
def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def two_stage(M, f):
    """-> (mean remaining carries, per-carry mean, remaining-yards mean, sd)"""
    z = (np.array([f[k] for k in M["car_feats"]]) - M["mu"]) / M["sd"]
    m = float(np.exp(np.clip(M["w"][0] + z @ M["w"][1:], -10, 5)))
    ypc = float(M["beta"][0] + sum(b * f[k] for b, k in zip(M["beta"][1:], M["ypc_feats"])))
    ypc = max(ypc, 2.0)
    mean = m * ypc
    var = m * M["carry_var"] + (m + M["alpha"] * m * m) * ypc * ypc + (M["tau"] * m) ** 2
    return m, ypc, mean, math.sqrt(var)

def p_over(mean, sd, needed):
    return 1 - norm_cdf((needed - 0.5 - mean) / sd)

def simulate_p(M, m, ypc, needed, S=4000, seed=0):
    """Compound simulation (the primary scorer): N ~ NB(m, alpha); yards = sum of N draws from the
    empirical per-carry distribution shifted to mean ypc, plus a game-level ypc shift ~ N(0, tau).
    Deterministic per (seed, inputs) so a rerun gives the same P."""
    g = np.random.default_rng(abs(hash((seed, round(m, 3), round(ypc, 3), round(needed, 1)))) % (2 ** 32))
    dist = np.array(M["carry_dist"], float); vals = np.arange(len(dist)) - 15
    pr = dist / dist.sum()
    shift = ypc - (pr * vals).sum()
    a = M["alpha"]
    lam = g.gamma(1 / a, a * m, S) if a > 0 else np.full(S, m)
    N = g.poisson(lam)
    nmax = int(N.max()) if N.max() > 0 else 1
    draws = g.choice(vals, size=(S, nmax), p=pr) + shift + (M["tau"] * g.standard_normal(S))[:, None]
    cs = np.concatenate([np.zeros((S, 1)), np.cumsum(draws, 1)], 1)
    tot = cs[np.arange(S), N]
    return float((tot > needed - 0.5).mean())

def def_form_top3(D, week):
    """Top-3 run defenses by RB rush yards allowed per game over each team's last 3 games
    entering `week` of 2026 (the production veto, recomputed point-in-time from the play log)."""
    per = collections.defaultdict(list)
    for gid, g in D["games"].items():
        if g["season"] != 2026 or g["week"] >= week:
            continue
        tot = collections.Counter()
        for gc, pos, deft, kind, y, rusher, hs, as_ in g["plays"]:
            if kind == "rush":
                tot[deft] += y
        for t in (g["home"], g["away"]):
            per[t].append((g["week"], tot[t]))
    form = {t: np.mean([y for _, y in sorted(v)[-3:]]) for t, v in per.items()}
    return set(sorted(form, key=form.get)[:3])

def stage_eval(D, pri, M, Q):
    print("\n==== Evaluation on live quotes ====")
    print("Pre-chosen protocol: P_model (pure two-stage, compound simulation), call = first quote in a player-game with "
          "P >= 0.60 inside gc 600-3060 passing the production gate (diff >= -3 and (3+ carries or cum >= 60% of line)) "
          "and the top-3 run-defense veto. Secondary (also pre-chosen): 50/50 blend of the model mean with the market's "
          "implied remaining yards (line - cum). Other thresholds shown for sensitivity only.")
    old = json.load(open(f"{ROOT}/data/week7/oos_preds.json"))["rush"]
    old_p = {(m[1], m[2], float(m[4])): p for p, m in zip(old["p"], old["meta"])}
    vetos = {wk: def_form_top3(D, wk) for wk in range(4, 9)}
    lines = rb_game_lines(D)
    # stale quotes (line already beaten, or within 3 yards of it, by the play log OR the archive's own
    # box-score yards) are fake free wins: v2_model drops them and so do we. Also report the lag between
    # the archive's yards (ESPN box at poll time) and the play log at the same game clock.
    lag = []
    keep = []
    for r in Q:
        g = D["games"][r["gid"]]
        st = state_at(g, r["gc"], r["team"], r["pid"])
        if r.get("cum_src") is not None:
            lag.append(st["yds"] - r["cum_src"])
        if r["L"] - st["yds"] < 3 or (r.get("cum_src") is not None and r["L"] - r["cum_src"] < 3):
            continue
        keep.append(r)
    lag = np.array(lag)
    print(f"dropped {len(Q) - len(keep)} stale quotes (needed < 3); archive yards vs play log at the same gc: "
          f"mean {lag.mean():+.1f}, |diff| > 5 in {(np.abs(lag) > 5).mean():.1%} of archive quotes (n={len(lag)})")
    Q = keep
    for r in Q:
        g = D["games"][r["gid"]]
        st = state_at(g, r["gc"], r["team"], r["pid"])
        f = feats(g, r["gc"], r["team"], r["pid"], pri, st)
        r["f"] = f
        r["cum"] = st["yds"]; r["car"] = st["car"]; r["diff"] = st["diff"]
        m, ypc, mean, sd = two_stage(M, f)
        needed = r["L"] - r["cum"]
        r["needed"] = needed
        r["m"], r["ypc"], r["mean"], r["sd"] = m, ypc, mean, sd
        r["p_norm"] = p_over(mean, sd, needed)
        r["p_model"] = simulate_p(M, m, ypc, needed)                    # primary: compound simulation
        r["p_blend"] = simulate_p(M, m * max(0.5 * mean + 0.5 * needed, 0.5) / max(mean, 1e-6), ypc, needed)
        for sd_ in range(1, 6):   # Monte Carlo sensitivity of the call record
            r[f"p_seed{sd_}"] = simulate_p(M, m, ypc, needed, 2000, seed=sd_)
        ypg0 = f["cpg0"] * f["ypc0"]
        r["p_pace"] = p_over(ypg0 * f["rem"], sd, needed)
        r["p_old"] = old_p.get((r["gid"], r["name"], float(r["gc"])))
        r["y"] = 1.0 if r["final"] > r["L"] else 0.0
        ev = lines[(r["gid"], r["pid"])]
        run = 0; mx = 0
        for gc_, y_, _, _ in ev:
            run += y_
            if gc_ > r["gc"]:
                mx = max(mx, run)
        r["y_live"] = 1.0 if max(mx, r["final"]) > r["L"] else 0.0
        opp = g["away"] if g["home"] == r["team"] else g["home"]
        r["gate"] = r["diff"] >= -3 and (r["car"] >= 3 or r["cum"] >= 0.6 * r["L"])
        r["veto"] = opp in vetos.get(r["wk"], set())
        r["inwin"] = 600 <= r["gc"] <= 3060
    pickle.dump(Q, open(f"{SCRATCH}/scored.pkl", "wb"))

    def wauc(rows, key):
        rows = [r for r in rows if r.get(key) is not None]
        if not rows:
            return float("nan"), 0
        n = collections.Counter((r["gid"], r["pid"]) for r in rows)
        p = np.array([r[key] for r in rows]); y = np.array([r["y"] for r in rows])
        w = np.array([1 / n[(r["gid"], r["pid"])] for r in rows])
        pos, neg = y == 1, y == 0
        if not pos.any() or not neg.any():
            return float("nan"), len(rows)
        # weighted AUC: P(p_pos > p_neg) with weights
        order = np.argsort(p)
        ps, ys, ws = p[order], y[order], w[order]
        cw_neg = np.cumsum(ws * (ys == 0)) - ws * (ys == 0)
        # ties get half credit
        tot = 0.0
        i = 0
        while i < len(ps):
            j = i
            while j < len(ps) and ps[j] == ps[i]:
                j += 1
            neg_before = cw_neg[i]
            neg_tie = (ws[i:j] * (ys[i:j] == 0)).sum()
            for k in range(i, j):
                if ys[k] == 1:
                    tot += ws[k] * (neg_before + 0.5 * neg_tie)
            i = j
        return tot / ((w * pos).sum() * (w * neg).sum()), len(rows)

    def wbrier(rows, key):
        rows = [r for r in rows if r.get(key) is not None]
        n = collections.Counter((r["gid"], r["pid"]) for r in rows)
        w = np.array([1 / n[(r["gid"], r["pid"])] for r in rows])
        p = np.array([r[key] for r in rows]); y = np.array([r["y"] for r in rows])
        return float((w * (p - y) ** 2).sum() / w.sum())

    def calls(rows, key, thr, gate=True, veto=True, window=True):
        first = {}
        for r in sorted(rows, key=lambda r: r["gc"]):
            k = (r["gid"], r["pid"])
            if k in first or r.get(key) is None or r[key] < thr:
                continue
            if window and not r["inwin"]:
                continue
            if gate and not r["gate"]:
                continue
            if veto and r["veto"]:
                continue
            first[k] = r
        return list(first.values())

    def units(cs, live=False):
        u = 0.0
        for r in cs:
            won = r["y_live"] if live else r["y"]
            pr = r["price"]
            u += (pr / 100 if pr > 0 else 100 / -pr) if won else -1.0
        return u

    def summarize(rows, label):
        print(f"\n--- {label}: {len(rows)} quotes, {len({(r['gid'], r['pid']) for r in rows})} player-games, "
              f"{len({r['gid'] for r in rows})} games; over rate (player-game weighted) "
              f"{np.average([r['y'] for r in rows], weights=[1 / collections.Counter((q['gid'], q['pid']) for q in rows)[(r['gid'], r['pid'])] for r in rows]):.2f}")
        for key, nm in (("p_model", "two-stage (simulation)"), ("p_norm", "two-stage (normal approx)"),
                        ("p_blend", "50/50 blend w/ line"), ("p_pace", "season pace"),
                        ("p_old", "existing logistic (LOGO)")):
            a, n = wauc(rows, key)
            if n == 0:
                continue
            print(f"  {nm:26s} AUC {a:.3f}  Brier {wbrier(rows, key):.3f}  (n={n})"
                  + ("  [market 'line is right' Brier = " + f"{wbrier(rows, 'p_half'):.3f}]" if key == "p_model" else ""))
        for key, nm in (("p_model", "two-stage"), ("p_blend", "blend"), ("p_old", "old logistic")):
            for thr in (0.55, 0.60, 0.65):
                for gv, tag in (((True, True), "gate+veto"), ((False, False), "no gate")):
                    cs = calls(rows, key, thr, *gv)
                    if not cs:
                        print(f"  {nm:12s} P>={thr:.2f} {tag:9s}: no calls"); continue
                    wn = sum(r["y"] for r in cs); wl = sum(r["y_live"] for r in cs)
                    print(f"  {nm:12s} P>={thr:.2f} {tag:9s}: {wn:.0f}-{len(cs) - wn:.0f} final>line "
                          f"({wl:.0f}-{len(cs) - wl:.0f} live-win), {units(cs):+.1f} u ({units(cs, True):+.1f} u live)"
                          + (" <= PRE-CHOSEN" if key == "p_model" and thr == 0.60 and tag == "gate+veto" else ""))
        return rows

    for r in Q:
        r["p_half"] = 0.5
    srcs = sorted({r["src"] for r in Q})
    hist = [r for r in Q if r["wk"] <= 6]
    summarize(hist, "FIT CHECK weeks 4-6 (raw odds, actual prices)")
    # calibration table on w4-6
    print("  reliability (w4-6, player-game weighted): P bin -> observed over rate")
    n = collections.Counter((r["gid"], r["pid"]) for r in hist)
    for lo, hi in ((0, .3), (.3, .4), (.4, .5), (.5, .6), (.6, .7), (.7, 1.01)):
        R = [r for r in hist if lo <= r["p_model"] < hi]
        if R:
            w = np.array([1 / n[(r["gid"], r["pid"])] for r in R])
            print(f"    [{lo:.1f},{hi:.1f}) n={len(R):4d} wt={w.sum():5.1f}  obs {np.average([r['y'] for r in R], weights=w):.2f}  "
                  f"mean P {np.mean([r['p_model'] for r in R]):.2f}")
    # model vs market vs truth: is the model's optimism a model bias or a market shading?
    def bias(rows, label):
        R = [r for r in rows if r["inwin"]]
        n = collections.Counter((r["gid"], r["pid"]) for r in R)
        w = np.array([1 / n[(r["gid"], r["pid"])] for r in R])
        act = np.array([r["final"] - r["cum"] for r in R]); mod = np.array([r["mean"] for r in R])
        mkt = np.array([r["needed"] for r in R]); pace = np.array([r["f"]["cpg0"] * r["f"]["ypc0"] * r["f"]["rem"] for r in R])
        print(f"  {label}: actual remaining {np.average(act, weights=w):.1f}, model {np.average(mod, weights=w):.1f}, "
              f"market needed {np.average(mkt, weights=w):.1f}, season pace {np.average(pace, weights=w):.1f}  "
              f"(player-game weighted, in window; P(model above line) {np.average(mod > mkt, weights=w):.2f}; "
              f"mean abs err model {np.average(np.abs(act - mod), weights=w):.1f} vs market {np.average(np.abs(act - mkt), weights=w):.1f})")
    bias(hist, "w4-6 bias check")
    def gate_only(rows, label):
        first = {}
        for r in sorted(rows, key=lambda r: r["gc"]):
            k = (r["gid"], r["pid"])
            if k not in first and r["inwin"] and r["gate"] and not r["veto"]:
                first[k] = r
        cs = list(first.values())
        wn = sum(r["y"] for r in cs)
        print(f"  {label} gate-only baseline (first in-window quote passing gate+veto, P ignored): "
              f"{wn:.0f}-{len(cs) - wn:.0f}, {units(cs):+.1f} u")
        for key, nm in (("p_pace", "season pace"), ("p_norm", "normal approx")):
            cs = calls(rows, key, 0.60)
            wn = sum(r["y"] for r in cs)
            print(f"  {label} {nm} P>=0.60 gate+veto: {wn:.0f}-{len(cs) - wn:.0f}, {units(cs):+.1f} u")
    gate_only(hist, "w4-6")
    # simulation tail check on w4-6 + w7 at a sample of quotes
    samp = [r for r in Q if r["inwin"]]
    ps = np.array([r["p_model"] for r in samp])
    pn = np.array([r["p_norm"] for r in samp])
    print(f"  normal approx vs compound simulation on {len(samp)} quotes: mean |diff| {np.abs(ps - pn).mean():.3f}, "
          f"max {np.abs(ps - pn).max():.3f}; at P_normal>=0.6 sim mean {ps[pn >= .6].mean() if (pn >= .6).any() else float('nan'):.3f} "
          f"vs normal {pn[pn >= .6].mean() if (pn >= .6).any() else float('nan'):.3f}")
    w7 = [r for r in Q if r["wk"] == 7]
    summarize(w7, "FINAL CHECK week 7 archive (assumed -110)")
    bias(w7, "w7 bias check")
    gate_only(w7, "w7")
    print("  reliability (w7, player-game weighted): P bin -> observed over rate")
    n7 = collections.Counter((r["gid"], r["pid"]) for r in w7)
    for lo, hi in ((0, .3), (.3, .4), (.4, .5), (.5, .6), (.6, .7), (.7, 1.01)):
        R = [r for r in w7 if lo <= r["p_model"] < hi]
        if R:
            w = np.array([1 / n7[(r["gid"], r["pid"])] for r in R])
            print(f"    [{lo:.1f},{hi:.1f}) n={len(R):4d} wt={w.sum():5.1f}  obs {np.average([r['y'] for r in R], weights=w):.2f}  "
                  f"mean P {np.mean([r['p_model'] for r in R]):.2f}")
    w8 = [r for r in Q if r["wk"] == 8]
    if w8:
        summarize(w8, "FINAL CHECK week 8 CAR @ GB (assumed -110)")
    allq = [r for r in Q]
    summarize(allq, "POOLED weeks 4-8")
    bias(allq, "pooled bias check")
    gate_only(allq, "pooled")

    # uncertainty: cluster bootstrap by game on the pre-chosen protocol, pooled and w7
    def boot(rows, key="p_model", thr=0.60, B=1000):
        gids = sorted({r["gid"] for r in rows})
        byg = {g: [r for r in rows if r["gid"] == g] for g in gids}
        out = []
        for _ in range(B):
            pick = rng.choice(gids, len(gids))
            cs = []
            for i, g in enumerate(pick):
                for r in calls(byg[g], key, thr):
                    cs.append(r)
            out.append(units(cs) if cs else 0.0)
        return np.percentile(out, [2.5, 50, 97.5])
    def shuffled(rows, key="p_model", thr=0.60, B=300):
        """Null: finals reassigned among player-games of the same week; calls re-made on the same quotes."""
        pg = sorted({(r["gid"], r["pid"]) for r in rows})
        fin = {k: next(r["final"] for r in rows if (r["gid"], r["pid"]) == k) for k in pg}
        byw = collections.defaultdict(list)
        for k in pg:
            byw[next(r["wk"] for r in rows if (r["gid"], r["pid"]) == k)].append(k)
        out, aucs = [], []
        for _ in range(B):
            perm = {}
            for wk, ks in byw.items():
                vals = [fin[k] for k in ks]
                rng.shuffle(vals)
                perm.update(zip(ks, vals))
            tmp = []
            for r in rows:
                q = dict(r); q["final"] = perm[(r["gid"], r["pid"])]
                q["y"] = q["y_live"] = 1.0 if q["final"] > q["L"] else 0.0
                tmp.append(q)
            cs = calls(tmp, key, thr)
            out.append(units(cs) if cs else 0.0)
            aucs.append(wauc(tmp, key)[0])
        return np.percentile(out, [2.5, 50, 97.5]), np.percentile(aucs, [2.5, 50, 97.5])
    print("\n  Monte Carlo sensitivity of the pre-chosen record (5 other simulation seeds, S=2000):")
    for label, rows in (("weeks 4-6", hist), ("week 7", w7), ("pooled 4-8", allq)):
        recs = []
        for sd_ in range(1, 6):
            cs = calls(rows, f"p_seed{sd_}", 0.60)
            recs.append(f"{sum(r['y'] for r in cs):.0f}-{len(cs) - sum(r['y'] for r in cs):.0f} ({units(cs):+.1f}u)")
        print(f"    {label}: " + ", ".join(recs))
    print("\n  reliability restricted to gate+veto-passing in-window quotes (what the protocol actually bets on):")
    for label, rows in (("w4-6", hist), ("w7", w7)):
        G = [r for r in rows if r["inwin"] and r["gate"] and not r["veto"]]
        ng = collections.Counter((r["gid"], r["pid"]) for r in G)
        for lo, hi in ((0, .5), (.5, .6), (.6, .7), (.7, 1.01)):
            R = [r for r in G if lo <= r["p_model"] < hi]
            if R:
                w = np.array([1 / ng[(r["gid"], r["pid"])] for r in R])
                print(f"    {label} [{lo:.1f},{hi:.1f}) n={len(R):4d} wt={w.sum():5.1f}  obs {np.average([r['y'] for r in R], weights=w):.2f}  "
                      f"mean P {np.mean([r['p_model'] for r in R]):.2f}")
    for label, rows in (("weeks 4-6", hist), ("week 7", w7), ("pooled 4-8", allq)):
        cs = calls(rows, "p_model", 0.60)
        lo, md, hi = boot(rows)
        (slo, smd, shi), (alo, amd, ahi) = shuffled(rows)
        print(f"\n  {label}: pre-chosen protocol {sum(r['y'] for r in cs):.0f}-{len(cs) - sum(r['y'] for r in cs):.0f}, "
              f"{units(cs):+.1f} u; game-bootstrap 95% [{lo:+.1f}, {hi:+.1f}]; "
              f"shuffled-final null units [{slo:+.1f}, {shi:+.1f}] (median {smd:+.1f}), null AUC [{alo:.2f}, {ahi:.2f}]")
    # the calls themselves
    print("\n  pre-chosen calls (pooled):")
    for r in sorted(calls(allq, "p_model", 0.60), key=lambda r: (r["wk"], r["gid"], r["gc"])):
        print(f"    {r['src']} {r['gid']:18s} {r['name']:20s} gc {int(r['gc']):4d} car {r['car']:2d} cum {r['cum']:4.0f} "
              f"line {r['L']:5.1f} P {r['p_model']:.2f} (blend {r['p_blend']:.2f}) mean rem {r['mean']:.0f}±{r['sd']:.0f} "
              f"carries {r['m']:.1f}x{r['ypc']:.2f} final {r['final']:.0f} {'WIN' if r['y'] else 'loss'}")
    # hypothesis part (c): does excess ypc add on quotes, controlling for the line?
    print("\n  (c) excess YPC on quotes: logistic of y on [p_model logit] +/- ex10, fit w4-6, scored w7 (player-game weights)")
    def lfit(rows, cols, l2=1e-2):
        n = collections.Counter((r["gid"], r["pid"]) for r in rows)
        w = np.array([1 / n[(r["gid"], r["pid"])] for r in rows]); w = w / w.mean()
        X = np.column_stack([np.ones(len(rows))] + [np.array([c(r) for r in rows]) for c in cols])
        y = np.array([r["y"] for r in rows])
        b = np.zeros(X.shape[1])
        for _ in range(500):
            p = 1 / (1 + np.exp(-(X @ b)))
            b -= 0.3 * (X.T @ (w * (p - y)) / len(y) + l2 * b)
        return b, X
    logit = lambda r: math.log(max(r["p_model"], 1e-3) / max(1 - r["p_model"], 1e-3))
    for cols, nm in (([logit], "p_model only"), ([logit, lambda r: r["f"]["ex10"]], "+ ex10"),
                     ([logit, lambda r: r["f"]["ex10"] * r["f"]["early"]], "+ ex10 (first 8 carries only)")):
        b, _ = lfit(hist, cols)
        pw7 = []
        for r in w7:
            x = [1] + [c(r) for c in cols]
            r["p_tmp"] = 1 / (1 + math.exp(-sum(bi * xi for bi, xi in zip(b, x))))
        a7, _ = wauc(w7, "p_tmp")
        print(f"    {nm:32s} coefs {np.round(b, 3).tolist()}  w7 AUC {a7:.3f} Brier {wbrier(w7, 'p_tmp'):.3f}")

# ------------------------------------------------------------------------------ main
if __name__ == "__main__":
    stage = sys.argv[1] if len(sys.argv) > 1 else "all"
    D = load_pbp()
    print(f"loaded {len(D['games'])} games")
    pri = point_in_time_priors(D)
    for yr in {g["season"] for g in D["games"].values()}:   # "entering the week after the last one loaded"
        pri[(yr, max(g["week"] for g in D["games"].values() if g["season"] == yr) + 1)] = pri[(yr, 99)]
    if stage in ("hyp", "all"):
        stage_hyp(D, pri)
    if stage in ("carries", "all"):
        M = stage_carries(D, pri)
    else:
        M = json.load(open(f"{SCRATCH}/rush_two_stage.json")) if os.path.exists(f"{SCRATCH}/rush_two_stage.json") else None
    if stage in ("quotes", "all"):
        Q = stage_quotes(D, pri)
    else:
        Q = pickle.load(open(f"{SCRATCH}/quotes.pkl", "rb")) if os.path.exists(f"{SCRATCH}/quotes.pkl") else None
        if Q and os.path.exists(f"{SCRATCH}/espn_games.pkl"):
            D["games"].update(pickle.load(open(f"{SCRATCH}/espn_games.pkl", "rb")))
    if stage in ("eval", "all"):
        stage_eval(D, pri, M, Q)
