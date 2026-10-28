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
from http.server import HTTPServer, SimpleHTTPRequestHandler

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

def call_gate(x, cum, L, dkey):
    """Week-7 review: every losing call was on a trailing team or a player whose
    volume never showed up. No call while down > 3 unless usage is already there,
    and never an over against a top-3 defense (going into the week)."""
    return dkey.rsplit("|", 1)[0] not in TOP3_DEF and x[9] >= -3 and (x[4] >= 3 or cum >= 0.6 * L)

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

def short_name(full):
    """'Brandon Lance Jr.' -> 'b.lance' (ESPN play text uses 'B.Lance')."""
    parts = [w for w in full.replace(".", " ").split() if w.lower() not in ("jr", "sr", "ii", "iii", "iv", "v")]
    return f"{parts[0][0]}.{parts[-1]}".lower() if len(parts) >= 2 else full.lower()

PASS_TO = re.compile(r"pass (?:incomplete )?(?:short |deep )?(?:left |right |middle )?to ([A-Z][\w'\-]*\.\s?[A-Z][\w'\-]+)")
ELIGIBLE = re.compile(r"[A-Z][\w.'\-]+ reported in as eligible\.\s*")
GAINED = re.compile(r"for (-?\d+) yards?|for (no gain)")
RUSHER = re.compile(r"^(?:\([^)]*\)\s*)*([A-Z][\w'\-]*\.\s?[A-Z][\w'\-]+)\s")

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
                    out.append((iso_ts(pl["wallclock"]), gc, "rec", m.group(1).replace(" ", "").lower(),
                                yds, t == "Pass Reception" or "Touchdown" in t))
            elif t.startswith("Rush") or t == "Rushing Touchdown":
                m = RUSHER.search(text)
                if m:
                    out.append((iso_ts(pl["wallclock"]), gc, "rush", m.group(1).replace(" ", "").lower(), yds, True))
    # ESPN sometimes repeats the live drive in previous + current
    return sorted(set(out)), sorted(set(team))

def espn_plays(espn_id):
    return espn_timeline(espn_id)[0]

