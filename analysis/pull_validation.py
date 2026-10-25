#!/usr/bin/env python3
"""Pull lines for the 7 unflagged week-6 games: the unbiased validation set."""
import csv, datetime, os
import prop_series
from pull_instances import NAMES

ROOT = os.path.join(os.path.dirname(__file__), "..")
REC = ["2026_06_HOU_JAX", "2026_06_BAL_CLE", "2026_06_TEN_IND", "2026_06_NYJ_NE",
       "2026_06_PIT_TB", "2026_06_LAC_KC", "2026_06_WAS_SF"]
RUSH = ["2026_06_HOU_JAX", "2026_06_BAL_CLE", "2026_06_TEN_IND", "2026_06_LAC_KC"]
TODO = [(g, "player_reception_yds") for g in REC] + [(g, "player_rush_yds") for g in RUSH]

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
