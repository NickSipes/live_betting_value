#!/usr/bin/env python3
"""Does a WR hit their over less often when the team's other top WR is out?

Cohort WR bets from gm_vs_lines.py (over at the closing line). For each bet, the team's other
top-2 WRs by receiving yards through the earlier weeks of the season; one counts only at MIN_YPG+ yds/game
(env, default 45) and is "out" when they drew no target in that game. Also shows how the closing line sits vs season yds/game in each case.

Run: .venv/bin/python analysis/wr_mate_out.py [2024 2025 2026]"""
import collections, csv, gzip, os, sys
import gm_vs_lines as G

MIN_YPG = float(os.environ.get("MIN_YPG", 45))   # a teammate counts as a top WR at this many rec yds/game

def targets(season, ros):
    """{(week, game, team): {wr pid: targets}} and {(week, team, pid): rec yds} for WRs."""
    tg, yd = collections.defaultdict(collections.Counter), collections.defaultdict(float)
    for r in csv.DictReader(gzip.open(f"{G.ROOT}/data/pbp_{season}.csv.gz", "rt")):
        p = r["receiver_player_id"]
        if r["season_type"] != "REG" or not p or ros.get(p, ("",))[0] != "WR":
            continue
        w = int(r["week"])
        tg[(w, r["game_id"], r["posteam"])][p] += 1
        if r["receiving_yards"] not in ("", "NA"):
            yd[(w, r["posteam"], p)] += float(r["receiving_yards"])
    return tg, yd

def rep(n, z, seasons):
    w, l = sum(r["res"] == "win" for r in z), sum(r["res"] == "loss" for r in z)
    if w + l < 5:
        return
    ys = " / ".join(f'{sum(r["res"] == "win" for r in z if r["y"] == y)}-{sum(r["res"] == "loss" for r in z if r["y"] == y)}' for y in seasons)
    beat = sum(r["yds"] - r["line"] for r in z) / len(z)
    gap = sum(r["ypg"] - r["line"] for r in z) / len(z)
    print(f"  {n:44} {w:3}-{l:<3} ({w / (w + l):.0%})  yds vs line {beat:+5.1f}  line sits {gap:+5.1f} under ypg  [{ys}]")

if __name__ == "__main__":
    seasons = [int(a) for a in sys.argv[1:]] or [2024, 2025, 2026]
    rows = []
    for season in seasons:
        ros = G.roster(season)
        tg, yd = targets(season, ros)
        for r in G.season_rows(season):
            if r["pos"] != "WR":
                continue
            prior, gms = collections.defaultdict(float), collections.Counter()
            for (w, t, p), v in yd.items():
                if t == r["team"] and w < r["wk"]:
                    prior[p] += v; gms[p] += 1
            top = [p for p, _ in sorted(prior.items(), key=lambda kv: -kv[1])[:3]]
            r["self_rank"] = top.index(r["pid"]) + 1 if r["pid"] in top else 4
            # the other top-2 WRs on the team, only if a real option: MIN_YPG+ yds/game so far this season
            mates = [p for p in top if p != r["pid"] and prior[p] / gms[p] >= MIN_YPG][:2]
            played = tg[(r["wk"], r["g"], r["team"])]
            r["out"] = [ros[p][1] for p in mates if played[p] == 0]
            rows.append(r)
    print(f"{len(rows)} cohort WR bets")
    rep("other top-2 WRs both played", [r for r in rows if not r["out"]], seasons)
    rep("a top-2 teammate WR out", [r for r in rows if r["out"]], seasons)
    print(" by who the bettor's player is on the team")
    rep("WR1, WR2 played", [r for r in rows if r["self_rank"] == 1 and not r["out"]], seasons)
    rep("WR1, WR2 out", [r for r in rows if r["self_rank"] == 1 and r["out"]], seasons)
    rep("WR2, WR1 played", [r for r in rows if r["self_rank"] == 2 and not r["out"]], seasons)
    rep("WR2, WR1 out", [r for r in rows if r["self_rank"] == 2 and r["out"]], seasons)
    print(" Good Matchup bets only")
    rep("GM, mates played", [r for r in rows if r["weak"] and not r["out"]], seasons)
    rep("GM, a mate out", [r for r in rows if r["weak"] and r["out"]], seasons)
    print("\nmate-out bets:")
    for r in rows:
        if r["out"]:
            print(f"  w{r['wk']} {r['name']:20} {r['team']} line {r['line']} -> {r['yds']:.0f} {r['res']:4}  out: {', '.join(r['out'])}")