def gc_label(gc):
    q = min(int(gc // 900) + 1, 4)
    rem = round(max(0, q * 900 - gc))
    return f"Q{q} {rem // 60}:{rem % 60:02d}"

def feature_vec(pr, kind, dv, gc, cum, nev, drought, e10, y10, diff, ts_, L, l0):
    """The model's 19 inputs; shared by live scoring and the P rebuild."""
    gc = max(gc, 60)
    ef = min(gc / 3600.0, 0.99)
    plays = ts_["pass_att"] + ts_["rush_att"]
    needed = L - cum
    return [ef, pr["ypg"], pr["ype"], dv, nev, cum, drought,
            e10, y10, diff, diff * ef,
            ts_["pass_att"] / plays if plays else 0.5,
            ts_["pass_yds"] / ts_["pass_att"] if ts_["pass_att"] else 0,
            ts_["rush_yds"] / ts_["rush_att"] if ts_["rush_att"] else 0,
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
        self.hist_path = f"{DATA}/history.json"
        self.hist = json.load(open(self.hist_path)) if os.path.exists(self.hist_path) else {}
        self.pl_path = f"{DATA}/pregame_lines.json"   # espn_id -> {"name|kind": closing line}
        self.pre_lines = json.load(open(self.pl_path)) if os.path.exists(self.pl_path) else {}
        self.bf_path = f"{DATA}/backfilled.json"
        self.backfilled = set(json.load(open(self.bf_path))) if os.path.exists(self.bf_path) else set()
        self.status = {}    # opp id -> NEAR/TRIGGER
        self.wl_lines_t = 0   # last watchlist line refresh (hourly; 2 credits a game)

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
                sn = short_name(name)
                mine = [p for p in done if p[2] == kind and p[3] == sn]
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
        players, tstats = {}, {}
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
        for bt in s.get("boxscore", {}).get("teams", []):
            tstats[bt["team"]["displayName"]] = team_stats(bt)
        return players, tstats

    def odds_quotes(self, odds_event):
        d = get(f"{ODDS}/events/{odds_event['id']}/odds?apiKey={KEY}"
                f"&bookmakers=draftkings,fanduel&markets={MARKETS['rec']},{MARKETS['rush']}"
                f"&oddsFormat=american")
        quotes = {}   # (name, kind) -> line (FD preferred)
        for b in d.get("bookmakers", []):
            for m in b.get("markets", []):
                kind = "rec" if m["key"] == MARKETS["rec"] else "rush"
                for o in m.get("outcomes", []):
                    if o["name"] != "Over" or o.get("point") is None:
                        continue
                    k = (o["description"], kind)
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
            mine = [q for q in pplays if q[2] == kind and q[3] == short_name(name)]

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
                          and (key != "call" or (j in xs and call_gate(xs[j], s[1], s[3], dkey)))), None)
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
            for key in ("call", "pre"):
                c = h.get(key)
                if not c:
                    continue
                if "result" not in c:
                    c.update(result="win" if final > c["line"] else "loss", settled_t=time.time())
                settled.append({"week": wk, "espn_id": eid, "game": h["meta"]["game"], "player": name,
                                "market": kind, "pregame_line": h.get("l0"), "final": final,
                                "settled": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                "type": "live", **c})
            done.setdefault(wk, {})[hkey] = {**h["meta"], "snaps": h["snaps"], "l0": h.get("l0"),
                                             "final": final, "close": last[3],
                                             "pmax": h.get("pmax", 0), "signal": h.get("signal"),
                                             "call": h.get("call"), "pre": h.get("pre")}
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

    def watchlist_extras(self):
        """Annotate Good Matchups with the injury report (every cycle, free) and the
        current pregame prop line (hourly, 2 credits per game, upcoming games only)."""
        try:
            wl = json.load(open(f"{DOCS}/watchlist.json"))
        except Exception:
            return
        entries = wl.get("entries", [])
        if not entries:
            return
        changed = False
        try:
            inj = espn_injuries()
            for w in entries:
                st = inj.get(short_name(w["player"]))
                if w.get("inj") != st:
                    w["inj"] = st   # None clears a healed player
                    changed = True
        except Exception as e:
            print("injury fetch error", repr(e))
        if time.time() - self.wl_lines_t > 3600:
            self.wl_lines_t = time.time()
            try:
                evs = get(f"{ODDS}/events?apiKey={KEY}")
                qbyg = {}
                for w in entries:
                    if w.get("kick_iso") and \
                            datetime.datetime.fromisoformat(w["kick_iso"]).timestamp() < time.time():
                        continue   # kicked off: the live board takes over
                    if w["game"] not in qbyg:
                        full = w.get("home_full", "")
                        oe = next((e for e in evs if full and
                                   (full in e["home_team"] or e["home_team"] in full)), None)
                        qbyg[w["game"]] = self.odds_quotes(oe) if oe else {}
                    L = next((v for (nm, kd), v in qbyg[w["game"]].items()
                              if kd == w["market"] and short_name(nm) == short_name(w["player"])), None)
                    if w.get("line") != L:
                        w["line"] = L
                        changed = True
            except Exception as e:
                print("watchlist line fetch error", repr(e))
        if changed:
            json.dump(wl, open(f"{DOCS}/watchlist.json", "w"), indent=1)
            print("watchlist extras updated (injuries/lines)")

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
                if p >= SIGNAL and "signal" not in h:   # first time the model liked the over
                    h["signal"] = {"clock": game["clock"], "line": L, "cum": cum, "p": round(p, 3), "rebuilt": False}
                if p >= CALL and "call" not in h and call_gate(x, cum, L, dkey):
                    h["call"] = {"clock": game["clock"], "gc": game["gc"], "line": L, "cum": cum,
                                 "p": round(p, 3), "rebuilt": False, "x": [round(v, 4) for v in x], "t": time.time(),
                                 "score": f'{game["away_abbr"]} {game["as"]} – {game["home_abbr"]} {game["hs"]}'}
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
                histout[oid] = {**meta, "snaps": self.hist[hkey]["snaps"]}
                status = ("TRIGGER" if p >= TRIG and call_gate(x, cum, L, dkey)
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
            for w in watchlist():
                if w.get("inj") in INJ_OUT:
                    continue   # injury report says no bet
                k = next((k for k in closing if k.endswith("|" + w["market"]) and
                          short_name(k.rsplit("|", 1)[0]) == short_name(w["player"])), None)
                if not k:
                    continue   # no pregame line = no bet was available
                name, kind = k.rsplit("|", 1)
                hkey = f'{game["espn_id"]}|{name}|{kind}'
                h = self.hist.setdefault(hkey, {"l0": closing[k], "l0_closing": True, "last_change": 0, "snaps": []})
                if not h.get("meta"):
                    team = ALIAS.get(w["team"], w["team"])
                    side = "hlogo" if team == game["home_abbr"] else "alogo" if team == game["away_abbr"] else None
                    h["meta"] = {"player": name, "market": kind, "logo": game.get(side) if side else None,
                                 "game": f'{game["away_abbr"]} @ {game["home_abbr"]}'}
                c = h.get("pre")
                if not c or (c.get("src") != "closing" and "result" not in c):
                    h["pre"] = c = {"type": "pregame", "src": "closing", "clock": "pregame", "gc": 0,
                                    "line": closing[k], "cum": 0, "p": None, "t": (c or {}).get("t", time.time()),
                                    "score": "kickoff", "rebuilt": False}
                cum = players.get(name, {}).get(kind, (0, 0))[0]
                if "result" not in c and cum > c["line"]:
                    c.update(result="win", won_at=game["clock"], won_yds=cum, settled_t=time.time())
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
            time.sleep(self.poll)

class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *a):   # the dashboard polls every 10s; don't log each fetch
        pass
    def end_headers(self):   # revalidate every load, so a deploy reaches open tabs and phones
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

def serve():
    HTTPServer((HOST, PORT), functools.partial(QuietHandler, directory=DOCS)).serve_forever()

if __name__ == "__main__":
    poll = int(sys.argv[sys.argv.index("--poll") + 1]) if "--poll" in sys.argv else 300
    if "--once" not in sys.argv:
        threading.Thread(target=serve, daemon=True).start()
        print(f"dashboard: http://{HOST}:{PORT}")
    Monitor(poll).run(demo="--demo" in sys.argv, once="--once" in sys.argv)
    if "--demo" in sys.argv:
        print("demo state written; serving dashboard (ctrl-c to stop)")
        while True:
            time.sleep(60)
