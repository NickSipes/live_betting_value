#!/usr/bin/env python3
"""Does team type change the game shape, or what a lead is worth, beyond the pregame spread?

Each team going into each week: win%, offense rank (points scored per game, last 3 played), defense
rank (points allowed per game, last 3 played). Rated -1 worst to +1 best in its league that week,
split into top / mid / bot thirds. Prior games only. Needs MIN_GAMES played.

Run: .venv/bin/python analysis/team_shapes.py [--refresh]"""
import csv, sys
from game_shapes import *
import urllib.request

MIN_GAMES = int(os.environ.get("MIN_GAMES", 2))   # ponytail: 2 lets last-2 stand in for last-3 in week 3; 3 is stricter, fewer games
ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard"

def cfb_results(season=2026):
    """[[week, id, home, away, hs, as]] for every finished FBS game, spread or not. No spread drops a
    game from the drive cache, but it still counts toward records and scoring."""
    cache = f"{ROOT}/data/cfb_scores_{season}.json"
    if "--refresh" in sys.argv or not os.path.exists(cache):
        out = []
        for wk in range(1, 17):
            u = f"{ESPN}?groups=80&limit=300&seasontype=2&week={wk}&dates={season}"
            sb = json.load(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=20))
            for e in sb.get("events", []):
                if e["status"]["type"]["state"] == "post":
                    c = {x["homeAway"]: x for x in e["competitions"][0]["competitors"]}
                    out.append([wk, e["id"], c["home"]["team"]["abbreviation"], c["away"]["team"]["abbreviation"],
                                int(c["home"]["score"]), int(c["away"]["score"])])
        json.dump(out, open(cache, "w"))
    return json.load(open(cache))

def nfl_results(season):
    with open(f"{ROOT}/data/games.csv") as fh:
        return [[int(r["week"]), r["game_id"], r["home_team"], r["away_team"], int(r["home_score"]), int(r["away_score"])]
                for r in csv.DictReader(fh) if r["season"] == str(season) and r["game_type"] == "REG" and r["home_score"]]

def rate(v):
    """{team: value} -> {team: +1 best .. -1 worst}, ties share a rate."""
    x = np.array(list(v.values()), float)
    return {t: 1 - 2 * ((x > a).sum() + .5 * ((x == a).sum() - 1)) / (len(x) - 1) for t, a in v.items()}

def ratings(res):
    """{(team, week): (win, off, def)}, each week from games of earlier weeks only."""
    played, out = collections.defaultdict(list), {}
    for wk in sorted({r[0] for r in res}):
        ok = {t: g for t, g in played.items() if len(g) >= MIN_GAMES}
        if len(ok) > 8:
            win = rate({t: np.mean([(a > b) + .5 * (a == b) for a, b in g]) for t, g in ok.items()})
            off = rate({t: np.mean([a for a, _ in g[-3:]]) for t, g in ok.items()})
            dfn = rate({t: -np.mean([b for _, b in g[-3:]]) for t, g in ok.items()})
            out.update({(t, wk): (win[t], off[t], dfn[t]) for t in ok})
        for r in res:
            if r[0] == wk:
                played[r[2]].append((r[4], r[5])); played[r[3]].append((r[5], r[4]))
    return out

def tag(games, res, rt):
    """Keep the games where both teams have ratings; g['h'], g['a'] = (win, off, def)."""
    by = {r[1]: r for r in res}
    out = []
    for g in games:
        r = by.get(g["id"])
        if r and (r[2], r[0]) in rt and (r[3], r[0]) in rt:
            out.append({**g, "h": rt[r[2], r[0]], "a": rt[r[3], r[0]]})
    return out

def tier(x):
    return "top" if x > 1 / 3 else "bot" if x < -1 / 3 else "mid"

def pair(g, i):
    a, b = sorted((tier(g["h"][i]), tier(g["a"][i])), key="top mid bot".index)
    return f"{a}/{b}"

def attack(g):
    """Each offense's rating plus the defense it faces: who should move the ball."""
    h, a = g["h"][1] - g["a"][2], g["a"][1] - g["h"][2]
    good, bad = max(h, a), min(h, a)
    return "both attack" if bad > .25 else "both stall" if good < -.25 else "one-sided" if good > .25 and bad < -.25 else "mixed"

