#!/usr/bin/env python3
"""CFB live spread monitor: ESPN scoreboard (score/clock/pregame line, free) +
the-odds-api live spreads (1 credit per poll for ALL live games, DK+FD).

Fair-line baseline: expected final home margin = current margin +
(-pregame_home_spread) * fraction of game remaining. Edge = live spread - fair
spread. Big |edge| = the market moved more (or less) than score+time justify —
e.g. a pick-6 swinging a -14 to -7.

ponytail: uncalibrated baseline; replace coefficients with a fitted model once
we have CFB historical lines. Also flags SWING when the line moved 4+ points in
~10 minutes beyond the score change.

Usage: python3 cfb_poller.py [--sport nfl|cfb] [--poll 180] [--once]
Writes docs/{cfb_state,nfl_spreads_state}.json (live), docs/{sport}_games.json
(schedule registry: pre/in/post for this week and next), and moves each game's
line history into docs/archive/{sport}_w{week}.json when it goes final.
Appends flags to live/{sport}_spread_log.csv.
"""
import json, os, sys, time, csv, re, datetime, subprocess, urllib.request

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
SWING_PTS = 4.0     # line move in ~10 min beyond score move

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
    return {"week": week, "date": ev["date"], "state": st,
            "detail": ev["status"]["type"].get("shortDetail"),
            "aabbr": t["away"]["team"]["abbreviation"], "habbr": t["home"]["team"]["abbreviation"],
            "alogo": t["away"]["team"].get("logo"), "hlogo": t["home"]["team"].get("logo"),
            "hid": t["home"]["id"], "as": score("away"), "hs": score("home"),
            "pregame": parse_pregame(comp.get("odds"), t["home"]["team"]["abbreviation"])}

