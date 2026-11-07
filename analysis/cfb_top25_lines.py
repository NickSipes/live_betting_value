#!/usr/bin/env python3
"""Top-25 line check: does the spread on a ranked team's game sit where the results say it should?

Rating: least-squares margin rating (margin = home - away + HFA, margins capped at +-28) fitted on the
season's earlier weeks, each team pulled toward half its final rating from the season before (a team with
no prior season sits at FCS level). Per game, against the posted line: the rating's spread, the
common-opponent spread, last meetings (2024-), each team's home/road margins and last-3 form.
Edge is in home-spread sign: positive = the line gives the home side more points than the rating says.

Run: .venv/bin/python analysis/cfb_top25_lines.py [--refresh]   # this week's ranked games, from ESPN
     .venv/bin/python analysis/cfb_top25_lines.py --backtest     # 2025 + 2026 weeks 5+, bet the rating's side
     .venv/bin/python analysis/cfb_top25_lines.py --check        # fit recovers a planted rating
     .venv/bin/python analysis/cfb_top25_lines.py --hfa          # home field by conference, 2025, 2026, pooled
  --conf on any of the above: home field per the home team's conference instead of one number
"""
import sys, json, collections, urllib.request
import numpy as np
from cfb_spots import *

CAP, LAM, SHRINK, FCS = 28, 2.0, 0.5, -20.0   # ponytail: hand-set; LAM = games of evidence the prior is worth
CONF = {"1": "ACC", "151": "American", "4": "Big 12", "5": "Big Ten", "12": "CUSA", "18": "Indep", "15": "MAC", "17": "MWC", "9": "Pac-12", "8": "SEC", "37": "Sun Belt"}
BY_CONF = "--conf" in sys.argv   # home field fitted per home team's conference instead of one number

def conf_of(season):
    """{team abbr: conference name}, FBS conferences only; everyone else is 'other' (FCS)."""
    m = json.load(open(f"{ROOT}/data/cfb_conf_{season}.json"))
    return {t: CONF.get(str(c), "other") for t, c in m.items()}

def fit(games, prior, default, conf=None, se=False):
    """games: [(home, away, margin, neutral)] -> ({team: rating}, hfa), ridge toward prior.get(t, default).
    conf = {team: conference}: hfa becomes {conference: hfa} by the home team's conference (se=True adds errors)."""
    teams = sorted({t for g in games for t in g[:2]})
    cs = sorted({conf.get(t, "other") for t in teams}) if conf else [""]
    ix, n = {t: i for i, t in enumerate(teams)}, len(teams)
    ic = {c: n + i for i, c in enumerate(cs)}
    rows, y = [], []
    for h, a, m, neu in games:
        r = np.zeros(n + len(cs)); r[ix[h]], r[ix[a]] = 1, -1
        if not neu:
            r[ic[conf.get(h, "other") if conf else ""]] = 1
        rows.append(r); y.append(max(-CAP, min(CAP, m)))
    for t in teams:
        r = np.zeros(n + len(cs)); r[ix[t]] = LAM ** .5
        rows.append(r); y.append(LAM ** .5 * prior.get(t, default))
    X, Y = np.array(rows), np.array(y)
    x = np.linalg.lstsq(X, Y, rcond=None)[0]
    hfa = {c: x[ic[c]] for c in cs} if conf else x[n]
    if se:
        res = Y - X @ x
        cov = np.linalg.pinv(X.T @ X) * (res[:len(games)] ** 2).sum() / (len(games) - X.shape[1] + n)
        hfa = {c: (x[ic[c]], cov[ic[c], ic[c]] ** .5) for c in cs}
    return dict(zip(teams, x[:n])), hfa

def hfa_of(hfa, conf, team):
    return hfa[conf.get(team, "other")] if isinstance(hfa, dict) else hfa

