#!/usr/bin/env python3
"""Top vs top with an 11+ lead: is the trailing team's live + spread worth taking?

Flag, knowable live: both teams top third by record going in, and the first score that puts either
team up 11+ in regulation. Bet: the trailing team at the first live quote that is fresher than that
score, on the first bulk historical snapshot 60s+ after it, within 15 min, lead still 11+. Settled on
the full final with OT at the quoted price, FanDuel first, DraftKings fallback (the project's rule).
Comparison: non-flagged rated games with an 11+ lead that happen to be on the same snapshots.
2026 is the season the pattern was found in; 2024-25 NFL and 2025 CFB are the fresh test, rules unchanged.

Run: .venv/bin/python analysis/lead_spread_value.py [--pull]   (--pull spends credits, capped; cached snapshots are never re-bought)"""
import calendar, csv, datetime, glob, sys, time
from concurrent.futures import ThreadPoolExecutor
from team_shapes import *
from cfb_spread_model import gc_of

KEY = open(f"{ROOT}/.env").read().split("ODDS_API_KEY=")[1].split()[0]
ODDS = "https://api.the-odds-api.com/v4/historical/sports/{}/odds"
SPORT = {"NFL": "americanfootball_nfl", "CFB": "americanfootball_ncaaf"}
RAW = f"{ROOT}/data/hist_spreads"
LEAD, MIN_WAIT, MAX_WAIT, STALE = 11, 60, 900, 360
CREDIT_CAP = 170   # per run; approved ~150 to finish the NFL
NFL_NAMES = dict(zip("ARI ATL BAL BUF CAR CHI CIN CLE DAL DEN DET GB HOU IND JAX KC LV LAC LA MIA MIN NE NO NYG NYJ PHI PIT SF SEA TB TEN WAS".split(),
    "Arizona Cardinals|Atlanta Falcons|Baltimore Ravens|Buffalo Bills|Carolina Panthers|Chicago Bears|Cincinnati Bengals|Cleveland Browns|Dallas Cowboys|Denver Broncos|Detroit Lions|Green Bay Packers|Houston Texans|Indianapolis Colts|Jacksonville Jaguars|Kansas City Chiefs|Las Vegas Raiders|Los Angeles Chargers|Los Angeles Rams|Miami Dolphins|Minnesota Vikings|New England Patriots|New Orleans Saints|New York Giants|New York Jets|Philadelphia Eagles|Pittsburgh Steelers|San Francisco 49ers|Seattle Seahawks|Tampa Bay Buccaneers|Tennessee Titans|Washington Commanders".split("|")))

def ts(s):
    return calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))

def nfl_timelines(season):
    """{game_id: [[wall, gc, home, away]]} at each score change, regulation."""
    out = collections.defaultdict(list)
    wall = {}
    with gzip.open(f"{ROOT}/data/pbp_{season}.csv.gz", "rt") as fh:
        for r in csv.DictReader(fh):
            if r["time_of_day"]:
                wall[r["game_id"]] = ts(r["time_of_day"])
            if r["qtr"] in "1234" and r["total_home_score"] and r["game_seconds_remaining"]:
                h, a, l = int(float(r["total_home_score"])), int(float(r["total_away_score"])), out[r["game_id"]]
                if (not l and h + a) or (l and (h, a) != (l[-1][2], l[-1][3])):
                    l.append([wall.get(r["game_id"], 0), 3600 - float(r["game_seconds_remaining"]), h, a])
    return out

def espn_wall(eid):
    u = f"https://site.api.espn.com/apis/site/v2/sports/football/college-football/summary?event={eid}"
    s = json.load(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=30))
    c = {x["homeAway"]: x["team"]["displayName"] for x in s["header"]["competitions"][0]["competitors"]}
    pc = s.get("pickcenter") or []
    wall = {p["id"]: p.get("wallclock") for d in s["drives"]["previous"] for p in d.get("plays", [])}
    tl, last = [], 0
    for p in s.get("scoringPlays", []):   # play-level scores in the drive feed run ahead and back; this list doesn't
        last = ts(wall[p["id"]]) if wall.get(p["id"]) else last
        if p["period"]["number"] <= 4:
            tl.append([last, gc_of(p), p["homeScore"], p["awayScore"]])
    return {"home": c["home"], "away": c["away"], "tl": tl, "sp": pc[0].get("spread") if pc else None}

