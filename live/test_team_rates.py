#!/usr/bin/env python3
"""Team rate shrinkage: one early carry can't carry a call. Run: .venv/bin/python live/test_team_rates.py"""
import os, sys, tempfile
os.environ["DOCS_DIR"] = os.environ["DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import poller as P

share, ypa, ypc = P.team_rates(5, 4, 59, 16)          # CIN @ ATL 3:49 Q1: 4 passes, one 16-yd carry
assert 4.5 < ypc < 5.5 and ypa < 9 and share < 0.7, (share, ypa, ypc)
assert abs(P.team_rates(55, 25, 175, 135)[2] - 4.375) < 1e-9   # 30 carries at 4.5: barely moved
x = P.feature_vec({"ypg": 70.3, "ype": 8.07}, "rec", 148.6, 671, 47.0, 1, 5, 1, 47.0, 0,
                  {"pass_att": 4, "rush_att": 1, "pass_yds": 59, "rush_yds": 16}, 116.5, 77.5)
assert P.score_row(x) < P.NEAR, P.score_row(x)        # Chase's 0.75 call is below NEAR now
print("ok")