def hfa_report():
    """Home field by the home team's conference, each season and pooled, with standard errors. FBS vs FBS only:
    an FCS visitor with one game would otherwise sit at the prior and hand its blowout loss to home field."""
    allg = []
    for season in (2025, 2026):
        conf = conf_of(season)
        g = [x for x in season_games(season) if conf.get(x[1], "other") != "other" and conf.get(x[2], "other") != "other"]
        allg += [(f"{h}_{season}", f"{a}_{season}", m, neu) for _, h, a, m, neu in g]
        rt, hfa = fit([x[1:] for x in g], {}, 0.0, conf, se=True)
        n = collections.Counter(conf.get(h, "other") for _, h, a, m, neu in g if not neu)
        print(f"\n{season}  conference      HFA   +-se   home games")
        for c, (v, e) in sorted(hfa.items(), key=lambda kv: -kv[1][0]):
            print(f"      {c:<14}{v:>+6.1f}  {e:>4.1f}   {n[c]:>5}")
    conf = {f"{t}_{s}": c for s in (2025, 2026) for t, c in conf_of(s).items()}
    rt, hfa = fit(allg, {}, 0.0, conf, se=True)
    print(f"\npooled  conference      HFA   +-se")
    for c, (v, e) in sorted(hfa.items(), key=lambda kv: -kv[1][0]):
        print(f"        {c:<14}{v:>+6.1f}  {e:>4.1f}")

def season_games(season):
    """[(wk, home, away, home margin, neutral)]"""
    res, meta = cfb_results(season), cfb_meta(season)
    return [(r[0], r[2], r[3], r[4] - r[5], meta.get(r[1], [0, 0])[1]) for r in res]

TO_PTS, TO_SKEW, OUTLIER = 4, 2, 14   # points a turnover is worth; |TO diff| that marks a skewed game; margin off the team's norm

def turnovers(season):
    """{(wk, home, away): (home turnovers, away turnovers)} from the halftime study's cache (ESPN drives)."""
    p = f"{ROOT}/data/hist_halftime/turnovers_{season}.json"
    tos = json.load(open(p)) if os.path.exists(p) else {}
    out = {}
    for wk, i, h, a, _, _ in cfb_results(season):
        t = tos.get(i)
        if t and h in t and a in t:
            out[wk, h, a] = (t[h][1], t[a][1])
    return out

def prior_for(season):
    g = season_games(season - 1)
    rt, _ = fit([x[1:] for x in g], {}, 0.0)
    n = collections.Counter(t for x in g for t in x[1:3])
    return {t: SHRINK * r for t, r in rt.items() if n[t] >= 6}