def spb(g):
    a = abs(g["sp"])
    return 0 if a <= 3.5 else 1 if a <= 7 else 2 if a <= 14 else 3 if a <= 24 else 4

def shape_table(nm, gs, labelf, rng):
    """Shape shares by group, minus what games with the same size spread do. p: permute the labels
    within spread buckets, chi-square style stat, so one p covers the whole table."""
    lab = np.array([labelf(g) for g in gs])
    sh = np.array([SHAPES.index(shape(orient(path(g["ev"])))) for g in gs])
    bk = np.array([spb(g) for g in gs])
    tot = np.array([sum(e[1] + e[2] for e in g["ev"]) for g in gs], float)
    hot = np.eye(len(SHAPES))[sh]
    exp = np.array([hot[bk == b].mean(0) for b in range(5) if (bk == b).any()])[np.searchsorted(np.unique(bk), bk)]
    groups = sorted(set(lab))
    def stat(l):
        return sum((((hot[l == k] - exp[l == k]).sum(0)) ** 2 / (exp[l == k].sum(0) + 1e-9)).sum() for k in groups)
    s0 = stat(lab)
    perm = lab.copy()
    n = 0
    for _ in range(2000):
        for b in np.unique(bk):
            i = np.where(bk == b)[0]
            perm[i] = perm[rng.permutation(i)]
        n += stat(perm) >= s0
    print(f"  {nm}  (p={(n + 1) / 2001:.3f})   each cell: share (vs same-spread games)")
    print("    " + " " * 17 + "".join(f"{s:>14s}" for s in SHAPES) + "    avg pts")
    for k in groups:
        m = lab == k
        print(f"    {k:12s}{m.sum():4d} " + "".join(f"{hot[m, j].mean():7.0%} ({(hot[m, j] - exp[m, j]).mean() * 100:+3.0f})  " for j in range(len(SHAPES))) + f"{tot[m].mean():7.1f}")

def team_x(g, m, t):
    """Pregame team gap (does the spread underrate it?) and how each side's rating bends the leak."""
    rem, h, a = 1 - t / 60, np.array(g["h"]), np.array(g["a"])
    s = np.sign(m[t])
    lead, trail = (h, a) if s >= 0 else (a, h)
    return [rem * x for x in (*(h - a), m[t] * lead[1], m[t] * lead[2], m[t] * trail[1], m[t] * trail[2], m[t] * lead[0], m[t] * trail[0])]
TEAM_X = ["win gap", "off gap", "def gap", "lead x leader off", "lead x leader def", "lead x trailer off", "lead x trailer def", "lead x leader win", "lead x trailer win"]

def trows(gs):
    gi, B, _, y = rows(gs)
    return gi, B, np.array([team_x(g, path(g["ev"]), t) for g in gs for t in CHECK]), y

def oos(nm, tr, te, rng):
    _, B, T, y = trows(tr)
    gi, Bt, Tt, yt = trows(te)
    e0 = yt - Bt @ ols(B, y)
    e1 = yt - np.c_[Bt, Tt] @ ols(np.c_[B, T], y)
    ng, ds = gi.max() + 1, []
    for _ in range(1000):
        w = np.bincount(rng.integers(0, ng, ng), minlength=ng)[gi]
        ds.append(1 - (w * e1 ** 2).sum() / (w * e0 ** 2).sum())
    print(f"  {nm:26s} {len(te):4d} test games  error cut {1 - (e1 ** 2).sum() / (e0 ** 2).sum():+.2%} [{np.percentile(ds, 2.5):+.2%}, {np.percentile(ds, 97.5):+.2%}]")

def coefs(nm, gs, rng):
    gi, B, T, y = trows(gs)
    X = np.c_[B, T]
    by = [np.where(gi == i)[0] for i in range(len(gs))]
    bs = np.array([ols(X[ix], y[ix]) for ix in (np.concatenate([by[j] for j in rng.integers(0, len(gs), len(gs))]) for _ in range(500))])
    b, (lo, hi) = ols(X, y), np.percentile(bs, [2.5, 97.5], axis=0)
    print(f"  {nm}: base leak {ols(B, y)[1]:+.3f}")
    for j, f in enumerate(TEAM_X):
        print(f"    {f:20s} {b[3 + j]:+.3f} [{lo[3 + j]:+.3f}, {hi[3 + j]:+.3f}]{' *' if lo[3 + j] > 0 or hi[3 + j] < 0 else ''}")

