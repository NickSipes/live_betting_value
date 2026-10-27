#!/usr/bin/env python3
"""Does in-game momentum predict future scoring beyond the market baseline?

Builders: first downs, touchdowns, explosive plays (rush 10+/pass 20+).
Killers: turnovers, three-and-outs, red-zone stalls, turnover on downs.

At the end of every drive, features are each team's events in the last 10 game
minutes (home minus away) plus the first downs on each side's most recent drive.
Two targets, both from the HOME side:
  next10  = net points (home - away) over the next 10 game minutes
  resid   = (final margin - current margin) - spread_line * fraction_remaining
            (what the home team does the rest of the game BEYOND what the
             pregame market expected at this point; betting edge lives here)
Controls: current margin, fraction remaining, their product, spread_line.
Inference: OLS on standardized features with a block bootstrap BY GAME (momentum
windows overlap within a game, so play-level t-stats would be fake).
Out-of-sample: fit weeks 1-4, test weeks 5-7.

Also: lead-holding by team quality, and whether week-7 live spreads already
price momentum (data/fly/docs/archive/nfl_w7.json).

Run: .venv/bin/python analysis/momentum.py [season]   (default 2026)
"""
import gzip, csv, json, os, sys, collections
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WINDOW = 600.0   # momentum lookback, game-seconds

COLS = ["week", "game_id", "home_team", "away_team", "spread_line", "posteam",
        "game_seconds_remaining", "fixed_drive", "fixed_drive_result",
        "drive_first_downs", "drive_inside20", "first_down", "td_team",
        "interception", "fumble_lost", "rush", "pass", "sack", "yards_gained",
        "total_home_score", "total_away_score", "qtr"]

def f(v, d=0.0):
    try:
        return float(v)
    except (ValueError, TypeError):
        return d

def load(season):
    games = collections.defaultdict(list)
    with gzip.open(f"{ROOT}/data/pbp_{season}.csv.gz", "rt") as fh:
        for r in csv.DictReader(fh):
            games[r["game_id"]].append({c: r[c] for c in COLS})
    return games

def game_events(plays):
    """(gsr, team, kind) builders/killers + drive summaries, in play order."""
    ev, drives = [], {}
    for p in plays:
        gsr, t = f(p["game_seconds_remaining"]), p["posteam"]
        if p["td_team"]:
            ev.append((gsr, p["td_team"], "td"))
        if not t:
            continue
        if p["first_down"] == "1":
            ev.append((gsr, t, "fd"))
        if p["sack"] != "1" and ((p["rush"] == "1" and f(p["yards_gained"]) >= 10)
                                 or (p["pass"] == "1" and f(p["yards_gained"]) >= 20)):
            ev.append((gsr, t, "expl"))
        if p["interception"] == "1" or p["fumble_lost"] == "1":
            ev.append((gsr, t, "to"))
        d = drives.setdefault(p["fixed_drive"], {"team": t, "end": gsr,
                                                 "result": p["fixed_drive_result"],
                                                 "fd": f(p["drive_first_downs"]),
                                                 "in20": p["drive_inside20"] == "1"})
        d["end"] = min(d["end"], gsr)
    for d in drives.values():
        if d["result"] == "Punt" and d["fd"] == 0:
            ev.append((d["end"], d["team"], "3out"))
        if d["in20"] and d["result"] != "Touchdown":
            ev.append((d["end"], d["team"], "rz_fg" if d["result"] == "Field goal" else "rz_fail"))
        if d["result"] == "Turnover on downs":
            ev.append((d["end"], d["team"], "downs"))
    return ev, drives

FEATS = ["fd", "expl", "td", "to", "3out", "rz_fail", "rz_fg", "downs", "last_drive_fd"]

def window_feats(ev, drives, home, away, gsr_t):
    """home-minus-away event counts in (gsr_t, gsr_t + WINDOW]."""
    c = collections.Counter()
    for gsr, team, kind in ev:
        if gsr_t < gsr <= gsr_t + WINDOW:
            c[kind] += 1 if team == home else -1
    last = {home: 0.0, away: 0.0}
    for d in sorted(drives.values(), key=lambda d: -d["end"]):
        if d["end"] >= gsr_t and d["team"] in last:
            last[d["team"]] = d["fd"]
    return [c[k] for k in FEATS[:-1]] + [last[home] - last[away]]

