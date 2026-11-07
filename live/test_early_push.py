#!/usr/bin/env python3
"""Pregame picks alert 20 min before kickoff, once (early_pushes). Run: .venv/bin/python live/test_early_push.py"""
import datetime, json, os, sys, tempfile, time
os.environ["DOCS_DIR"] = os.environ["DATA_DIR"] = D = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import poller as P

sent = []
P.push.send = lambda title, body, **kw: sent.append(title)
kick = lambda mins: (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=mins)).isoformat()
gm = lambda name, mins, **kw: {"player": name, "market": "rec", "line": 60.5, "book": "fanduel", "lines": {"fanduel": 60.5, "draftkings": 61.5},
                               "game": "TEN @ CIN", "kick_iso": kick(mins), **kw}
json.dump({"entries": [gm("Tee Higgins", 15), gm("Ja'Marr Chase", 45), gm("Puka Nacua", 10, inj="Out"),
                       gm("Davante Adams", 10, line=None), gm("Old Game", -5)]}, open(f"{D}/watchlist.json", "w"))
rb = lambda name, rank, team="CIN": {**gm(name, 18), "market": "rush", "line": 30.5, "off_rank": rank, "teams": 32, "team": team}
json.dump({"entries": [rb("Chase Brown", 30), rb("Top Offense RB", 3)]}, open(f"{D}/rb_unders.json", "w"))
json.dump({"entries": [gm("Courtland Sutton", 19)]}, open(f"{D}/wr_unders.json", "w"))
P.TEAM_EXPL = {}

m = P.Monitor(60)
m.early_pushes()
assert sent == ["Bet Tee Higgins Over 60.5 rec yds on FanDuel", "Bet Chase Brown Under 30.5 rush yds on FanDuel",
                "Bet Courtland Sutton Under 60.5 rec yds on FanDuel"], sent   # 45 min out, injured, no line, kicked off, top offense: no alert
m.early_pushes()
assert len(sent) == 3                                                        # once per pick
assert P.Monitor(60).alerted("Tee Higgins", "rec")                           # survives a restart, so kickoff doesn't push again
assert not m.alerted("Ja'Marr Chase", "rec")                                 # not alerted yet: kickoff still pushes
m.early[P.short_name("Tee Higgins") + "|rec"] = time.time() - 7 * 86400
assert not m.alerted("Tee Higgins", "rec")                                   # last week's alert doesn't count
print("ok")
