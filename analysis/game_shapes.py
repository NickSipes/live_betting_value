#!/usr/bin/env python3
"""Do games fall into shapes (trading scores / blowout / lead that evaporates), and does the shape
tell the model anything the score, clock and pregame spread don't?

Path = home margin on a 1-minute grid, regulation only, oriented so the team that held the biggest
lead is positive. NFL from nflverse play-by-play, CFB from the ESPN drive cache
(analysis/cfb_spread_model.py), both with the pregame spread.

Run: .venv/bin/python analysis/game_shapes.py"""
import collections, csv, glob, gzip, json, os
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GRID = 61      # minute 0..60
CHECK = (20, 30, 40, 45, 50)   # live checkpoints, game minutes

def nfl(season):
    """[{id, week, sp (home spread: negative = home favored), ev: [[gc, home_pts, away_pts]]}]"""
    plays = collections.defaultdict(list)
    with gzip.open(f"{ROOT}/data/pbp_{season}.csv.gz", "rt") as fh:
        for r in csv.DictReader(fh):
            if r["qtr"] in "1234" and r["total_home_score"] and r["game_seconds_remaining"]:
                plays[r["game_id"]].append(r)
    out = []
    for gid, ps in plays.items():
        ev, hs, as_, vol = [], 0, 0, []
        for p in ps:
            h, a, gc = int(float(p["total_home_score"])), int(float(p["total_away_score"])), 3600 - float(p["game_seconds_remaining"])
            if p["pass"] == "1" or p["rush"] == "1":
                vol.append((gc, p["posteam"] == p["home_team"], p["pass"] == "1"))
            if h != hs or a != as_:
                if ev and gc - ev[-1][0] < 15 and (h - hs > 0) == (ev[-1][1] > 0):   # TD + try = one score
                    ev[-1][1] += h - hs; ev[-1][2] += a - as_
                else:
                    ev.append([gc, h - hs, a - as_])
                hs, as_ = h, a
        out.append({"id": gid, "lg": "NFL", "season": season, "week": int(ps[0]["week"]),
                    "post": ps[0]["season_type"] == "POST", "sp": -float(ps[0]["spread_line"]), "ev": ev, "vol": vol})
    return out

def cfb():
    """Same shape from drive-end margins. The cache has margin only, so a drive's margin change
    stands in for that drive's points (a score apiece inside one drive slot nets out; rare)."""
    out = []
    for gid, g in json.load(open(f"{ROOT}/data/cfb_pbp_2026.json")).items():
        ev, m = [], 0
        for d in g["drives"]:
            if d[6] != m:
                dm = d[6] - m
                ev.append([d[2], max(dm, 0), max(-dm, 0)])
                m = d[6]
        out.append({"id": gid, "lg": "CFB", "season": 2026, "week": g["week"], "post": False, "sp": g["sp"], "ev": ev})
    return out

def path(ev):
    """Home margin at each minute: everything scored at or before that minute."""
    m = np.zeros(GRID)
    for gc, h, a in ev:
        m[min(GRID - 1, int(np.ceil(gc / 60))):] += h - a
    return m

def orient(m):
    """Flip so the team that held the biggest lead is positive (ties: the final leader)."""
    hi, lo = m.max(), -m.min()
    return m if hi > lo or (hi == lo and m[-1] >= 0) else -m

SHAPES = ["trade", "blowout", "late break", "held lead", "comeback", "pulled close"]

def shape(m):
    """m oriented. Thresholds in points: 8 = one score, 14 = two TDs."""
    P, F = m.max(), m[-1]
    if P <= 10: return "trade"                  # nobody ever got more than a score clear
    if P >= 14 and F <= 8: return "comeback"    # two-TD lead down to one score or less (or lost)
    if F <= 8: return "pulled close"            # 11-13 point lead, gone
    if m[45] <= 8: return "late break"          # one-score game through three quarters, then separated
    return "blowout" if F >= 14 and m[30] >= 8 else "held lead"

