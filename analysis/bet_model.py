#!/usr/bin/env python3
"""Live prop over-flagger, v0.

Cohort: top-20 WR / top-10 TE (receiving), top-15 RB (rushing), entering-week
ranks, vs bottom-10 defenses at that position (entering week), weeks 4+.

Three pieces:
 1. book-line proxy: L(t) = cum(t) + L0 * decay(rem_frac); decay fit from every
    quoted player in the games we pulled; L0 fit from season yards/game.
 2. remaining-yards model: E[rem] = a*(ypg*rem_frac) + b*(usage-projected pace)
    fit by OLS on the whole cohort (all defenses, for sample size).
 3. flag: at 5-game-min grid, fire when cum + E[rem] - L_hat >= EDGE_MIN with
    clock and usage guards. One flag (the first) per player-game.

Tuning output: flags on the 12 pulled games evaluated at REAL lines; then the
full cohort evaluated at proxy lines.
"""
import csv, gzip, json, os, bisect, collections, datetime

ROOT = os.path.join(os.path.dirname(__file__), "..")
MIN_WEEK = 4
TOP_N = {"WR": 20, "TE": 10, "RB": 15}
GRID = range(300, 3301, 300)          # game-clock evaluation points
EDGE_MIN = 15                          # yards of edge to flag (tuned below)
MIN_REM_FRAC, MAX_REM_FRAC = 0.15, 0.80
MIN_EVENTS = 2                         # usage guard: events so far at flag time

def ts(x):
    return datetime.datetime.fromisoformat(x.replace("Z", "+00:00")).timestamp()

def elapsed(qtr, qsr):
    full = min(qtr - 1, 4) * 900 + max(qtr - 5, 0) * 600
    return full + ((900 if qtr <= 4 else 600) - qsr)

pos_of, name_of = {}, {}
gsis_by_name = {}
for r in csv.DictReader(open(f"{ROOT}/data/roster_2026.csv")):
    if r["gsis_id"]:
        pos_of[r["gsis_id"]] = r["position"]
        name_of[r["gsis_id"]] = r["full_name"]
        gsis_by_name[r["full_name"]] = r["gsis_id"]

# ---- parse pbp -------------------------------------------------------------
events = {"rec": [], "rush": []}       # (week,gid,defteam,pid,yds,counts,tod,gc)
def_games = collections.defaultdict(set)
game_len = collections.defaultdict(int)
wall = collections.defaultdict(list)   # gid -> [(wall_ts, gc)]
with gzip.open(f"{ROOT}/data/pbp_2026.csv.gz", "rt") as f:
    for p in csv.DictReader(f):
        gid, wk = p["game_id"], int(p["week"])
        if p["qtr"] in ("", "NA") or p.get("quarter_seconds_remaining") in ("", "NA", None):
            continue
        gc = elapsed(int(float(p["qtr"])), float(p["quarter_seconds_remaining"]))
        game_len[gid] = max(game_len[gid], gc)
        if p.get("time_of_day"):
            wall[gid].append((ts(p["time_of_day"]), gc))
        if p.get("defteam"):
            def_games[p["defteam"]].add((wk, gid))
        rid = p.get("receiver_player_id")
        if rid and p.get("passer_player_name") and pos_of.get(rid) in ("WR", "TE"):
            y = p.get("receiving_yards")
            y = float(y) if y not in ("", "NA") else 0.0
            events["rec"].append((wk, gid, p["defteam"], rid, y, p.get("complete_pass") == "1", p.get("time_of_day"), gc))
        uid = p.get("rusher_player_id")
        if uid and p.get("rush_attempt") == "1" and pos_of.get(uid) == "RB":
            y = p.get("rushing_yards")
            y = float(y) if y not in ("", "NA") else 0.0
            events["rush"].append((wk, gid, p["defteam"], uid, y, True, p.get("time_of_day"), gc))
for g in wall:
    wall[g].sort()
max_week = max(t[0] for k in events for t in events[k])

def to_gc(gid, t):
    pts = wall[gid]
    i = bisect.bisect_right(pts, (t, float("inf"))) - 1
    return 0 if i < 0 else min(pts[i][1], game_len[gid])

