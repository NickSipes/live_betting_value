#!/usr/bin/env python3
"""CFB live spread monitor: ESPN scoreboard (score/clock/pregame line, free) +
the-odds-api live spreads (1 credit per poll for ALL live games, DK+FD).

Fair-line baseline: expected final home margin = current margin +
(-pregame_home_spread) * fraction of game remaining. Edge = live spread - fair
spread. Big |edge| = the market moved more (or less) than score+time justify —
e.g. a pick-6 swinging a -14 to -7.

Usage: python3 cfb_poller.py [--sport nfl|cfb] [--poll 180] [--once]
Writes docs/{cfb_state,nfl_spreads_state}.json (live), docs/{sport}_games.json
(schedule registry: pre/in/post for this week and next), and moves each game's
line history into docs/archive/{sport}_w{week}.json when it goes final.
Appends flags to live/{sport}_spread_log.csv.
"""
import json, os, sys, time, csv, re, datetime, subprocess, urllib.request
import push

HERE = os.path.dirname(os.path.abspath(__file__))
KEY = os.environ.get("ODDS_API_KEY") or \
    open(os.path.join(HERE, "..", ".env")).read().split("ODDS_API_KEY=")[1].strip()
SPORT = "nfl" if "--sport" in sys.argv and sys.argv[sys.argv.index("--sport") + 1] == "nfl" else "cfb"
ODDS_SPORT = {"cfb": "americanfootball_ncaaf", "nfl": "americanfootball_nfl"}[SPORT]
ESPN_PATH = {"cfb": "college-football", "nfl": "nfl"}[SPORT]
ODDS = f"https://api.the-odds-api.com/v4/sports/{ODDS_SPORT}/odds"
HIST_ODDS = f"https://api.the-odds-api.com/v4/historical/sports/{ODDS_SPORT}/odds"
BACKFILL_MAX = 40   # bulk snapshots per sweep, 10 credits each (all games per snapshot)
ESPN = f"https://site.api.espn.com/apis/site/v2/sports/football/{ESPN_PATH}/scoreboard" + \
    ("?groups=80&limit=250" if SPORT == "cfb" else "")
STATE = {"cfb": "cfb_state.json", "nfl": "nfl_spreads_state.json"}[SPORT]
PUBHIST = {"cfb": "cfb_history.json", "nfl": "nfl_spread_history.json"}[SPORT]
DOCS = os.environ.get("DOCS_DIR", f"{HERE}/../docs")   # served site + published state
DATA = os.environ.get("DATA_DIR", HERE)                # private state (history, logs)
REGISTRY = f"{DOCS}/{SPORT}_games.json"
ARCHIVE = f"{DOCS}/archive"
EDGE_FLAG = 4.0     # points of edge to flag
# drive-end fits: NFL 2025-26 pooled (analysis/momentum.py); CFB 2026 FBS 570 games
# (analysis/cfb_spread_model.py): leak 0.31 [0.18, 0.44], possession 0.73 [0.54, 0.92] x EP,
# fit jointly. Live backtest on 16 archived CFB games: EDGE flags cover ~47%, no edge yet.
LEAK = {"nfl": 0.28, "cfb": 0.31}[SPORT]
POSS = {"nfl": 1.0, "cfb": 0.73}[SPORT]   # multiplier on the linear EP possession term
# NFL only: both teams top third by record, take the trailing team, only if it was the pregame favorite,
# at the first fresh quote after a lead first hits 11+ (analysis/lead_spread_value.py: 14-5 at +37% on
# 2024-25 before minute 55, 2-0 in 2026). College failed this test, favorites included.
# College, same trigger, the reverse bet: take the unranked leader when only the trailing team is ranked.
# 13-5 flipped from the trailing bets (10-3, 3-2): found after the fact, leader prices never tested.
LEAD_CALL = 11
LEAD_LATEST = 3300   # leads reached from minute 55 aren't called: quotes get long (+630) and can't be trusted to fill
LEAD_MAX_GAP = 600   # game seconds back to the last poll; more means we can't say when the lead happened
LEAD_MAX_WAIT = 900  # seconds from the score; the backtest's bet window

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8.4"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)

def notify(title, msg):
    try:
        subprocess.run(["osascript", "-e",
                        f'display notification "{msg}" with title "{title}" sound name "Glass"'],
                       capture_output=True, timeout=5)
    except Exception:
        pass

def clock_elapsed(period, display):
    m = re.match(r"(\d+):(\d+)", display or "")
    rem = int(m.group(1)) * 60 + int(m.group(2)) if m else 0
    if period <= 4:
        return (period - 1) * 900 + (900 - rem)
    return 3600  # OT: treat as end of regulation for rem_frac

def parse_pregame(odds_list, home_abbr):
    """ESPN pregame odds 'details' like 'UGA -14.5' -> home spread."""
    for o in odds_list or []:
        d = o.get("details", "")
        m = re.match(r"([A-Z&;'\.\-]+)\s+(-?\d+(\.\d+)?)", d)
        if m:
            fav, num = m.group(1), float(m.group(2))
            return num if fav == home_abbr else -num
    return None

def iso_ts(s):
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()

