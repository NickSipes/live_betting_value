#!/usr/bin/env python3
"""Live prop monitor: polls the-odds-api (quotes) + ESPN (live stats), scores
every quoted player with the v2 model, writes web/state.json for the dashboard,
sends macOS notifications on NEAR/TRIGGER transitions, logs triggers.

Usage:  python3 poller.py [--poll 300] [--demo]
        --demo: one cycle of synthetic data (no API calls), for testing the UI.

Needs live/model_weights.json + live/priors.json (run analysis/export_live.py).
Costs 2 credits per live game per poll (2 markets, DK+FD).
"""
import json, os, sys, time, csv, math, bisect, datetime, subprocess, threading, re, zoneinfo
import urllib.request, functools
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import push, layers

HERE = os.path.dirname(os.path.abspath(__file__))
KEY = os.environ.get("ODDS_API_KEY") or \
    open(os.path.join(HERE, "..", ".env")).read().split("ODDS_API_KEY=")[1].strip()
ODDS = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl"
HIST_ODDS = "https://api.the-odds-api.com/v4/historical/sports/americanfootball_nfl"
BACKFILL_MAX = 40   # snapshots per game (20 credits each: 2 markets x DK+FD)
ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
MARKETS = {"rec": "player_reception_yds", "rush": "player_rush_yds"}
SIGNAL = 0.50              # first time a prop is worth showing (Past scorecard "flag")
NEAR, TRIG = 0.55, 0.60    # card status + notifications (calibrated P; -110 breakeven is 0.524)
WL_GAP, WL_NEAR_GAP = 3595, 55   # seconds between pregame line checks, then in a game's last WL_NEAR seconds
WL_NEAR = 3600
PRE_PUSH = 1200   # pregame picks alert 20 min before kickoff (the pick is still graded at the closing line)
CALL = TRIG                # a "call": if the player then clears that line, it's a win
SCORE_WINDOW = (600, 3060)  # game-clock seconds the v2 model was trained on (10:00-51:00 elapsed);
                            # outside it the inputs (pace, team rates) are unstable and P isn't trustworthy

ALIAS = {"LA": "LAR", "WAS": "WSH"}   # nflverse team codes that ESPN spells differently

def watchlist():
    try:
        return json.load(open(f"{DOCS}/watchlist.json")).get("entries", [])
    except Exception:
        return []

def in_window(gc):
    return gc is not None and SCORE_WINDOW[0] <= gc <= SCORE_WINDOW[1]
PORT = int(os.environ.get("PORT", 8766))
HOST = os.environ.get("HOST", "127.0.0.1")
DOCS = os.environ.get("DOCS_DIR", f"{HERE}/../docs")   # served site + published state
DATA = os.environ.get("DATA_DIR", HERE)                # private state (history, logs)

W = json.load(open(f"{HERE}/model_weights.json"))
PRIORS = json.load(open(f"{HERE}/priors.json"))     # name -> {kind:{ypg,ype,epg,pos}}
DEF = json.load(open(f"{HERE}/def_priors.json"))    # "kind|team|pos" -> ypg allowed

def _top3_def():
    """'kind|team' for the 3 stingiest defenses per market by last-3-games form
    (live/def_form.json, analysis/export_live.py). Season-to-date fallback."""
    try:
        d = json.load(open(f"{HERE}/def_form.json"))
        return {f"{kind}|{t}" for kind in ("rush", "rec")
                for t in sorted(d[kind], key=d[kind].get)[:3]}
    except Exception:
        by = {}
        for k, v in DEF.items():
            kind, team, _pos = k.split("|")
            by[(kind, team)] = by.get((kind, team), 0.0) + v
        return {f"{kind}|{t}" for kind in ("rush", "rec")
                for _, t in sorted((v, t) for (kd, t), v in by.items() if kd == kind)[:3]}
TOP3_DEF = _top3_def()   # never call an over into a top-3 form defense
try:  # isotonic P calibration (analysis/fit_calib.py); delete the file to run raw
    CAL = json.load(open(f"{HERE}/calib.json"))
except Exception:
    CAL = None

def calibrate(p, kind):
    c = CAL.get(kind) if CAL else None
    if not c:
        return p
    xs, ys = c["p"], c["cal"]
    i = bisect.bisect_right(xs, p) - 1
    if i >= len(xs) - 1:
        return ys[-1]
    return ys[i] + (ys[i + 1] - ys[i]) * (p - xs[i]) / (xs[i + 1] - xs[i])

def kick_in(w):
    """Seconds to a watchlist entry's kickoff; an entry without one counts as far off."""
    return datetime.datetime.fromisoformat(w["kick_iso"]).timestamp() - time.time() if w.get("kick_iso") else float("inf")

def first_over(bk):
    """(book, line, price): FanDuel first, as the quoted line. bk: {book: [line, over, under]}."""
    book = next((b for b in ("fanduel", "draftkings") if b in (bk or {})), None)
    return (book, bk[book][0], bk[book][1]) if book else None

def bet_open(call, bk):
    """Is the called book still at or better than the alert's good-to line? No quote: keep the last answer."""
    q = (bk or {}).get(call.get("book"))
    return q[0] <= call["line"] + 1 if q else call.get("open", True)

# No receiving over while the player's team leads by LEAD_VETO+ (from 2026 week 10). pbp 2020-26: rest-of-game receiving
# at up 14+ is 0.61x normal at half, 0.43x end Q3; receivers beat the book's lead-shaded line 27-45%, the worst bucket at
# every checkpoint. Replayed wk 6-9 calls: up 14+ 2-3, all else 13-3; held-out quotes up 14+ hit 44%. Up 8-13 showed
# nothing (3-0 replayed, 65% held-out), so it stays callable. Rushing is the mirror image (leaders run more): no veto.
LEAD_VETO = 14

def call_gate(x, dkey):
    """No call while down > 3 or (receiving) up LEAD_VETO+, and never an over against a top-3 defense
    (going into the week). A touch-count rule was dropped: on week 7 it only blocked winners."""
    return dkey.rsplit("|", 1)[0] not in TOP3_DEF and x[9] >= -3 and (x[15] or x[9] < LEAD_VETO)

TOP_N_WL = {"WR": 20, "TE": 10, "RB": 15}   # watchlist: top-N player vs bottom-10 defense
NFLV = {v: k for k, v in ALIAS.items()}     # ESPN -> nflverse team codes (priors/DEF keys)
INJ_OUT = ("Out", "Injured Reserve", "Doubtful", "Suspension")   # no pregame call

def espn_injuries():
    """League-wide injury report: short player name -> status."""
    out = {}
    for t in get(f"{ESPN}/injuries").get("injuries", []):
        for i in t.get("injuries", []):
            nm = (i.get("athlete") or {}).get("displayName")
            if nm and i.get("status") and i["status"] != "Active":   # Active = playing, not news
                out[short_name(nm)] = i["status"]
    return out

def qb_status(week, cache_path):
    """ESPN team abbr -> (usual starter, this week's QB1, QB1's starts so far). A start = most attempts in a
    game (box scores cached per game in cache_path); QB1 is ESPN's depth chart. Free ESPN calls."""
    import collections as _c
    cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}
    for wk in range(1, week):
        for ev in get(f"{ESPN}/scoreboard?week={wk}&seasontype=2").get("events", []):
            if ev["id"] in cache or ev["status"]["type"]["state"] != "post":
                continue
            starters = {}
            try:
                box = get(f"{ESPN}/summary?event={ev['id']}").get("boxscore", {})
            except Exception as e:
                print("QB box score fetch failed", ev["id"], repr(e)[:80])
                continue   # try again next check
            for tp in box.get("players", []):
                for cat in tp.get("statistics", []):
                    if cat.get("name") == "passing":
                        i = [l.upper() for l in cat.get("labels", [])].index("C/ATT")
                        att = lambda a: int((a.get("stats") or ["0/0"] * (i + 1))[i].split("/")[-1] or 0)
                        best = max(cat.get("athletes", []), key=att, default=None)
                        if best:
                            starters[tp["team"]["abbreviation"]] = best["athlete"]["displayName"]
            cache[ev["id"]] = starters
            json.dump(cache, open(cache_path, "w"))   # each game once, even if a later fetch fails
    starts = _c.defaultdict(_c.Counter)
    for g in cache.values():
        for t, q in g.items():
            starts[t][q] += 1
    out = {}
    for t in get(f"{ESPN}/teams").get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", []):
        tm = t["team"]; usual = starts[tm["abbreviation"]].most_common(1)
        if not usual:
            continue
        qb1 = next((dc["positions"]["qb"]["athletes"][0]["displayName"]
                    for dc in get(f"{ESPN}/teams/{tm['id']}/depthcharts").get("depthchart", []) if dc.get("positions", {}).get("qb")), None)
        out[tm["abbreviation"]] = (usual[0][0], qb1, starts[tm["abbreviation"]][qb1] if qb1 else None)
    return out

QB_EXEMPT = {"NYG"}   # never treated as playing a backup (user call)
RB_UNDER = (20.5, 41.5)   # RB rushing under when the line is above the first and at most the second
RB_EXPL = 0.12   # no RB under for a team whose season 10+ yd run rate is above this (live/team_expl.json, export_live.py)
try:
    TEAM_EXPL = json.load(open(f"{HERE}/team_expl.json"))   # nflverse team code -> rate; missing file = no veto
except Exception:
    TEAM_EXPL = {}
WR_MATE = 60.0            # a teammate WR at this many rec yds/game is a top WR (analysis/wr_mate_out.py)

def wr_mate_out(name, inj, quoted=None):
    """The WR_MATE+ yds/game WR teammate of `name` who is out, or None. Out = Out/IR/Doubtful/Suspension on the
    injury report, or no receiving line quoted for him while `quoted` (short names with a rec line in the game)
    has 4+ names, i.e. the game's receiving market is up. Only for the team's best WR still playing: WR1 with a real WR2 out went 7-18 at the closing over,
    2024-26 (the book raises the line ~6 yds for volume that never comes); the remaining WR2 showed nothing."""
    me = PRIORS.get(name, {})
    out = None
    for n, v in sorted(PRIORS.items(), key=lambda kv: -kv[1].get("rec", {}).get("ypg", 0)):
        r = v.get("rec", {})
        if n == name or v.get("team") != me.get("team") or r.get("pos") != "WR":
            continue
        gone = inj.get(short_name(n)) in INJ_OUT or (quoted is not None and len(quoted) >= 4 and short_name(n) not in quoted)
        if gone:
            out = out or (n if r["ypg"] >= WR_MATE else None)
        elif r["ypg"] > me.get("rec", {}).get("ypg", 0):
            return None   # a better WR is still playing: `name` isn't the one left to cover
    return out

