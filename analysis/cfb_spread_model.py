#!/usr/bin/env python3
"""CFB against-the-spread edge model: the NFL analysis (analysis/momentum.py) run on this
season's FBS games, from ESPN summaries (free). Feeds live/cfb_poller.py fair_spread.

At every drive end (home perspective):
  resid = final margin - current margin - exp*rem      exp = -pregame home spread,
                                                        rem = fraction of game left
Fit resid on
  lead leak    -margin*rem                              NFL: 0.28
  pregame      -sp*rem  (extra weight beyond the 1.0 already in resid; ~0 = market weight right)
  possession   EP(next drive start) * min(1, 8*rem)     NFL: ~0.9 x (0.065*yl - 1.3)
  momentum     home-minus-away events in the last 10 game-min:
               fd, expl (rush 10+/pass 20+), td, to, 3out, rz_fail, rz_fg, downs
Block bootstrap by game (rows within a game correlate). OOS: fit weeks 1-5, test 6+.
Then a live backtest: fair line vs the archived live spreads (data/fly/docs/archive/cfb_w*.json),
cover rate when betting WITH the model's edge, collapsed to independent game-sides.

Usage: .venv/bin/python analysis/cfb_spread_model.py [season] [--refresh]"""
import json, os, re, sys, glob, collections, urllib.request
from concurrent.futures import ThreadPoolExecutor
import numpy as np

SEASON = next((int(a) for a in sys.argv[1:] if a.isdigit()), 2026)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = f"{ROOT}/data/cfb_pbp_{SEASON}.json"
B = "https://site.api.espn.com/apis/site/v2/sports/football/college-football"
WINDOW = 600
FEATS = ["fd", "expl", "td", "to", "3out", "rz_fail", "rz_fg", "downs"]
NOT_SCRIMMAGE = {"Kickoff", "Punt", "Punt Return", "Timeout", "End Period", "End of Game",
                 "Field Goal Good", "Field Goal Missed", "Blocked Field Goal", "Penalty"}

def get(u):
    return json.load(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=20))

def gc_of(p):
    m, s = p["clock"]["displayValue"].split(":")
    return (p["period"]["number"] - 1) * 900 + 900 - (int(m) * 60 + int(s))

def own_yl(text, abbr):
    """'LIB 24' -> 24 if LIB has the ball, else 76. 50 at midfield / unparseable."""
    m = re.fullmatch(r"(\S+) (\d+)", text or "")
    return 50 if not m else int(m.group(2)) if m.group(1) == abbr else 100 - int(m.group(2))

def game(eid):
    """{sp, final, week, drives: [[side, gc0, gc1, yl0, result, in20, margin1]], ev: [[gc, side, kind]]}"""
    try:
        s = get(f"{B}/summary?event={eid}")
        pc = s.get("pickcenter") or []
        if not pc or pc[0].get("spread") is None:
            return None
        comps = s["header"]["competitions"][0]["competitors"]
        home = next(c for c in comps if c["homeAway"] == "home")
        score = {c["homeAway"]: int(c["score"]) for c in comps}
        drives, ev = [], []
        for d in s["drives"]["previous"]:
            plays = [p for p in d.get("plays", []) if p["period"]["number"] <= 4]
            if not plays:
                continue
            side = "home" if d["team"]["id"] == home["id"] else "away"
            ab = d["team"]["abbreviation"]
            prev = (plays[0]["homeScore"], plays[0]["awayScore"])
            for i, p in enumerate(plays):
                gc, t = gc_of(p), (p.get("type") or {}).get("text", "")
                hs, as_ = p["homeScore"], p["awayScore"]
                if i:   # score change since the previous play (defensive TDs land on the right side)
                    if hs - prev[0] >= 6: ev.append([gc, "home", "td"])
                    if as_ - prev[1] >= 6: ev.append([gc, "away", "td"])
                prev = (hs, as_)
                if t in NOT_SCRIMMAGE:
                    continue
                y = p.get("statYardage") or 0
                if (t.startswith("Rush") and y >= 10) or (t.startswith("Pass") and y >= 20):
                    ev.append([gc, side, "expl"])
                if p.get("isTurnover") or t.startswith("Interception") or t.startswith("Pass Interception") \
                        or t == "Fumble Recovery (Opponent)":
                    ev.append([gc, side, "to"])
                e = p.get("end") or {}
                if e.get("down") == 1 and not p.get("scoringPlay") and (e.get("team") or {}).get("id") == d["team"]["id"]:
                    ev.append([gc, side, "fd"])
            res = d.get("result", "")
            gc1 = gc_of(plays[-1])
            in20 = own_yl(d.get("end", {}).get("text"), ab) >= 80
            if res == "PUNT" and d.get("offensivePlays", 9) <= 3:
                ev.append([gc1, side, "3out"])
            if res == "DOWNS":
                ev.append([gc1, side, "downs"])
            if in20 and res not in ("TD", "END OF HALF", "END OF GAME"):
                ev.append([gc1, side, "rz_fg" if res == "FG" else "rz_fail"])
            drives.append([side, gc_of(plays[0]), gc1, own_yl(d.get("start", {}).get("text"), ab), res, in20,
                           plays[-1]["homeScore"] - plays[-1]["awayScore"]])
        if len(drives) < 10:
            return None
        return {"sp": float(pc[0]["spread"]), "final": score["home"] - score["away"],
                "week": s["header"].get("week", 0), "drives": drives, "ev": sorted(ev)}
    except Exception:
        return None