# ---- per-week priors and cohort -------------------------------------------
# priors[(kind,w)][pid] = (pos, rank, ypg, epg[events/gm], games)
priors = {}
worst10 = {}   # (kind,w) -> {(defteam,pos): rank}
for kind, evs in events.items():
    for w in range(MIN_WEEK, max_week + 1):
        tot = collections.defaultdict(float)
        gms = collections.defaultdict(set)
        nev = collections.defaultdict(int)
        for e in evs:
            if e[0] < w:
                gms[e[3]].add(e[1]); nev[e[3]] += 1
                if e[5]:
                    tot[e[3]] += e[4]
        pr = {}
        for pos in {"rec": ("WR", "TE"), "rush": ("RB",)}[kind]:
            ranked = sorted((p for p in tot if pos_of[p] == pos), key=lambda p: -tot[p])[:TOP_N[pos]]
            for i, p in enumerate(ranked):
                g = len(gms[p])
                pr[p] = (pos, i + 1, tot[p] / g, nev[p] / g, g)
        priors[(kind, w)] = pr
        dy = collections.defaultdict(float)
        for e in evs:
            if e[0] < w and e[5]:
                dy[(e[2], pos_of[e[3]])] += e[4]
        wr = {}
        for pos in {"rec": ("WR", "TE"), "rush": ("RB",)}[kind]:
            rows = [(dt, dy[(dt, pos)] / n) for dt in def_games
                    if (n := len([g for g in def_games[dt] if g[0] < w])) >= 2]
            rows.sort(key=lambda r: -r[1])
            for i, (dt, _) in enumerate(rows[:10]):
                wr[(dt, pos)] = i + 1
        worst10[(kind, w)] = wr

# player-games: (kind,gid,pid) -> dict(events sorted by gc, final, week, defteam)
pgames = {}
for kind, evs in events.items():
    for e in evs:
        if e[0] < MIN_WEEK:
            continue
        k = (kind, e[1], e[3])
        d = pgames.setdefault(k, {"week": e[0], "defteam": e[2], "ev": []})
        d["ev"].append(e)
for k, d in pgames.items():
    d["ev"].sort(key=lambda e: e[7])
    d["final"] = sum(e[4] for e in d["ev"] if e[5])

def state(d, gc):
    """(cum, n_events) at game-clock gc."""
    cum = n = 0
    for e in d["ev"]:
        if e[7] > gc:
            break
        n += 1
        if e[5]:
            cum += e[4]
    return cum, n

def in_cohort(kind, gid, pid, week, defteam):
    pr = priors[(kind, week)].get(pid)
    if not pr:
        return None
    if worst10[(kind, week)].get((defteam, pr[0])) is None:
        return None
    return pr

# ---- 1. book-line proxy ----------------------------------------------------
MARKET = {"rec": "player_reception_yds", "rush": "player_rush_yds"}
decay_pts = []          # (rem_frac, R/L0)
l0_fit_pts = []         # (ypg_entering, L0)
quoted = collections.defaultdict(dict)   # (kind,gid,pid) -> {gc: line} (FD, falls back DK)
for fn in os.listdir(f"{ROOT}/data/props_series"):
    gid, market = fn[:fn.index("_player")], fn[fn.index("player"):-4]
    kind = "rec" if market == MARKET["rec"] else "rush"
    week = int(gid.split("_")[1])
    rows = [x for x in csv.DictReader(open(f"{ROOT}/data/props_series/{fn}"))]
    by_player = collections.defaultdict(dict)
    for x in rows:
        if x["side"] != "Over" or not x["point"]:
            continue
        by_player[(x["player"], x["book"])][x["snapshot"]] = float(x["point"])
    for (pname, book), q in by_player.items():
        pid = gsis_by_name.get(pname)
        if not pid:
            continue
        d = pgames.get((kind, gid, pid))
        if d is None:
            continue
        series = sorted(q.items())
        L0 = series[0][1]
        if L0 < 25:
            continue        # fringe markets decay differently
        pr = priors.get((kind, week), {}).get(pid)
        if pr and book == "fanduel":
            l0_fit_pts.append((pr[2], L0))
        glen = game_len[gid]
        for snap, L in series:
            gc = to_gc(gid, ts(snap))
            if gc <= 0:
                continue
            cum, _ = state(d, gc)
            rem_frac = max(0.0, (glen - gc) / glen)
            decay_pts.append((rem_frac, max(L - cum, 0) / L0))
            if book == "fanduel" or gc not in quoted[(kind, gid, pid)]:
                quoted[(kind, gid, pid)][gc] = L

# decay curve: mean R/L0 in 10 rem_frac bins, linear interp between bin centers
bins = collections.defaultdict(list)
for rf, r in decay_pts:
    bins[min(int(rf * 10), 9)].append(r)
curve = sorted((b / 10 + 0.05, sum(v) / len(v)) for b, v in bins.items())
def decay(rem_frac):
    if rem_frac <= curve[0][0]:
        return curve[0][1] * rem_frac / curve[0][0]   # taper to 0
    for i in range(1, len(curve)):
        if rem_frac <= curve[i][0]:
            x0, y0 = curve[i - 1]; x1, y1 = curve[i]
            return y0 + (y1 - y0) * (rem_frac - x0) / (x1 - x0)
    return curve[-1][1]