def scoring_ranks(week):
    """ESPN team abbr -> scoring rank (1 = most points per game) from regular-season games before `week`."""
    pts = {}
    for wk in range(1, week):
        for ev in get(f"{ESPN}/scoreboard?week={wk}&seasontype=2").get("events", []):
            if ev["status"]["type"]["state"] == "post":
                for c in ev["competitions"][0]["competitors"]:
                    pts.setdefault(c["team"]["abbreviation"], []).append(int(c["score"]))
    ppg = {t: sum(v) / len(v) for t, v in pts.items()}
    return {t: 1 + sum(x > p for x in ppg.values()) for t, p in ppg.items()}

def expl_veto(team):
    """True when the team's season 10+ yd run rate rules out the RB under. team: nflverse code."""
    return (TEAM_EXPL.get(team) or 0) > RB_EXPL

# RB Unders layers (live/layers.py). The model proposes an RB rushing under in the line band on a bottom-third scoring
# team with its usual QB; a layer that catches it keeps it from being bet and the ledger keeps what each one dropped
# (docs/layers_nfl_rb_unders.json; docs/rb_skips.json stays as the graded log for analysis). Add a veto as a row here.
RB_UNDER_LAYERS = [
    ("explosive", f"team's season 10+ yd run rate is above {RB_EXPL:.0%}", lambda p, ctx: expl_veto(p["team"])),
]

# Good Matchups: 3 picks a week (from 2026 week 10). Every eligible matchup gets a point each for home, favored, offense
# not bottom third in scoring, record not bottom third (win pct > .4) and last-3 form >= 0.8x season ypg; the top 3 by
# points, then worst defense, are bet, the rest go to the ledger caught by "top3". 2024-26 at closing prices: top 3 a
# week this way 60-45 (+15/+5/+1%), worst-defense-first 45-60, random 3 -8% (5-95% -20 to +7%). Seven rankings were
# tried, so it's a lead under forward test, not a proven edge.
GM_PICKS = 3

def gm_score(w, ranks):
    """Cuts points for a Good Matchups entry (0-5). ranks: ESPN abbr -> scoring rank (1 = best)."""
    home = w["team"] == w["game"].split(" @ ")[1]
    sp, pct = w.get("spread_home"), w.get("pct_home" if home else "pct_away")   # from game_info
    me = [x[1] for x in (w.get("form") or {}).get("me", []) if x[1] is not None]
    rk = ranks.get(ALIAS.get(w["team"], w["team"]))
    return int(home + (sp is not None and (sp < 0 if home else sp > 0)) + (rk is not None and rk <= len(ranks) * 2 / 3)
               + (pct is None or pct > 0.4) + (bool(me) and sum(me) / len(me) >= 0.8 * w["ypg"]))

def gm_eligible(w):
    return w.get("line") is not None and w.get("inj") not in INJ_OUT and not w.get("qb_out") and not w.get("mate_out")

def gm_top(entries, ranks, now_kick=lambda w: kick_in(w)):
    """Mark the week's GM_PICKS picks in place (w["top3"]); a pick whose game kicked off is locked, the open slots go to
    games still to come. Returns True when a mark changed."""
    locked = [w for w in entries if w.get("top3") and now_kick(w) <= 0]
    open_ = sorted((w for w in entries if now_kick(w) > 0 and gm_eligible(w)),
                   key=lambda w: (-gm_score(w, ranks), -(w.get("def_ypg") or 0)))
    pick = {id(w) for w in locked + open_[:max(0, GM_PICKS - len(locked))]}
    changed = False
    for w in entries:
        if now_kick(w) > 0 or not w.get("top3"):   # kicked-off marks stay as they were
            top = id(w) in pick
            if w.get("top3", False) != top or w.get("score") != gm_score(w, ranks):
                w["top3"], w["score"] = top, gm_score(w, ranks)
                changed = True
    return changed

GM_LAYERS = [("top3", f"not one of the week's {GM_PICKS} best Good Matchups by cuts points", lambda p, ctx: not p.get("top3"))]

def qb_backup(qbs, inj, team):
    """The QB a team is playing instead of its usual starter, or None. team: ESPN abbr. qbs: qb_status()."""
    if team in QB_EXEMPT or team not in qbs:
        return None
    usual, qb1, n = qbs[team]
    backup = (qb1 is not None and n < 2) or (inj.get(short_name(usual or "")) in INJ_OUT and qb1 in (None, usual))
    return (qb1 if qb1 and qb1 != usual else usual) if backup else None

def refresh_watchlist():
    """Advance docs/watchlist.json to the upcoming week once the current slate ends.
    Same picks as analysis/export_live.py used to make: top-N player (season ypg rank
    from priors) vs bottom-10 defense; the schedule comes from ESPN. Player ranks go
    stale between exports, which only shuffles the fringe of the top-N cutoffs."""
    try:
        cur = json.load(open(f"{DOCS}/watchlist.json"))
    except Exception:
        cur = {}
    cur_wk = cur.get("week")
    sb = get(f"{ESPN}/scoreboard")
    wk = (sb.get("week") or {}).get("number")
    if wk is None:
        return
    evs = sb.get("events", [])
    nxt = wk + 1 if evs and all(e["status"]["type"]["state"] == "post" for e in evs) else wk
    if cur_wk == nxt and all("home_full" in e for e in cur.get("entries", [])):
        return
    sched = {}
    for ev in get(f"{ESPN}/scoreboard?week={nxt}").get("events", []):
        comps = ev["competitions"][0]["competitors"]
        teams = {c["homeAway"]: NFLV.get(c["team"]["abbreviation"], c["team"]["abbreviation"])
                 for c in comps}
        home_full = next(c["team"]["displayName"] for c in comps if c["homeAway"] == "home")
        dt = datetime.datetime.fromisoformat(ev["date"].replace("Z", "+00:00")) \
            .astimezone(zoneinfo.ZoneInfo("America/New_York"))
        lbl, kick, iso = f'{teams["away"]} @ {teams["home"]}', dt.strftime("%a %H:%M ET"), dt.isoformat()
        sched[teams["home"]] = (teams["away"], lbl, kick, iso, home_full)
        sched[teams["away"]] = (teams["home"], lbl, kick, iso, home_full)
    if not sched:
        return
    ranks, bypos, dranks, dbypos = {}, {}, {}, {}
    for name, kinds in PRIORS.items():
        for kind, st in kinds.items():
            if isinstance(st, dict):
                bypos.setdefault((kind, st["pos"]), []).append((-st["ypg"], name))
    for (kind, pos), rows in bypos.items():
        for i, (_, name) in enumerate(sorted(rows)):
            ranks[(kind, pos, name)] = i + 1
    for k, v in DEF.items():
        kind, team, pos = k.split("|")
        dbypos.setdefault((kind, pos), []).append((-v, team))
    for kp, rows in dbypos.items():
        for i, (_, team) in enumerate(sorted(rows)):
            dranks[(*kp, team)] = i + 1   # 1 = most yards allowed (worst defense)
    watch = []
    for name, kinds in PRIORS.items():
        team = kinds.get("team")
        if team not in sched:
            continue
        opp, lbl, kick, kick_iso, home_full = sched[team]
        for kind, st in kinds.items():
            if not isinstance(st, dict):
                continue
            pos = st["pos"]
            if (ranks.get((kind, pos, name), 999) <= TOP_N_WL.get(pos, 0)
                    and dranks.get((kind, pos, opp), 999) <= 10):
                watch.append({"player": name, "pos": pos, "rank": ranks[(kind, pos, name)],
                              "ypg": st["ypg"], "market": kind, "team": team, "opp": opp,
                              "def_rank": dranks[(kind, pos, opp)],
                              "def_ypg": DEF.get(f"{kind}|{opp}|{pos}", 0), "game": lbl,
                              "kick": kick, "kick_iso": kick_iso, "home_full": home_full})
    watch.sort(key=lambda w: (w["kick_iso"], w["def_rank"], w["rank"]))
    json.dump({"week": nxt, "entries": watch}, open(f"{DOCS}/watchlist.json", "w"), indent=1)
    print(f"watchlist advanced to week {nxt}: {len(watch)} matchups")

def espn_box(eid, _cache={}):
    """Finished game -> {ESPN team abbr: {kind: [(name, yds), ...] best first}}.
    ponytail: process-lifetime cache; ~16 games a week, a few KB each."""
    if eid not in _cache:
        out = {}
        for tp in get(f"{ESPN}/summary?event={eid}").get("boxscore", {}).get("players", []):
            for cat in tp.get("statistics", []):
                kind = {"receiving": "rec", "rushing": "rush"}.get(cat.get("name"))
                labels = [l.upper() for l in cat.get("labels", [])]
                if kind and "YDS" in labels:
                    i = labels.index("YDS")
                    rows = [(a["athlete"]["displayName"], float(a["stats"][i]))
                            for a in cat.get("athletes", []) if len(a.get("stats", [])) > i]
                    out.setdefault(tp["team"]["abbreviation"], {})[kind] = sorted(rows, key=lambda r: -r[1])
        _cache[eid] = out
    return _cache[eid]

def last3(team):
    """A team's last 3 finished games, oldest first: [(label 'w7 @ BAL', espn_id, opp_abbr)]."""
    ab = ALIAS.get(team, team)
    evs = [e for e in get(f"{ESPN}/teams/{ab.lower()}/schedule").get("events", [])
           if e["competitions"][0]["status"]["type"]["state"] == "post"]
    out = []
    for e in evs[-3:]:
        side = {c["homeAway"]: c["team"]["abbreviation"] for c in e["competitions"][0]["competitors"]}
        home = side["home"] == ab
        opp = side["away" if home else "home"]
        out.append((f'w{e["week"]["number"]} {"vs" if home else "@"} {opp}', e["id"], opp))
    return out