def espn_events(espn_id, home_id):
    """Scores + momentum plays as (gc, label, side, text, hs, as, cls).
    cls: 'score' | 'to' (turnover) | 'mom' (4th-down conversion / explosive)."""
    out = []
    try:
        s = get(f"https://site.api.espn.com/apis/site/v2/sports/football/{ESPN_PATH}/summary?event={espn_id}")
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
        self.next_t = 0    # last fetch of next week's schedule
        self.bf_path = f"{DATA}/backfilled_{SPORT}.json"
        self.backfilled = set(json.load(open(self.bf_path))) if os.path.exists(self.bf_path) else set()
        self.status = {}

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
                self.reg[ev["id"]] = new

    def archive_finals(self):
        """Move finished games' line history into the weekly archive."""
        done = {}
        for gid in list(self.hist):
            r = self.reg.get(gid)
            if not r or r["state"] != "post":
                continue
            h = self.hist.pop(gid)
            events = espn_events(gid, r["hid"]) or h.get("events", [])
            done.setdefault(r["week"], {})[gid] = {
                "meta": h.get("meta"), "pregame": h.get("pregame"), "snaps": h.get("snaps", []),
                "events": events, "final": f'{r["as"]}-{r["hs"]}'}
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
        need = [gid for gid, g in live.items() if g["gc"] > 300 and gid not in self.backfilled]
        if need:
            try:
                self.backfill(need, live)
            except Exception as e:
                print("backfill error", repr(e))
        quotes = {}   # odds home_team name -> home spread (FD pref, fresh quotes only)
        fresh_cut = (datetime.datetime.now(datetime.timezone.utc)
                     - datetime.timedelta(minutes=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for e in get(f"{ODDS}?apiKey={KEY}&bookmakers=draftkings,fanduel&markets=spreads&oddsFormat=american"):
            for b in e.get("bookmakers", []):
                if b.get("last_update", "") < fresh_cut:
                    continue   # suspended market = stale line, not an edge
                for m in b.get("markets", []):
                    for o in m.get("outcomes", []):
                        if o["name"] == e["home_team"] and o.get("point") is not None:
                            if b["key"] == "fanduel" or e["home_team"] not in quotes:
                                quotes[e["home_team"]] = float(o["point"])
        for gid, g in live.items():
            L = next((v for k, v in quotes.items() if g["home"] in k or k in g["home"]), None)
            h = self.hist.setdefault(gid, {"snaps": [], "pregame": g["pregame"]})
            h["t"] = time.time()
            h["meta"] = {"game": f'{g["aabbr"]} @ {g["habbr"]}', "habbr": g["habbr"],
                         "aabbr": g["aabbr"], "hlogo": g["hlogo"], "alogo": g["alogo"],
                         "hcolor": g.get("hcolor"), "acolor": g.get("acolor")}
            h["events"] = espn_events(gid, g["hid"])
            if gid in self.reg:
                self.reg[gid]["has_hist"] = True
            if h["pregame"] is None:
                h["pregame"] = g["pregame"] if g["pregame"] is not None else pickcenter_pregame(gid)
            S0 = h["pregame"]
            margin = g["hs"] - g["as"]
            rem = max(0.0, 1 - g["gc"] / 3600)
            # possession value: EP ~ 0.065*yardline - 1.3 (own 20 ~ 0, midfield ~ 2,
            # opp 20 ~ 3.9). ponytail: linear EP; swap for a fitted curve at calibration
            if g["poss_side"]:
                h["poss"] = (g["poss_side"], g["yl"] if g["yl"] is not None else 50,
                             g["poss_text"])
            side, yl, ptxt = h.get("poss") or (None, 50, None)
            ep = (0.065 * yl - 1.3) if side else 0.0
            ep = ep if side != "away" else -ep
            ep *= min(1.0, rem * 8)   # possessions stop mattering at the gun
            # leads leak: pooled 2025-26 NFL drive-end fit (analysis/momentum.py) gives
            # E[final] = margin*(1 - 0.28*rem) + pregame*rem; possession ~ +0.9 validated
            # ponytail: NFL coefficient applied to CFB too; refit per sport when CFB data allows
            fair = None if S0 is None else -(margin * (1 - 0.28 * rem) + ep + (-S0) * rem)
            edge = None if (fair is None or L is None) else round(L - fair, 1)
            now_gc = g["gc"]
            old = next(((s[0], s[1], s[2]) for s in reversed(h["snaps"])
                        if s[0] <= now_gc - 600 and s[1] is not None), None)
            dline = dmarg = None
            if old and L is not None:
                dline, dmarg = round(L - old[1], 1), margin - old[2]
            sit_now = None
            if side:
                ab = g["habbr"] if side == "home" else g["aabbr"]
                sit_now = f'{ab} ball' + (f' · {ptxt}' if ptxt else "") + (f' · {g["dd"]}' if g.get("dd") else "")
            h["snaps"].append((now_gc, L, margin, sit_now))
            h["snaps"] = h["snaps"][-200:]
            swing = dline is not None and abs(dline) >= SWING_PTS and abs(dline + dmarg) >= SWING_PTS / 2
            status = "EDGE" if (edge is not None and abs(edge) >= EDGE_FLAG) else \
                     "SWING" if swing else "watch"
            lbl = f'{g["aabbr"]} @ {g["habbr"]}'
            rows.append({"id": gid, "game": lbl, "habbr": g["habbr"], "aabbr": g["aabbr"],
                         "hlogo": g["hlogo"], "alogo": g["alogo"],
                         "poss": ptxt and f'{g["habbr"] if side == "home" else g["aabbr"]} ball, {ptxt}',
                         "poss_side": side,
                         "clock": g["clock"],
                         "score": f'{g["as"]}-{g["hs"]}', "pregame": S0, "line": L,
                         "fair": None if fair is None else round(fair, 1), "edge": edge,
                         "dline10": dline, "status": status,
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
            rows.sort(key=lambda r: -(abs(r["edge"]) if r["edge"] is not None else -1))
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

if __name__ == "__main__":
    poll = int(sys.argv[sys.argv.index("--poll") + 1]) if "--poll" in sys.argv else 180
    CFB(poll).run(once="--once" in sys.argv)