def shuffled(g, rng):
    """Same scores, same clock times, order of who scored shuffled: no dynamics, same final."""
    e = np.array(g["ev"], float)
    e[:, 1:] = e[rng.permutation(len(e)), 1:]
    return path(e)

def coinflip(g, rng):
    """Same scores and times, each one a coin weighted by the pregame spread: no game-day state."""
    e = np.array(g["ev"], float)
    pts, tot = e[:, 1] + e[:, 2], max(1, (e[:, 1] + e[:, 2]).sum())
    home = rng.random(len(e)) < np.clip(.5 - g["sp"] / (2 * tot), .05, .95)
    e[:, 1], e[:, 2] = pts * home, pts * ~home
    return path(e)

def kmeans(X, k, rng, n=10):
    best = None
    for _ in range(n):
        C = X[rng.choice(len(X), k, replace=False)]
        for _ in range(40):
            lab = ((X[:, None] - C[None]) ** 2).sum(2).argmin(1)
            C = np.array([X[lab == i].mean(0) if (lab == i).any() else C[i] for i in range(k)])
        w = ((X - C[lab]) ** 2).sum()
        if best is None or w < best[0]:
            best = (w, C, lab)
    return best

def section(t):
    print(f"\n{'=' * 10} {t} {'=' * 10}")

def main():
    assert shape(np.linspace(0, 30, GRID)) == "blowout" and shape(np.linspace(0, 7, GRID)) == "trade"
    assert shape(np.r_[np.linspace(0, 21, 31), np.linspace(21, 3, 30)]) == "comeback"
    assert list(path([[300, 7, 0], [1800, 0, 3]])[[4, 5, 30]]) == [0, 7, 4]
    rng = np.random.default_rng(7)
    games = [g for g in nfl(2026) + nfl(2025) + nfl(2024) + cfb() if g["ev"]]
    cuts = {"NFL 2026": [g for g in games if g["lg"] == "NFL" and g["season"] == 2026],
            "NFL 24-25": [g for g in games if g["lg"] == "NFL" and g["season"] < 2026],
            "CFB 2026": [g for g in games if g["lg"] == "CFB"]}

    alternation(cuts, rng)
    same_scoreboard(cuts)
    oos(cuts, rng)
    template(cuts)
    volume(cuts, rng)
    by_spread(cuts)
    classes(cuts, rng)

def alternation(cuts, rng):
    section("who scores next: P(the other team), actual vs shuffled, and final-margin sd vs coin")
    for nm, gs in cuts.items():
        def alt(ev):
            who = [e[1] > 0 for e in ev]
            return sum(a != b for a, b in zip(who, who[1:])), len(who) - 1
        a = np.sum([alt(g["ev"]) for g in gs], 0)
        b = np.sum([alt(np.array(g["ev"])[rng.permutation(len(g["ev"]))]) for g in gs for _ in range(20)], 0)
        sd = lambda f: np.std([f(g)[-1] for g in gs for _ in range(20)])
        print(f"  {nm:10s} alternates {a[0] / a[1]:.1%} vs shuffled {b[0] / b[1]:.1%} | final sd actual {np.std([path(g['ev'])[-1] for g in gs]):.1f} coin {sd(lambda g: coinflip(g, rng)):.1f}")

    section("is trading a state? P(other team scores next | the last two scores); all games, then |spread| <= 7")
    for nm, gs in cuts.items():
        for lim in (99, 7):
            c = collections.Counter()
            for g in gs:
                who = [e[1] > 0 for e in g["ev"]]
                if abs(g["sp"]) <= lim:
                    for x, y, z in zip(who, who[1:], who[2:]):
                        c[(x != y, y != z)] += 1
            print(f"  {nm:10s} |sp|<={lim:2d} after a trade {c[True, True] / (c[True, True] + c[True, False]):.1%} (n={c[True, True] + c[True, False]})"
                  f" | after same team twice {c[False, True] / (c[False, True] + c[False, False]):.1%} (n={c[False, True] + c[False, False]})")

    section("garbage time: leads of 14+ at minute 50, points over the last 10 minutes")
    for nm, gs in cuts.items():
        r = []
        for g in gs:
            m = path(g["ev"])
            if abs(m[50]) >= 14:
                s = np.sign(m[50])
                ld = sum(e[1] if s > 0 else e[2] for e in g["ev"] if e[0] > 3000)
                tr = sum(e[2] if s > 0 else e[1] for e in g["ev"] if e[0] > 3000)
                r.append((ld, tr))
        r = np.array(r, float)
        print(f"  {nm:10s} n={len(r)} leader {r[:, 0].mean():.1f} trailer {r[:, 1].mean():.1f}, trailer scored in {np.mean(r[:, 1] > 0):.0%}, leader in {np.mean(r[:, 0] > 0):.0%}")

