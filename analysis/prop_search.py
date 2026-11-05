#!/usr/bin/env python3
"""Search pregame player-prop conditions for an edge, with an out-of-sample check.

Every receiving/rushing yards prop with a closing line (data/props_hist, FanDuel first) is a row with
~25 conditions known before kickoff (player form, team/opponent quality, game script, weather, rest, QB).
Rules are single conditions or pairs (tertile bins cut on 2024), for overs and unders, by market and
position. Rules are FOUND on 2024 only and then TESTED on 2025: a real hole should still pay in 2025.
Grading uses the real over/under prices from the same book.

Run: .venv/bin/python analysis/prop_search.py"""
import collections, csv, gzip, itertools, math, random, re
import numpy as np
import pull_pregame_props as P

ROOT = P.ROOT
MKT = {"rec": "player_reception_yds", "rush": "player_rush_yds"}

def norm(n):
    n = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b\.?", "", n.lower().replace(".", "").replace("'", ""))
    return " ".join(re.sub(r"[^a-z ]", "", n).split())

def payout(price):
    return 100 / -price if price < 0 else price / 100

def build(season):
    props = P.load(season)
    ros = {r["gsis_id"]: (r["position"], norm(r["full_name"])) for r in csv.DictReader(open(f"{ROOT}/data/roster_{season}.csv")) if r.get("gsis_id")}
    by_name = collections.defaultdict(list)
    for gid, (pos, nm) in ros.items():
        by_name[nm].append(gid)
    games = {r["game_id"]: r for r in csv.DictReader(open(f"{ROOT}/data/games.csv")) if r["season"] == str(season) and r["game_type"] == "REG"}
    yds = {"rec": collections.defaultdict(float), "rush": collections.defaultdict(float)}
    touch = collections.defaultdict(lambda: collections.Counter())   # (game, player) -> targets/carries
    played = set(); tm = {}; team_vol = collections.defaultdict(lambda: collections.Counter())
    allowed = collections.defaultdict(float); att = collections.Counter()
    for r in csv.DictReader(gzip.open(f"{ROOT}/data/pbp_{season}.csv.gz", "rt")):
        if r["season_type"] != "REG" or not r["posteam"]:
            continue
        g, pt, d = r["game_id"], r["posteam"], r["defteam"]
        if r["pass_attempt"] == "1" and r["passer_player_id"]:
            att[(g, pt, r["passer_player_id"])] += 1
        for kind, idc, yc in (("rec", "receiver_player_id", "receiving_yards"), ("rush", "rusher_player_id", "rushing_yards")):
            p = r[idc]
            if not p:
                continue
            played.add((g, p)); tm[(g, p)] = (pt, d)
            touch[(g, p)][kind] += 1; team_vol[(g, pt)][kind] += 1
            if r[yc] not in ("", "NA"):
                v = float(r[yc]); yds[kind][(g, p)] += v
                allowed[(g, d, ros.get(p, ("",))[0], kind)] += v
    week_of = {g: int(v["week"]) for g, v in games.items()}
    # team results by week
    res = [(int(v["week"]), v["home_team"], v["away_team"], int(v["home_score"]), int(v["away_score"])) for v in games.values() if v["home_score"]]
    qb = {}
    for (g, pt, q), n in att.items():
        if n > qb.get((g, pt), ("", 0))[1]:
            qb[(g, pt)] = (q, n)
    rows = []
    for g, rec in props.items():
        if g not in games or not games[g]["home_score"]:
            continue
        gm, wk = games[g], week_of[g]
        if wk < 4:
            continue
        prior = {x for x in games if week_of[x] < wk}
        def team_rates(t):
            m = [(hs - as_) if h == t else (as_ - hs) for w, h, a, hs, as_ in res if w < wk and t in (h, a)]
            pf = [hs if h == t else as_ for w, h, a, hs, as_ in res if w < wk and t in (h, a)]
            pa = [as_ if h == t else hs for w, h, a, hs, as_ in res if w < wk and t in (h, a)]
            return (sum(x > 0 for x in m) + .5 * sum(x == 0 for x in m)) / len(m), np.mean(pf), np.mean(pa)
        teams = {gm["home_team"], gm["away_team"]}
        all_teams = {t for _, h, a, _, _ in res for t in (h, a)}
        tr = {t: team_rates(t) for t in all_teams}
        pf_rank = {t: sum(tr[u][1] > tr[t][1] for u in all_teams) for t in all_teams}   # 0 = best offense
        pa_rank = {t: sum(tr[u][2] < tr[t][2] for u in all_teams) for t in all_teams}   # 0 = best defense
        lines = {}
        for book in ("draftkings", "fanduel"):
            for b in rec["bookmakers"]:
                if b["key"] != book:
                    continue
                for m in b["markets"]:
                    kind = "rec" if m["key"] == MKT["rec"] else "rush"
                    ov = {norm(o["description"]): o for o in m["outcomes"] if o["name"] == "Over"}
                    un = {norm(o["description"]): o for o in m["outcomes"] if o["name"] == "Under"}
                    for nm, o in ov.items():
                        if nm in un and o.get("point") is not None:
                            lines[(nm, kind)] = (o["point"], o["price"], un[nm]["price"], book)
        for (nm, kind), (L, po, pu, book) in lines.items():
            gids = [x for x in by_name.get(nm, []) if (g, x) in played]
            if len(gids) != 1:
                continue
            p = gids[0]; pos = ros[p][0]
            if pos not in ("WR", "TE", "RB"):
                continue
            pt, d = tm[(g, p)]
            hist = [(x, yds[kind].get((x, p), 0.0)) for x in prior if (x, p) in played]
            hist.sort(key=lambda t: week_of[t[0]])
            if len(hist) < 3:
                continue
            ypg = np.mean([v for _, v in hist]); l3 = np.mean([v for _, v in hist[-3:]])
            share3 = np.mean([touch[(x, p)][kind] / max(1, team_vol[(x, pt)][kind]) for x, _ in hist[-3:]])
            # yards this defense allowed per game to this position in this market
            dg = [x for x in prior if d in (games[x]["home_team"], games[x]["away_team"])]
            d_allow = np.mean([allowed.get((x, d, pos, kind), 0.0) for x in dg]) if dg else np.nan
            home = pt == gm["home_team"]
            spread = float(gm["spread_line"] or 0) * (1 if home else -1)      # + = player's team favored
            total = float(gm["total_line"] or 0)
            usual = collections.Counter(qb.get((x, pt), ("", 0))[0] for x in prior if (x, pt) in qb).most_common(1)
            rest = gm["home_rest" if home else "away_rest"]
            rows.append(dict(
                season=season, pos=pos, kind=kind, line=L, po=po, pu=pu, yds=yds[kind].get((g, p), 0.0),
                ypg=ypg, l3=l3, line_vs_ypg=L / max(ypg, 1), line_vs_l3=L / max(l3, 1), l3_vs_ypg=l3 / max(ypg, 1),
                share=share3, opp_allow=d_allow, team_win=tr[pt][0], opp_win=tr[d][0], win_gap=tr[pt][0] - tr[d][0],
                off_rank=pf_rank[pt], opp_def_rank=pa_rank[d], spread=spread, total=total,
                implied=(total + spread) / 2, home=int(home), div=int(gm["div_game"] == "1"),
                dome=int(gm["roof"] in ("dome", "closed")), wind=float(gm["wind"] or 0) if gm["roof"] == "outdoors" else 0.0,
                temp=float(gm["temp"] or 65) if gm["roof"] == "outdoors" else 65.0, rest=int(rest or 7),
                backup=int(bool(usual) and qb.get((g, pt), ("", 0))[0] != usual[0][0]), week=wk,
                juice=po - pu, fd=int(book == "fanduel")))
    return rows