def espn_timeline(espn_id):
    """(wall_ts, gc, home_score, away_score) for every play so far."""
    s = get(f"https://site.api.espn.com/apis/site/v2/sports/football/{ESPN_PATH}/summary?event={espn_id}")
    out = []
    drives = s.get("drives", {})
    for d in drives.get("previous", []) + ([drives["current"]] if drives.get("current") else []):
        for pl in d.get("plays", []):
            if pl.get("wallclock") and pl.get("homeScore") is not None:
                out.append((iso_ts(pl["wallclock"]),
                            clock_elapsed(pl.get("period", {}).get("number", 1), pl.get("clock", {}).get("displayValue")),
                            pl["homeScore"], pl["awayScore"]))
    return sorted(set(out))

def home_spread(event, home_name):
    """FD-preferred home spread for the odds-API event matching an ESPN home team."""
    if not (home_name in event["home_team"] or event["home_team"] in home_name):
        return None
    best = None
    for b in event.get("bookmakers", []):
        for m in b.get("markets", []):
            for o in m.get("outcomes", []):
                if o["name"] == event["home_team"] and o.get("point") is not None:
                    if b["key"] == "fanduel" or best is None:
                        best = float(o["point"])
    return best

def fair_spread(S0, margin, gc, side, yl):
    """Model's fair home spread. side: 'home'/'away'/None has the ball, yl: yards from its own goal."""
    if S0 is None:
        return None
    rem = max(0.0, 1 - gc / 3600)
    # possession value: EP ~ 0.065*yardline - 1.3 (own 20 ~ 0, midfield ~ 2,
    # opp 20 ~ 3.9). ponytail: linear EP; swap for a fitted curve at calibration
    ep = (0.065 * yl - 1.3) if side else 0.0
    ep = ep if side != "away" else -ep
    ep *= min(1.0, rem * 8)   # possessions stop mattering at the gun
    # leads leak: E[final] = margin*(1 - LEAK*rem) + pregame*rem; possession ~ +0.9 validated
    f = -(margin * (1 - LEAK * rem) + POSS * ep + (-S0) * rem)
    return round(f * 2) / 2   # spreads only bet in half points, so fair (and edge) are too

def best_spread(row, bet, since=0):
    """(book, line on the bet side, price) with the most points, then the better price; None if no
    book is usable. row: {book: [home points, home price, away price, updated]}, this poll's quotes."""
    home = bet == "home"
    opts = [(q[0] if home else -q[0], q[1] if home else q[2], b) for b, q in (row or {}).items()
            if q[3] >= since and (q[1] if home else q[2]) is not None]
    if not opts:
        return None
    line, price, book = max(opts, key=lambda o: o[:2])
    return book, line, price

def top_third(week):
    """Teams in the top third by win% going into `week`: 2+ games played, ties share a rank, same
    as analysis/team_shapes.py ratings. Finished regular-season games only."""
    rec = {}
    sep = "&" if "?" in ESPN else "?"
    for wk in range(1, week):
        for ev in get(f"{ESPN}{sep}week={wk}&seasontype=2").get("events", []):
            if ev["status"]["type"]["state"] != "post":
                continue
            a, b = ev["competitions"][0]["competitors"]
            for x, y in ((a, b), (b, a)):
                r = rec.setdefault(x["team"]["abbreviation"], [0.0, 0])
                sx, sy = int(x["score"]), int(y["score"])
                r[0] += 1 if sx > sy else .5 if sx == sy else 0
                r[1] += 1
    pct = {t: w / n for t, (w, n) in rec.items() if n >= 2}
    v, n = list(pct.values()), len(pct)
    if n <= 8:
        return set()
    return {t for t, p in pct.items()
            if 1 - 2 * (sum(x > p for x in v) + .5 * (sum(x == p for x in v) - 1)) / (n - 1) > 1 / 3}

def nfl_side(pregame):
    """Lead call filter by leader sign (+1 home): the trailing team was the pregame favorite. A pick'em doesn't count."""
    return lambda sign: None if pregame is None else sign * pregame > 0

def cfb_side(hrank, arank):
    """Lead call filter by leader sign (+1 home): the leader is unranked and the trailing team is ranked."""
    def ok(sign):
        if hrank is None or arank is None:
            return None
        lead, trail = (hrank, arank) if sign > 0 else (arank, hrank)
        return lead > 25 and trail <= 25
    return ok

def lead_call(h, top, side_ok, margin, gc, scored, now, poll, quote):
    """The trailing team's bet ('home'/'away') the moment the lead call fires, else None; state in
    h["lead11"]. Only the first 11+ lead counts, and it has to be seen happening: no prior poll close
    to this one means an old lead, not a new one. top: both teams top third, None while that's unknown.
    side_ok: nfl_side or cfb_side, False blocks the call and None waits.
    The score has to stick for a poll and the quote has to be newer than it; a lead back under 11
    before that ends it, as it did in the backtest."""
    st = h.get("lead11")
    if st is None:
        if abs(margin) < LEAD_CALL or gc >= 3600:
            return None
        prior = h["snaps"][:-1]   # snaps[-1] is this poll
        seen = any(abs(s[2]) >= LEAD_CALL for s in prior)
        blind = not prior or gc - prior[-1][0] > LEAD_MAX_GAP
        why = "seen" if seen else "no_history" if blind else "late" if gc >= LEAD_LATEST else None
        st = h["lead11"] = {"t": now, "gc": gc, "margin": margin, "sign": 1 if margin > 0 else -1, "done": bool(why), "why": why}
    if st["done"]:
        return None
    fits = side_ok(st["sign"])
    why = ("not_top" if top is False else "side_rule" if fits is False else "lead_gone" if st["sign"] * margin < LEAD_CALL
           else "timeout" if now - st["t"] > LEAD_MAX_WAIT else None)
    if why:
        st.update(done=True, why=why)   # archived: blocked leads get graded later from h["quotes"]
        return None
    if top is None or fits is None or scored or not quote or quote["updated"] < st["t"] - poll:
        return None
    st.update(done=True, why="called")
    return "away" if st["sign"] > 0 else "home"