def game_info(week):
    """ESPN scoreboard extras per game label ('TEN @ CIN'): venue, the kickoff forecast, the home spread (ESPN's
    book; negative = home favored) and each team's win pct (ties count half) for the Good Matchups points."""
    out = {}
    for ev in get(f"{ESPN}/scoreboard?week={week}").get("events", []):
        c = ev["competitions"][0]
        t = {x["homeAway"]: NFLV.get(x["team"]["abbreviation"], x["team"]["abbreviation"]) for x in c["competitors"]}
        v, wx, addr = c.get("venue") or {}, ev.get("weather") or {}, (c.get("venue") or {}).get("address") or {}
        pct = {}
        for x in c["competitors"]:
            wlt = [int(n) for n in ((x.get("records") or [{}])[0].get("summary") or "").split("-") if n.isdigit()]
            if len(wlt) >= 2 and sum(wlt):
                pct[x["homeAway"]] = round((wlt[0] + (wlt[2] / 2 if len(wlt) > 2 else 0)) / sum(wlt), 3)
        out[f'{t["away"]} @ {t["home"]}'] = {
            "venue": ", ".join(filter(None, [v.get("fullName"), addr.get("city"), addr.get("state")])) or None,
            "wx": "Indoors" if v.get("indoor") else
                  f'{wx["displayValue"]}, {wx["temperature"]}°F' if wx.get("displayValue") else None,
            "spread_home": ((c.get("odds") or [{}])[0]).get("spread"), "pct_home": pct.get("home"), "pct_away": pct.get("away")}
    return out

FORM_V = 2   # bump to rebuild cached forms; 2 added the player's closing line each week

def preview_form(w, pre_lines=None):
    """Good Matchups card data: the player's yards in the prop's market over their last 3
    games (null = not in the box score) with that game's closing line (null = not tracked),
    and for each of the defense's last 3 games the top-2 opposing players in that market."""
    kind, me_ab, who = w["market"], ALIAS.get(w["team"], w["team"]), short_name(w["player"])
    line = lambda eid: next((v for k, v in (pre_lines or {}).get(eid, {}).items()
                             if k.endswith("|" + kind) and short_name(k.rsplit("|", 1)[0]) == who), None)
    me = [[lbl, next((y for n, y in espn_box(eid).get(me_ab, {}).get(kind, []) if short_name(n) == who), None), line(eid)]
          for lbl, eid, _ in last3(w["team"])]
    vs = [[lbl, [[abbrev(n), y] for n, y in espn_box(eid).get(opp, {}).get(kind, [])[:2]]]
          for lbl, eid, opp in last3(w["opp"])]
    return {"me": me, "vs": vs, "v": FORM_V}

def get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8.4"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)

def notify(title, msg):
    try:
        subprocess.run(["osascript", "-e",
                        f'display notification "{msg}" with title "{title}" sound name "Glass"'],
                       capture_output=True, timeout=5)
    except Exception:
        pass

def clock_to_elapsed(period, display):
    m = re.match(r"(\d+):(\d+)", display or "")
    rem = int(m.group(1)) * 60 + int(m.group(2)) if m else 0
    if period <= 4:
        return (period - 1) * 900 + (900 - rem)
    return 3600 + (600 - rem)

def iso_ts(s):
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()

def abbrev(full):
    """'Brandon Lance Jr.' -> 'B.Lance'."""
    parts = [w for w in full.replace(".", " ").split() if w.lower() not in ("jr", "sr", "ii", "iii", "iv", "v")]
    return f"{parts[0][0]}.{parts[-1]}" if len(parts) >= 2 else full

def short_name(full):
    """'Brandon Lance Jr.' -> 'b.lance' (ESPN play text uses 'B.Lance'), for matching."""
    return abbrev(full).lower()

KNOWN_AS = {"Bam Knight": "Zavier Knight"}   # ponytail: play logs use legal names; add as they surface

def same_player(play, full):
    """Play-log name vs full name: 'bi.robinson' is Bijan Robinson, 'a.brown' Amon-Ra St. Brown."""
    full = KNOWN_AS.get(full, full)
    first, _, last = play.partition(".")
    return last == abbrev(full).lower().split(".")[-1] and full.lower().startswith(first)

def play_name(text):
    """'A.St. Brown' -> 'a.brown', 'Bi.Robinson' -> 'bi.robinson'."""
    parts = text.replace(".", " ").split()
    return f"{parts[0]}.{parts[-1]}".lower()

PLAYER = r"[A-Z][\w'\-]*\.\s?(?:(?:St\.|Van|Von|Vander|De|Del|La|Le)\s)?[A-Z][\w'\-]+"
PASS_TO = re.compile(rf"pass (?:incomplete )?(?:short |deep )?(?:left |right |middle )?to ({PLAYER})")
ELIGIBLE = re.compile(r"[A-Z][\w.'\-]+ reported in as eligible\.\s*")
GAINED = re.compile(r"for (-?\d+) yards?|for (no gain)")
RUSHER = re.compile(rf"^(?:\([^)]*\)\s*)*({PLAYER})\s")

def espn_timeline(espn_id):
    """Player plays: (wall_ts, gc, kind, short_name, yards, counts_yards) per target/carry.
    Team plays: (wall_ts, gc, offense, 'pass'|'rush', yards, completed, home_score, away_score)."""
    s = get(f"{ESPN}/summary?event={espn_id}")
    out, team = [], []
    drives = s.get("drives", {})
    for d in drives.get("previous", []) + ([drives["current"]] if drives.get("current") else []):
        offense = d.get("team", {}).get("displayName")
        for pl in d.get("plays", []):
            text, t = pl.get("text", ""), pl.get("type", {}).get("text", "")
            if not pl.get("wallclock") or "no play" in text.lower():
                continue
            gc = clock_to_elapsed(pl.get("period", {}).get("number", 1), pl.get("clock", {}).get("displayValue"))
            text = ELIGIBLE.sub("", text)
            # yards from the text: statYardage folds in penalty yards, the box score doesn't
            g = GAINED.search(text)
            yds = 0 if not g or g.group(2) else int(g.group(1))
            if "Sack" not in t and ("Pass" in t or "Interception" in t or t.startswith("Rush") or t == "Rushing Touchdown"):
                kind = "rush" if t.startswith("Rush") or t == "Rushing Touchdown" else "pass"
                team.append((iso_ts(pl["wallclock"]), gc, offense, kind, yds,
                             kind == "rush" or t == "Pass Reception" or t == "Passing Touchdown",
                             pl.get("homeScore", 0), pl.get("awayScore", 0)))
            if "Pass" in t or "Interception" in t:
                m = PASS_TO.search(text)
                if m:
                    out.append((iso_ts(pl["wallclock"]), gc, "rec", play_name(m.group(1)),
                                yds, t == "Pass Reception" or "Touchdown" in t))
            elif t.startswith("Rush") or t == "Rushing Touchdown":
                m = RUSHER.search(text)
                if m:
                    out.append((iso_ts(pl["wallclock"]), gc, "rush", play_name(m.group(1)), yds, True))
    # ESPN sometimes repeats the live drive in previous + current
    return sorted(set(out)), sorted(set(team))

def espn_plays(espn_id):
    return espn_timeline(espn_id)[0]

