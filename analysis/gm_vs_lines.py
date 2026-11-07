#!/usr/bin/env python3
"""Good Matchups graded against real closing prop lines (data/props_hist, from pull_pregame_props.py).

Cohort per week (weeks 4+, ranks from games before that week, like the live watchlist):
top-20 WR and top-10 TE by receiving yards per game, top-15 RB by rushing yards per game.
A Good Matchup faces a bottom-10 defense at that position (yards allowed per game to it).
Each bet is the over at the closing line, FanDuel first; win if yards > line.
Splits: matchup vs other defenses, rank halves, and the team's usual QB vs a backup.

Run: .venv/bin/python analysis/gm_vs_lines.py [2024 2025]"""
import collections, csv, gzip, os, re, sys
import pull_pregame_props as P

ROOT = P.ROOT
CFG = {"WR": ("rec", 20), "TE": ("rec", 10), "RB": ("rush", 15)}
MKT = {"rec": "player_reception_yds", "rush": "player_rush_yds"}

def norm(n):
    n = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b\.?", "", n.lower().replace(".", "").replace("'", ""))
    return re.sub(r"[^a-z ]", "", n).split()

def roster(season):
    f = f"{ROOT}/data/roster_{season}.csv"
    if not os.path.exists(f):
        sys.exit(f"missing {f}: download https://github.com/nflverse/nflverse-data/releases/download/rosters/roster_{season}.csv")
    return {r["gsis_id"]: (r["position"], r["full_name"]) for r in csv.DictReader(open(f)) if r.get("gsis_id")}

def lines(rec, kind):
    """{name tokens: closing over line}, FanDuel first."""
    out = {}
    for book in ("draftkings", "fanduel"):   # FanDuel last, so it wins
        for b in rec["bookmakers"]:
            if b["key"] != book:
                continue
            for m in b["markets"]:
                if m["key"] == MKT[kind]:
                    for o in m["outcomes"]:
                        if o["name"] == "Over" and o.get("point") is not None:
                            out[" ".join(norm(o["description"]))] = o["point"]
    return out

def season_rows(season):
    props = P.load(season)
    ros = roster(season)
    yds = {"rec": collections.defaultdict(float), "rush": collections.defaultdict(float)}
    tm, allowed, att = {}, collections.defaultdict(float), collections.Counter()
    for r in csv.DictReader(gzip.open(f"{ROOT}/data/pbp_{season}.csv.gz", "rt")):
        if r["season_type"] != "REG" or not r["posteam"]:
            continue
        w, g, pt, d = int(r["week"]), r["game_id"], r["posteam"], r["defteam"]
        if r["pass_attempt"] == "1" and r["passer_player_id"]:
            att[(w, g, pt, r["passer_player_id"])] += 1
        for kind, idc, yc in (("rec", "receiver_player_id", "receiving_yards"), ("rush", "rusher_player_id", "rushing_yards")):
            p = r[idc]
            if p and r[yc] not in ("", "NA"):
                v = float(r[yc]); yds[kind][(w, g, p)] += v; tm[(w, g, p)] = (pt, d)
                pos = ros.get(p, ("",))[0]
                if pos in CFG and CFG[pos][0] == kind:
                    allowed[(w, d, pos)] += v
    gq = {}
    for (w, g, pt, q), n in att.items():
        if n > gq.get((w, pt), ("", 0))[1]:
            gq[(w, pt)] = (q, n)
    rows = []
    for wk in range(4, 19):
        qbs = collections.Counter((pt, q) for (w, pt), (q, n) in gq.items() if w < wk)
        for pos, (kind, top) in CFG.items():
            hist = collections.defaultdict(list)
            for (w, g, p), v in yds[kind].items():
                if w < wk and ros.get(p, ("",))[0] == pos:
                    hist[p].append(v)
            ypg = {p: sum(v) / len(v) for p, v in hist.items() if len(v) >= 3}
            rank = {p: i + 1 for i, (p, _) in enumerate(sorted(ypg.items(), key=lambda kv: -kv[1]))}
            al = collections.defaultdict(list)
            for (w, d, p2), v in allowed.items():
                if w < wk and p2 == pos:
                    al[d].append(v)
            worst = {d for d, _ in sorted({d: sum(v) / len(v) for d, v in al.items()}.items(), key=lambda kv: -kv[1])[:10]}
            for (w, g, p), v in yds[kind].items():
                if w != wk or rank.get(p, 999) > top or g not in props:
                    continue
                L = lines(props[g], kind).get(" ".join(norm(ros[p][1])))
                if L is None:
                    continue
                pt, d = tm[(w, g, p)]
                usual = max(((q, c) for (t, q), c in qbs.items() if t == pt), key=lambda x: x[1], default=(None, 0))[0]
                # the player's earlier lined games this season: [(week, line, yards)], for form-vs-line splits
                prior = sorted((w2, L2, v2) for (w2, g2, p2), v2 in yds[kind].items() if p2 == p and w2 < wk and g2 in props
                               for L2 in [lines(props[g2], kind).get(" ".join(norm(ros[p][1])))] if L2 is not None)
                rows.append(dict(y=season, wk=wk, g=g, team=pt, pid=p, name=ros[p][1], pos=pos, top=top, rank=rank[p], ypg=ypg[p], weak=d in worst, line=L, yds=v,
                                 res="win" if v > L else "loss" if v < L else "push", backup=gq.get((wk, pt), ("", 0))[0] != usual, prior=prior))
    return rows

def report(rows, seasons):
    def rep(n, z):
        w, l = sum(r["res"] == "win" for r in z), sum(r["res"] == "loss" for r in z)
        ys = " / ".join(f'{sum(r["res"] == "win" for r in z if r["y"] == y)}-{sum(r["res"] == "loss" for r in z if r["y"] == y)}' for y in seasons)
        avg = sum(r["yds"] - r["line"] for r in z) / max(1, len(z))
        print(f"  {n:38} {w}-{l} ({w / max(1, w + l):.0%}) flat {(w * 100 / 110 - l) / max(1, w + l):+.0%}  yds vs line {avg:+5.1f}  [{ys}]")
    for pos, (kind, top) in CFG.items():
        z = [r for r in rows if r["pos"] == pos]
        h = top // 2
        print(f"{pos} top {top}, over at the closing line, seasons {'/'.join(map(str, seasons))}")
        rep("Good Matchup (bottom-10 defense)", [r for r in z if r["weak"]])
        rep("other defenses (comparison)", [r for r in z if not r["weak"]])
        rep(f"GM ranks 1-{h}", [r for r in z if r["weak"] and r["rank"] <= h])
        rep(f"GM ranks {h + 1}-{top}", [r for r in z if r["weak"] and r["rank"] > h])
        rep("GM, usual QB", [r for r in z if r["weak"] and not r["backup"]])
        rep("GM, backup QB", [r for r in z if r["weak"] and r["backup"]])

if __name__ == "__main__":
    seasons = [int(a) for a in sys.argv[1:]] or [2024, 2025]
    rows = [r for s in seasons for r in season_rows(s)]
    print(f"{len(rows)} cohort player-games with a closing line")
    report(rows, seasons)