def cover(call, hs, as_):
    """'win' / 'loss' / 'push' for a settled call; call line is on the bet side."""
    m = (hs - as_ if call["bet"] == "home" else as_ - hs) + call["line"]
    return "win" if m > 0 else "loss" if m < 0 else "push"

def backfill_fair(h):
    """Snaps saved before fair was stored (4 fields) get it rebuilt from their own
    margin and situation text ("GB ball · CAR 26 · 1st & 10"). A no-op once filled."""
    meta = h.get("meta") or {}
    for i, sn in enumerate(h["snaps"]):
        if len(sn) >= 5:
            continue
        gc, L, margin, sit = sn[:4]
        side = yl = None
        if sit:
            parts = sit.split(" · ")
            ab = parts[0].removesuffix(" ball")
            side = "home" if ab == meta.get("habbr") else "away" if ab == meta.get("aabbr") else None
            m = re.fullmatch(r"(?:(\S+) )?(\d+)", parts[1]) if len(parts) > 1 else None
            yl = 50 if not m else int(m.group(2)) if m.group(1) in (None, ab) else 100 - int(m.group(2))
        f = fair_spread(h.get("pregame"), margin, gc, side, yl if yl is not None else 50)
        h["snaps"][i] = (gc, L, margin, sit, None if f is None else round(f, 1))

def merge_json(path, updates):
    d = json.load(open(path)) if os.path.exists(path) else {}
    d.update(updates)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(d, open(path, "w"))

def reg_entry(ev, week):
    comp = ev["competitions"][0]
    t = {c["homeAway"]: c for c in comp["competitors"]}
    st = ev["status"]["type"]["state"]
    score = lambda side: int(t[side]["score"]) if st != "pre" and t[side].get("score") not in (None, "") else None
    rank = lambda side: (t[side].get("curatedRank") or {}).get("current") or 99
    return {"week": week, "date": ev["date"], "state": st, "hrank": rank("home"), "arank": rank("away"),
            "detail": ev["status"]["type"].get("shortDetail"),
            "aabbr": t["away"]["team"]["abbreviation"], "habbr": t["home"]["team"]["abbreviation"],
            "alogo": t["away"]["team"].get("logo"), "hlogo": t["home"]["team"].get("logo"),
            "hid": t["home"]["id"], "aid": t["away"]["id"], "as": score("away"), "hs": score("home"),
            "pregame": parse_pregame(comp.get("odds"), t["home"]["team"]["abbreviation"])}

def finals_of(sb):
    """Finished games on a scoreboard: date, and [team id, abbr, AP rank that week, score] per side."""
    out = []
    for ev in sb.get("events", []):
        if ev["status"]["type"]["state"] != "post":
            continue
        t = {c["homeAway"]: c for c in ev["competitions"][0]["competitors"]}
        side = lambda s: [t[s]["id"], t[s]["team"]["abbreviation"], (t[s].get("curatedRank") or {}).get("current") or 99, int(t[s].get("score") or 0)]
        out.append({"date": ev["date"], "h": side("home"), "a": side("away")})
    return out

def watch_spot(r, finals):
    """College watch, no bet: a pregame underdog off a loss in a game with a ranked team (analysis/cfb_spots.py:
    97-64 ATS on 2025-26, about 6% by luck). Previous game goes by date: week numbers fold week 0 into week 1."""
    S0 = r.get("pregame")
    if not S0:
        return None
    side = "home" if S0 > 0 else "away"
    tid = r["hid"] if side == "home" else r.get("aid")
    prior = [f for f in finals if f["date"] < r["date"] and tid in (f["h"][0], f["a"][0])]
    if not prior:
        return None
    f = max(prior, key=lambda f: f["date"])
    me, you = (f["h"], f["a"]) if f["h"][0] == tid else (f["a"], f["h"])
    if me[3] >= you[3] or min(me[2], you[2]) > 25:
        return None
    return {"side": side, "team": r["habbr"] if side == "home" else r["aabbr"], "line": abs(S0),
            "opp": you[1], "last": f"{me[3]}-{you[3]}", "rank": me[2] if me[2] <= 25 else None, "opp_rank": you[2] if you[2] <= 25 else None}

def fetch_summary(espn_id):
    """ESPN game summary (scoring plays, drive log, box score); {} on failure."""
    try:
        return get(f"https://site.api.espn.com/apis/site/v2/sports/football/{ESPN_PATH}/summary?event={espn_id}")
    except Exception:
        return {}


