#!/usr/bin/env python3
"""The WR teammate-out rule (wr_mate_out). Run: .venv/bin/python live/test_wr_mate.py"""
import os, sys, tempfile
os.environ["DOCS_DIR"] = os.environ["DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import poller as P

wr = lambda ypg, team: {"rec": {"ypg": ypg, "pos": "WR"}, "team": team}
P.PRIORS = {"Puka Nacua": wr(94, "LA"), "Davante Adams": wr(80, "LA"), "Tutu Atwell": wr(21, "LA"),
            "Ja'Marr Chase": wr(70, "CIN"), "Tee Higgins": wr(104, "CIN"), "Chase Brown": {"rush": {"ypg": 70, "pos": "RB"}, "team": "CIN"}}
sn = P.short_name   # the injury report and the book's quotes are keyed by short name
OUT, Q = {sn("Davante Adams"): "Out"}, {sn("Puka Nacua"), sn("Tutu Atwell"), "K. Williams", "T. Higbee"}
assert P.wr_mate_out("Puka Nacua", OUT) == "Davante Adams"                 # on the injury report
assert P.wr_mate_out("Puka Nacua", {}, Q) == "Davante Adams"               # not on the report, but the book has no line for him
assert P.wr_mate_out("Puka Nacua", {}) is None                             # nothing says he's out
assert P.wr_mate_out("Puka Nacua", {}, set()) is None                      # no lines up yet: silence means nothing
assert P.wr_mate_out("Puka Nacua", {}, {sn("Puka Nacua")}) is None         # one or two names isn't a market yet
assert P.wr_mate_out("Puka Nacua", {sn("Tutu Atwell"): "Out"}) is None         # a 21 ypg teammate isn't a top WR
assert P.wr_mate_out("Tutu Atwell", OUT) is None                           # Nacua still plays: Atwell isn't the one left
assert P.wr_mate_out("Ja'Marr Chase", {sn("Tee Higgins"): "Doubtful"}) == "Tee Higgins"   # the lower-ypg WR becomes the WR1
assert P.wr_mate_out("Tee Higgins", {sn("Ja'Marr Chase"): "Out"}) == "Ja'Marr Chase"
assert P.wr_mate_out("Chase Brown", {sn("Tee Higgins"): "Out"}) is None        # only receivers count
assert P.wr_mate_out("Nobody", OUT) is None
print("ok")