def lead_table(nm, gs):
    """Leads of 9+: same scoreboard, sorted by who is chasing and who is defending the lead."""
    _, B, _, y = rows(gs)
    b = ols(B, y)
    for t in (30, 45):
        rem, tb = 1 - t / 60, collections.defaultdict(list)
        for g in gs:
            m = path(g["ev"])
            if abs(m[t]) < 9:
                continue
            s = np.sign(m[t])
            lead, trail = (g["h"], g["a"]) if s > 0 else (g["a"], g["h"])
            r = s * (m[-1] - m[t] + g["sp"] * rem - np.array(base_x(m, g["sp"], t)) @ b)
            row = (s * m[t], s * (m[-1] - m[t]), r, s * m[-1] > 0, abs(m[-1]) <= 8, sum(e[1] + e[2] for e in g["ev"] if e[0] > t * 60))
            tb["trailer off " + tier(trail[1])].append(row)
            tb["leader def " + tier(lead[2])].append(row)
            tb["trailer win% " + tier(trail[0])].append(row)
            tb["leader win% " + tier(lead[0])].append(row)
        print(f"  {nm} leads of 9+ at minute {t}")
        for k in sorted(tb):
            a = np.array(tb[k], float)
            print(f"    {k:20s} n={len(a):4d} lead {a[:, 0].mean():5.1f} | change {a[:, 1].mean():+5.1f} | vs fair {a[:, 2].mean():+5.1f} +-{1.96 * a[:, 2].std() / len(a) ** .5:4.1f} | sd {a[:, 2].std():4.1f} | wins {a[:, 3].mean():4.0%} | ends one-score {a[:, 4].mean():4.0%} | pts left {a[:, 5].mean():4.1f}")

def top_top(nf, cf, rng):
    """Spotted in the shape tables, so a hypothesis: leads between two winning teams get given back."""
    section("two top-third records: lead 11+ ends within one score, vs same-spread games (one-sided permutation)")
    both = lambda g: tier(g["h"][0]) == "top" and tier(g["a"][0]) == "top"
    for nm, gs in [(f"NFL {y}", [g for g in nf if g["season"] == y]) for y in (2024, 2025, 2026)] + [("CFB 2026", cf)]:
        y = np.array([shape(orient(path(g["ev"]))) in ("comeback", "pulled close") for g in gs], float)
        f, bk = np.array([both(g) for g in gs]), np.array([spb(g) for g in gs])
        exp = np.array([y[bk == b].mean() for b in bk])
        p, n = f.copy(), 0
        for _ in range(4000):
            for b in np.unique(bk):
                i = np.where(bk == b)[0]
                p[i] = f[rng.permutation(i)]
            n += (y - exp)[p].sum() >= (y - exp)[f].sum()
        print(f"  {nm}: n={f.sum()} {y[f].mean():.0%} vs {exp[f].mean():.0%}, p={(n + 1) / 4001:.3f}")
    est = []
    for nm, gs in (("NFL", nf), ("CFB", cf)):
        gi, B, _, y = rows(gs)
        X = np.c_[B, B[:, 1] * np.array([both(g) for g in gs for _ in CHECK], float)]
        by = [np.where(gi == i)[0] for i in range(len(gs))]
        bs = [ols(X[ix], y[ix])[-1] for ix in (np.concatenate([by[j] for j in rng.integers(0, len(gs), len(gs))]) for _ in range(500))]
        est.append((ols(X, y)[-1], np.std(bs)))
        print(f"  {nm} leak {ols(X, y)[1]:+.3f}, extra when both top {est[-1][0]:+.3f} [{np.percentile(bs, 2.5):+.3f}, {np.percentile(bs, 97.5):+.3f}]")
    w = np.array([1 / e[1] ** 2 for e in est])
    b, se = (w * [e[0] for e in est]).sum() / w.sum(), w.sum() ** -.5
    print(f"  pooled extra leak {b:+.3f} [{b - 1.96 * se:+.3f}, {b + 1.96 * se:+.3f}]")

