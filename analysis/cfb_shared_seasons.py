#!/usr/bin/env python3
"""The Game Spreads rule (two or more shared opponents met within 4 weeks, line 4+ from the spread they imply) on
past seasons, the same code for each: scores, ranks and kickoffs from ESPN's weekly scoreboards (cached
data/cfb_board_{season}.json), lines from the-odds-api historical snapshots in data/hist_spreads (one per game day
with a ranked team, a second late one when the day's kickoffs span 4+ hours; a snapshot lists every game not yet
kicked off, the last quote before kickoff is kept). Seasons 2021-2025; 2026 is the live forward test.
Run: .venv/bin/python analysis/cfb_shared_seasons.py [--pull] [--cap 3500]   (--pull spends credits, 10 per snapshot)"""
import sys, os, json, time, glob, collections, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ARGS, sys.argv = sys.argv[1:], sys.argv[:1]   # keep the imported modules' flag checks quiet
import numpy as np
import cfb_top25_lines as T

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
RAW = f"{ROOT}/data/hist_spreads"
ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard"
SEASONS = (2021, 2022, 2023, 2024, 2025)
PULL_SEASONS = (2021, 2022, 2023, 2024)
CAP = int(ARGS[ARGS.index("--cap") + 1]) if "--cap" in ARGS else 3500
PULL = "--pull" in ARGS
spent = 0

def get(u):
    return json.load(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=30))

def board(season):
    """Finished games: [{id, wk, t (kickoff unix), h, a (abbr), hn, an (full names), hs, as, hr, ar, neutral}]."""
    p = f"{ROOT}/data/cfb_board_{season}.json"
    if os.path.exists(p):
        return json.load(open(p))
    out = []
    for wk in range(1, 17):
        for e in get(f"{ESPN}?groups=80&limit=300&seasontype=2&week={wk}&dates={season}").get("events", []):
            c = e["competitions"][0]
            if e["status"]["type"]["state"] != "post":
                continue
            t = {x["homeAway"]: x for x in c["competitors"]}
            rk = lambda s: (t[s].get("curatedRank") or {}).get("current") or 99
            out.append({"id": e["id"], "wk": wk, "t": T.ts(e["date"].replace("Z", ":00Z") if len(e["date"]) == 17 else e["date"]), "h": t["home"]["team"]["abbreviation"], "a": t["away"]["team"]["abbreviation"],
                        "hn": t["home"]["team"]["displayName"], "an": t["away"]["team"]["displayName"],
                        "hs": int(t["home"].get("score") or 0), "as": int(t["away"].get("score") or 0),
                        "hr": rk("home"), "ar": rk("away"), "neutral": bool(c.get("neutralSite"))})
    json.dump(out, open(p, "w"))
    return out

def stamps(games):
    """Snapshot times covering every ranked game: before the first kickoff of each day unless one sits within 6 hours
    before it already, plus one before the day's last kickoff when that is 4+ hours later."""
    days = collections.defaultdict(list)
    for g in games:
        if min(g["hr"], g["ar"]) <= 25:
            days[g["t"] // 86400].append(g["t"])
    out = []
    for d in sorted(days):
        first, last = min(days[d]) - 600, max(days[d]) - 600
        if not out or out[-1] <= first - 6 * 3600:
            out.append(first)
        if last - out[-1] >= 4 * 3600:
            out.append(last)
    return out

def snapshot(t):
    """Cached snapshot at or before unix time t; fetched (10 credits) only with --pull and under the cap."""
    global spent
    stamp = T.iso(t)
    p = f"{RAW}/CFB_{stamp.replace(':', '')}.json"
    if os.path.exists(p):
        return json.load(open(p))
    if not PULL or spent + 10 > CAP:
        return None
    u = f"https://api.the-odds-api.com/v4/historical/sports/americanfootball_ncaaf/odds?apiKey={T.KEY}&date={stamp}&bookmakers=fanduel,draftkings&markets=spreads&oddsFormat=american"
    r = urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "curl/8.4"}), timeout=30)
    d = json.load(r)
    spent += int(r.headers.get("x-requests-last") or 10)
    print(f"  pulled {stamp}: {len(d.get('data', []))} events, {r.headers.get('x-requests-remaining')} credits left")
    json.dump(d, open(p, "w"))
    time.sleep(.2)
    return d

