#!/usr/bin/env python3
"""Does the pregame shared-opponent lean sort the live CFB spread bets? (README findings log, 2026-11-07: no.)

Lean per game: the <=4-week rule of analysis/cfb_top25_lines.py, rated on earlier weeks only: sign of line minus the
spread two or more shared opponents imply, when the gap is 4+; else none. Then every live bet, split by whether its side
agrees with the lean: first EDGE flag per game-side on the archived live games (fair line as live/cfb_poller.py), the 11+
lead bets of analysis/lead_spread_value.py (data/lead_spread_bets.json), and 11+ lead bets graded from the archive's own
quote rows (2026 weeks 8+, the poller's rule). Needs data/ from the main checkout (data/fly, lead_spread_bets.json).

Run: .venv/bin/python analysis/lean_vs_live.py [--check]"""
import sys, os, json, glob, re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
CHECK = "--check" in sys.argv
sys.argv = [sys.argv[0]]   # no --refresh for the cached loaders
from cfb_top25_lines import look, fit, season_games, prior_for, FCS, ROOT
from cfb_spots import cfb_meta, cfb_results
LEAK, POSS, EDGE_FLAG = 0.31, 0.73, 4.0   # live/cfb_poller.py
cache = {}
def lean(season, wk, h, a, neutral):
    """+1 home / -1 away / 0 none: sign of line - common_recent when n_recent>=2 and |gap|>=4. Needs the line."""
    if (season, wk) not in cache:
        games, prior = season_games(season), prior_for(season)
        rt, hfa = fit([x[1:] for x in games if x[0] < wk], prior, FCS)
        cache[season, wk] = (games, season_games(season - 1), rt, hfa)
    games, hist, rt, hfa = cache[season, wk]
    return look(games, hist, wk, h, a, neutral, rt, hfa)
def lean_sign(k, line):
    if k["n_recent"] >= 2 and k["common_recent"] is not None:
        gap = line - k["common_recent"]
        if abs(gap) >= 4:
            return 1 if gap > 0 else -1
    return 0
def ep(yl): return 0.065 * yl - 1.3
def rec(label, rows):
    """rows: [(agree sign: 1 agree / -1 disagree / 0 none, hit: True/False/None push)]"""
    out = {}
    for name, s in (("agree", 1), ("disagree", -1), ("no lean", 0)):
        rs = [h for g, h in rows if g == s]
        w, l, p = sum(h is True for h in rs), sum(h is False for h in rs), sum(h is None for h in rs)
        out[name] = f"{w}-{l}" + (f"-{p}" if p else "")
    print(f"  {label:<44}" + "  ".join(f"{k} {v:>7}" for k, v in out.items()))
    return out
if CHECK:   # the lean needs two shared opponents and a 4+ gap; rec counts W-L-P by agreement
    assert lean_sign({"n_recent": 2, "common_recent": 10.0}, 14.0) == 1 and lean_sign({"n_recent": 2, "common_recent": 10.0}, 6.0) == -1
    assert lean_sign({"n_recent": 1, "common_recent": 10.0}, 20.0) == 0 and lean_sign({"n_recent": 3, "common_recent": 10.0}, 13.5) == 0
    assert rec("check", [(1, True), (1, False), (-1, True), (0, None)]) == {"agree": "1-1", "disagree": "1-0", "no lean": "0-0-1"}
    print("ok"); sys.exit()

# ---------- 1+2: live EDGE flags on archived games ----------
print("== live EDGE flags (first flag per game-side), 2026 archive ==")
meta26 = cfb_meta(2026)
res26 = {r[1]: r for r in cfb_results(2026)}
arch = {}
for f in sorted(glob.glob(f"{ROOT}/data/fly/docs/archive/cfb_w*.json")):
    wk = int(re.search(r"cfb_w(\d+)", f).group(1))
    for gid, g in json.load(open(f)).items():
        g["wk"] = wk; arch[gid] = g