def gc_label(gc):
    q = min(int(gc // 900) + 1, 4)
    rem = round(max(0, q * 900 - gc))
    return f"Q{q} {rem // 60}:{rem % 60:02d}"

# team rates shrunk toward league norms by SHRINK_N pseudo-plays: one 16-yd carry at 3:49 Q1 made
# yards/carry 16.0 (+1.4 logits) and three Bengals calls (2026 wk10). Same formula in analysis/v2_model.py.
SHRINK_N, SHARE0, YPA0, YPC0 = 10, 0.6, 5.5, 4.0

def team_rates(plays, pass_att, pass_yds, rush_yds):
    """(pass share, yds/pass att, yds/carry), each pulled toward the league norm with SHRINK_N pseudo-plays."""
    return ((pass_att + SHRINK_N * SHARE0) / (plays + SHRINK_N),
            (pass_yds + SHRINK_N * YPA0) / (pass_att + SHRINK_N),
            (rush_yds + SHRINK_N * YPC0) / (plays - pass_att + SHRINK_N))

def feature_vec(pr, kind, dv, gc, cum, nev, drought, e10, y10, diff, ts_, L, l0):
    """The model's 19 inputs; shared by live scoring and the P rebuild."""
    gc = max(gc, 60)
    ef = min(gc / 3600.0, 0.99)
    plays = ts_["pass_att"] + ts_["rush_att"]
    needed = L - cum
    return [ef, pr["ypg"], pr["ype"], dv, nev, cum, drought,
            e10, y10, diff, diff * ef,
            *team_rates(plays, ts_["pass_att"], ts_["pass_yds"], ts_["rush_yds"]),
            plays / (gc / 60), 1.0 if kind == "rush" else 0.0,
            needed, needed / (pr["ypg"] * (1 - ef) + 1), L / l0]

def score_row(x):
    kind = "rush" if x[15] else "rec"   # is_rush; weights and calibration are per market
    m = W[kind]
    z = [(x[i] - m["mu"][i]) / m["sd"][i] for i in range(len(x))]
    s = sum(zi * wi for zi, wi in zip(z, m["w"])) + m["b"]
    return calibrate(1 / (1 + math.exp(-s)), kind)

def team_stats(box_team):
    st = {s["name"]: s.get("displayValue", "") for s in box_team.get("statistics", [])}
    def num(k, d=0.0):
        try:
            return float(st.get(k, d))
        except ValueError:
            return d
    ca = st.get("completionAttempts", "0/0").replace("-", "/").split("/")
    pass_att = float(ca[1]) if len(ca) > 1 and ca[1] else 0.0
    rush_att = num("rushingAttempts")
    return {"pass_att": pass_att, "rush_att": rush_att,
            "pass_yds": num("netPassingYards"), "rush_yds": num("rushingYards")}

class Monitor:
    def __init__(self, poll):
        self.poll = poll
        self.books, self.slips = {}, {}
        self.hist_path = f"{DATA}/history.json"
        self.hist = json.load(open(self.hist_path)) if os.path.exists(self.hist_path) else {}
        self.pl_path = f"{DATA}/pregame_lines.json"   # espn_id -> {"name|kind": closing line}
        self.pre_lines = json.load(open(self.pl_path)) if os.path.exists(self.pl_path) else {}
        self.bf_path = f"{DATA}/backfilled.json"
        self.backfilled = set(json.load(open(self.bf_path))) if os.path.exists(self.bf_path) else set()
        self.status = {}    # opp id -> NEAR/TRIGGER
        self.wl_t = {}        # game -> last watchlist line refresh (2 credits a game)
        self.form_t = 0       # last preview-form fetch attempt (hourly retry on ESPN errors)
        self.qb_t = 0         # last usual-QB check for WR Good Matchups and RB unders (every 3h)
        self.qbs, self.inj = {}, {}
        self.rb_t = {}        # Odds API event id -> last RB rushing-line refresh (1 credit)
        self.early_path = f"{DATA}/early_pushes.json"   # "b.lance|rec" -> when its pregame alert went out
        self.early = json.load(open(self.early_path)) if os.path.exists(self.early_path) else {}
        try:   # RB unders the explosive-run veto skipped (forward test of the veto)
            self.rb_skips = json.load(open(f"{DOCS}/rb_skips.json"))
        except Exception:
            self.rb_skips = {}
        self.rb_ledger = layers.Ledger("nfl_rb_unders", RB_UNDER_LAYERS, DOCS)
        self.gm_ledger = layers.Ledger("nfl_good_matchups", GM_LAYERS, DOCS)   # every Good Matchup at kickoff, graded at the final
        self.rb_games = {}    # event id -> {kick, home, away, lines: {name: {book: [line, under price, under link]}}}
        self.ranks = (0, {})  # (fetched at, scoring rank by ESPN abbr), refreshed every 3h
        self.full2ab = {}     # ESPN full team name -> nflverse code

    def closing_lines(self, game, oe):
        """Each player's closing pregame line: the last historical snapshot at kickoff
        (one request, ~20 credits per game). Cached on the volume."""
        eid = game["espn_id"]
        if eid in self.pre_lines:
            return self.pre_lines[eid]
        lines = {}
        try:
            d = get(f"{HIST_ODDS}/events/{oe['id']}/odds?apiKey={KEY}&date={oe['commence_time']}"
                    f"&bookmakers=draftkings,fanduel&markets={MARKETS['rec']},{MARKETS['rush']}&oddsFormat=american")
            for b in (d.get("data") or {}).get("bookmakers", []):
                for m in b.get("markets", []):
                    kind = "rec" if m["key"] == MARKETS["rec"] else "rush"
                    for o in m.get("outcomes", []):
                        if o["name"] == "Over" and o.get("point") is not None:
                            k = f'{o["description"]}|{kind}'
                            if b["key"] == "fanduel" or k not in lines:
                                lines[k] = float(o["point"])
        except Exception as e:
            print("closing-line fetch failed", eid, repr(e))
            return {}
        self.pre_lines[eid] = lines
        json.dump(self.pre_lines, open(self.pl_path, "w"))
        print(f"closing lines: {len(lines)} props for {game['away_abbr']} @ {game['home_abbr']}")
        return lines

    def backfill(self, game, oe):
        """Joined a game after kickoff: rebuild each quoted player's line + production
        from historical odds snapshots and ESPN play-by-play. Runs once per game."""
        eid = game["espn_id"]
        self.backfilled.add(eid)
        json.dump(sorted(self.backfilled), open(self.bf_path, "w"))
        plays = espn_plays(eid)
        if not plays:
            return
        t = iso_ts(oe["commence_time"]) - 1500
        stop = time.time() - 240
        series = {}   # hkey -> [(gc, cum, nev, L)]
        for _ in range(BACKFILL_MAX):
            if t >= stop:
                break
            stamp = datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            try:
                d = get(f"{HIST_ODDS}/events/{oe['id']}/odds?apiKey={KEY}&date={stamp}"
                        f"&bookmakers=draftkings,fanduel&markets={MARKETS['rec']},{MARKETS['rush']}&oddsFormat=american")
            except Exception as e:
                print("backfill fetch failed", stamp, repr(e))
                break
            snap_t = iso_ts(d.get("timestamp", stamp))
            done = [p for p in plays if p[0] <= snap_t]
            gc = done[-1][1] if done else 0
            quotes = {}
            for b in (d.get("data") or {}).get("bookmakers", []):
                for m in b.get("markets", []):
                    kind = "rec" if m["key"] == MARKETS["rec"] else "rush"
                    for o in m.get("outcomes", []):
                        if o["name"] == "Over" and o.get("point") is not None:
                            k = (o["description"], kind)
                            if b["key"] == "fanduel" or k not in quotes:
                                quotes[k] = float(o["point"])
            for (name, kind), L in quotes.items():
                mine = [p for p in done if p[2] == kind and same_player(p[3], name)]
                cum = sum(p[4] for p in mine if p[5])
                series.setdefault(f"{eid}|{name}|{kind}", []).append((gc, cum, len(mine), L))
            nxt = d.get("next_timestamp")
            t = iso_ts(nxt) if nxt else t + 300
        for hkey, pts in series.items():
            h = self.hist.setdefault(hkey, {"l0": pts[0][3], "last_change": 0, "snaps": []})
            first_live = h["snaps"][0][0] if h["snaps"] else float("inf")
            h["snaps"] = [p for p in pts if p[0] < first_live] + h["snaps"]
            h["l0"] = pts[0][3]          # the true pregame line, not the first live quote
            for a, b in zip(h["snaps"], h["snaps"][1:]):
                if b[1] > a[1]:
                    h["last_change"] = b[0]
        print(f"backfilled {len(series)} players for {game['away_abbr']} @ {game['home_abbr']}")

    def espn_live(self):
        sb = get(f"{ESPN}/scoreboard")
        out = {}
        self.finished = {}   # espn_id -> week, for archiving props of games that ended
        for ev in sb.get("events", []):
            st = ev["status"]
            if st["type"]["state"] == "post":
                self.finished[ev["id"]] = (ev.get("week") or sb.get("week") or {}).get("number")
            if st["type"]["state"] != "in":
                continue
            comp = ev["competitions"][0]
            teams = {c["homeAway"]: c for c in comp["competitors"]}
            out[ev["id"]] = {
                "espn_id": ev["id"],
                "home": teams["home"]["team"]["displayName"], "away": teams["away"]["team"]["displayName"],
                "home_abbr": teams["home"]["team"]["abbreviation"], "away_abbr": teams["away"]["team"]["abbreviation"],
                "hs": int(teams["home"]["score"]), "as": int(teams["away"]["score"]),
                "hlogo": teams["home"]["team"].get("logo"), "alogo": teams["away"]["team"].get("logo"),
                "gc": clock_to_elapsed(st["period"], st.get("displayClock")),
                "clock": f'Q{st["period"]} {st.get("displayClock","")}',
            }
        return out

    def espn_players(self, espn_id):
        """name -> per-kind (yds, events) + team side; plus per-side team stats."""
        s = get(f"{ESPN}/summary?event={espn_id}")
        players, tstats = box_players(s), {}
        for bt in s.get("boxscore", {}).get("teams", []):
            tstats[bt["team"]["displayName"]] = team_stats(bt)
        return players, tstats

    def odds_quotes(self, odds_event):
        d = get(f"{ODDS}/events/{odds_event['id']}/odds?apiKey={KEY}"
                f"&bookmakers=draftkings,fanduel&markets={MARKETS['rec']},{MARKETS['rush']}"
                f"&oddsFormat=american&includeLinks=true")
        quotes = {}   # (name, kind) -> line (FD preferred)
        self.books = {}   # (name, kind) -> {book: [line, over price, under price]}, archived per change
        self.slips = {}   # (name, kind) -> {book: over's bet slip link}, saved on a call
        for b in d.get("bookmakers", []):
            for m in b.get("markets", []):
                kind = "rec" if m["key"] == MARKETS["rec"] else "rush"
                for o in m.get("outcomes", []):
                    if o["name"] != "Over" or o.get("point") is None:
                        continue
                    k = (o["description"], kind)
                    under = next((u.get("price") for u in m["outcomes"] if u["name"] == "Under"
                                  and u.get("description") == o["description"] and u.get("point") == o["point"]), None)
                    self.books.setdefault(k, {})[b["key"]] = [float(o["point"]), o.get("price"), under]
                    self.slips.setdefault(k, {})[b["key"]] = o.get("link")
                    if b["key"] == "fanduel" or k not in quotes:
                        quotes[k] = float(o["point"])
        return quotes

    def features(self, name, kind, L, game, players, tstats):
        pr = PRIORS.get(name, {}).get(kind)
        pdata = players.get(name)
        if not pr or not pdata or kind not in pdata:
            return None
        cum, nev = pdata[kind]
        team = pdata["team"]
        gc = max(game["gc"], 60)
        hkey = f'{game["espn_id"]}|{name}|{kind}'
        h = self.hist.setdefault(hkey, {"l0": L, "last_change": 0 if cum == 0 else gc, "snaps": []})
        if h["snaps"] and cum > h["snaps"][-1][1]:
            h["last_change"] = gc
        h["snaps"].append((gc, cum, nev, L))
        h["snaps"] = h["snaps"][-220:]
        old = next((s for s in reversed(h["snaps"]) if s[0] <= gc - 600), (0, 0, 0, None))
        e10, y10 = nev - old[2], cum - old[1]
        home = team == game["home"]
        diff = (game["hs"] - game["as"]) if home else (game["as"] - game["hs"])
        ts_ = tstats.get(team, {"pass_att": 0, "rush_att": 0, "pass_yds": 0, "rush_yds": 0})
        opp = game["away"] if home else game["home"]
        opp_abbr = game["away_abbr"] if home else game["home_abbr"]
        dkey = f'{kind}|{opp_abbr}|{pr["pos"]}'
        dv = DEF.get(dkey, 0)
        needed = L - cum
        if needed < 3 or L < 15 or not in_window(gc):
            h["snaps"][-1] = [gc, cum, nev, L, None]
            return None
        x = feature_vec(pr, kind, dv, gc, cum, nev, (gc - h["last_change"]) / 60,
                        e10, y10, diff, ts_, L, h["l0"])
        return x, cum, nev, (gc - h["last_change"]) / 60, opp, hkey, dkey

    def rescore(self, game, players):
        """Score the snapshots that have no P yet (backfilled, or saved before P was
        recorded) by rebuilding every model input at that moment from the play log.
        Also fills in the inputs for calls saved before calls recorded them."""
        eid = game["espn_id"]
        def stale(h):
            return (any(len(s) < 5 for s in h["snaps"])
                    or any(len(s) >= 5 and s[4] is not None and not in_window(s[0]) for s in h["snaps"])
                    or any(h.get(k) and not in_window(h[k].get("gc")) for k in ("signal", "call"))
                    or (h.get("call") and ("x" not in h["call"] or "score" not in h["call"])))
        todo = [k for k, h in self.hist.items() if k.startswith(eid + "|") and stale(h)]
        if not todo:
            return
        pplays, tplays = espn_timeline(eid)
        for hkey in todo:
            _, name, kind = hkey.split("|", 2)
            h = self.hist[hkey]
            pr = PRIORS.get(name, {}).get(kind)
            team = players.get(name, {}).get("team")
            home = team == game["home"]
            dkey = f'{kind}|{game["away_abbr"] if home else game["home_abbr"]}|{pr["pos"]}' if pr else ""
            dv = DEF.get(dkey, 0)
            mine = [q for q in pplays if q[2] == kind and same_player(q[3], name)]

            def inputs(s):
                gc, cum, nev, L = s[:4]
                if not (pr and team and in_window(gc) and L is not None and L - cum >= 3 and L >= 15):
                    return None
                upto = [q for q in mine if q[1] <= gc]
                gains = [q[1] for q in upto if q[5] and q[4] > 0]
                recent = [q for q in upto if q[1] > gc - 600]
                tp = [q for q in tplays if q[1] <= gc]
                hs, as_ = (tp[-1][6], tp[-1][7]) if tp else (0, 0)
                mt = [q for q in tp if q[2] == team]
                ts_ = {"pass_att": sum(q[3] == "pass" for q in mt), "rush_att": sum(q[3] == "rush" for q in mt),
                       "pass_yds": sum(q[4] for q in mt if q[3] == "pass" and q[5]),
                       "rush_yds": sum(q[4] for q in mt if q[3] == "rush")}
                return feature_vec(pr, kind, dv, gc, cum, nev, (max(gc, 60) - (gains[-1] if gains else 0)) / 60,
                                   len(recent), sum(q[4] for q in recent if q[5]),
                                   (hs - as_) if home else (as_ - hs), ts_, L, h["l0"])

            out, xs, fresh = [], {}, set()   # fresh = indices scored by this rebuild
            for s in h["snaps"]:
                x = inputs(s)
                if x is not None:
                    xs[len(out)] = x
                if len(s) >= 5:
                    out.append(s if in_window(s[0]) else list(s[:4]) + [None])
                else:
                    if x is not None:
                        fresh.add(len(out))
                    out.append(list(s[:4]) + [None if x is None else round(score_row(x), 3)])
            h["snaps"] = out
            for key, thr in (("signal", SIGNAL), ("call", CALL)):
                i = next((j for j, s in enumerate(out) if s[4] is not None and s[4] >= thr
                          and (key != "call" or (j in xs and call_gate(xs[j], dkey)))), None)
                if i is None:
                    continue
                cur = h.get(key)
                if cur and not in_window(cur.get("gc")):   # made outside the trained window: not a real signal
                    h.pop(key)
                    cur = None
                if i in fresh or cur is None:   # first crossing is rebuilt (predates any live one), or no record yet
                    keep = {k: v for k, v in (cur or {}).items() if k in ("result", "won_at", "won_yds")}
                    h[key] = {**keep, "clock": gc_label(out[i][0]), "gc": out[i][0], "line": out[i][3],
                              "cum": out[i][1], "p": out[i][4], "rebuilt": i in fresh,
                              "x": [round(v, 4) for v in xs[i]] if i in xs else None,
                              "t": (cur or {}).get("t", time.time())}
                elif cur and not cur.get("x") and i in xs:   # older record: attach its inputs
                    cur["x"] = [round(v, 4) for v in xs[i]]
                    cur.setdefault("gc", out[i][0])
            c = h.get("call")
            if c and "score" not in c and c.get("gc") is not None:
                tp = [q for q in tplays if q[1] <= c["gc"]]
                hs, as_ = (tp[-1][6], tp[-1][7]) if tp else (0, 0)
                c["score"] = f'{game["away_abbr"]} {as_} – {game["home_abbr"]} {hs}'
            scored = [s[4] for s in out if s[4] is not None]
            if scored:
                h["pmax"] = max([h.get("pmax", 0)] + scored)
        print(f"rescored {len(todo)} props for {game['away_abbr']} @ {game['home_abbr']}")

    def archive_finals(self):
        """Move tracked players from finished games into docs/archive/props_w{week}.json,
        settle each 70% call (win if final yards cleared the line at the call), and
        append settled calls to the season ledger docs/calls.json."""
        done, box, settled = {}, {}, []
        for hkey in list(self.hist):
            eid, name, kind = hkey.split("|", 2)
            wk = self.finished.get(eid)
            h = self.hist[hkey]
            if wk is None or not h.get("meta"):
                continue
            if eid not in box:
                try:
                    box[eid] = self.espn_players(eid)[0]
                except Exception as e:
                    print("final box score fetch failed", eid, repr(e))
                    box[eid] = {}
            self.hist.pop(hkey)
            last = h["snaps"][-1] if h["snaps"] else (0, 0, 0, None)
            final = box[eid].get(name, {}).get(kind, (last[1], 0))[0]   # box score beats a stale snapshot
            gp = self.gm_ledger.d["picks"].get(hkey)
            if gp:
                self.gm_ledger.grade(hkey, "win" if final > gp["line"] else "loss" if final < gp["line"] else "push", final=final)
            for key in ("call", "pre"):
                c = h.get(key)
                if not c:
                    continue
                if "result" not in c:
                    won = final < c["line"] if c.get("side") == "under" else final > c["line"]
                    c.update(result="win" if won else "loss", settled_t=time.time())
                settled.append({"week": wk, "espn_id": eid, "game": h["meta"]["game"], "player": name,
                                "market": kind, "pregame_line": h.get("l0"), "final": final,
                                "settled": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                "type": "live", **c})
            done.setdefault(wk, {})[hkey] = {**h["meta"], "snaps": h["snaps"], "l0": h.get("l0"),
                                             "final": final, "close": last[3],
                                             "pmax": h.get("pmax", 0), "signal": h.get("signal"),
                                             "call": h.get("call"), "pre": h.get("pre"),
                                             "quotes": h.get("quotes", []), "blocked": h.get("blocked")}
        if settled:
            path = f"{DOCS}/calls.json"
            ledger = json.load(open(path)) if os.path.exists(path) else {"feats": W["feats"], "calls": []}
            ident = lambda r: (r["espn_id"], r["player"], r["market"], r.get("type", "live"))
            have = {ident(r) for r in ledger["calls"]}
            ledger["calls"] += [r for r in settled if ident(r) not in have]
            json.dump(ledger, open(path, "w"), indent=1)
            print(f"ledger: settled {len(settled)} calls "
                  f"({sum(r['result'] == 'win' for r in settled)} wins)")
        for wk, recs in done.items():
            path = f"{DOCS}/archive/props_w{wk}.json"
            d = json.load(open(path)) if os.path.exists(path) else {}
            d.update(recs)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            json.dump(d, open(path, "w"))
            print(f"archived {len(recs)} player props to week {wk}")

    def rb_unders(self):
        """Upcoming RB unders for the page (docs/rb_unders.json): every RB rushing line of 20.5-41.5 whose
        team's usual QB starts and whose team is not explosive (expl_veto). Lines refresh every 3h, every 5 min
        in a game's last hour, 1 credit a game. The picks themselves are still made at kickoff on the closing line (cycle)."""
        now = time.time()
        evs = get(f"{ODDS}/events?apiKey={KEY}")   # free
        for e in evs:
            kick = datetime.datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00")).timestamp()
            gap = 300 if kick - now <= 3600 else 10800
            if not 0 < kick - now < 7 * 86400 or now - self.rb_t.get(e["id"], 0) < gap:
                continue
            self.rb_t[e["id"]] = now
            d = get(f"{ODDS}/events/{e['id']}/odds?apiKey={KEY}&bookmakers=fanduel,draftkings"
                    f"&markets={MARKETS['rush']}&oddsFormat=american&includeLinks=true")
            lines = {}
            for b in d.get("bookmakers", []):
                for m in b.get("markets", []):
                    for o in m.get("outcomes", []):
                        if o["name"] == "Under" and o.get("point") is not None:
                            lines.setdefault(o["description"], {})[b["key"]] = [float(o["point"]), o.get("price"), o.get("link")]
            self.rb_games[e["id"]] = {"kick": e["commence_time"], "home": e["home_team"], "away": e["away_team"], "lines": lines}
        self.rb_games = {k: g for k, g in self.rb_games.items()
                         if datetime.datetime.fromisoformat(g["kick"].replace("Z", "+00:00")).timestamp() > now - 6 * 3600}
        if now - self.ranks[0] > 10800:
            wk = (get(f"{ESPN}/scoreboard").get("week") or {}).get("number") or 1
            self.ranks = (now, scoring_ranks(wk))
        ranks = self.ranks[1]
        if not self.full2ab:   # Odds API full names -> nflverse codes, for each under's opponent
            self.full2ab = {t["team"]["displayName"]: NFLV.get(t["team"]["abbreviation"], t["team"]["abbreviation"])
                            for t in get(f"{ESPN}/teams")["sports"][0]["leagues"][0]["teams"]}
        out = []
        for gid, g in self.rb_games.items():
            sides = [self.full2ab.get(g["home"]), self.full2ab.get(g["away"])]
            for name, books in g.get("lines", {}).items():
                pr = PRIORS.get(name, {})
                book = "fanduel" if "fanduel" in books else "draftkings"
                L = books[book][0]
                team = ALIAS.get(pr.get("team"), pr.get("team"))
                if (self.inj.get(short_name(name)) in INJ_OUT or pr.get("rush", {}).get("pos") != "RB"
                        or not RB_UNDER[0] < L <= RB_UNDER[1] or not self.qbs or qb_backup(self.qbs, self.inj, team)
                        or expl_veto(pr.get("team"))):
                    continue
                out.append({"player": name, "team": pr.get("team"), "market": "rush", "side": "under", "line": L,
                            "opp": next((t for t in sides if t and t != pr.get("team")), None),
                            "price": books[book][1], "book": book, "link": books[book][2],
                            "links": {b: v[2] for b, v in books.items()}, "lines": {b: v[0] for b, v in books.items()},
                            "kick_iso": g["kick"], "game": f'{g["away"].split()[-1]} @ {g["home"].split()[-1]}',
                            "inj": self.inj.get(short_name(name)), "off_rank": ranks.get(team), "teams": len(ranks),
                            "ypg": pr.get("rush", {}).get("ypg"), "team_expl": TEAM_EXPL.get(pr.get("team"))})
        forms = {}   # last-3 forms already built (survive restarts): one ESPN pass per player a week
        try:
            forms.update({(e["player"], e["market"], e["kick_iso"]): e["form"]
                          for e in json.load(open(f"{DOCS}/rb_unders.json"))["entries"] if (e.get("form") or {}).get("v") == FORM_V})
        except Exception:
            pass
        for w in out:   # only the cards the page shows: RBs on bottom-third scoring teams
            if w["opp"] and w["team"] and (w["off_rank"] or 0) > w["teams"] * 2 / 3:
                w["form"] = forms.get((w["player"], "rush", w["kick_iso"]))
                if not w["form"]:
                    try:
                        w["form"] = preview_form(w, self.pre_lines)
                    except Exception as e:   # ponytail: retried next cycle; no backoff
                        print("unders form error", w["player"], repr(e))
        out.sort(key=lambda w: (w["kick_iso"], w["game"], w["player"]))
        tmp = f"{DOCS}/rb_unders.json.tmp"
        json.dump({"ts": now, "entries": out}, open(tmp, "w"))
        os.replace(tmp, f"{DOCS}/rb_unders.json")

    def alerted(self, name, kind):
        """The pregame alert for this prop already went out (this week)."""
        return time.time() - self.early.get(f"{short_name(name)}|{kind}", 0) < 6 * 3600

    def early_pushes(self):
        """Pregame picks alert PRE_PUSH before kickoff at the current line: the week's GM_PICKS Good Matchups,
        RB unders by the kickoff rule. The pick itself is still made at kickoff (cycle), whose push
        only fires for a pick this missed (added late, or a restart)."""
        picks = [(w, "Over") for w in watchlist() if w.get("top3") and gm_eligible(w)]
        try:
            picks += [(w, "Under") for w in json.load(open(f"{DOCS}/rb_unders.json"))["entries"]
                      if (w.get("off_rank") or 0) > w["teams"] * 2 / 3 and not expl_veto(w["team"])]
        except Exception:
            pass
        sent = False
        for w, side in picks:
            key, mins = f'{short_name(w["player"])}|{w["market"]}', kick_in(w) / 60
            if not 0 < mins <= PRE_PUSH / 60 or self.alerted(w["player"], w["market"]):
                continue
            self.early[key], sent = time.time(), True
            lines, kind = w.get("lines") or {}, "rec" if w["market"] == "rec" else "rush"
            book = w.get("book") if w.get("book") in push.BOOK else "fanduel"
            push.send(f'Bet {w["player"]} {side} {w["line"]} {kind} yds on {push.BOOK[book]}',
                      f'Pregame pick, kickoff in {round(mins)} min · {w["game"]}', tag=key + "|pre", url="./#nfl/upcoming",
                      variants={b: f'Bet {w["player"]} {side} {lines.get(b, w["line"])} {kind} yds on {n}' for b, n in push.BOOK.items()})
            print("pregame alert", w["player"], side, w["line"])
        if sent:
            self.early = {k: t for k, t in self.early.items() if time.time() - t < 8 * 86400}
            json.dump(self.early, open(self.early_path, "w"))

    def watchlist_extras(self):
        """Annotate Good Matchups with the injury report (every cycle, free), the
        current pregame prop line (hourly, every minute in a game's last hour:
        2 credits per game, upcoming games only) and, once per entry, the last-3
        form behind the preview card (ESPN schedule + box scores)."""
        try:
            wl = json.load(open(f"{DOCS}/watchlist.json"))
        except Exception:
            return
        entries = wl.get("entries", [])
        if not entries:
            return
        changed, inj = False, {}
        try:
            inj = espn_injuries()
            for w in entries:
                st = inj.get(short_name(w["player"]))
                if w.get("inj") != st:
                    w["inj"] = st   # None clears a healed player
                    changed = True
                mo = wr_mate_out(w["player"], inj, w.get("quoted")) if w["pos"] == "WR" else None
                if w.get("mate_out") != mo:
                    w["mate_out"] = mo   # his top WR teammate is out: no over (that over went 23-34, 2024-26)
                    changed = True
        except Exception as e:
            print("injury fetch error", repr(e))
        if time.time() - self.qb_t > 10800 or any(0 < kick_in(w) <= WL_NEAR for w in entries):
            self.qb_t = time.time()
            try:   # WR overs need the usual QB: skip the pick when he's out or not QB1 this week
                qbs = self.qbs = qb_status(wl.get("week") or 1, f"{DATA}/qb_starts.json")
                self.inj = inj
                for w in entries:
                    if w["pos"] != "WR":
                        continue
                    out = qb_backup(qbs, inj, ALIAS.get(w["team"], w["team"]))
                    if w.get("qb_out") != out:
                        w["qb_out"] = out
                        changed = True
            except Exception as e:
                print("QB check error", repr(e))
        try:   # venue + forecast for the preview card (free; the forecast moves through the week)
            info = game_info(wl.get("week"))
            for w in entries:
                for k, v in info.get(w["game"], {}).items():
                    if w.get(k) != v:
                        w[k] = v
                        changed = True
        except Exception as e:
            print("game info fetch error", repr(e))
        now = time.time()
        due = {w["game"] for w in entries if 0 < kick_in(w) and
               now - self.wl_t.get(w["game"], 0) >= (WL_NEAR_GAP if kick_in(w) <= WL_NEAR else WL_GAP)}
        if due:
            try:
                evs = get(f"{ODDS}/events?apiKey={KEY}")
                qbyg = {}
                for w in entries:
                    if w["game"] not in due:
                        continue   # kicked off (the live board takes over), or not due yet
                    if w["game"] not in qbyg:
                        self.wl_t[w["game"]] = now
                        full = w.get("home_full", "")
                        oe = next((e for e in evs if full and
                                   (full in e["home_team"] or e["home_team"] in full)), None)
                        qbyg[w["game"]] = (self.odds_quotes(oe), self.books, self.slips) if oe else ({}, {}, {})
                    quotes, books, slips = qbyg[w["game"]]
                    k = next((k for k in quotes if k[1] == w["market"] and short_name(k[0]) == short_name(w["player"])), None)
                    L = quotes.get(k)
                    book = (first_over(books.get(k)) or (None,))[0]
                    link, links = slips.get(k, {}).get(book), slips.get(k, {})
                    lines = {b: q[0] for b, q in books.get(k, {}).items()}
                    quoted = sorted({short_name(q[0]) for q in quotes if q[1] == "rec"})   # who the book has a rec line for
                    if (w.get("line"), w.get("book"), w.get("link"), w.get("links"), w.get("lines"), w.get("quoted")) != (L, book, link, links, lines, quoted):
                        w.update(line=L, book=book, link=link, links=links, lines=lines, quoted=quoted)
                        changed = True
            except Exception as e:
                print("watchlist line fetch error", repr(e))
        if any((w.get("form") or {}).get("v") != FORM_V for w in entries) and time.time() - self.form_t > 3600:
            self.form_t = time.time()   # new week: last-3 form for the preview cards, once per entry
            try:
                for w in entries:
                    if (w.get("form") or {}).get("v") != FORM_V:
                        w["form"] = preview_form(w, self.pre_lines)
                        changed = True
            except Exception as e:
                print("preview form error", repr(e))
        changed |= gm_top(entries, self.ranks[1])   # the week's GM_PICKS picks, after injuries/QBs/lines/form are fresh
        if changed:
            json.dump(wl, open(f"{DOCS}/watchlist.json", "w"), indent=1)
            print("watchlist extras updated (injuries/venue/lines/form)")

    def cycle(self):
        opps, games_out, histout = [], [], {}
        live = self.espn_live()
        self.archive_finals()
        if not live:
            return opps, games_out, histout
        odds_events = get(f"{ODDS}/events?apiKey={KEY}")
        for game in live.values():
            listed = set()
            oe = next((e for e in odds_events
                       if game["home"] in e["home_team"] or e["home_team"] in game["home"]), None)
            games_out.append({"label": f'{game["away_abbr"]} @ {game["home_abbr"]}',
                              "score": f'{game["as"]}-{game["hs"]}', "clock": game["clock"],
                              "hlogo": game.get("hlogo"), "alogo": game.get("alogo")})
            if not oe:
                continue
            if game["gc"] > 300 and game["espn_id"] not in self.backfilled:
                try:
                    self.backfill(game, oe)
                except Exception as e:
                    print("backfill error", repr(e))
            try:
                players, tstats = self.espn_players(game["espn_id"])
                quotes = self.odds_quotes(oe)
            except Exception as e:
                print("fetch error", game["home_abbr"], repr(e))
                continue
            try:
                self.rescore(game, players)
            except Exception as e:
                print("rescore error", repr(e))
            for (name, kind), L in quotes.items():
                f = self.features(name, kind, L, game, players, tstats)
                hkey = f'{game["espn_id"]}|{name}|{kind}'
                c = self.hist.get(hkey, {}).get("call")
                if c and "result" not in c:
                    c["open"] = bet_open(c, self.books.get((name, kind)))
                if hkey in self.hist and name in players:
                    side = "hlogo" if players[name]["team"] == game["home"] else "alogo"
                    self.hist[hkey]["meta"] = {"player": name, "market": kind, "logo": game.get(side),
                                               "game": f'{game["away_abbr"]} @ {game["home_abbr"]}'}
                if not f:
                    continue
                x, cum, nev, drought, opp, hkey, dkey = f
                p = score_row(x)
                h = self.hist[hkey]
                h["pmax"] = max(h.get("pmax", 0), round(p, 3))
                bk = self.books.get((name, kind))
                if not h.get("quotes") or h["quotes"][-1][2] != bk:   # ponytail: on change only, props repeat for most polls
                    h.setdefault("quotes", []).append((round(time.time()), game["gc"], bk))
                if p >= SIGNAL and "signal" not in h:   # first time the model liked the over
                    h["signal"] = {"clock": game["clock"], "line": L, "cum": cum, "p": round(p, 3), "rebuilt": False}
                snap = {"clock": game["clock"], "gc": game["gc"], "line": L, "cum": cum, "books": bk,
                        "p": round(p, 3), "rebuilt": False, "x": [round(v, 4) for v in x], "t": time.time(),
                        "score": f'{game["away_abbr"]} {game["as"]} – {game["home_abbr"]} {game["hs"]}'}
                if p >= CALL and "call" not in h and not call_gate(x, dkey) and "blocked" not in h:
                    h["blocked"] = {**snap, "why": "top3_def" if dkey.rsplit("|", 1)[0] in TOP3_DEF else "trailing" if x[9] < -3 else "leading"}
                if p >= CALL and "call" not in h and call_gate(x, dkey):
                    book, line, price = first_over(bk) or ("fanduel", L, None)
                    h["call"] = {**snap, "line": line, "book": book, "price": price, "link": self.slips.get((name, kind), {}).get(book),
                                 "links": self.slips.get((name, kind), {})}
                    push.send(f'Bet {name} Over {line} {"rec" if kind == "rec" else "rush"} yds'
                              f'{f" ({price:+d})" if price else ""} on {push.BOOK[book]}, good to {line + 1:g}',
                              f'Model gives it {round(p * 100)}% · {game["away_abbr"]} {game["as"]}–{game["home_abbr"]} {game["hs"]}, '
                              f'{game["clock"]}', tag=hkey, url="./#nfl/live",
                              variants={b: f'Bet {name} Over {q[0]} {"rec" if kind == "rec" else "rush"} yds ({q[1]:+d}) on {push.BOOK[b]}, good to {q[0] + 1:g}'
                                        for b, q in (bk or {}).items() if q[1] is not None})
                c = h.get("call")
                if c and "result" not in c and cum > c["line"]:
                    c.update(result="win", won_at=game["clock"], won_yds=cum, settled_t=time.time())
                h["snaps"][-1] = list(h["snaps"][-1][:4]) + [round(p, 3)]   # (gc, cum, nev, L, P)
                oid = f'{name}|{kind}'
                pteam = players[name]["team"]
                logo = game.get("hlogo") if pteam == game["home"] else game.get("alogo")
                meta = {"player": name, "market": kind, "logo": logo,
                        "game": f'{game["away_abbr"]} @ {game["home_abbr"]}'}
                self.hist[hkey]["meta"] = meta
                if dkey.rsplit("|", 1)[0] in TOP3_DEF:
                    continue   # top-3 form defense: keep collecting snaps, never show the over
                histout[oid] = {**meta, "snaps": self.hist[hkey]["snaps"]}
                status = ("TRIGGER" if p >= TRIG and call_gate(x, dkey)
                          else "NEAR" if p >= NEAR else "watch")
                listed.add(oid)
                opps.append({"id": oid, "eid": game["espn_id"], "player": name, "market": kind, "logo": logo, "game": f'{game["away_abbr"]} @ {game["home_abbr"]}',
                             "clock": game["clock"], "line": L, "cum": cum, "events": nev,
                             "drought": round(drought), "p": round(p, 3), "status": status, "vs": opp,
                             "call": h.get("call"), "pre": h.get("pre")})
                prev = self.status.get(oid)
                if status != prev and status in ("NEAR", "TRIGGER"):
                    notify(f"{'🔔 TRIGGER' if status=='TRIGGER' else 'Approaching'}: {name}",
                           f'{kind} O{L} | {cum} yds, P={p:.2f} | {game["clock"]}')
                    if status == "TRIGGER":
                        with open(f"{DATA}/paper_log.csv", "a") as lf:
                            csv.writer(lf).writerow([datetime.datetime.now(datetime.timezone.utc).isoformat(), name, kind,
                                                     L, cum, nev, round(p, 3), game["clock"]])
                self.status[oid] = status
            # every prop's l0 is its closing pregame line (what the model was trained on);
            # watchlist props (top player vs bottom-10 defense) are pregame calls at that line
            closing = self.closing_lines(game, oe)
            for k, line in closing.items():
                hk = f'{game["espn_id"]}|{k}'
                if hk in self.hist and not self.hist[hk].get("l0_closing"):
                    self.hist[hk].update(l0=line, l0_closing=True)
            quoted = {short_name(k.rsplit("|", 1)[0]) for k in closing if k.endswith("|rec")}
            for w in watchlist():
                k = next((k for k in closing if k.endswith("|" + w["market"]) and
                          short_name(k.rsplit("|", 1)[0]) == short_name(w["player"])), None)
                if not k:
                    continue   # no pregame line = no bet was available
                name, kind = k.rsplit("|", 1)
                hkey = f'{game["espn_id"]}|{name}|{kind}'
                mate = w["pos"] == "WR" and wr_mate_out(w["player"], self.inj, quoted)
                if w.get("inj") in INJ_OUT or w.get("qb_out") or mate:   # injured, a WR without his usual QB, or his top WR teammate out: no over
                    if name not in players and "result" not in self.hist.get(hkey, {}).get("pre", {"result": 0}):
                        del self.hist[hkey]["pre"]   # ruled out after the pick: he never played, so no card or grade
                    continue
                h = self.hist.setdefault(hkey, {"l0": closing[k], "l0_closing": True, "last_change": 0, "snaps": []})
                if not h.get("meta"):
                    team = ALIAS.get(w["team"], w["team"])
                    side = "hlogo" if team == game["home_abbr"] else "alogo" if team == game["away_abbr"] else None
                    h["meta"] = {"player": name, "market": kind, "logo": game.get(side) if side else None,
                                 "game": f'{game["away_abbr"]} @ {game["home_abbr"]}'}
                c = h.get("pre")
                caught = layers.vetoes(GM_LAYERS, w)
                if not c and game["gc"] <= 300:   # the ledger keeps every candidate at the closing line, picked or not
                    self.gm_ledger.put(hkey, {"player": name, "market": kind, "line": closing[k], "side": "over",
                                              "score": w.get("score"), "game": h["meta"]["game"]}, caught)
                if caught and not c:
                    continue   # not one of the week's picks: graded in the ledger only
                if not c and not self.alerted(name, kind):   # this prop just joined the week's picks
                    book = "fanduel" if "fanduel" in self.books.get((name, kind), {"fanduel": 0}) else "draftkings"
                    push.send(f'Bet {name} Over {closing[k]} {"rec" if kind == "rec" else "rush"} yds on {push.BOOK[book]}',
                              f'Pregame pick, game is live · {game["away_abbr"]} @ {game["home_abbr"]}',
                              tag=hkey + "|pre", url="./#nfl/live",
                              variants={b: f'Bet {name} Over {closing[k]} {"rec" if kind == "rec" else "rush"} yds on {n}' for b, n in push.BOOK.items()})
                if not c or (c.get("src") != "closing" and "result" not in c):
                    h["pre"] = c = {"type": "pregame", "src": "closing", "clock": "pregame", "gc": 0,
                                    "line": closing[k], "cum": 0, "p": None, "t": (c or {}).get("t", time.time()),
                                    "score": "kickoff", "rebuilt": False}
                cum = players.get(name, {}).get(kind, (0, 0))[0]
                if "result" not in c and cum > c["line"]:
                    c.update(result="win", won_at=game["clock"], won_yds=cum, settled_t=time.time())
            # Pregame unders at the closing line. RB rushing (analysis/prop_search.py: 2024 +21%, 2025 +5%, 2026 +12%
            # at real prices), only when the team's usual QB starts; no QB check yet means no bet. From 2026 week 9 also
            # vetoed when the team's season 10+ yd run rate is above RB_EXPL (bottom-third teams above it went 12-12 on
            # the under 2024-25, the rest 53-22); the skipped bets go to docs/rb_skips.json for the forward test. The WR teammate-out under
            # was dropped 2026-11-12: those WRs' unders win as often with the teammate playing (132-97), so "out" wasn't it.
            for k, L in closing.items():
                name, kind = k.rsplit("|", 1)
                pr = PRIORS.get(name, {})
                team = ALIAS.get(pr.get("team"), pr.get("team"))
                ranks = self.ranks[1]   # bottom third of scoring only; no ranks yet means no bet
                if team not in (game["home_abbr"], game["away_abbr"]) or self.inj.get(short_name(name)) in INJ_OUT:
                    continue
                if (kind == "rush" and RB_UNDER[0] < L <= RB_UNDER[1] and pr.get("rush", {}).get("pos") == "RB" and self.qbs
                        and not qb_backup(self.qbs, self.inj, team) and ranks.get(team, 0) > len(ranks) * 2 / 3):
                    rule = "rb_under"
                    caught = layers.vetoes(RB_UNDER_LAYERS, {"team": pr.get("team")})
                    if caught:   # forward evidence for the layer: log the bet not made, grade it later from pbp
                        skey = f'{game["espn_id"]}|{name}|{kind}'
                        if game["gc"] <= 300 and skey not in self.rb_skips:
                            self.rb_ledger.put(skey, {"player": name, "team": pr.get("team"), "line": L, "side": "under",
                                                      "game": f'{game["away_abbr"]} @ {game["home_abbr"]}'}, caught)
                            self.rb_skips[skey] = {"t": time.time(), "player": name, "team": pr.get("team"), "line": L, "game": f'{game["away_abbr"]} @ {game["home_abbr"]}',
                                                   "team_expl": TEAM_EXPL.get(pr.get("team")), "off_rank": ranks.get(team)}
                            json.dump(self.rb_skips, open(f"{DOCS}/rb_skips.json", "w"), indent=1)
                        continue
                else:
                    continue
                hkey = f'{game["espn_id"]}|{name}|{kind}'
                if hkey not in self.hist and game["gc"] > 300:
                    continue
                h = self.hist.setdefault(hkey, {"l0": L, "l0_closing": True, "last_change": 0, "snaps": []})
                if not h.get("meta"):
                    side = "hlogo" if team == game["home_abbr"] else "alogo"
                    h["meta"] = {"player": name, "market": kind, "logo": game.get(side), "game": f'{game["away_abbr"]} @ {game["home_abbr"]}'}
                c = h.get("pre")
                if c and c.get("side") != "under":
                    continue   # already a Good Matchup over
                if not c and game["gc"] > 300:
                    continue   # a pregame bet: only made around kickoff, never mid-game (e.g. after a restart)
                if not c:
                    book = "fanduel" if "fanduel" in self.books.get((name, kind), {"fanduel": 0}) else "draftkings"
                    if not self.alerted(name, kind):   # the 20-minute alert missed it
                        push.send(f'Bet {name} Under {L} {kind} yds on {push.BOOK[book]}', f'Pregame pick, game is live · {game["away_abbr"]} @ {game["home_abbr"]}',
                                  tag=hkey + "|pre", url="./#nfl/live",
                                  variants={b: f'Bet {name} Under {L} {kind} yds on {n}' for b, n in push.BOOK.items()})
                    h["pre"] = c = {"type": "pregame", "side": "under", "rule": rule, "src": "closing", "clock": "pregame",
                                    "gc": 0, "line": L, "cum": 0, "p": None, "t": time.time(), "score": "kickoff", "rebuilt": False,
                                    "team_expl": TEAM_EXPL.get(pr.get("team"))}
                cum = players.get(name, {}).get(kind, (0, 0))[0]
                if "result" not in c and cum > c["line"]:
                    c.update(result="loss", lost_at=game["clock"], settled_t=time.time())
            # a call stays listed until it settles, even when the prop isn't scored this
            # cycle (line pulled, within 3 yds of the line, or past the scoring window)
            for hkey, h in self.hist.items():
                eid, name, kind = hkey.split("|", 2)
                oid = f"{name}|{kind}"
                if eid != game["espn_id"] or not (h.get("call") or h.get("pre")) or oid in listed or not h.get("meta"):
                    continue
                last = h["snaps"][-1] if h["snaps"] else [game["gc"], 0, 0, h["l0"], None]
                cum, nev = players.get(name, {}).get(kind, (last[1], last[2]))
                c = h.get("call")
                if c and "result" not in c and cum > c["line"]:
                    c.update(result="win", won_at=game["clock"], won_yds=cum, settled_t=time.time())
                histout[oid] = {**h["meta"], "snaps": h["snaps"]}
                opps.append({"id": oid, "eid": eid, **h["meta"], "clock": game["clock"], "line": last[3], "cum": cum,
                             "events": nev, "drought": round((max(game["gc"], 60) - h.get("last_change", 0)) / 60),
                             "p": last[4] if len(last) > 4 and last[4] is not None else 0,
                             "status": "watch", "vs": "", "call": c, "pre": h.get("pre"), "noline": True})
        return opps, games_out, histout

    def run(self, demo=False, once=False):
        while True:
            if demo:
                opps = [
                    {"id": "demo1", "player": "Brock Bowers", "market": "rec", "game": "KC @ LV", "clock": "Q3 8:12",
                     "line": 38.5, "cum": 12, "events": 6, "drought": 22, "p": 0.67, "status": "TRIGGER", "vs": "KC"},
                    {"id": "demo2", "player": "Puka Nacua", "market": "rec", "game": "LA @ SEA", "clock": "Q2 3:40",
                     "line": 52.5, "cum": 21, "events": 4, "drought": 12, "p": 0.55, "status": "NEAR", "vs": "SEA"},
                    {"id": "demo3", "player": "Derrick Henry", "market": "rush", "game": "BAL @ CIN", "clock": "Q2 9:05",
                     "line": 61.5, "cum": 28, "events": 9, "drought": 8, "p": 0.41, "status": "watch", "vs": "CIN"},
                ]
                games_out = [{"label": "KC @ LV", "score": "17-10", "clock": "Q3 8:12"},
                             {"label": "LA @ SEA", "score": "7-13", "clock": "Q2 3:40"}]
            else:
                try:
                    opps, games_out, histout = self.cycle()
                except Exception as e:
                    print("cycle error", repr(e))
                    opps, games_out, histout = [], [], {}
                try:
                    refresh_watchlist()
                    self.watchlist_extras()
                except Exception as e:
                    print("watchlist refresh error", repr(e))
                try:
                    self.rb_unders()
                except Exception as e:
                    print("RB unders error", repr(e))
                json.dump(histout, open(f"{DOCS}/nfl_history.json", "w"))
            opps.sort(key=lambda o: -o["p"])
            json.dump({"updated": datetime.datetime.now().strftime("%H:%M:%S"),
                       "ts": time.time(),
                       "near": NEAR, "trigger": TRIG, "games": games_out, "opps": opps},
                      open(f"{DOCS}/state.json", "w"))
            json.dump(self.hist, open(self.hist_path, "w"))
            print(f'{datetime.datetime.now():%H:%M:%S} {len(games_out)} live games, {len(opps)} tracked, '
                  f'{sum(1 for o in opps if o["status"]=="TRIGGER")} triggered')
            if demo or once:
                break
            end = time.time() + self.poll
            while time.time() < end:   # live polling stays at --poll; a game's last hour ticks every minute
                time.sleep(max(0, min(60, end - time.time())))
                try:
                    self.early_pushes()
                except Exception as e:
                    print("pregame alert error", repr(e))
                if any(0 < kick_in(w) <= WL_NEAR for w in watchlist()):
                    try:
                        self.watchlist_extras()
                    except Exception as e:
                        print("watchlist refresh error", repr(e))

SCOREBOARDS = {"nfl": f"{ESPN}/scoreboard",
               "cfb": "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard?groups=80&limit=250"}

def live_score(ev):
    comp = ev["competitions"][0]
    t = {c["homeAway"]: c for c in comp["competitors"]}
    sit = comp.get("situation", {})
    side = {c["id"]: c["homeAway"] for c in comp["competitors"]}.get(sit.get("possession"))
    st = ev["status"]
    out = {"state": st["type"]["state"], "detail": st["type"].get("shortDetail"),
           "as": int(t["away"]["score"] or 0), "hs": int(t["home"]["score"] or 0),
           "clock": f'Q{st["period"]} {st.get("displayClock", "")}'}
    if side:
        ab = t[side]["team"]["abbreviation"]
        out.update(poss_side=side, poss=f'{ab} ball' + (f', {sit["possessionText"]}' if sit.get("possessionText") else ""))
    return out

def box_players(s):
    """ESPN game summary -> name -> per-kind (yds, events) + team side."""
    players = {}
    for tp in s.get("boxscore", {}).get("players", []):
        side = tp["team"]["displayName"]
        for cat in tp.get("statistics", []):
            kind = {"receiving": "rec", "rushing": "rush"}.get(cat.get("name"))
            if not kind:
                continue
            labels = [l.upper() for l in cat.get("labels", [])]
            for a in cat.get("athletes", []):
                row = dict(zip(labels, a.get("stats", [])))
                name = a["athlete"]["displayName"]
                try:
                    yds = float(row.get("YDS", 0) or 0)
                    nev = float(row.get("TGTS") or row.get("CAR") or row.get("REC") or 0)
                except ValueError:
                    continue
                players.setdefault(name, {"team": side})[kind] = (yds, nev)
    return players

def tracked_yards(summary, ids):
    """{"name|kind": yards} for the props on the board. ids: those same keys."""
    out = {}
    for name, p in box_players(summary).items():
        for kind in ("rec", "rush"):
            if f"{name}|{kind}" in ids and kind in p:
                out[f"{name}|{kind}"] = p[kind][0]
    return out

def stats_loop():
    """Tracked players' yards from ESPN's free box scores every 10s, so cards move between odds
    polls. Apart from scores_loop: a slow box score can't delay scores or alerts. Display only:
    P, calls and wins stay on the odds poll."""
    while True:
        out = {}
        try:
            opps = json.load(open(f"{DOCS}/state.json")).get("opps", [])
            for eid in {o["eid"] for o in opps if o.get("eid")}:
                out[eid] = tracked_yards(get(f"{ESPN}/summary?event={eid}", timeout=8), {o["id"] for o in opps if o.get("eid") == eid})
        except Exception as e:
            print("stats error", repr(e)[:120])
        if out:
            tmp = f"{DOCS}/yards.json.tmp"
            json.dump({"ts": time.time(), **out}, open(tmp, "w"))
            os.replace(tmp, f"{DOCS}/yards.json")
        time.sleep(10 if out else 60)

def scores_loop():
    """Score/clock/possession from ESPN's free scoreboards every 10s, so the board moves
    between odds polls (which stay at --poll to save credits). The page overlays it."""
    while True:
        out, live = {}, False
        for sport, url in SCOREBOARDS.items():
            try:
                evs = get(url, timeout=8).get("events", [])
                out[sport] = {ev["id"]: live_score(ev) for ev in evs if ev["status"]["type"]["state"] != "pre"}
                live = live or any(v["state"] == "in" for v in out[sport].values())
            except Exception as e:
                print("scores error", sport, repr(e)[:120])
        tmp = f"{DOCS}/scores.json.tmp"
        json.dump({"ts": time.time(), **out}, open(tmp, "w"))
        os.replace(tmp, f"{DOCS}/scores.json")   # the page reads this every 10s; never half-written
        push.drain()   # spread EDGE alerts queued by the spread pollers
        time.sleep(10 if live else 60)

class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *a):   # the dashboard polls every 10s; don't log each fetch
        pass
    def end_headers(self):   # revalidate every load, so a deploy reaches open tabs and phones
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()
    def do_POST(self):   # /api/subscribe and /api/unsubscribe, a PushSubscription as JSON
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n)) if 0 < n <= 4096 else None
        except ValueError:
            body = None
        ok = False
        if self.path == "/api/subscribe":
            ok = push.subscribe(body)
        elif self.path == "/api/unsubscribe" and isinstance(body, dict):
            push.unsubscribe(body.get("endpoint")); ok = True
        self.send_response(204 if ok else 400)
        self.end_headers()

def serve():
    ThreadingHTTPServer((HOST, PORT), functools.partial(QuietHandler, directory=DOCS)).serve_forever()

if __name__ == "__main__":
    poll = int(sys.argv[sys.argv.index("--poll") + 1]) if "--poll" in sys.argv else 300
    if "--once" not in sys.argv:
        push.init(DOCS)
        threading.Thread(target=serve, daemon=True).start()
        threading.Thread(target=scores_loop, daemon=True).start()
        threading.Thread(target=stats_loop, daemon=True).start()
        print(f"dashboard: http://{HOST}:{PORT}")
    Monitor(poll).run(demo="--demo" in sys.argv, once="--once" in sys.argv)
    if "--demo" in sys.argv:
        print("demo state written; serving dashboard (ctrl-c to stop)")
        while True:
            time.sleep(60)