def lines(games):
    """{game id: home spread}: the last pre-kick FanDuel (else DraftKings) quote across the season's snapshots."""
    q, season = {}, T.iso(games[0]["t"])[:4]
    if int(season) in PULL_SEASONS:   # 2025's lines come from the lead-spread study's intraday snapshots already cached
        for t in stamps(games):
            snapshot(t)
    for f in sorted(glob.glob(f"{RAW}/CFB_{season}-*.json")):   # the stamps plus any snapshot cached by another study
        d = json.load(open(f))
        st = T.ts(d["timestamp"])
        for e in d.get("data", []):
            if T.ts(e["commence_time"]) <= st:
                continue
            pts = {b["key"]: o["point"] for b in e.get("bookmakers", []) for m in b["markets"] for o in m["outcomes"] if o["name"] == e["home_team"]}
            if pts:
                q[T.norm(e["home_team"]), T.norm(e["away_team"]), T.ts(e["commence_time"]) // 86400] = pts.get("fanduel", pts.get("draftkings"))
    out = {}
    for g in games:
        day = g["t"] // 86400
        sp = next((q[k] for d in (day, day - 1, day + 1) if (k := (T.norm(g["hn"]), T.norm(g["an"]), d)) in q), None)
        if sp is not None:
            out[g["id"]] = float(sp)
    return out

def tuples(games):
    return [(g["wk"], g["h"], g["a"], g["hs"] - g["as"], g["neutral"]) for g in games]

def prior(season):
    g = tuples(board(season - 1))
    rt, _ = T.fit([x[1:] for x in g], {}, 0.0)
    n = collections.Counter(t for x in g for t in x[1:3])
    return {t: T.SHRINK * r for t, r in rt.items() if n[t] >= 6}

def season_bets(season):
    """Every ranked game from week 5 with a line and one or more shared opponents met within 4 weeks:
    (n shared, gap from the bet side's view, won, both ranked, home bet)."""
    games, hist, pri = board(season), tuples(board(season - 1)), prior(season)
    sp = lines(games)
    ranked = [g for g in games if min(g["hr"], g["ar"]) <= 25 and g["wk"] >= 5]
    print(f"{season}: {len(games)} games, {len(ranked)} ranked from week 5, {sum(g['id'] in sp for g in ranked)} with a line")
    tup, out = tuples(games), []
    for wk in sorted({g["wk"] for g in ranked}):
        rt, hfa = T.fit([x[1:] for x in tup if x[0] < wk], pri, T.FCS, None)
        for g in ranked:
            if g["wk"] != wk or g["id"] not in sp:
                continue
            k = T.look(tup, hist, wk, g["h"], g["a"], g["neutral"], rt, hfa, {}, {}, {})
            if k["n_recent"] < 1 or k["common_recent"] is None:
                continue
            gap = sp[g["id"]] - k["common_recent"]                 # +: the line gives the home side too much, bet home
            cover = np.sign(g["hs"] - g["as"] + sp[g["id"]])        # +: home covered
            if gap == 0 or cover == 0:
                continue
            out.append((k["n_recent"], abs(gap), (cover > 0) == (gap > 0), max(g["hr"], g["ar"]) <= 25, gap > 0, season))
    return out

def table(bets):
    def row(title, f):
        cells = []
        for s in SEASONS + ("all",):
            v = [b for b in bets if f(b) and (s == "all" or b[5] == s)]
            w = sum(b[2] for b in v)
            cells.append(f"{w}-{len(v) - w}" + (f" ({100 * w / len(v):.0f}%)" if s == "all" and v else ""))
        print(f"  {title:<44}" + "".join(f"{c:>12}" for c in cells))
    print(f"\n  {'':<44}" + "".join(f"{s:>12}" for s in SEASONS + ("all",)))
    print("the rule: 2+ shared opponents <=4 weeks apart, ranked game")
    for th in (4, 6, 8):
        row(f"gap >= {th}", lambda b, th=th: b[0] >= 2 and b[1] >= th)
    row("gap >= 4, both ranked", lambda b: b[0] >= 2 and b[1] >= 4 and b[3])
    row("gap >= 4, one ranked", lambda b: b[0] >= 2 and b[1] >= 4 and not b[3])
    row("gap >= 4, bet home", lambda b: b[0] >= 2 and b[1] >= 4 and b[4])
    row("gap >= 4, bet away", lambda b: b[0] >= 2 and b[1] >= 4 and not b[4])
    row("gap >= 4, 3+ shared", lambda b: b[0] >= 3 and b[1] >= 4)
    print("one shared opponent only")
    for th in (4, 8, 14):
        row(f"gap >= {th}", lambda b, th=th: b[0] == 1 and b[1] >= th)

if __name__ == "__main__":
    need = {s: sum(1 for t in stamps(board(s)) if not os.path.exists(f"{RAW}/CFB_{T.iso(t).replace(':', '')}.json")) for s in PULL_SEASONS}
    print(f"snapshots still to pull: {need} = {10 * sum(need.values())} credits; cap {CAP}, pull {'on' if PULL else 'off'}")
    bets = [b for s in SEASONS for b in season_bets(s)]
    table(bets)
    print(f"\ncredits spent this run: {spent}")