def try_wall(eid):
    for _ in range(3):
        try:
            return espn_wall(eid)
        except Exception:
            time.sleep(1)

def cfb_timelines(ids, season):
    path_ = f"{ROOT}/data/cfb_wall_{season}.json"
    wall = json.load(open(path_)) if os.path.exists(path_) else {}
    miss = [i for i in ids if i not in wall]
    if miss:
        with ThreadPoolExecutor(12) as ex:
            wall.update(zip(miss, ex.map(try_wall, miss)))
        json.dump(wall, open(path_, "w"))
    return wall

def cfb_ranks(season):
    """{event id: (home AP rank, away AP rank)} at game time, 99 = unranked."""
    p = f"{ROOT}/data/cfb_ranks_{season}.json"
    if os.path.exists(p):
        return json.load(open(p))
    out = {}
    for wk in range(1, 17):
        u = f"{ESPN}?groups=80&limit=300&seasontype=2&week={wk}&dates={season}"
        sb = json.load(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=20))
        for e in sb.get("events", []):
            c = {x["homeAway"]: (x.get("curatedRank") or {}).get("current") or 99 for x in e["competitions"][0]["competitors"]}
            out[e["id"]] = (c["home"], c["away"])
    json.dump(out, open(p, "w"))
    return out

def trigger(tl):
    """First score that puts a team up LEAD+: (wall, gc, leader sign, h, a), or None."""
    for w, gc, h, a in tl:
        if abs(h - a) >= LEAD:
            return w, gc, 1 if h > a else -1, h, a

def margin_at(tl, t):
    return next((h - a for w, _, h, a in reversed(tl) if w <= t), 0)

def season_games(lg, season):
    """Rated games with an 11+ lead."""
    res = nfl_results(season) if lg == "NFL" else cfb_results(season)
    by = {r[1]: r for r in res}
    if lg == "NFL":
        gs = [g for g in nfl(season) if g["ev"] and not g["post"]]
        tln = nfl_timelines(season)
    elif season == 2026:
        gs = [g for g in cfb() if g["ev"]]
        wall = cfb_timelines([g["id"] for g in gs], season)
    else:
        wall = cfb_timelines([r[1] for r in res], season)
        gs = [{"id": i, "lg": "CFB", "season": season, "week": by[i][0], "post": False, "sp": None if w.get("sp") is None else float(w["sp"]), "ev": []}
              for i, w in wall.items() if w and by.get(i)]
    out, ranks = [], cfb_ranks(season) if lg == "CFB" else {}
    for g in tag(gs, res, ratings(res)):
        r = by[g["id"]]
        g["rk"] = ranks.get(g["id"], (99, 99))
        g.update(dict(home=NFL_NAMES[r[2]], away=NFL_NAMES[r[3]], tl=tln[g["id"]]) if lg == "NFL" else
                 {k: wall[g["id"]][k] for k in ("home", "away", "tl")})
        g.update(habbr=r[2], aabbr=r[3], fh=r[4], fa=r[5], trig=trigger(g["tl"]),
                 top=tier(g["h"][0]) == "top" and tier(g["a"][0]) == "top")
        if g["trig"] and g["trig"][0]:
            out.append(g)
    return out

spent = 0
def snapshot(lg, stamp):
    """Cached bulk snapshot at or before stamp; a credit is only ever spent once per stamp."""
    global spent
    p = f"{RAW}/{lg}_{stamp.replace(':', '')}.json"
    if os.path.exists(p):
        return json.load(open(p))
    if "--pull" not in sys.argv:
        return None
    assert spent + 10 <= CREDIT_CAP, f"credit cap hit at {spent}"
    u = ODDS.format(SPORT[lg]) + f"?apiKey={KEY}&date={stamp}&bookmakers=fanduel,draftkings&markets=spreads&oddsFormat=american"
    r = urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "curl/8.4"}), timeout=30)
    d = json.load(r)
    spent += int(r.headers.get("x-requests-last") or 10)
    json.dump(d, open(p, "w"))
    time.sleep(.1)
    return d