# L0 from season ypg: least squares L0 = d0 + d1*ypg
n = len(l0_fit_pts)
sx = sum(x for x, _ in l0_fit_pts); sy = sum(y for _, y in l0_fit_pts)
sxx = sum(x * x for x, _ in l0_fit_pts); sxy = sum(x * y for x, y in l0_fit_pts)
d1 = (n * sxy - sx * sy) / (n * sxx - sx * sx)
d0 = (sy - d1 * sx) / n

def line_proxy(ypg, cum, rem_frac):
    return cum + (d0 + d1 * ypg) * decay(rem_frac)

# ---- 2. remaining-yards model (OLS, 2 features, through data) --------------
# rem ~ a * (ypg*rem_frac)  +  b * (usage_pace*rem_frac)
# usage_pace = (events_so_far / elapsed_frac) * (season yards per event)
X1 = X2 = Y1 = Y2 = X11 = X12 = X22 = 0.0
nfit = 0
for (kind, gid, pid), d in pgames.items():
    pr = priors[(kind, d["week"])].get(pid)
    if not pr:
        continue
    ypg, epg = pr[2], pr[3]
    ype = ypg / epg if epg else 0    # season yards per event
    glen = game_len[gid]
    for gc in GRID:
        if gc >= glen:
            break
        cum, nev = state(d, gc)
        ef = gc / glen
        rem = d["final"] - cum
        x1 = ypg * (1 - ef)
        x2 = (nev / ef) * ype * (1 - ef)
        X11 += x1 * x1; X12 += x1 * x2; X22 += x2 * x2
        Y1 += x1 * rem; Y2 += x2 * rem
        nfit += 1
det = X11 * X22 - X12 * X12
a = (Y1 * X22 - Y2 * X12) / det
b = (X11 * Y2 - X12 * Y1) / det

def expect_rem(ypg, epg, nev, gc, glen):
    ef = gc / glen
    ype = ypg / epg if epg else 0
    return a * ypg * (1 - ef) + b * (nev / ef) * ype * (1 - ef)

# ---- 3. flagger ------------------------------------------------------------
def flags_for(key, d, use_real_lines):
    kind, gid, pid = key
    pr = in_cohort(kind, gid, pid, d["week"], d["defteam"])
    if not pr:
        return None
    pos, rank, ypg, epg, _ = pr
    glen = game_len[gid]
    q = quoted.get(key, {})
    # real mode: evaluate AT quote times so price and game state are synchronous
    # (a grid time lets a quote from before a burst be "bet" after it — lookahead)
    points = sorted(q) if use_real_lines else [g for g in GRID if g < glen]
    for gc in points:
        rem_frac = (glen - gc) / glen
        if not (MIN_REM_FRAC <= rem_frac <= MAX_REM_FRAC):
            continue
        cum, nev = state(d, gc)
        if nev < MIN_EVENTS:
            continue
        L = q[gc] if use_real_lines else line_proxy(ypg, cum, rem_frac)
        proj = cum + expect_rem(ypg, epg, nev, gc, glen)
        if proj - L >= EDGE_MIN:
            return {"kind": kind, "gid": gid, "player": name_of[pid], "pos": pos,
                    "rank": rank, "week": d["week"], "def": d["defteam"],
                    "gc": gc, "line": round(L, 1), "proj": round(proj, 1),
                    "cum": round(cum), "nev": nev, "final": round(d["final"]),
                    "win": d["final"] > L}
    return None

if __name__ == "__main__":
    print(f"decay curve (rem_frac -> R/L0): {[(round(x,2), round(y,2)) for x,y in curve]}")
    print(f"L0 proxy: L0 = {d0:.1f} + {d1:.2f} * ypg   (n={n})")
    print(f"remaining-yards OLS (n={nfit}): rem = {a:.2f}*baseline_pace + {b:.2f}*usage_pace")
    known = {(r["kind"], r["game_id"], gsis_by_name[r["player"]])
             for r in json.load(open(f"{ROOT}/data/blanked_instances.json"))}
    for label, use_real in (("--- 12 known instances, REAL lines ---", True),
                            ("--- full cohort, PROXY lines ---", False)):
        print(f"\n{label}  (EDGE_MIN={EDGE_MIN})")
        wins = tot = 0
        for key, d in sorted(pgames.items(), key=lambda kv: (kv[1]["week"], kv[0])):
            if use_real and key not in known:
                continue
            f = flags_for(key, d, use_real)
            if not f:
                continue
            wins += f["win"]; tot += 1
            print(f"wk{f['week']} {f['player']:22s} {f['pos']}#{f['rank']:<2d} vs {f['def']:3s} "
                  f"@{f['gc']//60}min line {f['line']:5.1f} proj {f['proj']:5.1f} "
                  f"(cum {f['cum']}, {f['nev']} ev) final {f['final']} {'WIN' if f['win'] else 'loss'}")
        if tot:
            print(f"==> {wins}/{tot} = {wins/tot:.0%}")
        else:
            print("==> no flags")