FEATS = ["ypg", "l3", "line_vs_ypg", "line_vs_l3", "l3_vs_ypg", "share", "opp_allow", "team_win", "opp_win", "win_gap",
         "off_rank", "opp_def_rank", "spread", "total", "implied", "home", "div", "dome", "wind", "temp", "rest", "backup",
         "week", "juice", "line"]
BINARY = {"home", "div", "dome", "backup"}

def grade(r, side):
    if r["yds"] == r["line"]:
        return 0.0
    win = r["yds"] > r["line"] if side == "over" else r["yds"] < r["line"]
    return payout(r["po"] if side == "over" else r["pu"]) if win else -1.0

def conditions(train):
    """[(name, test)] single-feature bins, cut at 2024 tertiles (binary features: 0/1)."""
    out = []
    for f in FEATS:
        if f in BINARY:
            out += [(f"{f}=1", lambda r, f=f: r[f] == 1), (f"{f}=0", lambda r, f=f: r[f] == 0)]
            continue
        v = np.array([r[f] for r in train if not (isinstance(r[f], float) and math.isnan(r[f]))])
        if len(set(v)) < 3:
            continue
        a, b = np.quantile(v, [1 / 3, 2 / 3])
        if a == b:
            continue
        out += [(f"{f}<={a:.3g}", lambda r, f=f, a=a: r[f] <= a), (f"{a:.3g}<{f}<={b:.3g}", lambda r, f=f, a=a, b=b: a < r[f] <= b),
                (f"{f}>{b:.3g}", lambda r, f=f, b=b: r[f] > b)]
    return out

