#!/usr/bin/env python3
"""College spots: home/road, off a big loss, conference play, off a bye, ranked. Two questions:
pregame, does the spot cover its spread; live, does it change the trailing team's + points bet?
A spot has to hold in 2025 and 2026 on its own: the two seasons are the only protection here,
because many spots are tried. Pregame spreads: ESPN for 2026, the morning-of bulk snapshots for 2025.

Run: .venv/bin/python analysis/cfb_spots.py"""
from lead_spread_value import *

def cfb_meta(season):
    """{event id: [conference game, neutral site]}"""
    p = f"{ROOT}/data/cfb_meta_{season}.json"
    if os.path.exists(p):
        return json.load(open(p))
    out = {}
    for wk in range(1, 17):
        u = f"{ESPN}?groups=80&limit=300&seasontype=2&week={wk}&dates={season}"
        sb = json.load(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=20))
        for e in sb.get("events", []):
            c = e["competitions"][0]
            out[e["id"]] = [bool(c.get("conferenceCompetition")), bool(c.get("neutralSite"))]
    json.dump(out, open(p, "w"))
    return out

def morning_spreads():
    """{(home, away): [(commence, home spread)]} from every cached pre-kick CFB quote, last one kept."""
    out = {}
    for d in cached()["CFB"]:
        st = ts(d["timestamp"])
        for e in d.get("data", []):
            k = ts(e["commence_time"])
            if k <= st:
                continue
            q = {b["key"]: o["point"] for b in e.get("bookmakers", []) for m in b["markets"] for o in m["outcomes"] if o["name"] == e["home_team"]}
            if q:
                out[norm(e["home_team"]), norm(e["away_team"]), k // 86400] = q.get("fanduel", q.get("draftkings"))
    return out

def history(res):
    """{(team, week): last game before it: (margin, event id, week)}"""
    seen, out = collections.defaultdict(list), {}
    for r in sorted(res, key=lambda r: r[0]):
        for t in r[2:4]:
            if seen[t]:
                out[t, r[0]] = seen[t][-1]
        seen[r[2]].append((r[4] - r[5], r[1], r[0]))
        seen[r[3]].append((r[5] - r[4], r[1], r[0]))
    return out

def games_for(season, spreads):
    res, ranks, meta = cfb_results(season), cfb_ranks(season), cfb_meta(season)
    wall = json.load(open(f"{ROOT}/data/cfb_wall_{season}.json"))
    cache_sp = {g["id"]: g["sp"] for g in cfb()} if season == 2026 else {}
    hist, out = history(res), []
    for r in res:
        wk, i, h, a, hs, as_ = r
        w = wall.get(i)
        sp = cache_sp.get(i)
        if sp is None and w:
            sp = w.get("sp")
            if sp is None and w.get("tl"):
                day = w["tl"][0][0] // 86400
                sp = next((spreads[k] for d in (day, day - 1, day + 1) if (k := (norm(w["home"]), norm(w["away"]), d)) in spreads), None)
        if sp is None or i not in meta:
            continue
        out.append({"id": i, "season": season, "wk": wk, "h": h, "a": a, "m": hs - as_, "sp": float(sp), "conf": meta[i][0], "neutral": meta[i][1],
                    "rk": ranks.get(i, (99, 99)), "hl": hist.get((h, wk)), "al": hist.get((a, wk))})
    return out, ranks

def spots(g, side, ranks):
    """Facts about one team going into this game. side +1 home, -1 away."""
    me = g["hl"] if side > 0 else g["al"]
    you = g["al"] if side > 0 else g["hl"]
    sp = side * g["sp"]                            # this team's spread, negative = favored
    idx = 0 if side > 0 else 1
    def big(last):                                 # a ranked team was on the field
        return last and any(x <= 25 for x in ranks.get(last[1], (99, 99)))
    f = {"dog": sp > 0, "fav": sp < 0}
    f["home"] = side > 0 and not g["neutral"]
    f["road"] = side < 0 and not g["neutral"]
    f["conference game"] = g["conf"]
    f["non-conference game"] = not g["conf"]
    f["ranked"] = g["rk"][idx] <= 25
    if me:
        f["off a loss"] = me[0] < 0
        f["off a 17+ loss"] = me[0] <= -17
        f["off a one-score loss"] = -8 <= me[0] < 0
        f["off a loss in a ranked game"] = me[0] < 0 and bool(big(me))
        f["off a 21+ win"] = me[0] >= 21
        f["off a bye"] = me[2] <= g["wk"] - 2
    if you:
        f["opponent off a 21+ win"] = you[0] >= 21
        f["opponent off a loss in a ranked game"] = you[0] < 0 and bool(big(you))
    return f

SPOTS = [("home dog", ("home", "dog")), ("road dog", ("road", "dog")), ("home fav", ("home", "fav")), ("road fav", ("road", "fav")),
         ("conference dog", ("conference game", "dog")), ("conference home dog", ("conference game", "home", "dog")),
         ("conference road dog", ("conference game", "road", "dog")), ("non-conference dog", ("non-conference game", "dog")),
         ("off a loss", ("off a loss",)), ("off a loss, dog", ("off a loss", "dog")), ("off a loss, fav", ("off a loss", "fav")),
         ("off a 17+ loss", ("off a 17+ loss",)), ("off a 17+ loss, dog", ("off a 17+ loss", "dog")),
         ("off a one-score loss", ("off a one-score loss",)),
         ("off a loss in a ranked game", ("off a loss in a ranked game",)), ("off a ranked-game loss, dog", ("off a loss in a ranked game", "dog")),
         ("off a bye", ("off a bye",)), ("off a bye, dog", ("off a bye", "dog")),
         ("off a 21+ win (let-down)", ("off a 21+ win",)),
         ("vs team off a ranked-game loss", ("opponent off a loss in a ranked game",)), ("vs team off a 21+ win", ("opponent off a 21+ win",)),
         ("ranked fav", ("ranked", "fav")), ("ranked dog", ("ranked", "dog")), ("unranked dog vs ranked, conference", ("dog", "conference game", "vs ranked"))]

def ats(gs_ranks, keys):
    """(wins, losses) betting every side that has the spot, at the spread; pushes dropped."""
    w = l = 0
    for g, ranks in gs_ranks:
        for side in (1, -1):
            f = spots(g, side, ranks)
            f["vs ranked"] = g["rk"][1 if side > 0 else 0] <= 25 and g["rk"][0 if side > 0 else 1] > 25
            if all(f.get(k) for k in keys):
                c = side * g["m"] + side * g["sp"]
                w += c > 0; l += c < 0
    return w, l

def z(w, l):
    n = w + l
    return (w - .5238 * n) / (n * .5238 * .4762) ** .5 if n else 0

def line(nm, a, b):
    (w1, l1), (w2, l2) = a, b
    n1, n2 = w1 + l1, w2 + l2
    ok = "  both" if n1 and n2 and w1 / n1 > .5238 and w2 / n2 > .5238 else ""
    print(f"  {nm:38s} 2025 {w1:3d}-{l1:<3d} {w1 / max(n1, 1):4.0%} | 2026 {w2:3d}-{l2:<3d} {w2 / max(n2, 1):4.0%} | all {(w1 + w2) / max(n1 + n2, 1):4.0%} z {z(w1 + w2, l1 + l2):+.1f}{ok}")

def main():
    spreads = morning_spreads()
    g25, r25 = games_for(2025, spreads)
    g26, r26 = games_for(2026, spreads)
    print(f"games with a spread: 2025 {len(g25)}, 2026 {len(g26)}; corr with margin {np.corrcoef([g['sp'] for g in g25], [g['m'] for g in g25])[0, 1]:.2f}, {np.corrcoef([g['sp'] for g in g26], [g['m'] for g in g26])[0, 1]:.2f}")
    a, b = [(g, r25) for g in g25], [(g, r26) for g in g26]
    section("pregame, against the spread (need 52.4% at -110; 'both' = above that in both seasons)")
    for nm, keys in SPOTS:
        line(nm, ats(a, keys), ats(b, keys))

    section("live: trailing team + points at the first 11+ lead, flagged and comparison bets, by the trailing team's spot")
    rows_ = [r for r in json.load(open(f"{ROOT}/data/lead_spread_bets.json")) if r["lg"] == "CFB"]
    by = {(g["id"]): (g, r25 if g["season"] == 2025 else r26) for g in g25 + g26}
    def live(keys, season):
        w = l = pnl = 0
        for r in rows_:
            if r["season"] != season or r["id"] not in by:
                continue
            g, ranks = by[r["id"]]
            side = 1 if r["trailing_home"] else -1
            f = spots(g, side, ranks)
            f["vs ranked"] = g["rk"][1 if side > 0 else 0] <= 25 and g["rk"][0 if side > 0 else 1] > 25
            if all(f.get(k) for k in keys):
                w += r["pnl"] > 0; l += r["pnl"] < 0; pnl += r["pnl"]
        return w, l, pnl
    for nm, keys in SPOTS:
        (w1, l1, p1), (w2, l2, p2) = live(keys, 2025), live(keys, 2026)
        n1, n2 = w1 + l1, w2 + l2
        print(f"  {nm:38s} 2025 {w1:2d}-{l1:<2d} ROI {p1 / max(n1, 1):+5.0%} | 2026 {w2:2d}-{l2:<2d} ROI {p2 / max(n2, 1):+5.0%}{'  both' if p1 > 0 and p2 > 0 else ''}")

if __name__ == "__main__":
    main()
