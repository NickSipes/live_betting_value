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
            out.append((k["n_recent"], abs(gap), (cover > 0) == (gap > 0), max(g["hr"], g["ar"]) <= 25, gap > 0, season,
                        {"line": sp[g["id"]], "recent": k["common_recent"], "common": k["common"], "model": k["model"],
                         "opp_rank": g["ar"] if gap > 0 else g["hr"]}))
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

def veto_table(bets):
    """Would a veto have caught the bad disagreements (Ole Miss +3.5 vs Georgia, 2026-11-07: implied Ole Miss -29.6,
    lost by 35)? The rule's bets (2+ shared, gap 4+) split by each candidate: record of the bets it would drop | keep."""
    rule = [b for b in bets if b[0] >= 2 and b[1] >= 4]
    rec = lambda v: f"{sum(b[2] for b in v)}-{len(v) - sum(b[2] for b in v)}"
    pct = lambda v: f" ({100 * sum(b[2] for b in v) / len(v):.0f}%)" if v else ""
    print(f"\nveto candidates, dropped | kept{'':<22}" + "".join(f"{s:>14}" for s in SEASONS) + f"{'all dropped':>18}{'all kept':>16}")
    def row(title, f):
        cells = []
        for s in SEASONS:
            v = [b for b in rule if b[5] == s]
            cells.append(f"{rec([b for b in v if f(b)])} | {rec([b for b in v if not f(b)])}")
        d, k = [b for b in rule if f(b)], [b for b in rule if not f(b)]
        print(f"  {title:<44}" + "".join(f"{c:>14}" for c in cells) + f"{rec(d) + pct(d):>18}{rec(k) + pct(k):>16}")
    X = lambda b: b[6]
    for th in (14, 20, 25):
        row(f"gap over {th}", lambda b, th=th: b[1] > th)
    for th in (10, 14, 20):
        row(f"implied {th}+ from the rating's spread", lambda b, th=th: abs(X(b)["recent"] - X(b)["model"]) >= th)
    row("rating leans the other way", lambda b: (X(b)["line"] - X(b)["model"] > 0) != b[4])
    for th in (7, 14):
        row(f"4-week filter moved implied {th}+", lambda b, th=th: abs(X(b)["recent"] - X(b)["common"]) >= th)
    row("all shared opponents lean the other way", lambda b: (X(b)["line"] - X(b)["common"] > 0) != b[4])
    row("line's underdog made a 3+ favorite", lambda b: X(b)["line"] * X(b)["recent"] < 0 and abs(X(b)["recent"]) >= 3 and ((X(b)["line"] > 0) == b[4]))
    row("bet on the line's underdog", lambda b: (X(b)["line"] > 0) == b[4])
    row("bet against a top-5 team", lambda b: X(b)["opp_rank"] <= 5)
    row("bet against a top-10 team", lambda b: X(b)["opp_rank"] <= 10)
    row("gap over 14 and implied 14+ from rating", lambda b: b[1] > 14 and abs(X(b)["recent"] - X(b)["model"]) >= 14)
    # Swiss cheese: stack the layers. Each was picked for looking bad on all five seasons, so the honest reading is
    # the split: layers chosen on 2021-23 (top-5, gap over 20, implied 14+ from rating, away bets) graded on 2024-25
    L = {"top5": lambda b: X(b)["opp_rank"] <= 5, "gap20": lambda b: b[1] > 20,
         "rating14": lambda b: abs(X(b)["recent"] - X(b)["model"]) >= 14, "away": lambda b: not b[4]}
    row("stack: top-5 or gap over 20", lambda b: L["top5"](b) or L["gap20"](b))
    row("stack: top-5, gap 20, implied 14+ rating", lambda b: L["top5"](b) or L["gap20"](b) or L["rating14"](b))
    row("stack: all three plus away bets", lambda b: any(f(b) for f in L.values()))
    print("\n  layers judged on 2021-23, then graded on 2024-25 (dropped | kept):")
    for name, f in list(L.items()) + [("all four stacked", lambda b: any(g(b) for g in L.values()))]:
        tr, te = [b for b in rule if b[5] <= 2023], [b for b in rule if b[5] >= 2024]
        d = lambda v: [b for b in v if f(b)]; k = lambda v: [b for b in v if not f(b)]
        print(f"    {name:<20} 2021-23 {rec(d(tr)):>7}{pct(d(tr)):>6} | {rec(k(tr)):>7}{pct(k(tr)):>6}     2024-25 {rec(d(te)):>7}{pct(d(te)):>6} | {rec(k(te)):>7}{pct(k(te)):>6}")