leans = {}
flags = []
for gid, g in arch.items():
    if g.get("pregame") is None or not g.get("final"):
        continue
    m = g["meta"]; h, a = m["habbr"], m["aabbr"]
    wk = res26[gid][0] if gid in res26 else g["wk"]
    neutral = meta26.get(gid, [0, 0])[1]
    k = lean(2026, wk, h, a, neutral)
    L0 = g["pregame"]
    ls = lean_sign(k, L0)
    leans[gid] = (ls, k["n_recent"], k["common_recent"])
    print(f"  {m['game']:<14} wk{wk} line {L0:+5.1f} shared<=4wk n={k['n_recent']} implied {k['common_recent'] if k['common_recent'] is None else round(k['common_recent'],1)} lean {ls:+d}")
    as_, hs = map(float, g["final"].split("-")); fin = hs - as_
    seen, sides = set(), {}
    for sn in g["snaps"]:
        gc, L, mg = sn[0], sn[1], sn[2]
        if L is None or not (300 <= gc <= 3300) or gc // 60 in seen:
            continue
        seen.add(gc // 60)
        rem = 1 - gc / 3600
        sit = sn[3] if len(sn) > 3 else None
        e = 0.0
        if sit:
            parts = sit.split(" · "); ab = parts[0].removesuffix(" ball")
            side = 1 if ab == h else -1 if ab == a else 0
            mm = re.fullmatch(r"(?:(\S+) )?(\d+)", parts[1]) if len(parts) > 1 else None
            yl = 50 if not mm else int(mm.group(2)) if mm.group(1) in (None, ab) else 100 - int(mm.group(2))
            e = side * ep(yl) * min(1.0, 8 * rem)
        fair = -(mg * (1 - LEAK * rem) + POSS * e + (-L0) * rem)
        fair = round(fair * 2) / 2
        edge = L - fair
        if abs(edge) >= EDGE_FLAG:
            bet = 1 if edge > 0 else -1          # +1 home side
            if (gid, bet) not in sides:
                cov = fin + L
                hit = None if cov == 0 else (cov > 0) == (bet > 0)
                sides[gid, bet] = hit
                flags.append((gid, bet, hit, ls, gc, L, fair))
print(f"\n{len(flags)} first EDGE flags on {len({f[0] for f in flags})} games, {len(arch)} archived games")
rec("EDGE flags vs lean (2026 archive)", [((ls * bet) if ls else 0, hit) for gid, bet, hit, ls, *_ in flags])
for gid, bet, hit, ls, gc, L, fair in flags:
    print(f"    {arch[gid]['meta']['game']:<14} min {gc//60:>2} live {L:+5.1f} fair {fair:+5.1f} bet {'home' if bet>0 else 'away'} lean {ls:+d} -> {'W' if hit else 'L' if hit is False else 'P'}")
# ---------- 3: lead bets ----------
print("\n== 11+ lead bets (data/lead_spread_bets.json), CFB ==")
bets = [r for r in json.load(open(f"{ROOT}/data/lead_spread_bets.json")) if r["lg"] == "CFB"]
rows = []
for r in bets:
    a, h = r["game"].split(" @ ")
    neutral = cfb_meta(r["season"]).get(r["id"], [0, 0])[1]
    k = lean(r["season"], r["wk"], h, a, neutral)
    ls = lean_sign(k, r["sp"]) if r["sp"] is not None else 0
    trail = 1 if r["trailing_home"] else -1      # trailing side, +1 home
    hit_trail = None if r["pnl"] == 0 else r["pnl"] > 0
    hit_lead = None if hit_trail is None else not hit_trail
    r.update(ls=ls, trail=trail, hit_trail=hit_trail, hit_lead=hit_lead, n_recent=k["n_recent"])
    rows.append(r)
ranked = lambda x: x <= 25
for season in (2025, 2026):
    rs = [r for r in rows if r["season"] == season]
    print(f"\n{season}: {len(rs)} bets, lean available on {sum(bool(r['ls']) for r in rs)}")
    top = [r for r in rs if r["top"]]
    rec(f"trailing +pts, flagged top-vs-top (n={len(top)})", [((r["ls"] * r["trail"]) if r["ls"] else 0, r["hit_trail"]) for r in top])
    rec(f"trailing +pts, all 11+ leads (n={len(rs)})", [((r["ls"] * r["trail"]) if r["ls"] else 0, r["hit_trail"]) for r in rs])
    var = [r for r in top if ranked(r["trk"]) and not ranked(r["lrk"])]
    rec(f"leader -pts, college variant (n={len(var)})", [((r["ls"] * -r["trail"]) if r["ls"] else 0, r["hit_lead"]) for r in var])
    var2 = [r for r in rs if ranked(r["trk"]) and not ranked(r["lrk"])]
    rec(f"leader -pts, only trailing ranked, any record (n={len(var2)})", [((r["ls"] * -r["trail"]) if r["ls"] else 0, r["hit_lead"]) for r in var2])
    rk = [r for r in rs if ranked(r["trk"]) or ranked(r["lrk"])]
    rec(f"trailing +pts, a ranked team in it (n={len(rk)})", [((r["ls"] * r["trail"]) if r["ls"] else 0, r["hit_trail"]) for r in rk])
print("\nleaned lead bets:")
for r in sorted(rows, key=lambda r: (r["season"], r["wk"])):
    if r["ls"]:
        print(f"  {r['season']} wk{r['wk']:>2} {r['game']:<14} sp {r['sp']:+5.1f} lean {'home' if r['ls']>0 else 'away'} trailing {'home' if r['trail']>0 else 'away'} top={r['top']} ranks L{r['lrk']}/T{r['trk']} +{r['pts']} fin {r['fin']:+d} trailing {'W' if r['hit_trail'] else 'L' if r['hit_trail'] is False else 'P'}")
# ---------- 3b: 11+ lead bets graded from the archive's own quotes (2026 w8-10, the weeks a lean exists) ----------
print("\n== 11+ lead bets from archive quotes (poller rule: first 11+ lead before min 55, first fresh quote within 15 min) ==")
rows2 = []
for gid, g in arch.items():
    if not g.get("quotes") or not g.get("final") or gid not in leans:
        continue
    as_, hs = map(float, g["final"].split("-")); fin = hs - as_
    qs = g["quotes"]
    t0 = None
    for t, gc, aa, hh, bk in qs:
        m = hh - aa
        if t0 is None and abs(m) >= 11 and gc < 3300:
            t0, sign = t, 1 if m > 0 else -1
            continue
        if t0 is not None:
            if sign * m < 11 or t - t0 > 900:
                t0 = "dead"; break
            q = (bk or {}).get("fanduel") or (bk or {}).get("draftkings")
            if q and q[0] is not None and q[3] >= t0:
                trail = -sign
                pts = q[0] if trail > 0 else -q[0]
                cov = trail * fin + pts
                hit = None if cov == 0 else cov > 0
                ls = leans[gid][0]
                rows2.append((gid, trail, pts, hit, ls, g["wk"]))
                break
print(f"  {len(rows2)} bets on {len([g for g in arch.values() if g.get('quotes')])} games with quotes")
rec("trailing +pts, 2026 archive, any record", [((ls * trail) if ls else 0, hit) for gid, trail, pts, hit, ls, wk in rows2])
rec("leader -pts, 2026 archive, any record", [((ls * -trail) if ls else 0, None if hit is None else not hit) for gid, trail, pts, hit, ls, wk in rows2])
for gid, trail, pts, hit, ls, wk in rows2:
    if ls:
        print(f"    {arch[gid]['meta']['game']:<14} wk{wk} trailing {'home' if trail>0 else 'away'} +{pts} lean {ls:+d} -> trailing {'W' if hit else 'L' if hit is False else 'P'}")
