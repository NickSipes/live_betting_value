#!/usr/bin/env python3
"""Closing pregame player-prop lines for past NFL games, saved for reuse.

For each regular-season game (weeks 4-18 by default), one historical snapshot at kickoff of
receiving and rushing yards from FanDuel and DraftKings: about 20 credits a game, plus 1 credit
per kickoff time to look up the games. Everything is saved gzipped under data/props_hist/<season>/
and never bought twice. Stops at --cap credits spent this run.

Run: .venv/bin/python analysis/pull_pregame_props.py 2024 2025 [--weeks 4-18] [--cap 10000] [--limit N]
Read: load(season) -> {nflverse game_id: {"commence", "event_id", "bookmakers": [...]}}"""
import csv, datetime, gzip, json, os, sys, time, urllib.error, urllib.request

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
OUT = f"{ROOT}/data/props_hist"
KEY = os.environ.get("ODDS_API_KEY") or open(f"{ROOT}/.env").read().split("ODDS_API_KEY=")[1].split()[0]
H = "https://api.the-odds-api.com/v4/historical/sports/americanfootball_nfl"
MARKETS = "player_reception_yds,player_rush_yds"
NICK = {"ARI": "Cardinals", "ATL": "Falcons", "BAL": "Ravens", "BUF": "Bills", "CAR": "Panthers", "CHI": "Bears", "CIN": "Bengals",
        "CLE": "Browns", "DAL": "Cowboys", "DEN": "Broncos", "DET": "Lions", "GB": "Packers", "HOU": "Texans", "IND": "Colts",
        "JAX": "Jaguars", "KC": "Chiefs", "LA": "Rams", "LAC": "Chargers", "LV": "Raiders", "MIA": "Dolphins", "MIN": "Vikings",
        "NE": "Patriots", "NO": "Saints", "NYG": "Giants", "NYJ": "Jets", "PHI": "Eagles", "PIT": "Steelers", "SEA": "Seahawks",
        "SF": "49ers", "TB": "Buccaneers", "TEN": "Titans", "WAS": "Commanders"}   # nflverse abbreviation -> nickname in Odds API names
spent = 0

def get(url, cap):
    """GET with retries on server errors; counts credits from the response header."""
    global spent
    for i in range(4):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "curl/8.4"}), timeout=40)
            spent += int(r.headers.get("x-requests-last") or 0)
            if spent > cap:
                raise SystemExit(f"credit cap {cap} passed ({spent} spent); rerun to continue, saved games are skipped")
            return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code < 500 or i == 3:
                raise
            time.sleep(2 ** (i + 1))

def kickoffs(season, weeks):
    """nflverse games -> {game_id: (week, kickoff UTC iso, home full name)} for regular-season games."""
    names = {}
    out = {}
    for r in csv.DictReader(open(f"{ROOT}/data/games.csv")):
        if r["season"] != str(season) or r["game_type"] != "REG" or int(r["week"]) not in weeks:
            continue
        # gametime is US Eastern; DST ends the first Sunday of November
        local = datetime.datetime.fromisoformat(f'{r["gameday"]} {r["gametime"]}')
        nov1 = datetime.date(local.year, 11, 1)
        dst_end = nov1 + datetime.timedelta(days=(6 - nov1.weekday()) % 7)
        off = 4 if (local.date() < dst_end and local.month >= 3) else 5
        utc = (local + datetime.timedelta(hours=off)).replace(tzinfo=datetime.timezone.utc)
        out[r["game_id"]] = (int(r["week"]), utc, r["home_team"])
    return out

def load(season):
    d = f"{OUT}/{season}"
    return {f[:-8]: json.load(gzip.open(f"{d}/{f}", "rt")) for f in sorted(os.listdir(d)) if f.endswith(".json.gz")} if os.path.isdir(d) else {}

def main():
    argv = sys.argv[1:]
    args = [a for i, a in enumerate(argv) if not a.startswith("--") and not (i and argv[i - 1].startswith("--"))]
    opt = lambda k, d: sys.argv[sys.argv.index(k) + 1] if k in sys.argv else d
    lo, hi = map(int, opt("--weeks", "4-18").split("-"))
    cap, limit = int(opt("--cap", "10000")), int(opt("--limit", "0"))
    seasons = [int(a) for a in args] or [2025]
    done = 0
    for season in seasons:
        os.makedirs(f"{OUT}/{season}", exist_ok=True)
        games = kickoffs(season, range(lo, hi + 1))
        todo = {g: v for g, v in games.items() if not os.path.exists(f"{OUT}/{season}/{g}.json.gz")}
        print(f"{season}: {len(games)} games, {len(todo)} to pull")
        by_time = {}
        for g, (wk, t, home) in todo.items():
            by_time.setdefault(t, []).append(g)
        for t, gids in sorted(by_time.items()):
            stamp = (t - datetime.timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
            evs = get(f"{H}/events?apiKey={KEY}&date={stamp}", cap).get("data", [])
            for g in gids:
                away, home = g.split("_")[2], g.split("_")[3]
                cands = [e for e in evs if abs(datetime.datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00")) - t).total_seconds() < 3 * 3600]
                ev = next((e for e in cands if NICK[home] in e["home_team"] and NICK[away] in e["away_team"]), None)
                if ev is None:
                    print(f"  {g}: no matching event at {stamp}")
                    continue
                d = get(f"{H}/events/{ev['id']}/odds?apiKey={KEY}&date={ev['commence_time']}&bookmakers=fanduel,draftkings"
                        f"&markets={MARKETS}&oddsFormat=american", cap)
                rec = {"game_id": g, "event_id": ev["id"], "home_team": ev["home_team"], "away_team": ev["away_team"],
                       "commence": ev["commence_time"], "snapshot": d.get("timestamp"), "bookmakers": (d.get("data") or {}).get("bookmakers", [])}
                json.dump(rec, gzip.open(f"{OUT}/{season}/{g}.json.gz", "wt"))
                done += 1
                print(f"  {g}: {sum(len(m['outcomes']) for b in rec['bookmakers'] for m in b['markets'])} outcomes · {spent} credits so far", flush=True)
                if limit and done >= limit:
                    print(f"stopped at --limit {limit}; credits spent {spent}")
                    return
    print(f"done; credits spent this run {spent}")

if __name__ == "__main__":
    main()