def pull():
    ids = set()
    for wk in range(1, 17):
        try:
            sb = get(f"{B}/scoreboard?groups=80&limit=300&seasontype=2&week={wk}&dates={SEASON}")
        except Exception:
            continue
        ids |= {e["id"] for e in sb.get("events", []) if e["status"]["type"]["state"] == "post"}
    ids = sorted(ids)
    with ThreadPoolExecutor(16) as ex:
        return {i: g for i, g in zip(ids, ex.map(game, ids)) if g}

def ep(yl):
    return 0.065 * yl - 1.3   # same linear EP the poller uses (own 20 ~ 0, midfield ~ 2)

def build(games):
    rows = []
    for gid, g in games.items():
        sp, fin = g["sp"], g["final"]
        for i, (side, gc0, gc1, yl0, res, in20, m) in enumerate(g["drives"][:-1]):
            rem = 1 - gc1 / 3600
            if rem <= 0.05:
                continue
            nside, _, _, nyl = g["drives"][i + 1][:4]
            poss = ep(nyl) * (1 if nside == "home" else -1) * min(1.0, 8 * rem)
            c = collections.Counter()
            for gc, s, k in g["ev"]:
                if gc1 - WINDOW < gc <= gc1:
                    c[k] += 1 if s == "home" else -1
            rows.append({"gid": gid, "week": g["week"], "gc": gc1, "margin": m, "rem": rem, "sp": sp,
                         "poss": poss, "x": [c[k] for k in FEATS],
                         "resid": fin - m - (-sp) * rem, "fin": fin})
    return rows

def design(rows, mom):
    C = np.array([[1.0, -r["margin"] * r["rem"], -r["sp"] * r["rem"], r["poss"]] for r in rows])
    return np.hstack([C, np.array([r["x"] for r in rows], float)]) if mom else C

def ols(X, y):
    return np.linalg.lstsq(X, y, rcond=None)[0]

def r2(X, y, b):
    e = y - X @ b
    return 1 - e @ e / ((y - y.mean()) @ (y - y.mean()))

def boot(rows, n=1000, seed=7):
    bygame = collections.defaultdict(list)
    for r in rows:
        bygame[r["gid"]].append(r)
    gids = list(bygame)
    y = np.array([r["resid"] for r in rows])
    rng = np.random.default_rng(seed)
    coefs = []
    for _ in range(n):
        samp = [r for g in rng.choice(gids, len(gids)) for r in bygame[g]]
        coefs.append(ols(design(samp, True), np.array([r["resid"] for r in samp])))
    return ols(design(rows, True), y), np.percentile(coefs, [2.5, 97.5], axis=0)

def section(t):
    print(f"\n{'='*12} {t} {'='*12}")