def iso(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def norm(s):
    return "".join(c for c in s.lower() if c.isalnum())

def quote(snap, g, side, after):
    """{book: (points, price)} for side's team (+1 home, -1 away), quotes fresher than `after`."""
    s_t, out = ts(snap["timestamp"]), {}
    name = norm(g["home"] if side > 0 else g["away"])
    for e in snap.get("data", []):
        if {norm(e["home_team"]), norm(e["away_team"])} != {norm(g["home"]), norm(g["away"])}:
            continue
        for b in e.get("bookmakers", []):
            for m in b.get("markets", []):
                upd = ts(m.get("last_update") or b["last_update"])
                if m["key"] == "spreads" and upd >= after and s_t - upd <= STALE:
                    out.update({b["key"]: (o["point"], o["price"]) for o in m["outcomes"] if norm(o["name"]) == name})
    return out

def entry(g, snaps):
    """First usable quote for the trailing team, for a game and its snapshots in time order."""
    t0, _, lead, *_ = g["trig"]
    for sn in snaps:
        s_t = ts(sn["timestamp"])
        if s_t < t0 + MIN_WAIT or s_t > t0 + MAX_WAIT:
            continue
        m = margin_at(g["tl"], s_t)
        if lead * m < LEAD:
            return ("lead gone", s_t, m)
        q = quote(sn, g, -lead, t0)
        if q:
            return ("bet", s_t, m, q)
    return None

def cached():
    out = collections.defaultdict(list)
    for f in sorted(glob.glob(f"{RAW}/*.json")):
        out[os.path.basename(f)[:3]].append(json.load(open(f)))
    for v in out.values():
        v.sort(key=lambda d: d["timestamp"])
    return out

def near(snaps, t0):
    return [d for d in snaps if t0 < ts(d["timestamp"]) <= t0 + MAX_WAIT]

def pull(flagged, have):
    """Reuses any snapshot already held: nearby games share one bulk call."""
    for g in flagged:
        t0 = g["trig"][0]
        if entry(g, near(have[g["lg"]], t0)):
            continue
        stamp = iso(t0 + 300)
        for _ in range(4):
            d = snapshot(g["lg"], stamp)
            if not d:
                break
            have[g["lg"]].append(d)
            have[g["lg"]].sort(key=lambda x: x["timestamp"])
            if entry(g, near(have[g["lg"]], t0)) or not d.get("next_timestamp"):
                break
            stamp = d["next_timestamp"]
    print(f"credits spent so far this run: {spent}")

def find_event(snap, g):
    want = {norm(g["home"]), norm(g["away"])}
    return next((e for e in snap.get("data", []) if {norm(e["home_team"]), norm(e["away_team"])} == want), None)

def kick_of(g, snaps):
    t0 = g["trig"][0]
    return next((ts(e["commence_time"]) for d in near(snaps, t0)[:2] if (e := find_event(d, g))), None)

def pull_pregame(fl, have):
    """ESPN no longer carries 2025 CFB spreads. Morning-of bulk snapshot, one call per slate."""
    stamps = set()
    for g in fl:
        if g["sp"] is None and (k := kick_of(g, have["CFB"])):
            noon = k - k % 86400 + 15 * 3600 + 55 * 60          # 15:55 UTC that day, before the first Saturday window
            stamps.add(iso(noon if noon < k - 600 else k - 600))
    print("pregame slates:", len(stamps))
    for st in sorted(stamps):
        d = snapshot("CFB", st)
        if d:
            have["CFB"].append(d)
    have["CFB"].sort(key=lambda x: x["timestamp"])

def fill_spreads(games, have):
    """Last quote before kickoff, FanDuel first, for games with no ESPN spread."""
    for g in games:
        if g["sp"] is None and (k := kick_of(g, have["CFB"])):
            for d in reversed(have["CFB"]):
                e = find_event(d, g)
                if ts(d["timestamp"]) < k and e:
                    q = {b["key"]: o["point"] for b in e.get("bookmakers", []) for m in b["markets"] for o in m["outcomes"] if norm(o["name"]) == norm(g["home"])}
                    if q:
                        g["sp"] = q.get("fanduel", q.get("draftkings"))
                        break

def profit(margin, pts, price):
    """Trailing team +pts; margin = leader's final margin. 1 unit risked."""
    return 0.0 if margin == pts else -1.0 if margin > pts else (price / 100 if price > 0 else 100 / -price)

def implied(price):
    return 100 / (price + 100) if price > 0 else -price / (100 - price)

def bet_row(g, e):
    """Entry -> one row. 'rule' = FanDuel first, 'best' = more points across the two books, then price."""
    t0, gc, lead, *_ = g["trig"]
    q, s_t, m = e[3], e[1], e[2]
    fin = lead * (g["fh"] - g["fa"])
    rem = 1 - gc / 3600
    k = .28 if g["lg"] == "NFL" else .31
    fair = lead * m * (1 - k * rem) + lead * -g["sp"] * rem        # deployed fair line, leader's margin, no possession term
    rule, best = q.get("fanduel") or q.get("draftkings"), max(q.values(), key=lambda v: (v[0], -implied(v[1])))
    return {"id": g["id"], "trailing_home": lead < 0, "lg": g["lg"], "season": g["season"], "wk": g["week"], "game": f"{g['aabbr']} @ {g['habbr']}", "top": g["top"], "gc": gc, "lead": lead * m,
            "pts": rule[0], "price": rule[1], "bpts": best[0], "bprice": best[1], "fin": fin, "fair": fair,
            "fav": (lead * g["sp"] > 0), "sp": g["sp"], "wait": s_t - t0, "lrk": g["rk"][0 if lead > 0 else 1], "trk": g["rk"][1 if lead > 0 else 0],
            "pnl": profit(fin, *rule), "bpnl": profit(fin, *best), "be": implied(rule[1]), "bbe": implied(best[1])}

def wilson(w, n, z=1.96):
    p, d = w / n, 1 + z * z / n
    c, h = (p + z * z / (2 * n)) / d, z * (p * (1 - p) / n + z * z / (4 * n * n)) ** .5 / d
    return c - h, c + h

def summary(nm, rs, rng):
    if not rs:
        print(f"  {nm:34s} n=0")
        return
    pnl, bp = np.array([r["pnl"] for r in rs]), np.array([r["bpnl"] for r in rs])
    w, l, p = (pnl > 0).sum(), (pnl < 0).sum(), (pnl == 0).sum()
    lo, hi = wilson(w, max(w + l, 1)) if w + l else (0, 0)
    be = np.mean([r["be"] for r in rs if r["pnl"] != 0] or [0])
    ci = np.percentile([rng.choice(pnl, len(pnl)).mean() for _ in range(4000)], [2.5, 97.5])
    ci2 = np.percentile([rng.choice(bp, len(bp)).mean() for _ in range(4000)], [2.5, 97.5])
    print(f"  {nm:34s} n={len(rs):3d} {w}-{l}-{p} cover {w / max(w + l, 1):4.0%} [{lo:.0%}, {hi:.0%}] need {be:4.0%} | ROI {pnl.mean():+.1%} [{ci[0]:+.0%}, {ci[1]:+.0%}]"
          f" | best book {bp.mean():+.1%} [{ci2[0]:+.0%}, {ci2[1]:+.0%}] | line-fair {np.mean([r['pts'] - r['fair'] for r in rs]):+.1f}")

def report(games, snaps, rng):
    rows_, drop = [], collections.Counter()
    for g in games:
        t0 = g["trig"][0]
        by = near(snaps[g["lg"]], t0)
        e = entry(g, by) if by else None
        if e and e[0] == "bet" and g["sp"] is None:
            drop["no pregame spread"] += g["top"]
        elif e and e[0] == "bet":
            rows_.append(bet_row(g, e))
        elif g["top"]:
            drop[e[0] if e else "no quote"] += 1
            print(f"  no bet: {g['season']} wk{g['week']} {g['aabbr']} @ {g['habbr']} ({e[0] if e else 'no live quote'}), game min {g['trig'][1] / 60:.0f}")
    print("flagged without a bet:", dict(drop))
    json.dump(rows_, open(f"{ROOT}/data/lead_spread_bets.json", "w"))
    for lg, samples in (("NFL", (("found in 2026", (2026,)), ("FRESH 2024-25", (2024, 2025)))), ("CFB", (("found in 2026", (2026,)), ("FRESH 2025", (2025,))))):
        for tag_, yrs in samples:
            section(f"{lg} {tag_}: trailing team at the live line, first 11+ lead")
            rs = [r for r in rows_ if r["lg"] == lg and r["season"] in yrs]
            fl, ctl = [r for r in rs if r["top"]], [r for r in rs if not r["top"]]
            summary("flagged top vs top", fl, rng)
            summary("comparison: other 11+ leads", ctl, rng)
            if fl:
                print(f"  flagged ending within 8 of the leader: {np.mean([r['fin'] <= 8 for r in fl]):.0%}; comparison: {np.mean([r['fin'] <= 8 for r in ctl] or [0]):.0%}")
            for nm, f in (("first half", lambda r: r["gc"] < 1800), ("second half", lambda r: r["gc"] >= 1800),
                          ("trailing team was the pregame favorite", lambda r: r["fav"]), ("trailing team was the pregame dog", lambda r: not r["fav"]),
                          ("line above fair by 2+", lambda r: r["pts"] - r["fair"] >= 2), ("line below fair", lambda r: r["pts"] - r["fair"] < 0)):
                summary("flagged, " + nm, [r for r in fl if f(r)], rng)
                summary("other, " + nm, [r for r in ctl if f(r)], rng)
            if "FRESH" in tag_:
                print("  flagged bets:")
                for r in sorted(fl, key=lambda r: (r["season"], r["wk"], r["game"])):
                    print(f"    {r['season']} wk{r['wk']} {r['game']:14s} min {r['gc'] / 60:4.0f} lead {r['lead']:3.0f} +{r['pts']:<5} {r['price']:+5d} fair {r['fair']:5.1f} final by {r['fin']:+4d} -> {r['pnl']:+.2f} (best +{r['bpts']} {r['bprice']:+d})")

def ranked_report(rows_, rng):
    """Post-hoc filter on college, so every cut is shown by season: it has to work in both years."""
    is_r = lambda x: x <= 25
    cuts = (("at least one ranked", lambda r: is_r(r["lrk"]) or is_r(r["trk"])), ("both ranked", lambda r: is_r(r["lrk"]) and is_r(r["trk"])),
            ("neither ranked", lambda r: not is_r(r["lrk"]) and not is_r(r["trk"])), ("only the leader ranked", lambda r: is_r(r["lrk"]) and not is_r(r["trk"])),
            ("only the trailing team ranked", lambda r: is_r(r["trk"]) and not is_r(r["lrk"])),
            ("at least one ranked, trailing pregame dog", lambda r: (is_r(r["lrk"]) or is_r(r["trk"])) and not r["fav"]))
    for nm, f in cuts:
        section(f"CFB flagged: {nm}")
        for lab, yrs in (("2025", (2025,)), ("2026", (2026,)), ("both", (2025, 2026))):
            summary(lab, [r for r in rows_ if r["lg"] == "CFB" and r["top"] and r["season"] in yrs and f(r)], rng)
    section("CFB both ranked, any record (non-flagged games only where a snapshot already existed)")
    for lab, yrs in (("2025", (2025,)), ("2026", (2026,))):
        summary(lab, [r for r in rows_ if r["lg"] == "CFB" and r["season"] in yrs and is_r(r["lrk"]) and is_r(r["trk"])], rng)

def main():
    assert profit(7, 7.5, -110) == 100 / 110 and profit(8, 7.5, -110) == -1 and profit(7, 7, -110) == 0 and profit(3, 3.5, 120) == 1.2
    assert trigger([[1, 60, 7, 0], [2, 120, 14, 3], [3, 180, 14, 10]])[:3] == (2, 120, 1) and margin_at([[5, 0, 7, 0], [9, 0, 7, 7]], 8) == 7
    games = [g for lg, y in (("NFL", 2026), ("CFB", 2026), ("NFL", 2024), ("NFL", 2025), ("CFB", 2025)) for g in season_games(lg, y)]
    fl = [g for g in games if g["top"]]
    print("flagged:", sorted(collections.Counter((g["lg"], g["season"]) for g in fl).items()), "missing wall", sum(not g["trig"][0] for g in fl))
    os.makedirs(RAW, exist_ok=True)
    have = cached()
    if "--pull" in sys.argv:
        try:
            pull([g for g in fl if g["lg"] == "CFB" and g["season"] == 2025], have)
            pull_pregame(fl, have)
            pull([g for g in fl if g["lg"] == "NFL"], have)
        except AssertionError as e:
            print("STOPPED:", e)
        print(f"credits spent this run: {spent}")
    fill_spreads(games, have)
    report(games, have, np.random.default_rng(7))
    ranked_report(json.load(open(f"{ROOT}/data/lead_spread_bets.json")), np.random.default_rng(7))

if __name__ == "__main__":
    main()