def latest_poss(s, home_id):
    """Who has the ball after the last logged play: (side, yards from own goal, 'KENN 25', tag).
    Every play's end state names the team with the ball (a punt or turnover flips it at once,
    muffs included; ESPN sometimes leaves a turnover on downs with the offense, flipped here). A scored drive means the kickoff is pending: the team
    scored on starts from its 25 (after a safety, the scorer); at the half, the team that kicked
    off the game receives. Tag 'kickoff' for both. None when nothing is logged."""
    try:
        drives = s.get("drives", {})
        prev = drives.get("previous") or []
        d = drives.get("current") if (drives.get("current") or {}).get("plays") else prev[-1]
        p = d["plays"][-1]
        ab = {c["id"]: c["team"]["abbreviation"] for c in s["header"]["competitions"][0]["competitors"]}
        mine = str(d["team"]["id"]); other = next(i for i in ab if i != mine)
        res = (d.get("result") or "").upper()
        side = lambda t: "home" if t == str(home_id) else "away"
        end = p.get("end") or {}
        if res == "END OF HALF" or ((p.get("type") or {}).get("text") == "End Period" and p["period"]["number"] == 2):
            rec = next(i for i in ab if i != str(prev[0]["team"]["id"]))   # opening kicker receives
            return (side(rec), 25, f"{ab[rec]} 25", "kickoff")
        if d.get("isScore") or p.get("scoringPlay") or res in ("TD", "FG") or res.endswith(" TD"):
            if res in ("SF", "SAFETY") or "safety" in p.get("text", "").lower():
                rec = other
            elif p.get("scoringPlay"):
                rec = next(i for i in ab if i != str((end.get("team") or {}).get("id") or mine))
            else:
                rec = other if res in ("TD", "FG") else mine   # 'INT TD' etc: defense scored
            return (side(rec), 25, f"{ab[rec]} 25", "kickoff")
        team = str((end.get("team") or {}).get("id") or mine)
        m = re.fullmatch(r"(\S+) (\d+)", end.get("possessionText") or "")
        yl = 50 if not m else int(m.group(2)) if m.group(1) == ab.get(team) else 100 - int(m.group(2))
        if res == "DOWNS" and team == mine:
            team, yl = other, 100 - yl
        return (side(team), yl, end.get("possessionText"), None)
    except (KeyError, IndexError, StopIteration, TypeError):
        return None

def espn_events(s, home_id):
    """Scores + momentum plays as (gc, label, side, text, hs, as, cls) from a fetch_summary().
    cls: 'score' | 'to' (turnover) | 'mom' (4th-down conversion / explosive)."""
    out = []
    try:
        for p in s.get("scoringPlays", []):
            gc = clock_elapsed(p["period"]["number"], p.get("clock", {}).get("displayValue"))
            text = p.get("text", "")
            kind = p.get("scoringType", {}).get("displayName", "score")
            low = (kind + " " + text).lower()
            cls = "score"
            is_td = "touchdown" in low
            if is_td and ("interception" in low or "fumble" in low):
                kind, cls = "DEF TD", "to"
            elif is_td and (m := re.search(r"(\d+)\s*y(ar)?d", low)) and int(m.group(1)) >= 30:
                kind, cls = f"⚡ {kind}", "mom"   # explosive TD, never a field goal
            side = "home" if str(p.get("team", {}).get("id")) == str(home_id) else "away"
            out.append([gc, kind, side, text[:120], p.get("homeScore"), p.get("awayScore"), cls])
        # player stat lines, to grade how meaningful an injured player is
        stats = {}
        for tp in s.get("boxscore", {}).get("players", []):
            for cat in tp.get("statistics", []):
                labels = [l.upper() for l in cat.get("labels", [])]
                for a in cat.get("athletes", []):
                    row = dict(zip(labels, a.get("stats", [])))
                    nm = a["athlete"]["displayName"]
                    try:
                        yds = float(row.get("YDS", 0) or 0)
                    except ValueError:
                        yds = 0
                    e = stats.setdefault(nm, {"yds": 0.0})
                    e["yds"] += abs(yds)
        # momentum plays from the drive log (non-scoring)
        drives = s.get("drives", {})
        for d in (drives.get("previous", []) + ([drives["current"]] if drives.get("current") else [])):
            off_home = str(d.get("team", {}).get("id", "")) == str(home_id)
            for pl in d.get("plays", []):
                if pl.get("scoringPlay"):
                    continue
                t = pl.get("type", {}).get("text", "")
                text = pl.get("text", "")
                gc = clock_elapsed(pl.get("period", {}).get("number", 1),
                                   pl.get("clock", {}).get("displayValue"))
                yds = pl.get("statYardage", 0) or 0
                start = pl.get("start", {})
                kind = cls = None
                if "Interception" in t or "Fumble Recovery (Opponent)" in t:
                    kind, cls, flip = "TURNOVER", "to", True
                elif t == "Turnover on Downs" or "Downs" in t and start.get("down") == 4:
                    kind, cls, flip = "4th DOWN STOP", "to", True
                elif start.get("down") == 4 and yds >= (start.get("distance") or 99) \
                        and "punt" not in t.lower() and "field goal" not in t.lower() \
                        and "kick" not in t.lower() and ("Pass" in t or "Rush" in t or "Sack" in t):
                    kind, cls, flip = "4th DOWN CONV", "mom", False
                elif yds >= 30 and ("Pass" in t or "Rush" in t or "Reception" in t):
                    kind, cls, flip = f"⚡ {yds} yd play", "mom", False
                if not kind and "injur" in text.lower():
                    m = re.search(r"#?\d*\s*([A-Z][\w.'\-]*\.?\s?[A-Z][\w'\-]+)\s+(?:was\s+)?injured", text)
                    who = m.group(1) if m else None
                    yds_line = 0
                    if who:
                        last = who.split()[-1].lower()
                        for nm, st in stats.items():
                            if nm.split()[-1].lower() == last:
                                yds_line = max(yds_line, st["yds"])
                    grade = "KEY " if yds_line >= 25 else ""
                    kind, cls, flip = f"🚑 {grade}INJURY" + (f" ({who}, {yds_line:.0f} yds)" if who else ""), "inj", False
                if not kind:
                    continue
                side_home = (not off_home) if flip else off_home   # credit the gaining side
                out.append([gc, kind, "home" if side_home else "away", text[:120], None, None, cls])
        # the live drive shows up in both drives.previous and drives.current
        seen, uniq = set(), []
        for e in out:
            if (e[0], e[3]) not in seen:
                seen.add((e[0], e[3]))
                uniq.append(e)
        out = sorted(uniq, key=lambda e: e[0])
    except Exception:
        pass
    return out

