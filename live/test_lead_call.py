#!/usr/bin/env python3
"""The NFL lead call: rules, plus a replay of the backtest's games at poll speed.
Run: .venv/bin/python live/test_lead_call.py"""
import json, os, sys, tempfile
os.environ["DOCS_DIR"] = os.environ["DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "analysis"))
import cfb_poller as C

Q = {"home": -110, "away": -110, "updated": 1000}
def run(polls, top=True, snaps=((800, None, 3),), pre="fav"):
    """polls: (margin, gc, seconds, scored, quote). Returns the first bet fired. top: one value, or one per poll."""
    h = {"snaps": list(snaps)}
    tops = top if isinstance(top, list) else [top] * len(polls)
    for (m, gc, t, sc, q), tp in zip(polls, tops):
        h["snaps"].append((gc, None, m))     # the poller logs the snap before it checks
        if b := C.lead_call(h, tp, C.nfl_side((3 if m > 0 else -3) if pre == "fav" else pre), m, gc, sc, t, 180, q):
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
assert run(good, pre=-3) is None                                                     # trailing team was the underdog
assert run(good, pre=0) is None                                                      # a pick'em isn't a favorite
assert run(good, pre=None) is None                                                   # unknown never fires
def why(polls, **kw):
    h = {"snaps": [(kw.get("gc0", 800), None, 3)]}
    for m, gc, t, sc in polls:
        h["snaps"].append((gc, None, m))
        C.lead_call(h, kw.get("top", True), C.nfl_side(kw.get("pre", 3)), m, gc, sc, t, 180, Q)
    return h["lead11"]["why"]
assert why([(14, 900, 1000, True), (14, 1000, 1180, False)]) == "called"
assert why([(14, 900, 1000, True)], pre=-3) == "side_rule"
assert why([(14, 900, 1000, True)], top=False) == "not_top"
assert why([(14, 3300, 1000, True)], gc0=3200) == "late"
assert why([(14, 900, 1000, True), (7, 1000, 1180, True)]) == "lead_gone"
assert run([(10, 900, 1000, True, Q), (10, 1000, 1180, False, Q)]) is None
assert C.cover({"bet": "away", "line": 8.5}, 24, 17) == "win" and C.cover({"bet": "away", "line": 8.5}, 27, 17) == "loss"
assert C.cover({"bet": "home", "line": 3}, 10, 13) == "push"

# the book: FanDuel first, even when DraftKings looks better; books 2+ apart wait
row = {"fanduel": [-20.5, -106, -125, 100], "draftkings": [-17.5, 100, -133, 100]}   # WIS @ IOWA, 10/31
assert C.pick_quote(row, "home") == ("fanduel", -20.5, -106)
assert C.pick_quote(row, "away") == ("fanduel", 20.5, -125)
assert C.pick_quote(row, "home", since=200) is None and C.pick_quote(None, "home") is None
assert C.pick_quote({"draftkings": [-3, -110, -110, 100]}, "home") == ("draftkings", -3, -110)
assert C.pick_quote({"fanduel": [-3, -110, -110, 1], "draftkings": [-4, -105, -115, 100]}, "home", since=50)[0] == "draftkings"   # FD stale
assert not C.books_agree(row) and not C.books_agree({"fanduel": [-3, 0, 0, 0], "draftkings": [-5, 0, 0, 0]})
assert C.books_agree({"fanduel": [-3, 0, 0, 0], "draftkings": [-4, 0, 0, 0]}) and C.books_agree({"fanduel": [-3, 0, 0, 0]}) and C.books_agree(None)

# college lead rule: unranked leader, ranked trailer
assert C.cfb_side(99, 10)(1) is True and C.cfb_side(10, 99)(-1) is True              # leader unranked, trailer ranked
assert C.cfb_side(10, 99)(1) is False and C.cfb_side(99, 99)(1) is False and C.cfb_side(5, 10)(1) is False
assert C.cfb_side(None, 10)(1) is None

# college watch: the dog's last game by date was a loss with a ranked team on the field
f = lambda d, h, a: {"date": d, "h": h, "a": a}
R = {"pregame": 7, "date": "2026-10-03T12:00Z", "hid": "1", "aid": "2", "habbr": "H", "aabbr": "A"}
F = [f("2026-09-26T12:00Z", ["1", "H", 99, 10], ["3", "X", 12, 30])]
assert C.watch_spot(R, F) == {"side": "home", "team": "H", "line": 7, "opp": "X", "last": "10-30", "rank": None, "opp_rank": 12}
assert C.watch_spot({**R, "pregame": -7}, F) is None                                  # away is the dog, no game on file
assert C.watch_spot({**R, "pregame": 0}, F) is None and C.watch_spot({**R, "pregame": None}, F) is None
assert C.watch_spot(R, [f("2026-09-26T12:00Z", ["1", "H", 99, 10], ["3", "X", 99, 30])]) is None   # nobody ranked
assert C.watch_spot(R, [f("2026-09-26T12:00Z", ["1", "H", 99, 40], ["3", "X", 12, 30])]) is None   # won
assert C.watch_spot(R, F + [f("2026-09-30T12:00Z", ["1", "H", 99, 40], ["4", "Y", 99, 30])]) is None   # only the latest game counts
assert C.watch_spot(R, [f("2026-10-10T12:00Z", ["1", "H", 99, 10], ["3", "X", 12, 30])]) is None   # never a later game

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

# every analysis field reaches the archive
P.hist = {"H": {"snaps": [], "meta": {}, "quotes": [[1, 900, 7, 21, {"fanduel": [-9.5, -110, -110, 1]}]], "lead11": {"why": "underdog"}, "top": True}}
P.reg = {"H": {"state": "post", "hs": 21, "as": 7, "hid": "1", "week": 3, "date": "2026-09-20T17:00Z"}}
P.archive_finals()
rec = json.load(open(f"{C.ARCHIVE}/{C.SPORT}_w3.json"))["H"]
assert rec["quotes"][0][4]["fanduel"][1] == -110 and rec["lead11"]["why"] == "underdog" and rec["top"] is True and rec["date"]

# replay: every flagged NFL game before minute 55 should fire; the bet goes to the trailing side
from lead_spread_value import season_games
fired = n = 0
for season in (2024, 2025):
    for g in season_games("NFL", season):
        if not g["top"] or g["trig"][1] >= C.LEAD_LATEST or g["sp"] is None or g["trig"][2] * g["sp"] <= 0:
            continue
        n += 1
        t0, gc0, lead, h0, a0 = g["trig"]
        h = {"snaps": []}
        bet, last = None, (0, 0)
        for t in range(t0 - 3600, t0 + 900, 180):      # polls on a 3-minute grid around the score
            hs, as_ = next(((x[2], x[3]) for x in reversed(g["tl"]) if x[0] <= t), (0, 0))
            margin = hs - as_
            h["snaps"].append((gc0, None, margin))
            bet = bet or C.lead_call(h, True, C.nfl_side(g["sp"]), margin, gc0, margin != last[0] - last[1], t, 180, {"updated": t})
            last = (hs, as_)
        fired += bet == ("away" if lead > 0 else "home")
print(f"replay fired {fired}/{n}")
assert fired >= .85 * n

# college: the 18 flagged games with only the trailing team ranked, at poll speed
fired = n = 0
for season in (2025, 2026):
    for g in season_games("CFB", season):
        t0, gc0, lead, h0, a0 = g["trig"]
        hr, ar = g["rk"]
        if not g["top"] or not C.cfb_side(hr, ar)(lead):
            continue
        n += 1
        h = {"snaps": []}
        bet, last = None, (0, 0)
        for t in range(t0 - 3600, t0 + 900, 180):
            hs, as_ = next(((x[2], x[3]) for x in reversed(g["tl"]) if x[0] <= t), (0, 0))
            margin = hs - as_
            h["snaps"].append((gc0, None, margin))
            bet = bet or C.lead_call(h, True, C.cfb_side(hr, ar), margin, gc0, margin != last[0] - last[1], t, 180, {"updated": t})
            last = (hs, as_)
        fired += bet == ("away" if lead > 0 else "home")
print(f"college replay fired {fired}/{n}")
assert n >= 18 and fired >= .85 * n
print("ok")
