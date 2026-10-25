#!/usr/bin/env python3
"""Pull DK+FD series for the burst-model's flagged test games."""
import csv, datetime, os
import prop_series
from pull_instances import NAMES

ROOT = os.path.join(os.path.dirname(__file__), "..")
TODO = [
    ("2026_06_ARI_LA", "player_reception_yds"),
    ("2026_06_CAR_PHI", "player_reception_yds"),
    ("2026_06_CHI_ATL", "player_reception_yds"),
    ("2026_06_CHI_ATL", "player_rush_yds"),
    ("2026_06_DAL_GB", "player_reception_yds"),
    ("2026_06_SEA_DEN", "player_reception_yds"),
    ("2026_07_NE_CHI", "player_reception_yds"),
    ("2026_06_BUF_LV", "player_rush_yds"),
]
games = {r["game_id"]: r for r in csv.DictReader(open(f"{ROOT}/data/games.csv"))}
for gid, market in TODO:
    out = f"{ROOT}/data/props_series/{gid}_{market}.csv"
    if os.path.exists(out):
        print("have", out)
        continue
    g = games[gid]
    kick = datetime.datetime.fromisoformat(f"{g['gameday']}T{g['gametime']}:00-04:00").astimezone(datetime.timezone.utc)
    start = (kick - datetime.timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (kick + datetime.timedelta(hours=4, minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"--- {gid} {market}")
    try:
        prop_series.main(NAMES[g["away_team"]], NAMES[g["home_team"]], start, end, market, out)
    except Exception as e:
        print("FAILED", gid, market, repr(e))