def base_x(m, sp, t):
    rem = 1 - t / 60
    return [1, -m[t] * rem, -sp * rem]

def extras(m, t):
    """Shape so far, signed toward home, all scaled by time left like the leak term."""
    rem, now, seen = 1 - t / 60, m[t], m[:t + 1]
    off = (seen.max() - now) if now > 0 else (seen.min() - now) if now < 0 else 0   # lead given back
    return [rem * x for x in (off, now - m[t - 10], seen.mean() - now,
                              np.sign(now) * max(abs(now) - 14, 0), np.sign(now) * max(abs(now) - 24, 0))]
EXTRA = ["lead given back", "last-10 swing", "avg margin - now", "lead over 14", "lead over 24"]

def rows(gs):
    """One row per game per checkpoint: id, X base, X shape, resid."""
    gi, B, S, y = [], [], [], []
    for i, g in enumerate(gs):
        m = path(g["ev"])
        for t in CHECK:
            gi.append(i); B.append(base_x(m, g["sp"], t)); S.append(extras(m, t))
            y.append(m[-1] - m[t] + g["sp"] * (1 - t / 60))
    return np.array(gi), np.array(B), np.array(S), np.array(y)

def ols(X, y):
    return np.linalg.lstsq(X, y, rcond=None)[0]

def oos(cuts, rng):
    section("out of sample: resid = final - margin - exp*rem; R2 at the 5 checkpoints; delta from shape features")
    allnfl = cuts["NFL 24-25"] + cuts["NFL 2026"]
    splits = (("NFL fit 24-25 test 26", cuts["NFL 24-25"], cuts["NFL 2026"]),
              ("NFL fit 2024 test 25-26", [g for g in allnfl if g["season"] == 2024], [g for g in allnfl if g["season"] > 2024]),
              ("CFB fit wk1-5 test 6+", [g for g in cuts["CFB 2026"] if g["week"] <= 5], [g for g in cuts["CFB 2026"] if g["week"] > 5]))
    r2 = lambda e, y: 1 - (e ** 2).sum() / ((y - y.mean()) ** 2).sum()
    for nm, tr, te in splits:
        _, B, S, y = rows(tr)
        gi, Bt, St, yt = rows(te)
        b0, b1 = ols(B, y), ols(np.hstack([B, S]), y)
        e0, e1 = yt - Bt @ b0, yt - np.hstack([Bt, St]) @ b1
        ng = gi.max() + 1
        ds = []
        for _ in range(1000):
            w = np.bincount(rng.integers(0, ng, ng), minlength=ng)[gi]
            ds.append(1 - (w * e1 ** 2).sum() / (w * e0 ** 2).sum())
        print(f"  {nm:24s} k={b0[1]:.3f} R2 {r2(e0, yt):+.4f} -> {r2(e1, yt):+.4f}  error cut {1 - (e1 ** 2).sum() / (e0 ** 2).sum():+.2%} [{np.percentile(ds, 2.5):+.2%}, {np.percentile(ds, 97.5):+.2%}]")
    section("full-sample coefficients on shape features (game bootstrap 95% CI)")
    for nm, gs in (("NFL 24-26", allnfl), ("CFB 2026", cuts["CFB 2026"])):
        gi, B, S, y = rows(gs)
        X = np.hstack([B, S])
        by = [np.where(gi == i)[0] for i in range(len(gs))]
        boots = [ols(X[ix], y[ix]) for ix in (np.concatenate([by[j] for j in rng.integers(0, len(gs), len(gs))]) for _ in range(500))]
        lo, hi = np.percentile(boots, [2.5, 97.5], axis=0)
        print(f"  {nm}: leak with extras {ols(X, y)[1]:+.3f}, alone {ols(B, y)[1]:+.3f}")
        for j, f in enumerate(EXTRA):
            print(f"    {f:18s} {ols(X, y)[3 + j]:+.3f} [{lo[3 + j]:+.3f}, {hi[3 + j]:+.3f}]{' *' if lo[3 + j] > 0 or hi[3 + j] < 0 else ''}")