def build(games, min_gsr=300):
    """One row per drive end (gsr > min_gsr): features, controls, targets."""
    rows = []
    for gid, plays in games.items():
        home, away = plays[0]["home_team"], plays[0]["away_team"]
        spread = f(plays[0]["spread_line"])   # expected home margin (positive = home favored)
        week = int(plays[0]["week"])
        ev, drives = game_events(plays)
        th = [(f(p["game_seconds_remaining"]), f(p["total_home_score"]), f(p["total_away_score"]))
              for p in plays]
        fin = th[-1][1] - th[-1][2]
        def margin_at(g):
            past = [x for x in th if x[0] >= g]
            return (past[-1][1] - past[-1][2]) if past else 0.0
        def pts_next(g, w=WINDOW):
            a, b = margin_at(g), margin_at(max(g - w, 0.0))
            return b - a
        dsort = sorted(drives.values(), key=lambda d: -d["end"])
        for d in drives.values():
            g = d["end"]
            if g < min_gsr or g > 3600 - WINDOW:   # need history behind and game ahead
                continue
            m = margin_at(g)
            fr = g / 3600.0
            nxt = next((dd for dd in dsort if dd["end"] < g), None)   # who possesses next
            rows.append({"gid": gid, "week": week, "gsr": g, "margin": m, "spread": spread,
                         "next_poss": 0 if not nxt else 1 if nxt["team"] == home else -1,
                         "x": window_feats(ev, drives, home, away, g),
                         "next10": pts_next(g),
                         "resid": (fin - m) - spread * fr, "frac": fr})
    return rows

def ols(X, y):
    b, *_ = np.linalg.lstsq(X, y, rcond=None)
    return b

def r2(X, y, b):
    e = y - X @ b
    return 1 - e @ e / ((y - y.mean()) @ (y - y.mean()))

def design(rows, with_mom):
    C = np.array([[1.0, r["margin"], r["frac"], r["margin"] * r["frac"], r["spread"], r["next_poss"]]
                  for r in rows])
    if not with_mom:
        return C
    M = np.array([r["x"] for r in rows], float)
    return np.hstack([C, M])

def boot_ci(rows, target, n=2000, seed=7):
    """Block bootstrap by game: 95% CI for each momentum coefficient (standardized)."""
    bygame = collections.defaultdict(list)
    for r in rows:
        bygame[r["gid"]].append(r)
    gids = list(bygame)
    X = design(rows, True)
    sd = X.std(0); sd[sd == 0] = 1
    y = np.array([r[target] for r in rows])
    rng = np.random.default_rng(seed)
    coefs = []
    for _ in range(n):
        samp = [r for g in rng.choice(gids, len(gids)) for r in bygame[g]]
        Xs = design(samp, True)
        ys = np.array([r[target] for r in samp])
        coefs.append(ols(Xs / sd, ys))
    coefs = np.array(coefs)
    full = ols(X / sd, y)
    return full, np.percentile(coefs, [2.5, 97.5], axis=0)

def section(t):
    print(f"\n{'='*12} {t} {'='*12}")

