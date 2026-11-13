#!/usr/bin/env python3
"""Good Matchups: cuts points and the week's 3 picks (gm_score, gm_top). Run: .venv/bin/python live/test_gm_top.py"""
import os, sys, tempfile
os.environ["DOCS_DIR"] = os.environ["DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import poller as P, layers

RANKS = {f"T{i}": i for i in range(1, 31)} | {"CIN": 3, "TEN": 30}   # 30 teams: bottom third is rank 21+

def e(name, team="CIN", game="TEN @ CIN", sp=-3.5, ph=0.6, pa=0.3, form=(80, 70, 90), ypg=80, dv=100, kick=60, **kw):
    return {"player": name, "team": team, "game": game, "spread_home": sp, "pct_home": ph, "pct_away": pa, "ypg": ypg,
            "form": {"me": [["w", y, None] for y in form]}, "def_ypg": dv, "line": 60.5, "k": kick, **kw}

# points: home, favored, offense not bottom third, record > .400, last-3 >= 0.8x ypg
assert P.gm_score(e("all five"), RANKS) == 5
assert P.gm_score(e("road dog", team="TEN"), RANKS) == 1                    # away, CIN favored, rank 30, .300; form ok
assert P.gm_score(e("road dog", team="TEN", form=(10, 10, 10)), RANKS) == 0
assert P.gm_score(e("road fav", team="TEN", sp=4.5, pa=0.5), RANKS) == 3     # favored, record, form
assert P.gm_score(e("cold", form=(20, None, 30)), RANKS) == 4                # 25 avg vs 80 ypg; a DNP doesn't count
assert P.gm_score(e("no spread yet", sp=None), RANKS) == 4

kick = lambda w: w["k"]
wk = [e("A"), e("B", dv=150), e("C", team="TEN"), e("D", sp=None), e("E", team="TEN", dv=200), e("Hurt", inj="Out"), e("NoLine", line=None)]
assert P.gm_top(wk, RANKS, kick)
assert [w["player"] for w in wk if w["top3"]] == ["A", "B", "D"]            # 5, 5, 4 points: C and E (1) miss, B leads A on defense
assert not P.gm_top(wk, RANKS, kick)                                          # nothing new: no rewrite

wk[0]["k"] = -10                                                             # A's game kicked off: A is locked in
wk[1]["inj"] = "Out"                                                         # B ruled out before his game
P.gm_top(wk, RANKS, kick)
assert [w["player"] for w in wk if w["top3"]] == ["A", "D", "E"]             # B's slot goes to the best game still to come
wk[2]["k"] = -10                                                             # C kicked off unpicked: stays unpicked
P.gm_top(wk, RANKS, kick)
assert not wk[2]["top3"] and wk[0]["top3"]

assert layers.vetoes(P.GM_LAYERS, wk[0]) == [] and layers.vetoes(P.GM_LAYERS, wk[2]) == ["top3"]
print("ok")
