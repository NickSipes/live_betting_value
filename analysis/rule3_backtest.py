#!/usr/bin/env python3
"""The proposed NFL lead call, priced on real live lines: the trailing team is top third by record and
a top-third scoring offense on the season (points per game, games before this one), and a team first
leads by 10+ before minute 55. Reports with and without the trailing-favorite rule.

2024-25 is where the rule was shaped (on estimated lines); 2026 was never looked at, so it is the test.
Run: .venv/bin/python analysis/rule3_backtest.py [--pull]   (--pull spends credits up to CREDIT_CAP;
snapshots already in data/hist_spreads are reused, never bought twice)"""
import collections, json, sys
import numpy as np
import lead_spread_value as M
from team_shapes import tier

M.LEAD = 10
M.CREDIT_CAP = 2000   # approved: about 1,000-2,000
SEASONS = (2024, 2025, 2026)

def offense_pct(res):
    """(team, week) -> share of the league it out-scores per game, from games before that week."""
    cache = {}
    def pct(team, wk):
        if wk not in cache:
            pf = collections.defaultdict(list)
            for r in res:
                if r[0] < wk:
                    pf[r[2]].append(r[4]); pf[r[3]].append(r[5])
            ppg = {t: np.mean(v) for t, v in pf.items() if len(v) >= 2}
            cache[wk] = {t: sum(x < v for x in ppg.values()) / (len(ppg) - 1) for t, v in ppg.items()}
        return cache[wk].get(team)
    return pct

def pool():
    out = []
    for s in SEASONS:
        pct = offense_pct(M.nfl_results(s))
        for g in M.season_games("NFL", s):
            t0, gc, lead, *_ = g["trig"]
            if gc >= 3300 or g.get("sp") is None:
                continue
            trail, tab = (g["a"], g["aabbr"]) if lead > 0 else (g["h"], g["habbr"])
            off = pct(tab, g["week"])
            if tier(trail[0]) != "top" or off is None or off < 2 / 3:
                continue
            out.append(g)
    return out

def main():
    games = pool()
    have = M.cached()
    priced = lambda: [g for g in games if (e := M.entry(g, M.near(have["NFL"], g["trig"][0]))) and e[0] == "bet"]
    print(f"rule games: {len(games)} ({collections.Counter(g['season'] for g in games)}); priced from cache: {len(priced())}")
    if "--pull" in sys.argv:
        try:
            M.pull(games, have)
        except AssertionError as e:
            print("STOPPED:", e)
        print(f"credits spent this run: {M.spent}")
    rows = []
    for g in games:
        e = M.entry(g, M.near(have["NFL"], g["trig"][0]))
        if e and e[0] == "bet":
            rows.append(M.bet_row(g, e))
    json.dump(rows, open(f"{M.ROOT}/data/rule3_bets.json", "w"))
    no_price = len(games) - len(rows)
    print(f"priced {len(rows)} of {len(games)}; {no_price} had no usable quote (lead gone first, or no snapshot)")
    def rep(name, rs):
        w, l = sum(r["pnl"] > 0 for r in rs), sum(r["pnl"] < 0 for r in rs)
        p = len(rs) - w - l
        act = sum(r["pnl"] for r in rs) / len(rs) if rs else 0
        flat = (w * 100 / 110 - l) / len(rs) if rs else 0
        print(f"  {name:28} {w}-{l}{f'-{p}' if p else ''}  win {w / max(1, w + l):.0%}  ROI actual {act:+.1%}  flat -110 {flat:+.1%}")
    for label, keep in (("WITHOUT favorites rule", lambda r: True), ("WITH favorites rule", lambda r: r["fav"])):
        print(label)
        for s in SEASONS:
            rep(f"{s}", [r for r in rows if r["season"] == s and keep(r)])
        rep("2024-25 (rule shaped here)", [r for r in rows if r["season"] < 2026 and keep(r)])
        rep("all", [r for r in rows if keep(r)])

if __name__ == "__main__":
    main()