def look(games, hist, wk, h, a, neutral, rt, hfa, conf={}, lines={}, tos={}):
    """One game's evidence. games = this season, hist = earlier seasons (for last meetings),
    lines = {(wk, home, away): home spread} for against-the-spread margins, tos = turnovers per game."""
    past = [x for x in games if x[0] < wk]
    venue = 0 if neutral else hfa_of(hfa, conf, h)
    def rec(t, src):   # {opp: [(week, t's margin with the venue taken out, t's margin against the spread or None, t's turnovers minus opp's or None)]}
        d = collections.defaultdict(list)
        for w, hh, aa, m, neu in src:
            v, sp, to = (0 if neu else hfa_of(hfa, conf, hh)), lines.get((w, hh, aa)), tos.get((w, hh, aa))
            if hh == t: d[aa].append((w, m - v, None if sp is None else m + sp, None if to is None else to[0] - to[1]))
            elif aa == t: d[hh].append((w, -m + v, None if sp is None else -(m + sp), None if to is None else to[1] - to[0]))
        return d
    vh, va = rec(h, past), rec(a, past)
    shared = [o for o in vh if o in va]
    mean = lambda xs, i: np.mean([x[i] for x in xs if x[i] is not None]) if any(x[i] is not None for x in xs) else None
    com = [mean(vh[o], 1) - mean(va[o], 1) for o in shared]
    # strength: drop shared opponents in the bottom half of the rating; weight by closeness to the two teams
    # (a two-TD gap halves the weight); cap each margin at 21 so blowouts of weak teams count the same
    played = collections.Counter(t for _, hh, aa, _, _ in past for t in (hh, aa))
    floor = np.median([rt[t] for t in rt if played[t] >= 3]) if rt else 0
    strong = [rt.get(o, FCS) >= floor for o in shared]
    top = [d for d, ok in zip(com, strong) if ok]
    wts = [.5 ** (abs(rt.get(o, FCS) - (rt.get(h, FCS) + rt.get(a, FCS)) / 2) / 14) for o in shared]
    cap = lambda xs: np.mean([max(-21, min(21, x[1])) for x in xs])
    capped = [cap(vh[o]) - cap(va[o]) for o in shared]
    # time: the two meetings with the shared opponent no more than 4 weeks apart
    recent = [min(abs(x[0] - y[0]) for x in vh[o] for y in va[o]) <= 4 for o in shared]
    # turnovers: credit each team TO_PTS per turnover it committed more than its opponent in the shared game;
    # or drop a shared opponent when either meeting had a TO_SKEW+ turnover swing; or when either meeting was
    # OUTLIER+ points off that team's mean over its other games (a game unlike the team's season)
    known = lambda o: all(x[3] is not None for x in vh[o] + va[o])
    adj = lambda xs: np.mean([x[1] + TO_PTS * x[3] for x in xs])
    com_to = [adj(vh[o]) - adj(va[o]) if known(o) else d for o, d in zip(shared, com)]
    calm = [known(o) and max(abs(x[3]) for x in vh[o] + va[o]) < TO_SKEW for o in shared]
    def typical(t, d, o):
        ms = [x[1] for xs in d.values() for x in xs]
        return all(len(ms) < 3 or abs(x[1] - (sum(ms) - x[1]) / (len(ms) - 1)) <= OUTLIER for x in d[o])
    usual = [typical(h, vh, o) and typical(a, va, o) for o in shared]
    # against the spread: how much more one team beat the number against the same opponent (positive favors h)
    ats = [(mean(vh[o], 2), mean(va[o], 2), ok) for o, ok in zip(shared, strong)]
    ats = [(x - y, ok) for x, y, ok in ats if x is not None and y is not None]
    h2h = [x[1] for x in rec(h, hist).get(a, []) + vh.get(a, [])]
    def split(t):
        hm = [m for _, hh, aa, m, neu in past if hh == t and not neu]
        am = [-m for _, hh, aa, m, neu in past if aa == t and not neu]
        return (np.mean(hm) if hm else None, np.mean(am) if am else None)
    def form(t):
        ms = [x[1] for x in sorted((x for xs in rec(t, past).values() for x in xs), key=lambda x: x[0])]
        return np.mean(ms[-3:]) - np.mean(ms) if len(ms) >= 4 else None
    sp = lambda ds: -(np.mean(ds) + venue) if ds else None
    return {"model": -(rt.get(h, FCS) - rt.get(a, FCS) + venue),
            "common": sp(com), "n_common": len(com),
            "common_top": sp(top), "n_top": len(top),
            "common_w": -(np.average(com, weights=wts) + venue) if com else None,
            "common_cap": sp(capped),
            "common_recent": sp([d for d, ok in zip(com, recent) if ok]), "n_recent": sum(recent),
            "recent_to": sp([d for d, ok in zip(com_to, recent) if ok]),
            "recent_calm": sp([d for d, ok, ok2 in zip(com, recent, calm) if ok and ok2]), "n_calm": sum(a and b for a, b in zip(recent, calm)),
            "recent_usual": sp([d for d, ok, ok2 in zip(com, recent, usual) if ok and ok2]), "n_usual": sum(a and b for a, b in zip(recent, usual)),
            "to_detail": [(o, [x[3] for x in vh[o]], [x[3] for x in va[o]], ok) for o, ok in zip(shared, recent) if ok],
            "common_top_recent": sp([d for d, ok, ok2 in zip(com, strong, recent) if ok and ok2]), "n_top_recent": sum(a and b for a, b in zip(strong, recent)),
            "ats": np.mean([d for d, _ in ats]) if ats else None, "n_ats": len(ats),
            "ats_top": np.mean([d for d, ok in ats if ok]) if any(ok for _, ok in ats) else None, "n_ats_top": sum(ok for _, ok in ats),
            "h2h": sp(h2h), "n_h2h": len(h2h),
            "split_h": split(h), "split_a": split(a), "form_h": form(h), "form_a": form(a)}

