#!/usr/bin/env python3
"""The NFL lead call: rules, plus a replay of the backtest's games at poll speed.
Run: .venv/bin/python live/test_lead_call.py"""
import json, os, sys, tempfile
os.environ["DOCS_DIR"] = os.environ["DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "analysis"))
import cfb_poller as C

Q = {"home": -110, "away": -110, "updated": 1000}
def run(polls, top=True, snaps=((800, None, 3),)):
    """polls: (margin, gc, seconds, scored, quote). Returns the first bet fired. top: one value, or one per poll."""
    h = {"snaps": list(snaps)}
    tops = top if isinstance(top, list) else [top] * len(polls)
    for (m, gc, t, sc, q), tp in zip(polls, tops):
        h["snaps"].append((gc, None, m))     # the poller logs the snap before it checks
        if b := C.lead_call(h, tp, m, gc, sc, t, 180, q):
            return b

assert run([(14, 900, 1000, True, Q), (14, 1000, 1180, False, Q)]) == "away"        # home leads, away gets the points
assert run([(-14, 900, 1000, True, Q), (-14, 1000, 1180, False, Q)]) == "home"
assert run([(14, 900, 1000, True, Q)]) is None                                      # score has to stick a poll
assert run([(14, 900, 1000, True, Q), (14, 1000, 1180, False, None)]) is None       # no quote, no call
assert run([(14, 900, 1000, True, Q), (14, 1000, 1180, False, {**Q, "updated": 700})]) is None   # quote older than the score
assert run([(14, 900, 1000, True, Q), (7, 1000, 1180, True, Q), (14, 1100, 1360, False, Q)]) is None   # lead gone ends it
assert run([(14, 900, 1000, True, Q), (14, 1000, 1180, False, Q)], top=False) is None
assert run([(14, 3300, 1000, True, Q), (14, 3350, 1180, False, Q)], snaps=[(3200, None, 3)]) is None     # lead reached from minute 55
assert run([(14, 3250, 1000, True, Q), (14, 3310, 1180, False, Q)], snaps=[(3150, None, 3)]) == "away"   # lead before 55 fires, as backtested
assert run([(14, 900, 1000, True, Q), (14, 1000, 2000, False, Q)]) is None           # 15 min window
assert run([(14, 900, 1000, False, Q)], snaps=[(800, -9, 14)]) is None               # joined late: already seen 11+
assert run([(14, 1500, 1000, False, Q), (14, 1600, 1180, False, Q)], snaps=()) is None   # no history: an old lead
assert run([(14, 1500, 1000, True, Q), (14, 1600, 1180, False, Q)], snaps=[(200, None, 0)]) is None   # gap too long to date it
good = [(14, 900, 1000, True, Q), (14, 1000, 1180, False, Q)]
assert run(good + [(14, 1100, 1360, False, Q)], top=[None, None, True]) == "away"    # lookup failed at the lead, recovered
assert run(good, top=[True, None]) is None                                           # unknown never fires
assert run(good + [(14, 1100, 1360, False, Q)], top=[None, False, True]) is None     # a real no ends it
assert run([(10, 900, 1000, True, Q), (10, 1000, 1180, False, Q)]) is None
assert C.cover({"bet": "away", "line": 8.5}, 24, 17) == "win" and C.cover({"bet": "away", "line": 8.5}, 27, 17) == "loss"
assert C.cover({"bet": "home", "line": 3}, 10, 13) == "push"

# a blank final can't lose a call: the game waits in hist, then settles
P = C.CFB(180)
C.fetch_summary = lambda gid: {}
P.hist = {"G": {"snaps": [], "meta": {}, "call": {"id": "G", "bet": "away", "line": 8.5, "result": None}}}
P.reg = {"G": {"state": "post", "hs": None, "as": None, "hid": "1", "week": 8}}
P.archive_finals()
assert "G" in P.hist
P.reg["G"].update(hs=24, **{"as": 17})
P.archive_finals()
assert "G" not in P.hist and json.load(open(P.calls_path))[0]["result"] == "win"

# replay: every flagged NFL game before minute 55 should fire; the bet goes to the trailing side
from lead_spread_value import season_games
fired = n = 0
for season in (2024, 2025):
    for g in season_games("NFL", season):
        if not g["top"] or g["trig"][1] >= C.LEAD_LATEST:
            continue
        n += 1
        t0, gc0, lead, h0, a0 = g["trig"]
        h = {"snaps": []}
        bet, last = None, (0, 0)
        for t in range(t0 - 3600, t0 + 900, 180):      # polls on a 3-minute grid around the score
            hs, as_ = next(((x[2], x[3]) for x in reversed(g["tl"]) if x[0] <= t), (0, 0))
            margin = hs - as_
            h["snaps"].append((gc0, None, margin))
            bet = bet or C.lead_call(h, True, margin, gc0, margin != last[0] - last[1], t, 180, {"updated": t})
            last = (hs, as_)
        fired += bet == ("away" if lead > 0 else "home")
print(f"replay fired {fired}/{n}")
assert fired >= .85 * n
print("ok")