def template(cuts):
    """The literal version of fitting a game to a shape: at each checkpoint find the 40 most similar
    games so far (path every 5 min + spread) and add their average base-model miss to the forecast."""
    section("template match: base model + 40 nearest past paths, test-set error change")
    allnfl = cuts["NFL 24-25"] + cuts["NFL 2026"]
    for nm, tr, te in (("NFL fit 24-25 test 26", cuts["NFL 24-25"], cuts["NFL 2026"]),
                       ("NFL fit 2024 test 25-26", [g for g in allnfl if g["season"] == 2024], [g for g in allnfl if g["season"] > 2024]),
                       ("CFB fit wk1-5 test 6+", [g for g in cuts["CFB 2026"] if g["week"] <= 5], [g for g in cuts["CFB 2026"] if g["week"] > 5])):
        _, B, _, y = rows(tr)
        b = ols(B, y)
        res = []
        for ti, t in enumerate(CHECK):
            feat = lambda gs: np.array([np.r_[path(g["ev"])[5:t + 1:5], g["sp"]] for g in gs])
            ytr = (y - B @ b)[ti::len(CHECK)]
            Ft, Fe = feat(tr), feat(te)
            _, Be, _, ye = rows(te)
            e0 = (ye - Be @ b)[ti::len(CHECK)]
            nn = ((Fe[:, None] - Ft[None]) ** 2).sum(2).argsort(1)[:, :40]
            e1 = e0 - ytr[nn].mean(1)
            res.append(f"min {t}: {1 - (e1 ** 2).sum() / (e0 ** 2).sum():+.1%}")
        print(f"  {nm:24s}", "  ".join(res))

def vol_rows(gs):
    gi, B, S, Y = [], [], [], []
    for i, g in enumerate(gs):
        m = path(g["ev"])
        for t in (30, 40, 45, 50):
            rem = 1 - t / 60
            for home in (True, False):
                sg = 1 if home else -1
                d, mine = sg * m, [v for v in g["vol"] if v[1] == home]
                now, seen = d[t], d[:t + 1]
                n = lambda past, ispass: sum((v[0] <= t * 60) == past and v[2] == ispass for v in mine)
                gi.append(i)
                B.append([1, rem, now, now * rem, abs(now), abs(now) * rem, n(True, False) / t * rem, n(True, True) / t * rem, sg * g["sp"] * rem])
                S.append([rem * x for x in ((seen.max() - now) if now > 0 else (seen.min() - now) if now < 0 else 0, now - d[t - 10], seen.mean() - now)])
                Y.append([n(False, False), n(False, True)])
    return np.array(gi), np.array(B), np.array(S), np.array(Y, float)