def live_backtest(k, cp):
    """Fair line with the fitted leak/possession vs archived live lines. Snaps before the
    situation field existed have no possession: EP term is 0 there (noted in the output)."""
    arch = {}
    for f in sorted(glob.glob(f"{ROOT}/data/fly/docs/archive/cfb_w*.json")):
        arch.update(json.load(open(f)))
    pts, nosit = [], 0
    for gid, g in arch.items():
        if g.get("pregame") is None or not g.get("final"):
            continue
        a, h = map(float, g["final"].split("-"))
        fin, meta, seen = h - a, g["meta"], set()
        for sn in g["snaps"]:
            gc, L, m = sn[0], sn[1], sn[2]
            if L is None or not (300 <= gc <= 3300) or gc // 60 in seen:
                continue
            seen.add(gc // 60)
            rem = 1 - gc / 3600
            sit = sn[3] if len(sn) > 3 else None
            e = 0.0
            if sit:
                parts = sit.split(" · ")
                ab = parts[0].removesuffix(" ball")
                side = 1 if ab == meta["habbr"] else -1 if ab == meta["aabbr"] else 0
                mm = re.fullmatch(r"(?:(\S+) )?(\d+)", parts[1]) if len(parts) > 1 else None
                yl = 50 if not mm else int(mm.group(2)) if mm.group(1) in (None, ab) else 100 - int(mm.group(2))
                e = side * ep(yl) * min(1.0, 8 * rem)
            else:
                nosit += 1
            fair = -(m * (1 - k * rem) + cp * e + (-g["pregame"]) * rem)
            pts.append((gid, L - fair, fin + L))   # edge > 0: home side; fin + L > 0: home covered
    print(f"  {len(pts)} deduped snapshots, {len({p[0] for p in pts})} games "
          f"({nosit} snapshots without possession info: EP term 0)")
    for thr in (2, 3, 4, 6):
        sides, n, w = {}, 0, 0
        for gid, edge, cov in pts:
            if abs(edge) < thr:
                continue
            hit = (cov > 0) if edge > 0 else (cov < 0)
            n += 1; w += hit
            sides.setdefault((gid, edge > 0), hit)   # first flag of each game-side = the bet
        gs = sum(sides.values())
        print(f"  |edge| >= {thr}: snapshots {w}/{n}" + (f" = {w/n:.0%}" if n else "") +
              f" | game-sides {gs}/{len(sides)}" + (f" = {gs/len(sides):.0%}" if sides else "") +
              "   (breakeven 52.4%)")

def main():
    if "--refresh" in sys.argv or not os.path.exists(CACHE):
        json.dump(pull(), open(CACHE, "w"))
    games = json.load(open(CACHE))
    rows = build(games)
    weeks = sorted({r["week"] for r in rows})
    print(f"{SEASON}: {len(games)} FBS games, {len(rows)} drive ends, weeks {weeks[0]}-{weeks[-1]}")
    assert abs(np.corrcoef([g["sp"] for g in games.values()], [g["final"] for g in games.values()])[0, 1] + 0.7) < 0.2, \
        "pregame spread should track the final margin (sign convention / key alignment)"

    section("resid = final - margin - exp*rem  on controls + momentum (game-block bootstrap 95% CI)")
    names = ["const", "lead leak k", "pregame extra c", "possession (x EP)"] + FEATS
    full, ci = boot(rows)
    for i, nm in enumerate(names):
        star = " *" if ci[0][i] > 0 or ci[1][i] < 0 else ""
        ref = {"lead leak k": "  NFL 0.28", "pregame extra c": "  NFL 0 (weight 1.0)", "possession (x EP)": "  NFL ~0.9"}.get(nm, "")
        print(f"  {nm:18s} {full[i]:+.3f}  [{ci[0][i]:+.3f}, {ci[1][i]:+.3f}]{star}{ref}")
    k, cp = full[1], full[3]

    cut = 5
    tr = [r for r in rows if r["week"] <= cut]; te = [r for r in rows if r["week"] > cut]
    ytr = np.array([r["resid"] for r in tr]); yte = np.array([r["resid"] for r in te])
    section(f"out of sample: fit weeks {weeks[0]}-{cut}, test {cut+1}-{weeks[-1]} ({len(te)} drive ends)")
    for lab, mom in (("controls only", False), ("controls+momentum", True)):
        b = ols(design(tr, mom), ytr)
        print(f"  R2 {lab:18s}: {r2(design(te, mom), yte, b):+.4f}")
    bM = ols(design(tr, True), ytr)
    mom = design(te, True)[:, 4:] @ bM[4:]
    strong = np.abs(mom) >= np.percentile(np.abs(mom), 75)
    print(f"  top-quartile |momentum| sign hit rate OOS: {np.mean(np.sign(mom[strong]) == np.sign(yte[strong])):.1%} "
          f"(n={strong.sum()}, home-positive base {np.mean(yte[strong] > 0):.1%})")

    section("lead holding by team quality (leads of 4+ at any Q2/Q3 drive end)")
    seen = {}
    for r in rows:
        if 900 <= r["gc"] <= 2700 and abs(r["margin"]) >= 4:
            lh = r["margin"] > 0
            sp = -r["sp"] if lh else r["sp"]     # leader's expected margin, positive = favorite
            won = r["fin"] > 0 if lh else r["fin"] < 0
            seen.setdefault((r["gid"], lh), (sp, abs(r["margin"]), won))
    buckets = collections.defaultdict(lambda: [0, 0])
    for sp, lead, won in seen.values():
        q = "favorite (-3 or more)" if sp >= 3 else "underdog (+3 or more)" if sp <= -3 else "near pick'em"
        l = "4-7" if lead <= 7 else "8-13" if lead <= 13 else "14+"
        for key in ((q, l), (q, "all")):
            buckets[key][0] += won; buckets[key][1] += 1
    for (q, l) in sorted(buckets):
        w, n = buckets[(q, l)]
        print(f"  {q:22s} lead {l:4s}: held to a win {w}/{n} = {w/n:.0%}")

    section(f"live backtest vs archived CFB lines, fair = margin*(1-{k:.2f}*rem) + {cp:.2f}*EP + exp*rem")
    live_backtest(k, cp)
    section("same backtest with the deployed poller coefficients (k=0.24, EP x1.0)")
    live_backtest(0.24, 1.0)

if __name__ == "__main__":
    main()