def main(season):
    games = load(season)
    rows = build(games)
    weeks = sorted({r["week"] for r in rows})
    print(f"season {season}: {len(games)} games, {len(rows)} drive-end evaluation points, weeks {weeks[0]}-{weeks[-1]}")

    names = ["const", "margin", "frac_left", "margin*frac", "spread", "next_poss"] + FEATS
    for target in ("next10", "resid"):
        section(f"target: {target}  (home perspective, standardized coefficients)")
        full, ci = boot_ci(rows, target)
        for i, nm in enumerate(names):
            star = " *" if ci[0][i] > 0 or ci[1][i] < 0 else ""
            print(f"  {nm:13s} {full[i]:+.3f}  [{ci[0][i]:+.3f}, {ci[1][i]:+.3f}]{star}")
        # out of sample: weeks 1-4 -> 5+
        cut = 4
        tr = [r for r in rows if r["week"] <= cut]
        te = [r for r in rows if r["week"] > cut]
        ytr = np.array([r[target] for r in tr]); yte = np.array([r[target] for r in te])
        for lab, wm in (("controls only", False), ("controls+momentum", True)):
            Xtr, Xte = design(tr, wm), design(te, wm)
            b = ols(Xtr, ytr)
            print(f"  OOS w{cut+1}-{weeks[-1]} R2 {lab:18s}: {r2(Xte, yte, b):+.4f}")
        # does the sign of the momentum part alone predict the target's sign?
        nC = design(tr, False).shape[1]
        bM = ols(design(tr, True), ytr)
        mom = design(te, True)[:, nC:] @ bM[nC:]
        strong = np.abs(mom) >= np.percentile(np.abs(mom), 75)
        hit = np.mean(np.sign(mom[strong]) == np.sign(yte[strong]))
        base = np.mean(yte[strong] > 0)
        print(f"  top-quartile |momentum| sign hit rate OOS: {hit:.1%} (n={strong.sum()}, home-positive base {base:.1%})")

    section("lead holding by team quality (leads of 4+ at any Q2/Q3 drive end)")
    seen = {}
    for r in rows:
        if 900 <= 3600 - r["gsr"] <= 2700 and abs(r["margin"]) >= 4:
            leader_home = r["margin"] > 0
            sp = r["spread"] if leader_home else -r["spread"]
            fin_m = r["margin"] + (r["resid"] + r["spread"] * r["frac"])
            won = fin_m > 0 if leader_home else fin_m < 0
            key = (r["gid"], leader_home)
            seen.setdefault(key, (sp, abs(r["margin"]), won))
    buckets = collections.defaultdict(lambda: [0, 0])
    for sp, lead, won in seen.values():
        q = "favorite (-3 or more)" if sp >= 3 else "underdog (+3 or more)" if sp <= -3 else "near pick'em"
        l = "4-7" if lead <= 7 else "8-13" if lead <= 13 else "14+"
        for k in ((q, l), (q, "all")):
            buckets[k][0] += won
            buckets[k][1] += 1
    for (q, l) in sorted(buckets):
        w, n = buckets[(q, l)]
        print(f"  {q:22s} lead {l:4s}: held to a win {w}/{n} = {w/n:.0%}")

    section("week-7 live spreads: is momentum already priced in? (12 games)")
    if season != 2026:
        print("  (2026 archive only; skipped)")
        return
    try:
        arch = json.load(open(f"{ROOT}/data/fly/docs/archive/nfl_w7.json"))
    except FileNotFoundError:
        print("  no live archive; skipped")
        return
    A = {"LAR": "LA", "WSH": "WAS"}
    bygame = {}
    for gid, plays in games.items():
        if int(plays[0]["week"]) == 7:
            bygame[(plays[0]["away_team"], plays[0]["home_team"])] = plays
    pts = []
    for g in arch.values():
        key = (A.get(g["meta"]["aabbr"], g["meta"]["aabbr"]), A.get(g["meta"]["habbr"], g["meta"]["habbr"]))
        plays = bygame.get(key)
        if not plays:
            continue
        ev, drives = game_events(plays)
        home, away = key[1], key[0]
        asf, hsf = map(float, g["final"].split("-"))
        fin = hsf - asf
        seen_gc = set()
        for s in g["snaps"]:
            gc, live, m = s[0], s[1], s[2]
            if live is None or not (600 <= gc <= 3000) or round(gc / 60) in seen_gc:
                continue
            seen_gc.add(round(gc / 60))
            gsr = 3600 - gc
            x = window_feats(ev, drives, home, away, gsr)
            fair = -(m + g["pregame"] * gsr / 3600.0)
            pts.append({"x": x, "gap": live - fair, "cover": fin + live,
                        "gid": key, "live": live, "m": m})
    M = np.array([p["x"] for p in pts], float)
    w_simple = np.array([1, 1, 2, -3, -1, -2, -1, -2, 0.5])   # builders +, killers - (a priori)
    mom = M @ w_simple
    gap = np.array([p["gap"] for p in pts])
    cov = np.array([p["cover"] for p in pts])   # >0: home covered the live line
    print(f"  {len(pts)} deduped snapshots across {len({p['gid'] for p in pts})} games")
    c = np.corrcoef(mom, gap)[0, 1]
    print(f"  corr(momentum score, live - fair gap): {c:+.2f}  "
          f"({'books shade WITH momentum' if c > 0.1 else 'books do NOT shade with momentum' if abs(c) <= 0.1 else 'books shade AGAINST momentum'})")
    for thr in (2, 3, 4):
        sel = mom >= thr
        n, wn = sel.sum(), (cov[sel] > 0).sum()
        sel2 = mom <= -thr
        n2, wn2 = sel2.sum(), (cov[sel2] < 0).sum()
        print(f"  bet WITH momentum |M|>={thr}: home side {wn}/{n}, away side {wn2}/{n2}, "
              f"combined {wn+wn2}/{n+n2}" + (f" = {(wn+wn2)/(n+n2):.0%}" if n + n2 else ""))
    # snapshots within a game are one bet, not many: collapse |M|>=4 to game-sides
    sides = collections.defaultdict(lambda: [0, 0])
    for p, mv, cv in zip(pts, mom, cov):
        if mv >= 4:
            sides[(p["gid"], "home")][int(cv > 0)] += 1
        elif mv <= -4:
            sides[(p["gid"], "away")][int(cv < 0)] += 1
    print(f"  |M|>=4 collapsed to {len(sides)} independent game-sides:")
    for (gid, side), (l, w) in sorted(sides.items()):
        print(f"    {gid[0]} @ {gid[1]:3s} {side:4s}: covered on {w}/{w+l} selected snaps")

if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 2026)