def main():
    data = {s: build(s) for s in (2024, 2025)}
    tr, te = data[2024], data[2025]
    print(f"rows: 2024 {len(tr)}, 2025 {len(te)}")
    for s, rs in data.items():
        for side in ("over", "under"):
            g = [grade(r, side) for r in rs]
            print(f"  {s} every {side}: {sum(x > 0 for x in g)}-{sum(x < 0 for x in g)}  ROI {np.mean(g):+.1%}")
    groups = [("all", lambda r: True), ("rec", lambda r: r["kind"] == "rec"), ("rush", lambda r: r["kind"] == "rush"),
              ("WR", lambda r: r["pos"] == "WR"), ("TE", lambda r: r["pos"] == "TE"), ("RB rush", lambda r: r["pos"] == "RB" and r["kind"] == "rush"),
              ("RB rec", lambda r: r["pos"] == "RB" and r["kind"] == "rec")]
    conds = conditions(tr)
    rules = []
    for gname, gf in groups:
        base = [i for i, r in enumerate(tr) if gf(r)]
        cmask = [(n, [i for i in base if f(tr[i])]) for n, f in conds]
        combos = [(n, ix) for n, ix in cmask] + [(f"{a} & {b}", sorted(set(ia) & set(ib)))
                                                for (a, ia), (b, ib) in itertools.combinations(cmask, 2) if a.split("<")[0] != b.split("<")[0]]
        for name, ix in combos:
            if len(ix) < 60:
                continue
            for side in ("over", "under"):
                g = np.array([grade(tr[i], side) for i in ix])
                rules.append((np.mean(g) / (g.std() / math.sqrt(len(g)) + 1e-9), np.mean(g), len(ix), gname, name, side))
    rules.sort(reverse=True)
    print(f"\nrules searched on 2024 (n>=60): {len(rules)}")
    # test the top rules on 2025 with the same definitions
    look = {}
    for gname, gf in groups:
        look[gname] = gf
    cond_f = dict(conds)
    def test(gname, name, side):
        fs = [cond_f[c] for c in name.split(" & ")]
        ix = [r for r in te if look[gname](r) and all(f(r) for f in fs)]
        g = [grade(r, side) for r in ix]
        return len(g), (np.mean(g) if g else float("nan")), sum(x > 0 for x in g), sum(x < 0 for x in g)
    top = rules[:40]
    held = 0
    print(f"\n{'2024 z':>6} {'2024 ROI':>8} {'n':>5}  rule  ->  2025")
    for z, roi, n, gname, name, side in top:
        n2, roi2, w2, l2 = test(gname, name, side)
        held += roi2 > 0
        print(f"{z:6.2f} {roi:+8.1%} {n:5d}  {gname:7} {side:5} {name:48.48}  -> {w2}-{l2} n {n2} ROI {roi2:+.1%}")
    print(f"\nof the top {len(top)} 2024 rules, {held} were profitable in 2025 (about half expected by chance)")
    # random-label check: how strong is the best 2024 rule when outcomes are shuffled?
    rng = random.Random(7)
    best = []
    sample = rules[:2000]
    for _ in range(20):
        perm = tr[:]; ys = [r["yds"] for r in perm]; rng.shuffle(ys)
        shuf = [dict(r, yds=y) for r, y in zip(perm, ys)]
        bz = 0
        for z, roi, n, gname, name, side in sample[:300]:
            fs = [cond_f[c] for c in name.split(" & ")]
            g = np.array([grade(r, side) for r in shuf if look[gname](r) and all(f(r) for f in fs)])
            if len(g) >= 60:
                bz = max(bz, np.mean(g) / (g.std() / math.sqrt(len(g)) + 1e-9))
        best.append(bz)
    print(f"best z among those rules with shuffled outcomes (20 runs): median {np.median(best):.2f}, max {max(best):.2f}; real best {rules[0][0]:.2f}")

if __name__ == "__main__":
    main()
