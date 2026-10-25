#!/usr/bin/env python3
"""Pull DK+FD historical prop series for every game in blanked_instances.json.
Market: player_reception_yds (rec) / player_rush_yds (rush).
Caches raw snapshots (prop_series does), writes data/props_series/<gid>_<market>.csv."""
import csv, json, os, datetime
import prop_series

ROOT = os.path.join(os.path.dirname(__file__), "..")
NAMES = {
    "ARI": "Arizona Cardinals", "ATL": "Atlanta Falcons", "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills", "CAR": "Carolina Panthers", "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals", "CLE": "Cleveland Browns", "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos", "DET": "Detroit Lions", "GB": "Green Bay Packers",
    "HOU": "Houston Texans", "IND": "Indianapolis Colts", "JAX": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs", "LA": "Los Angeles Rams", "LAC": "Los Angeles Chargers",
    "LV": "Las Vegas Raiders", "MIA": "Miami Dolphins", "MIN": "Minnesota Vikings",
    "NE": "New England Patriots", "NO": "New Orleans Saints", "NYG": "New York Giants",
    "NYJ": "New York Jets", "PHI": "Philadelphia Eagles", "PIT": "Pittsburgh Steelers",
    "SEA": "Seattle Seahawks", "SF": "San Francisco 49ers", "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans", "WAS": "Washington Commanders",
}
MARKET = {"rec": "player_reception_yds", "rush": "player_rush_yds"}

games = {r["game_id"]: r for r in csv.DictReader(open(f"{ROOT}/data/games.csv"))}
inst = json.load(open(f"{ROOT}/data/blanked_instances.json"))
os.makedirs(f"{ROOT}/data/props_series", exist_ok=True)

todo = sorted({(r["game_id"], MARKET[r["kind"]]) for r in inst})
print(f"{len(todo)} game-market pulls")
for gid, market in todo:
    out = f"{ROOT}/data/props_series/{gid}_{market}.csv"
    if os.path.exists(out):
        print("have", out)
        continue
    g = games[gid]
    # gametime is ET; Sept/Oct = EDT = UTC-4
    kick = datetime.datetime.fromisoformat(f"{g['gameday']}T{g['gametime']}:00-04:00")
    kick = kick.astimezone(datetime.timezone.utc)
    start = (kick - datetime.timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (kick + datetime.timedelta(hours=4, minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"--- {gid} {market} {start}..{end}")
    try:
        prop_series.main(NAMES[g["away_team"]], NAMES[g["home_team"]], start, end, market, out)
    except Exception as e:  # keep going; a missing market shouldn't kill the batch
        print("FAILED", gid, market, repr(e))