def volume(cuts, rng):
    """Props: does the path so far change a team's remaining rushes/passes, given the scoreboard,
    the clock and the team's own usage so far?"""
    section("NFL props: remaining team rushes / passes, shape features on top of score+clock+usage")
    nflg = cuts["NFL 24-25"] + cuts["NFL 2026"]
    for held in (2024, 2025, 2026):
        tr, te = [g for g in nflg if g["season"] != held], [g for g in nflg if g["season"] == held]
        _, B, S, Y = vol_rows(tr)
        gi, Bt, St, Yt = vol_rows(te)
        for j, lab in enumerate(("rushes left", "passes left")):
            b1 = ols(np.c_[B, S], Y[:, j])
            e0, e1 = Yt[:, j] - Bt @ ols(B, Y[:, j]), Yt[:, j] - np.c_[Bt, St] @ b1
            ng = gi.max() + 1
            ds = []
            for _ in range(1000):
                w = np.bincount(rng.integers(0, ng, ng), minlength=ng)[gi]
                ds.append(1 - (w * e1 ** 2).sum() / (w * e0 ** 2).sum())
            print(f"  hold out {held} {lab}: error cut {1 - (e1 ** 2).sum() / (e0 ** 2).sum():+.2%} [{np.percentile(ds, 2.5):+.2%}, {np.percentile(ds, 97.5):+.2%}]"
                  f" rmse {(e0 ** 2).mean() ** .5:.2f}, coefs given-back/last-10/avg {np.round(b1[-3:], 2)}")

def by_spread(cuts):
    section("the pregame spread already calls much of the shape (share of games by |spread|)")
    for nm, gs in (("NFL 24-26", cuts["NFL 24-25"] + cuts["NFL 2026"]), ("CFB 2026", cuts["CFB 2026"])):
        tb = collections.defaultdict(collections.Counter)
        for g in gs:
            a = abs(g["sp"])
            tb["0-3.5" if a <= 3.5 else "4-7" if a <= 7 else "7.5-14" if a <= 14 else "14.5-24" if a <= 24 else "24.5+"][shape(orient(path(g["ev"])))] += 1
        print(f"  {nm:10s}" + "".join(f"{s:>13s}" for s in SHAPES))
        for k in ("0-3.5", "4-7", "7.5-14", "14.5-24", "24.5+"):
            n = sum(tb[k].values())
            if n:
                print(f"  {k:7s}{n:4d}" + "".join(f"{tb[k][s] / n:13.0%}" for s in SHAPES))

def same_scoreboard(cuts):
    section("same scoreboard, different history (leader = the team ahead/that was ahead; resid vs fair line)")
    for nm, gs in (("NFL 24-26", cuts["NFL 24-25"] + cuts["NFL 2026"]), ("CFB 2026", cuts["CFB 2026"])):
        _, B, _, y = rows(gs)
        b = ols(B, y)
        for t in (30, 45):
            buckets = collections.defaultdict(list)
            rem = 1 - t / 60
            for g in gs:
                m = path(g["ev"])
                now, seen = m[t], m[:t + 1]
                r = m[-1] - now + g["sp"] * rem - np.array(base_x(m, g["sp"], t)) @ b
                big = 1 if seen.max() >= -seen.min() else -1          # team with the biggest lead so far
                pk = (big * seen).max()
                pts = sum(e[1] + e[2] for e in g["ev"] if e[0] > t * 60)
                if abs(now) <= 8:
                    k = "one score, never separated" if pk <= 10 else "one score, 14+ lead blown" if pk >= 14 else None
                    s = big
                else:
                    s = np.sign(now)
                    lead, swing = abs(now), s * (now - m[t - 10])
                    if lead < 14: k = "lead 9-13, at peak" if lead >= pk else "lead 9-13, was bigger"
                    else: k = "lead 14+, last-10 run 7+" if swing >= 7 else "lead 14+, was 7+ bigger" if pk - lead >= 7 else "lead 14+, steady"
                if k:
                    buckets[k].append((s * r, r, pts, s * (m[-1] - now), s * m[-1] > 0, s * m[t]))
            print(f"  {nm} minute {t}")
            for k in sorted(buckets):
                a = np.array(buckets[k], float)
                n = len(a)
                print(f"    {k:28s} n={n:4d} avg lead {a[:, 5].mean():+5.1f} | margin-change {a[:, 3].mean():+5.1f} | resid {a[:, 0].mean():+5.1f} +-{1.96 * a[:, 0].std() / n ** .5:4.1f} | sd {a[:, 0].std():4.1f} | pts left {a[:, 2].mean():4.1f} | wins {a[:, 4].mean():4.0%}")