def late_lines(season, have):
    """{event id: home spread} from ESPN's game summary for finished games the pbp cache has no line for
    (it keeps the last posted line; only this season's recent games still carry one)."""
    p = f"{ROOT}/data/cfb_lines_{season}.json"
    out = json.load(open(p)) if os.path.exists(p) else {}
    miss = [r[1] for r in cfb_results(season) if r[1] not in have and r[1] not in out]
    for i in miss:
        try:
            pc = json.load(urllib.request.urlopen(urllib.request.Request(f"{ESPN.replace('scoreboard', 'summary')}?event={i}",
                                                                          headers={"User-Agent": "Mozilla/5.0"}), timeout=20)).get("pickcenter") or []
            out[i] = next((p["spread"] for p in pc if p.get("spread") is not None), None)
        except Exception:
            out[i] = None
    if miss:
        json.dump(out, open(p, "w"))
    return {i: sp for i, sp in out.items() if sp is not None}

RULES = (("*", "common", "n_common"), ("+", "common_top", "n_top"), ("~", "common_recent", "n_recent"), ("$", "ats", "n_ats"))
RULE_NAMES = {"*": "plain common opponents", "+": "top-half shared opponents only", "~": "meetings <=4 weeks apart", "$": "against the spread vs shared opponents"}

def f(x, w=5):
    return f"{x:+{w}.1f}" if x is not None else " " * (w - 1) + "-"

def slate():
    season = 2026
    games, hist, prior = season_games(season), season_games(season - 1), prior_for(season)
    sb = json.load(urllib.request.urlopen(urllib.request.Request(f"{ESPN}?groups=80&limit=300", headers={"User-Agent": "Mozilla/5.0"}), timeout=20))
    wk = sb["week"]["number"]
    conf = conf_of(season) if BY_CONF else {}
    gs, _ = games_for(season, {})
    lines = {(g["wk"], g["h"], g["a"]): g["sp"] for g in gs}
    lines.update({(r[0], r[2], r[3]): sp for r in cfb_results(season) for sp in [late_lines(season, {g["id"] for g in gs}).get(r[1])] if sp is not None})
    tos = turnovers(season)
    rt, hfa = fit([x[1:] for x in games if x[0] < wk], prior, FCS, conf or None)
    print(f"week {wk}, rating from {sum(x[0] < wk for x in games)} games, HFA {({c: round(float(v), 1) for c, v in hfa.items()} if BY_CONF else round(hfa, 1))}. Spreads are the home team's; "
          f"edge = line - rating (positive: take the home side).\n")
    print(f"{'game':<34}{'line':>6}{'rating':>8}{'edge':>6}{'common':>9}{'tophalf':>9}{'<=4wks':>9}{'ATS':>8}{'h2h':>8}  home h/a   away h/a  form h/a")
    rows, picks = [], collections.defaultdict(list)
    for e in sb["events"]:
        c = e["competitions"][0]; t = {x["homeAway"]: x for x in c["competitors"]}
        rk = {k: (x.get("curatedRank") or {}).get("current") or 99 for k, x in t.items()}
        od = (c.get("odds") or [{}])[0]
        if min(rk.values()) > 25 or od.get("spread") is None:
            continue
        h, a = t["home"]["team"]["abbreviation"], t["away"]["team"]["abbreviation"]
        L = float(od["spread"])
        k = look(games, hist, wk, h, a, bool(c.get("neutralSite")), rt, hfa, conf, lines, tos)
        tag = lambda side, abbr: abbr + (f" ({rk[side]})" if rk[side] <= 25 else "")
        pick = ""
        for mark, key, nkey in RULES:
            gap = k[key] if key.startswith("ats") else (L - k[key] if k[key] is not None else None)   # positive favors home
            if k[nkey] >= 2 and abs(gap) >= 4:
                pick += mark
                picks[mark].append((abs(gap), f"{h} {L:+.1f}" if gap > 0 else f"{a} {-L:+.1f}", k[key], k[nkey],
                                    "" if mark != "~" else f"  TO-adjusted {k['recent_to']:+.1f}; TO diff " + ", ".join(f"{o}: {h} {th} / {a} {ta}" for o, th, ta, _ in k["to_detail"])))
        rows.append((L - k["model"], f"{tag('away', a)} @ {tag('home', h)}{' (N)' if c.get('neutralSite') else ''}", L, k, pick))
    for edge, name, L, k, pick in sorted(rows, key=lambda r: -abs(r[0])):
        sh, sa = k["split_h"], k["split_a"]
        print(f"{name:<34}{L:>+6.1f}{k['model']:>+8.1f}{edge:>+6.1f}{f(k['common'], 6)}{'(' + str(k['n_common']) + ')':<3}"
              f"{f(k['common_top'], 6)}{'(' + str(k['n_top']) + ')':<3}{f(k['common_recent'], 6)}{'(' + str(k['n_recent']) + ')':<3}"
              f"{f(k['ats'], 5)}{'(' + str(k['n_ats']) + ')':<3}{f(k['h2h'], 6)}{'(' + str(k['n_h2h']) + ')':<3} {f(sh[0])}/{f(sh[1])} {f(sa[0])}/{f(sa[1])}  {f(k['form_h'])}/{f(k['form_a'])}"
              f"   {pick}")
    for mark, key, _ in RULES:
        print(f"\n{mark} {RULE_NAMES[mark]}, biggest gap first:")
        for gap, bet, sp, n, extra in sorted(picks[mark], reverse=True):
            print(f"  {bet:<12} gap {gap:>4.1f}  {'beats the number by' if key.startswith('ats') else 'shared opponents say'} {sp:+.1f} ({n}){extra}")

