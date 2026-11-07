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
print("ok")