def classes(cuts, rng):
    section("what the halftime margin says about the final shape (share of games)")
    for nm, gs in (("NFL 24-26", cuts["NFL 24-25"] + cuts["NFL 2026"]), ("CFB 2026", cuts["CFB 2026"])):
        tb = collections.defaultdict(collections.Counter)
        for g in gs:
            m = path(g["ev"])
            h = abs(m[30])
            tb["0-3" if h <= 3 else "4-8" if h <= 8 else "9-13" if h <= 13 else "14-20" if h <= 20 else "21+"][shape(orient(m))] += 1
        print(f"  {nm:10s}" + "".join(f"{s:>13s}" for s in SHAPES))
        for k in ("0-3", "4-8", "9-13", "14-20", "21+"):
            n = sum(tb[k].values())
            print(f"  {k:6s}{n:4d}" + "".join(f"{tb[k][s] / n:13.0%}" for s in SHAPES))

    section("shape shares: actual vs shuffled order vs spread-weighted coin (200 draws)")
    for nm, gs in cuts.items():
        act = collections.Counter(shape(orient(path(g["ev"]))) for g in gs)
        a, b = collections.Counter(), collections.Counter()
        for g in gs:
            for _ in range(200):
                a[shape(orient(shuffled(g, rng)))] += 1
                b[shape(orient(coinflip(g, rng)))] += 1
        print(f"{nm} ({len(gs)} games)")
        for s in SHAPES:
            p, n = act[s] / len(gs), len(gs)
            print(f"  {s:13s} {act[s]:4d} {p:5.1%} +-{1.96 * (p * (1 - p) / n) ** .5:4.1%}   shuffled {a[s] / n / 200:5.1%}   coin {b[s] / n / 200:5.1%}")

    section("path PCA: share of variance in the first 4 components (random walk = same as shuffled)")
    for nm, gs in cuts.items():
        for lab, X in (("actual", np.array([orient(path(g["ev"])) for g in gs])),
                       ("shuffled", np.array([orient(shuffled(g, rng)) for g in gs for _ in range(5)]))):
            sv = np.linalg.svd(X[:, 5::5] - X[:, 5::5].mean(0), compute_uv=False) ** 2
            print(f"  {nm:10s} {lab:9s}", np.round(sv[:4] / sv.sum(), 3))

    section("k-means within-cluster variance, actual/shuffled: no elbow or gap = no natural clusters")
    for nm, gs in cuts.items():
        X = np.array([orient(path(g["ev"]))[5::5] for g in gs])
        Y = np.array([orient(shuffled(g, rng))[5::5] for g in gs])
        print(f"  {nm:10s}", " ".join(f"k{k}:{kmeans(X, k, rng)[0] / ((X - X.mean(0)) ** 2).sum():.3f}/{kmeans(Y, k, rng)[0] / ((Y - Y.mean(0)) ** 2).sum():.3f}" for k in range(2, 8)))
    for nm in ("NFL 24-25", "CFB 2026"):
        X = np.array([orient(path(g["ev"]))[5::5] / max(12, orient(path(g["ev"])).max()) for g in cuts[nm]])
        _, C, lab = kmeans(X, 6, rng, 20)
        Y = np.array([(lambda o: o[5::5] / max(12, o.max()))(orient(shuffled(g, rng))) for g in cuts[nm] for _ in range(20)])
        ylab = ((Y[:, None] - C[None]) ** 2).sum(2).argmin(1)
        print(f"  {nm} shape-only k=6 centroids (share of peak at min 10,20..60), peak floor 12; shuffled games on the same centroids")
        for i in np.argsort(-np.bincount(lab)):
            print(f"    {np.mean(lab == i):5.1%} vs {np.mean(ylab == i):5.1%}", np.round(C[i, 1::2], 2))

if __name__ == "__main__":
    main()