def pickcenter_pregame(espn_id):
    """Pregame home spread from the game summary (scoreboard drops odds once live)."""
    try:
        s = get(f"https://site.api.espn.com/apis/site/v2/sports/football/college-football/summary?event={espn_id}")
        for p in s.get("pickcenter", []):
            if p.get("spread") is not None:
                return float(p["spread"])
    except Exception:
        pass
    return None

class CFB:
    def __init__(self, poll):
        self.poll = poll
        self.hist_path = f"{DATA}/hist_{SPORT}_spreads.json"
        self.hist = json.load(open(self.hist_path)) if os.path.exists(self.hist_path) else {}
        self.reg = json.load(open(REGISTRY)) if os.path.exists(REGISTRY) else {}
        self.finals, self.finals_t = {}, 0
        self.next_t = 0    # last fetch of next week's schedule
        self.bf_path = f"{DATA}/backfilled_{SPORT}.json"
        self.backfilled = set(json.load(open(self.bf_path))) if os.path.exists(self.bf_path) else set()
        self.status = {}
        self.tops = (None, set())
        self.calls_path = f'{DOCS}/{"spread" if SPORT == "nfl" else "cfb_spread"}_calls.json'   # each poller owns its file

    def save_call(self, call):
        d = json.load(open(self.calls_path)) if os.path.exists(self.calls_path) else []
        d = [c for c in d if c["id"] != call["id"]] + [call]
        json.dump(d, open(self.calls_path, "w"))

    def backfill(self, need, live):
        """Joined games after kickoff: rebuild the line + margin from kickoff using
        bulk historical snapshots (one request covers every game) and ESPN plays."""
        self.backfilled |= set(need)
        json.dump(sorted(self.backfilled), open(self.bf_path, "w"))
        kick = {gid: iso_ts(self.reg[gid]["date"]) for gid in need if gid in self.reg}
        if not kick:
            return
        timelines = {gid: espn_timeline(gid) for gid in kick}
        stop = time.time() - 240
        t = max(min(kick.values()) - 1500, stop - BACKFILL_MAX * 300)
        series = {gid: [] for gid in kick}
        for _ in range(BACKFILL_MAX):
            if t >= stop:
                break
            stamp = datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            try:
                d = get(f"{HIST_ODDS}?apiKey={KEY}&date={stamp}&bookmakers=draftkings,fanduel"
                        f"&markets=spreads&oddsFormat=american")
            except Exception as e:
                print("backfill fetch failed", stamp, repr(e))
                break
            snap_t = iso_ts(d.get("timestamp", stamp))
            for gid in kick:
                L = next((v for e in d.get("data", []) if (v := home_spread(e, live[gid]["home"])) is not None), None)
                done = [p for p in timelines[gid] if p[0] <= snap_t]
                if snap_t < kick[gid] or not done:
                    series[gid].append((0, L, 0, None))
                else:
                    series[gid].append((done[-1][1], L, done[-1][2] - done[-1][3], None))
            nxt = d.get("next_timestamp")
            t = iso_ts(nxt) if nxt else t + 300
        for gid, pts in series.items():
            h = self.hist.setdefault(gid, {"snaps": [], "pregame": live[gid]["pregame"]})
            first_live = h["snaps"][0][0] if h["snaps"] else float("inf")
            h["snaps"] = [p for p in pts if p[0] < first_live] + h["snaps"]
            pre = [p[1] for p in pts if p[0] == 0 and p[1] is not None]
            if h.get("pregame") is None and pre:
                h["pregame"] = pre[-1]   # last pre-kick quote = the closing line
        print(f"backfilled {len(series)} {SPORT} games")

    def update_registry(self, sb):
        """Merge this week's (and, every 30 min, next week's) schedule."""
        week = (sb.get("week") or {}).get("number")
        boards = [(sb, week)]
        if SPORT == "cfb" and week and (sb.get("season") or {}).get("type") == 2 and time.time() - self.finals_t > 1800:
            self.finals_t = time.time()
            try:   # past weeks once, this week every 30 min: free ESPN calls
                sep = "&" if "?" in ESPN else "?"
                for wk in range(1, week + 1):
                    if wk == week or wk not in self.finals:
                        self.finals[wk] = finals_of(get(f"{ESPN}{sep}week={wk}&seasontype=2"))
            except Exception as e:
                print("finals fetch failed", repr(e))
        finals = [f for fs in self.finals.values() for f in fs]
        if week and time.time() - self.next_t > 1800:
            self.next_t = time.time()
            stype = (sb.get("season") or {}).get("type", 2)
            sep = "&" if "?" in ESPN else "?"
            try:
                boards.append((get(f"{ESPN}{sep}week={week + 1}&seasontype={stype}"), week + 1))
            except Exception as e:
                print("next-week fetch failed", repr(e))
        for board, wk in boards:
            for ev in board.get("events", []):
                new = reg_entry(ev, (ev.get("week") or {}).get("number") or wk)
                old = self.reg.get(ev["id"], {})
                if new["pregame"] is None:
                    new["pregame"] = old.get("pregame")   # ESPN drops odds once a game starts
                new["has_hist"] = old.get("has_hist", False)
                w = old.get("watch")
                if finals and new["state"] == "pre":
                    w = watch_spot(new, finals)      # frozen at kickoff, so the closing line grades it
                if w and new["state"] == "post" and new["hs"] is not None and "result" not in w:
                    w.update(result=cover({"bet": w["side"], "line": w["line"]}, new["hs"], new["as"]), final=f'{new["as"]}-{new["hs"]}')
                if w:
                    new["watch"] = w
                self.reg[ev["id"]] = new

    def archive_finals(self):
        """Move finished games' line history into the weekly archive."""
        done = {}
        for gid in list(self.hist):
            r = self.reg.get(gid)
            if not r or r["state"] != "post":
                continue
            if self.hist[gid].get("call") and (r["hs"] is None or r["as"] is None):
                continue   # a call has to settle: wait for ESPN to post the final
            h = self.hist.pop(gid)
            events = espn_events(fetch_summary(gid), r["hid"]) or h.get("events", [])
            if h.get("call"):
                h["call"].update(result=cover(h["call"], r["hs"], r["as"]), final=f'{r["as"]}-{r["hs"]}')
                self.save_call(h["call"])
            done.setdefault(r["week"], {})[gid] = {
                "meta": h.get("meta"), "pregame": h.get("pregame"), "snaps": h.get("snaps", []),
                "events": events, "final": f'{r["as"]}-{r["hs"]}', "call": h.get("call"),
                "date": r.get("date"), "quotes": h.get("quotes", []), "lead11": h.get("lead11"), "top": h.get("top")}
            r["has_hist"] = True
        for wk, recs in done.items():
            merge_json(f"{ARCHIVE}/{SPORT}_w{wk}.json", recs)
            print(f"archived {len(recs)} {SPORT} games to week {wk}")

    def cycle(self):
        rows = []
        sb = get(ESPN)
        self.update_registry(sb)
        self.archive_finals()
        live = {}
        for ev in sb.get("events", []):
            st = ev["status"]
            if st["type"]["state"] != "in":
                continue
            comp = ev["competitions"][0]
            t = {c["homeAway"]: c for c in comp["competitors"]}
            sit = comp.get("situation", {})
            ids = {c["id"]: c["homeAway"] for c in comp["competitors"]}
            live[ev["id"]] = {
                "poss_side": ids.get(sit.get("possession")),   # 'home'/'away'/None
                "yl": sit.get("yardLine"),                      # yards from own goal
                "poss_text": sit.get("possessionText"),
                "dd": sit.get("shortDownDistanceText") or sit.get("downDistanceText"),
                "hlogo": t["home"]["team"].get("logo"), "alogo": t["away"]["team"].get("logo"),
                "hcolor": t["home"]["team"].get("color"), "acolor": t["away"]["team"].get("color"),
                "hid": t["home"]["id"],
                "home": t["home"]["team"]["displayName"], "away": t["away"]["team"]["displayName"],
                "habbr": t["home"]["team"]["abbreviation"], "aabbr": t["away"]["team"]["abbreviation"],
                "hs": int(t["home"]["score"]), "as": int(t["away"]["score"]),
                "gc": clock_elapsed(st["period"], st.get("displayClock")),
                "clock": f'Q{st["period"]} {st.get("displayClock", "")}',
                "pregame": parse_pregame(comp.get("odds"), t["home"]["team"]["abbreviation"]),
            }
        if not live:
            return rows
        week = (sb.get("week") or {}).get("number")
        if self.tops[0] != week:
            try:
                self.tops = (week, top_third(week) if (sb.get("season") or {}).get("type") == 2 else set())
            except Exception as e:
                print("top-third fetch failed", repr(e))
        need = [gid for gid, g in live.items() if g["gc"] > 300 and gid not in self.backfilled]
        if need:
            try:
                self.backfill(need, live)
            except Exception as e:
                print("backfill error", repr(e))
        quotes = {}   # odds home_team name -> home spread (FD pref, fresh quotes only)
        detail = {}   # same key -> {home, away: price, updated: unix time}, for the lead call
        books = {}    # same key -> {book: [home points, home price, away price, updated]}, archived per poll
        fresh_cut = (datetime.datetime.now(datetime.timezone.utc)
                     - datetime.timedelta(minutes=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for e in get(f"{ODDS}?apiKey={KEY}&bookmakers=draftkings,fanduel&markets=spreads&oddsFormat=american"):
            for b in e.get("bookmakers", []):
                if b.get("last_update", "") < fresh_cut:
                    continue   # suspended market = stale line, not an edge
                for m in b.get("markets", []):
                    for o in m.get("outcomes", []):
                        if o["name"] == e["home_team"] and o.get("point") is not None:
                            other = next((x for x in m["outcomes"] if x["name"] != e["home_team"]), {})
                            books.setdefault(e["home_team"], {})[b["key"]] = [
                                float(o["point"]), o["price"], other.get("price"), iso_ts(m.get("last_update") or b["last_update"])]
                            if b["key"] == "fanduel" or e["home_team"] not in quotes:
                                quotes[e["home_team"]] = float(o["point"])
                                detail[e["home_team"]] = {"home": o["price"], "away": other.get("price"),
                                                          "updated": iso_ts(m.get("last_update") or b["last_update"])}
        for gid, g in live.items():
            L = next((v for k, v in quotes.items() if g["home"] in k or k in g["home"]), None)
            h = self.hist.setdefault(gid, {"snaps": [], "pregame": g["pregame"]})
            h["t"] = time.time()
            h["meta"] = {"game": f'{g["aabbr"]} @ {g["habbr"]}', "habbr": g["habbr"],
                         "aabbr": g["aabbr"], "hlogo": g["hlogo"], "alogo": g["alogo"],
                         "hcolor": g.get("hcolor"), "acolor": g.get("acolor")}
            summ = fetch_summary(gid)
            h["events"] = espn_events(summ, g["hid"]) or h.get("events", [])
            if gid in self.reg:
                self.reg[gid]["has_hist"] = True
            if h["pregame"] is None:
                h["pregame"] = g["pregame"] if g["pregame"] is not None else pickcenter_pregame(gid)
            S0 = h["pregame"]
            margin = g["hs"] - g["as"]
            pp = latest_poss(summ, g["hid"])   # play log first: handles punts, turnovers, pending kickoffs
            if pp:
                h["poss"] = (*pp[:3], margin)
            elif g["poss_side"]:   # scoreboard situation, then the cached one
                h["poss"] = (g["poss_side"], g["yl"] if g["yl"] is not None else 50,
                             g["poss_text"], margin)
            p = h.get("poss")
            if p and len(p) > 3 and p[3] != margin:
                p = None   # cached possession from before a score: the side already banked
                           # those points, don't also count its EP
            side, yl, ptxt = (p or (None, 50, None))[:3]
            dd = "kickoff" if pp and pp[3] else g.get("dd")
            # points banked since the last poll: the play log can lag the scoreboard
            # (10/30 TULN@CLT fired two losing EDGEs on pre-TD possession), and a
            # score flicker is one poll wide -- no EP and no flag until the score sticks
            prev = h["snaps"][-1] if h["snaps"] else None
            scored = prev is not None and prev[2] != margin
            fair = fair_spread(S0, margin, g["gc"], None if scored else side, yl)
            edge = None if (fair is None or L is None) else round(L - fair, 1)
            now_gc = g["gc"]
            sit_now = None
            if side:
                ab = g["habbr"] if side == "home" else g["aabbr"]
                sit_now = f'{ab} ball' + (f' · {ptxt}' if ptxt else "") + (f' · {dd}' if dd else "")
            backfill_fair(h)
            h["snaps"].append((now_gc, L, margin, sit_now, None if fair is None else round(fair, 1)))
            h["snaps"] = h["snaps"][-200:]
            # every poll, kept apart from snaps so their shape stays put: wall clock, scores, each book's price
            h.setdefault("quotes", []).append((round(time.time()), g["gc"], g["as"], g["hs"],
                                               next((v for k, v in books.items() if g["home"] in k or k in g["home"]), None)))
            status = "EDGE" if (not scored and edge is not None and abs(edge) >= EDGE_FLAG) else "watch"
            lbl = f'{g["aabbr"]} @ {g["habbr"]}'
            q = next((v for k, v in detail.items() if g["home"] in k or k in g["home"]), None)
            top = None if self.tops[0] != week else g["habbr"] in self.tops[1] and g["aabbr"] in self.tops[1]
            h["top"] = top
            rr = self.reg.get(gid, {})
            bet = lead_call(h, top, nfl_side(S0) if SPORT == "nfl" else cfb_side(rr.get("hrank"), rr.get("arank")),
                            margin, g["gc"], scored, time.time(), self.poll, q if L is not None else None)
            if bet and SPORT == "cfb":
                bet = "home" if bet == "away" else "away"   # the college rule takes the leader
            pick = best_spread(h["quotes"][-1][4], bet, h["lead11"]["t"] - self.poll) if bet else None
            if pick:
                book, line, price = pick
                ab = g["habbr"] if bet == "home" else g["aabbr"]
                h["call"] = {"id": gid, "game": lbl, "week": self.reg.get(gid, {}).get("week"), "bet": bet, "team": ab,
                             "line": line, "price": price, "book": book, "clock": g["clock"], "gc": g["gc"], "t": time.time(),
                             "score": f'{g["as"]}-{g["hs"]}', "pregame": S0, "fair": fair, "result": None, "final": None,
                             "books": h["quotes"][-1][4]}
                self.save_call(h["call"])
                push.queue(f'Bet {ab} {line:+g} ({price:+d}) on {push.BOOK[book]}', f'{"Top-vs-top" if SPORT == "nfl" else "Unranked leader"}, 11+ lead · {g["aabbr"]} {g["as"]}–{g["habbr"]} {g["hs"]}, {g["clock"]}',
                           tag=f"{SPORT}call|{gid}", url=f"./#{SPORT}/live")
                notify(f"{SPORT.upper()} CALL: {lbl}", f'{ab} {line:+g} ({price:+d}) {push.BOOK[book]} | {g["as"]}-{g["hs"]} {g["clock"]}')
            rows.append({"id": gid, "game": lbl, "habbr": g["habbr"], "aabbr": g["aabbr"],
                         "hlogo": g["hlogo"], "alogo": g["alogo"],
                         "poss": ptxt and f'{g["habbr"] if side == "home" else g["aabbr"]} ball, {ptxt}',
                         "poss_side": side,
                         "clock": g["clock"],
                         "score": f'{g["as"]}-{g["hs"]}', "pregame": S0, "line": L,
                         "fair": None if fair is None else round(fair, 1), "edge": edge,
                         "status": "CALL" if h.get("call") else status, "call": h.get("call"),
                         "side": None if edge is None else (g["habbr"] if edge > 0 else g["aabbr"])})
            if status != self.status.get(gid) and status != "watch":
                notify(f"{SPORT.upper()} {status}: {lbl}",
                       f'live {L} vs fair {fair:.1f} | {g["as"]}-{g["hs"]} {g["clock"]}')
                with open(f"{DATA}/{SPORT}_spread_log.csv", "a") as lf:
                    csv.writer(lf).writerow([datetime.datetime.now(datetime.timezone.utc).isoformat(), lbl, g["clock"],
                                             f'{g["as"]}-{g["hs"]}', S0, L, fair, edge, status])
            self.status[gid] = status
        return rows

    def run(self, once=False):
        while True:
            try:
                rows = self.cycle()
            except Exception as e:
                print("cycle error", repr(e))
                rows = []
            rows.sort(key=lambda r: (not r.get("call"), -(abs(r["edge"]) if r["edge"] is not None else -1)))
            json.dump({"updated": datetime.datetime.now().strftime("%H:%M:%S"),
                       "ts": time.time(),
                       "edge_flag": EDGE_FLAG, "games": rows},
                      open(f"{DOCS}/{STATE}", "w"))
            # finals are archived each cycle; this only drops games ESPN stopped listing
            cut = time.time() - 3 * 86400
            self.hist = {k: v for k, v in self.hist.items() if v.get("t", 0) > cut}
            json.dump(self.hist, open(self.hist_path, "w"))
            json.dump(self.reg, open(REGISTRY, "w"))
            json.dump({k: {"meta": v.get("meta"), "pregame": v.get("pregame"),
                           "snaps": v.get("snaps", []), "events": v.get("events", [])}
                       for k, v in self.hist.items() if v.get("meta")},
                      open(f"{DOCS}/{PUBHIST}", "w"))
            print(f'{datetime.datetime.now():%H:%M:%S} {len(rows)} live {SPORT.upper()} games, '
                  f'{sum(1 for r in rows if r["status"]!="watch")} flagged')
            if once:
                break
            time.sleep(self.poll)

def check_poss(eids):
    """Replay finished games: after each drive's last play, does latest_poss name the team
    (and spot) that actually started the next drive?"""
    side_ok = n = 0; err_k, err_p = [], []
    for eid in eids:
        s = fetch_summary(eid)
        hid = next(c["id"] for c in s["header"]["competitions"][0]["competitors"] if c["homeAway"] == "home")
        ab = {c["id"]: c["team"]["abbreviation"] for c in s["header"]["competitions"][0]["competitors"]}
        prev = s["drives"]["previous"]
        for i, (d, nxt) in enumerate(zip(prev, prev[1:])):
            if not d.get("plays") or not nxt.get("plays") or nxt["plays"][0]["period"]["number"] > 4:
                continue
            fake = {"header": s["header"], "drives": {"current": d, "previous": prev[:i + 1]}}
            got = latest_poss(fake, hid)
            want_side = "home" if nxt["team"]["id"] == hid else "away"
            m = re.fullmatch(r"(\S+) (\d+)", nxt["start"].get("text") or "")
            want_yl = 50 if not m else int(m.group(2)) if m.group(1) == ab[nxt["team"]["id"]] else 100 - int(m.group(2))
            n += 1
            if got and got[0] == want_side:
                side_ok += 1
                (err_k if got[3] else err_p).append(abs(got[1] - want_yl))
            else:
                print(f"  miss {eid}: after {d['result']} got {got}, next drive {ab[nxt['team']['id']]} at {nxt['start'].get('text')}")
    print(f"{n} drive boundaries in {len(eids)} games: side right {side_ok}/{n}")
    print(f"  spot error, plays (punts/turnovers/downs): mean {sum(err_p)/max(1,len(err_p)):.1f} yds over {len(err_p)}")
    print(f"  spot error, pending kickoff (assumed own 25): mean {sum(err_k)/max(1,len(err_k)):.1f} yds over {len(err_k)}")
    assert side_ok / n > 0.97, "possession side from the play log should almost always match the next drive"

if __name__ == "__main__":
    if "--check-poss" in sys.argv:
        check_poss(sys.argv[sys.argv.index("--check-poss") + 1:])
        sys.exit()
    poll = int(sys.argv[sys.argv.index("--poll") + 1]) if "--poll" in sys.argv else 180
    CFB(poll).run(once="--once" in sys.argv)