def backtest():
    out = collections.defaultdict(list)   # key -> [(edge_home, home_cover, season)]
    for season in (2025, 2026):
        games, hist, prior = season_games(season), season_games(season - 1), prior_for(season)
        gs, _ = games_for(season, morning_spreads() if season == 2025 else {})
        conf = conf_of(season) if BY_CONF else {}
        if season == 2026:
            res, ranks, meta = cfb_results(season), cfb_ranks(season), cfb_meta(season)
            for r in res:
                sp = late_lines(season, {g["id"] for g in gs}).get(r[1])
                if sp is not None and r[1] in meta:
                    gs.append({"id": r[1], "wk": r[0], "h": r[2], "a": r[3], "m": r[4] - r[5], "sp": float(sp), "neutral": meta[r[1]][1], "rk": ranks.get(r[1], (99, 99))})
        lines = {(g["wk"], g["h"], g["a"]): g["sp"] for g in gs}
        tos = turnovers(season)
        for wk in sorted({g["wk"] for g in gs}):
            if wk < 5:
                continue
            rt, hfa = fit([x[1:] for x in games if x[0] < wk], prior, FCS, conf or None)
            for g in gs:
                if g["wk"] != wk:
                    continue
                k = look(games, hist, wk, g["h"], g["a"], g["neutral"], rt, hfa, conf, lines, tos)
                cover, ranked = np.sign(g["m"] + g["sp"]), min(g["rk"]) <= 25
                for key, val, n in (("ATS vs shared (n>=2)", k["ats"], k["n_ats"]), ("ATS vs top-half shared (n>=2)", k["ats_top"], k["n_ats_top"])):
                    if n >= 2:
                        out["all games, " + key].append((val, cover, season))
                        if ranked:
                            out["ranked, " + key].append((val, cover, season))
                for key, val, n in (("common, meetings <=4 wks apart (n>=2)", k["common_recent"], k["n_recent"]),
                                    ("common top-half & <=4 wks apart (n>=2)", k["common_top_recent"], k["n_top_recent"]),
                                    ("<=4 wks, margins adjusted 4 pts per turnover", k["recent_to"], k["n_recent"]),
                                    ("<=4 wks, shared games with a 2+ turnover swing dropped", k["recent_calm"], k["n_calm"]),
                                    ("<=4 wks, shared games 14+ off a team's norm dropped", k["recent_usual"], k["n_usual"])):
                    if n >= 2:
                        out["all games, " + key].append((g["sp"] - val, cover, season))
                        if ranked:
                            out["ranked, " + key].append((g["sp"] - val, cover, season))
                            out[("both ranked, " if max(g["rk"]) <= 25 else "one ranked, ") + key].append((g["sp"] - val, cover, season))
                        else:
                            out["unranked, " + key].append((g["sp"] - val, cover, season))
                rk_home = g["rk"][0] <= 25 and g["rk"][1] > 25
                rk_away = g["rk"][1] <= 25 and g["rk"][0] > 25
                e = g["sp"] - k["model"]
                out["all games, rating"].append((e, cover, season))
                if k["common"] is not None and k["n_common"] >= 2:
                    out["all games, common opps (n>=2)"].append((g["sp"] - k["common"], cover, season))
                    out["all games, common weighted by strength"].append((g["sp"] - k["common_w"], cover, season))
                    out["all games, common margins capped 21"].append((g["sp"] - k["common_cap"], cover, season))
                    if ranked:
                        out["ranked, common weighted by strength"].append((g["sp"] - k["common_w"], cover, season))
                        out["ranked, common margins capped 21"].append((g["sp"] - k["common_cap"], cover, season))
                if k["common_top"] is not None and k["n_top"] >= 2:
                    et = g["sp"] - k["common_top"]
                    out["all games, common top-half opps only (n>=2)"].append((et, cover, season))
                    out[f"all games, top-half, {'3+' if k['n_top'] >= 3 else 'exactly 2'} shared"].append((et, cover, season))
                    out[f"all games, top-half, rating {'agrees' if np.sign(e) == np.sign(et) else 'disagrees'}"].append((et, cover, season))
                    out[f"all games, top-half, gap {'<= 8' if abs(et) <= 8 else '> 8'}"].append((et, cover, season))
                    if ranked:
                        out["ranked, common top-half opps only (n>=2)"].append((g["sp"] - k["common_top"], cover, season))
                if ranked:
                    out["ranked games, rating"].append((e, cover, season))
                    if k["common"] is not None and k["n_common"] >= 2:
                        ec = g["sp"] - k["common"]
                        out["ranked, common opps (n>=2)"].append((ec, cover, season))
                        out["ranked, rating & common agree"].append((e if np.sign(e) == np.sign(ec) else 0, cover, season))
                    if rk_home or rk_away:
                        # edge from the ranked team's side: positive = line undervalues the ranked team
                        er = e if rk_home else -e
                        cr = cover if rk_home else -cover
                        out["ranked vs unranked: ranked side"].append((er, cr, season))
    for key, v in out.items():
        print(f"\n{key} ({len(v)} games)")
        print(f"  {'|edge|>=':>9} {'bets':>5} {'W-L-P':>12} {'win%':>6}   2025 / 2026")
        for th in (0, 2, 3, 4, 6, 8):
            wl = lambda yr, sg: sum(1 for e, c, s in v if abs(e) >= th and e != 0 and np.sign(e) == sg * c and s in yr)
            w, l, p = wl((2025, 2026), 1), wl((2025, 2026), -1), sum(1 for e, c, s in v if abs(e) >= th and e != 0 and c == 0)
            if w + l:
                print(f"  {th:>9} {w + l + p:>5} {f'{w}-{l}-{p}':>12} {100 * w / (w + l):>5.1f}%   "
                      f"{wl((2025,), 1)}-{wl((2025,), -1)} / {wl((2026,), 1)}-{wl((2026,), -1)}")
    v = out["ranked vs unranked: ranked side"]
    print("\nranked vs unranked, by which way the rating leans (ranked side bet):")
    for lo, hi in ((-99, -4), (-4, -1), (-1, 1), (1, 4), (4, 99)):
        s = [c for e, c, _ in v if lo <= e < hi]
        if s:
            w, l = sum(1 for c in s if c > 0), sum(1 for c in s if c < 0)
            print(f"  rating says ranked side worth {lo:>+3} to {hi:>+3} vs line: ranked covers {w}-{l} ({100 * w / max(1, w + l):.0f}%)")

def check():
    rng = np.random.default_rng(0)
    true = {t: v for t, v in zip("ABCDEFGH", rng.normal(0, 10, 8))}
    games = [(h, a, true[h] - true[a] + 3 + rng.normal(0, 1), False) for _ in range(200)
             for h, a in [rng.choice(list(true), 2, replace=False)]]
    rt, hfa = fit(games, {}, 0.0)
    off = np.mean([rt[t] - true[t] for t in true])
    assert abs(hfa - 3) < .5 and all(abs(rt[t] - off - true[t]) < 1 for t in true), (hfa, rt)
    print("ok")

if __name__ == "__main__":
    check() if "--check" in sys.argv else hfa_report() if "--hfa" in sys.argv else backtest() if "--backtest" in sys.argv else slate()