def spots_games(season):
    """cfb_spots-shaped games for one season: every game with a line, each team's previous game by kickoff time."""
    games, meta, sp = board(season), T.cfb_meta(season), lines(board(season))
    ranks = {g["id"]: (g["hr"], g["ar"]) for g in games}
    by_team = collections.defaultdict(list)
    for g in sorted(games, key=lambda g: g["t"]):
        by_team[g["h"]].append((g["t"], g["hs"] - g["as"], g["id"], g["wk"]))
        by_team[g["a"]].append((g["t"], g["as"] - g["hs"], g["id"], g["wk"]))
    def last(team, t):
        prev = [x for x in by_team[team] if x[0] < t]
        return prev[-1][1:] if prev else None
    out = [{"id": g["id"], "wk": g["wk"], "h": g["h"], "a": g["a"], "m": g["hs"] - g["as"], "sp": sp[g["id"]], "neutral": g["neutral"],
            "conf": meta.get(g["id"], [False])[0], "rk": ranks[g["id"]], "hl": last(g["h"], g["t"]), "al": last(g["a"], g["t"])}
           for g in games if g["id"] in sp and g["id"] in meta and g["wk"] >= 2]
    return [(g, ranks) for g in out]

def spots_table():
    """The cfb_spots situational list (the watch tag is 'off a ranked-game loss, dog') and the rating-vs-line check, by season."""
    per = {s: spots_games(s) for s in SEASONS}
    print("\n" + " " * 36 + "".join(f"{s:>12}" for s in SEASONS) + f"{'all':>14}")
    def row(nm, f):
        tot = [0, 0]; cells = []
        for s in SEASONS:
            w, l = f(per[s]); tot[0] += w; tot[1] += l
            cells.append(f"{w}-{l}")
        n = sum(tot)
        print(f"  {nm:<34}" + "".join(f"{c:>12}" for c in cells) + f"{tot[0]}-{tot[1]} {100 * tot[0] / max(1, n):3.0f}% z{T.z(*tot):+.1f}")
    print("pregame spots, against the spread (need 52.4%)")
    for nm, keys in T.SPOTS:
        row(nm, lambda gr, keys=keys: T.ats(gr, keys))
    print("rating vs line: bet the side the ridge rating likes by the edge or more (ranked games)")
    def rating(gr, th):
        season = int(gr[0][0]["id"] in {} or 0) if False else None
        return None
    for th in (4, 6, 8):
        def f(gr, th=th):
            gs = [g for g, _ in gr]
            season = next(s for s in SEASONS if per[s] is gr)
            tup, hist, pri = tuples(board(season)), tuples(board(season - 1)), prior(season)
            w = l = 0
            for wk in sorted({g["wk"] for g in gs if g["wk"] >= 5}):
                rt, hfa = T.fit([x[1:] for x in tup if x[0] < wk], pri, T.FCS, None)
                for g in gs:
                    if g["wk"] != wk or min(g["rk"]) > 25:
                        continue
                    e = g["sp"] - T.look(tup, hist, wk, g["h"], g["a"], g["neutral"], rt, hfa, {}, {}, {})["model"]
                    c = np.sign(g["m"] + g["sp"])
                    if abs(e) >= th and c:
                        w += (c > 0) == (e > 0); l += (c > 0) != (e > 0)
            return w, l
        row(f"rating edge >= {th}", f)

if __name__ == "__main__":
    if "--spots" in ARGS:
        spots_table(); sys.exit()
    need = {s: sum(1 for t in stamps(board(s)) if not os.path.exists(f"{RAW}/CFB_{T.iso(t).replace(':', '')}.json")) for s in PULL_SEASONS}
    print(f"snapshots still to pull: {need} = {10 * sum(need.values())} credits; cap {CAP}, pull {'on' if PULL else 'off'}")
    bets = [b for s in SEASONS for b in season_bets(s)]
    table(bets)
    veto_table(bets)
    print(f"\ncredits spent this run: {spent}")
