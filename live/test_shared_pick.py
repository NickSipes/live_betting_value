#!/usr/bin/env python3
"""Game Spreads rule (shared_pick): two shared opponents met within four week numbers, venue taken out, 4+ gap.
Run: .venv/bin/python live/test_shared_pick.py"""
import os, sys, tempfile
os.environ["DOCS_DIR"] = os.environ["DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cfb_poller as C

T = lambda i, s=0: [str(i), f"T{i}", 99, s]
f = lambda wk, h, a, hs, as_, n=False: {"id": f"g{wk}{h}{a}", "date": f"2026-09-{wk + 1:02d}T17:00Z", "wk": wk, "h": T(h, hs), "a": T(a, as_), "n": n}
game = {"date": "2026-11-07T17:00Z", "hid": "1", "aid": "2", "habbr": "T1", "aabbr": "T2", "pregame": 3.5}
# T1 beat T3 at home by 24 (20.2 neutral), T2 beat T3 away by 35 (38.8): T2 by 18.6
# T1 lost to T4 at home by 3 (-6.8), T2 beat T4 at home by 30 (26.2): T2 by 33   -> T2 by 25.8 neutral; T1 at home: +22.0
fin = [f(5, 1, 3, 24, 0), f(6, 3, 2, 0, 35), f(7, 1, 4, 0, 3), f(8, 2, 4, 30, 0)]
# turnovers: T1 gave it away 3 times against T4 (T4 once); every other meeting even
tos = {"g714": {"T1": 3, "T4": 1}}.get
p = C.shared_pick(game, fin, tos=lambda gid: tos(gid, {"T1": 0, "T2": 0, "T3": 0, "T4": 0}))
assert p["side"] == "away" and p["team"] == "T2" and p["line"] == -3.5 and p["implied"] == 22.0 and p["gap"] == 18.5, p
assert [s[0] for s in p["shared"]] == ["T3", "T4"] and p["shared"][0][3] == 1
assert p["shared"][1][4:] == [2, 0] and p["implied_to"] == 18.0, p    # T1 credited 8: T2 by 29.8 - 8 = 21.8 neutral, home +18.0
assert C.shared_pick(game, fin, tos=lambda gid: None)["implied_to"] is None
# four weeks apart counts (the backtest's rule); five does not, leaving one shared opponent and no pick
assert C.shared_pick(game, [f(2, 1, 3, 24, 0), fin[1], fin[2], fin[3]], tos=None) is not None
assert C.shared_pick(game, [f(1, 1, 3, 24, 0), fin[1], fin[2], fin[3]], tos=None) is None
# gap under 4: the line already has it
assert C.shared_pick({**game, "pregame": 20}, fin, tos=None) is None
# home side when the line gives the home team too little credit
assert C.shared_pick({**game, "hid": "2", "aid": "1", "habbr": "T2", "aabbr": "T1", "pregame": -3.5}, fin, tos=None)["side"] == "home"
# only games before kickoff count
assert C.shared_pick({**game, "date": "2026-09-01T00:00Z"}, fin, tos=None) is None
# the final grades it
assert C.cover({"bet": p["side"], "line": p["line"]}, 20, 24) == "win"
# what the Watchlist Games card says the rule still lacks
ranked = {**game, "hrank": 8, "arank": 99}
assert C.spread_need(game, fin) is None                 # unranked: nothing changes that before kickoff
assert C.spread_need(ranked, fin) is None                # it would pick
assert C.spread_need(ranked, fin[:2]) is None            # one shared opponent and no game left to add another
left = [{"state": "pre", "date": "2026-11-01T17:00Z", "hid": "1", "aid": "5"}]
assert C.spread_need(ranked, fin[:2], left) == "1 more shared opponent (has T3) met within 4 weeks"
assert C.spread_need(ranked, [], left) == "2 more shared opponents met within 4 weeks"
assert C.spread_need(ranked, fin[:2], [{**left[0], "state": "post"}]) is None      # that game is played
assert C.spread_need(ranked, fin[:2], [{**left[0], "date": "2026-11-08T17:00Z"}]) is None   # or after this one
assert C.spread_need({**ranked, "pregame": None}, fin) == "a posted line"
assert C.spread_need({**ranked, "pregame": 20}, fin) == "a line 4+ from the shared-opponent spread (T1 +22.0); it is 2.0 away"
print("ok")

# ---- layers on a pick (live/layers.py): any one catches it
import layers
base = {"side": "home", "gap": 8.0, "implied": -10.0, "model": -6.0, "opp_rank": 12}
V = lambda **kw: layers.vetoes(C.SPREAD_LAYERS, {**base, **kw})
assert V() == []
assert V(opp_rank=5) == ["top5"] and V(opp_rank=6) == []
assert V(gap=20.5) == ["gap20"] and V(gap=20) == []
assert V(model=4.0) == ["rating14"] and V(model=3.9) == [] and V(model=None) == []     # no rating: nothing to judge by
assert V(side="away") == ["away"]
assert V(side="away", opp_rank=2, gap=33.1, implied=-29.6, model=-2.4) == ["top5", "gap20", "rating14", "away"]

# ---- the stdlib rating is the ridge least-squares solution (checked against the normal equations)
import numpy as np, random
random.seed(1)
teams = [str(i) for i in range(12)]
true = {t: random.gauss(0, 10) for t in teams}
fins = []
for _ in range(60):
    h, a = random.sample(teams, 2)
    m = round(true[h] - true[a] + C.HFA + random.gauss(0, 8))
    fins.append({"h": [h, h, 99, max(m, 0)], "a": [a, a, 99, max(-m, 0)], "n": random.random() < .1})
prior = {t: true[t] / 2 for t in teams[:8]}
got = C.ridge_ratings(fins, prior)
A, y = np.zeros((len(teams), len(teams))), np.zeros(len(teams))
for f in fins:
    i, j = teams.index(f["h"][0]), teams.index(f["a"][0])
    mm = max(-C.CAP, min(C.CAP, f["h"][3] - f["a"][3])) - (0 if f["n"] else C.HFA)
    A[i, i] += 1; A[j, j] += 1; A[i, j] -= 1; A[j, i] -= 1; y[i] += mm; y[j] -= mm
for i, t in enumerate(teams):
    A[i, i] += C.LAM; y[i] += C.LAM * prior.get(t, C.FCS)
want = np.linalg.solve(A, y)
assert max(abs(got[t] - want[i]) for i, t in enumerate(teams)) < .01, [(got[t], want[i]) for i, t in enumerate(teams)]
print("layers ok")