def volume(gs, rng):
    section("NFL props: remaining team rushes / passes, team ratings on top of score+clock+usage")
    def build(sub):
        gi, B, _, Y = vol_rows(sub)
        T = []
        for g in sub:
            m = path(g["ev"])
            for t in (30, 40, 45, 50):
                for me, op, sg in ((g["h"], g["a"], 1), (g["a"], g["h"], -1)):
                    rem, d = 1 - t / 60, sg * m[t]
                    T.append([rem * x for x in (*me, *op, d * me[1], d * op[2])])
        return gi, B, np.array(T), Y
    for held in (2024, 2025, 2026):
        tr, te = [g for g in gs if g["season"] != held], [g for g in gs if g["season"] == held]
        _, B, T, Y = build(tr)
        gi, Bt, Tt, Yt = build(te)
        for j, lab in enumerate(("rushes left", "passes left")):
            e0, e1 = Yt[:, j] - Bt @ ols(B, Y[:, j]), Yt[:, j] - np.c_[Bt, Tt] @ ols(np.c_[B, T], Y[:, j])
            ng, ds = gi.max() + 1, []
            for _ in range(1000):
                w = np.bincount(rng.integers(0, ng, ng), minlength=ng)[gi]
                ds.append(1 - (w * e1 ** 2).sum() / (w * e0 ** 2).sum())
            print(f"  hold out {held} {lab}: error cut {1 - (e1 ** 2).sum() / (e0 ** 2).sum():+.2%} [{np.percentile(ds, 2.5):+.2%}, {np.percentile(ds, 97.5):+.2%}] rmse {(e0 ** 2).mean() ** .5:.2f}")

def main():
    r = rate({"a": 30, "b": 20, "c": 20, "d": 10})
    assert (r["a"], r["b"], r["d"]) == (1, 0, -1)
    wk = lambda w: [[w, f"{w}{i}", f"H{i}", f"A{i}", 10 + i, 0] for i in range(5)]
    r = ratings(wk(1) + wk(2) + wk(3))
    assert ("H0", 3) in r and ("H0", 2) not in r, "needs two prior games"
    assert r["H4", 3][1] == 1 and r["H0", 3][2] > r["A4", 3][2] and r["H0", 3][0] == -r["A0", 3][0] > 0
    rng = np.random.default_rng(7)
    nf = []
    for y in (2024, 2025, 2026):
        res = nfl_results(y)
        nf += tag([g for g in nfl(y) if g["ev"] and not g["post"]], res, ratings(res))
    res = cfb_results()
    cf = tag([g for g in cfb() if g["ev"]], res, ratings(res))
    print(f"tagged: NFL {len(nf)} ({sum(g['season'] == 2026 for g in nf)} in 2026), CFB {len(cf)}, CFB results {len(res)}")
    for nm, gs in (("NFL", nf), ("CFB", cf)):
        sp = np.array([g["sp"] for g in gs])
        print(f"  {nm} corr with spread, home minus away: " + ", ".join(f"{lab} {np.corrcoef([g['h'][i] - g['a'][i] for g in gs], sp)[0, 1]:+.2f}" for i, lab in enumerate(("win%", "off", "def"))))

    section("shapes by matchup, compared with games of the same spread size")
    for nm, gs in (("NFL 24-26", nf), ("CFB 2026", cf)):
        shape_table(f"{nm} record tiers", gs, lambda g: pair(g, 0), rng)
        shape_table(f"{nm} last-3 offense tiers", gs, lambda g: pair(g, 1), rng)
        shape_table(f"{nm} last-3 defense tiers", gs, lambda g: pair(g, 2), rng)
        shape_table(f"{nm} attack vs defense", gs, attack, rng)

    section("same lead, different teams")
    lead_table("NFL 24-26", nf)
    lead_table("CFB 2026", cf)

    section("fair line + team features, out of sample")
    oos("NFL fit 24-25 test 26", [g for g in nf if g["season"] < 2026], [g for g in nf if g["season"] == 2026], rng)
    oos("NFL fit 2024 test 25-26", [g for g in nf if g["season"] == 2024], [g for g in nf if g["season"] > 2024], rng)
    wk = int(np.median([g["week"] for g in cf]))
    oos(f"CFB fit wk<={wk} test later", [g for g in cf if g["week"] <= wk], [g for g in cf if g["week"] > wk], rng)
    section("full-sample coefficients (game bootstrap 95% CI)")
    coefs("NFL 24-26", nf, rng)
    coefs("CFB 2026", cf, rng)
    top_top(nf, cf, rng)
    volume(nf, rng)

if __name__ == "__main__":
    main()
