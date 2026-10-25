#!/usr/bin/env python3
"""Live prop monitor: polls the-odds-api (quotes) + ESPN (live stats), scores
every quoted player with the v2 model, writes web/state.json for the dashboard,
sends macOS notifications on NEAR/TRIGGER transitions, logs triggers.

Usage:  python3 poller.py [--poll 300] [--demo]
        --demo: one cycle of synthetic data (no API calls), for testing the UI.

Needs live/model_weights.json + live/priors.json (run analysis/export_live.py).
Costs 2 credits per live game per poll (2 markets, DK+FD).
"""
import json, os, sys, time, csv, math, bisect, datetime, subprocess, threading, re
import urllib.request, functools
from http.server import HTTPServer, SimpleHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
KEY = os.environ.get("ODDS_API_KEY") or \
    open(os.path.join(HERE, "..", ".env")).read().split("ODDS_API_KEY=")[1].strip()
ODDS = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl"
ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
MARKETS = {"rec": "player_reception_yds", "rush": "player_rush_yds"}
NEAR, TRIG = 0.50, 0.60
PORT = int(os.environ.get("PORT", 8766))
HOST = os.environ.get("HOST", "127.0.0.1")
DOCS = os.environ.get("DOCS_DIR", f"{HERE}/../docs")   # served site + published state
DATA = os.environ.get("DATA_DIR", HERE)                # private state (history, logs)

W = json.load(open(f"{HERE}/model_weights.json"))
PRIORS = json.load(open(f"{HERE}/priors.json"))     # name -> {kind:{ypg,ype,epg,pos}}
DEF = json.load(open(f"{HERE}/def_priors.json"))    # "kind|team|pos" -> ypg allowed

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

def score_row(x):
    z = [(x[i] - W["mu"][i]) / W["sd"][i] for i in range(len(x))]
    s = sum(zi * wi for zi, wi in zip(z, W["w"])) + W["b"]
    return 1 / (1 + math.exp(-s))

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
        self.status = {}    # opp id -> NEAR/TRIGGER

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
        glen = 3600.0
        ef = min(gc / glen, 0.99)
        rem_frac = 1 - ef
        hkey = f'{game["espn_id"]}|{name}|{kind}'
        h = self.hist.setdefault(hkey, {"l0": L, "last_change": 0 if cum == 0 else gc, "snaps": []})
        if h["snaps"] and cum > h["snaps"][-1][1]:
            h["last_change"] = gc
        h["snaps"].append((gc, cum, nev, L))
        h["snaps"] = h["snaps"][-120:]
        old = next((s for s in reversed(h["snaps"]) if s[0] <= gc - 600), (0, 0, 0, None))
        e10, y10 = nev - old[2], cum - old[1]
        home = team == game["home"]
        diff = (game["hs"] - game["as"]) if home else (game["as"] - game["hs"])
        ts_ = tstats.get(team, {"pass_att": 0, "rush_att": 0, "pass_yds": 0, "rush_yds": 0})
        plays = ts_["pass_att"] + ts_["rush_att"]
        opp = game["away"] if home else game["home"]
        opp_abbr = game["away_abbr"] if home else game["home_abbr"]
        dv = DEF.get(f'{kind}|{opp_abbr}|{pr["pos"]}', 0)
        needed = L - cum
        if needed < 3 or L < 15:
            return None
        x = [ef, pr["ypg"], pr["ype"], dv, nev, cum, (gc - h["last_change"]) / 60,
             e10, y10, diff, diff * ef,
             ts_["pass_att"] / plays if plays else 0.5,
             ts_["pass_yds"] / ts_["pass_att"] if ts_["pass_att"] else 0,
             ts_["rush_yds"] / ts_["rush_att"] if ts_["rush_att"] else 0,
             plays / (gc / 60), 1.0 if kind == "rush" else 0.0,
             needed, needed / (pr["ypg"] * rem_frac + 1), L / h["l0"]]
        return x, cum, nev, (gc - h["last_change"]) / 60, opp, hkey

    def archive_finals(self):
        """Move tracked players from finished games into docs/archive/props_w{week}.json."""
        done = {}
        for hkey in list(self.hist):
            wk = self.finished.get(hkey.split("|")[0])
            h = self.hist[hkey]
            if wk is None or not h.get("meta"):
                continue
            self.hist.pop(hkey)
            last = h["snaps"][-1] if h["snaps"] else (0, 0, 0, None)
            done.setdefault(wk, {})[hkey] = {**h["meta"], "snaps": h["snaps"], "l0": h.get("l0"),
                                             "final": last[1], "close": last[3]}
        for wk, recs in done.items():
            path = f"{DOCS}/archive/props_w{wk}.json"
            d = json.load(open(path)) if os.path.exists(path) else {}
            d.update(recs)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            json.dump(d, open(path, "w"))
            print(f"archived {len(recs)} player props to week {wk}")

    def cycle(self):
        opps, games_out, histout = [], [], {}
        live = self.espn_live()
        self.archive_finals()
        if not live:
            return opps, games_out, histout
        odds_events = get(f"{ODDS}/events?apiKey={KEY}")
        for game in live.values():
            oe = next((e for e in odds_events
                       if game["home"] in e["home_team"] or e["home_team"] in game["home"]), None)
            games_out.append({"label": f'{game["away_abbr"]} @ {game["home_abbr"]}',
                              "score": f'{game["as"]}-{game["hs"]}', "clock": game["clock"],
                              "hlogo": game.get("hlogo"), "alogo": game.get("alogo")})
            if not oe:
                continue
            try:
                players, tstats = self.espn_players(game["espn_id"])
                quotes = self.odds_quotes(oe)
            except Exception as e:
                print("fetch error", game["home_abbr"], repr(e))
                continue
            for (name, kind), L in quotes.items():
                f = self.features(name, kind, L, game, players, tstats)
                if not f:
                    continue
                x, cum, nev, drought, opp, hkey = f
                p = score_row(x)
                oid = f'{name}|{kind}'
                pteam = players[name]["team"]
                logo = game.get("hlogo") if pteam == game["home"] else game.get("alogo")
                meta = {"player": name, "market": kind, "logo": logo,
                        "game": f'{game["away_abbr"]} @ {game["home_abbr"]}'}
                self.hist[hkey]["meta"] = meta
                histout[oid] = {**meta, "snaps": self.hist[hkey]["snaps"][-100:]}
                status = "TRIGGER" if p >= TRIG else "NEAR" if p >= NEAR else "watch"
                opps.append({"id": oid, "player": name, "market": kind, "logo": logo, "game": f'{game["away_abbr"]} @ {game["home_abbr"]}',
                             "clock": game["clock"], "line": L, "cum": cum, "events": nev,
                             "drought": round(drought), "p": round(p, 3), "status": status, "vs": opp})
                prev = self.status.get(oid)
                if status != prev and status in ("NEAR", "TRIGGER"):
                    notify(f"{'🔔 TRIGGER' if status=='TRIGGER' else 'Approaching'}: {name}",
                           f'{kind} O{L} | {cum} yds, P={p:.2f} | {game["clock"]}')
                    if status == "TRIGGER":
                        with open(f"{DATA}/paper_log.csv", "a") as lf:
                            csv.writer(lf).writerow([datetime.datetime.now(datetime.timezone.utc).isoformat(), name, kind,
                                                     L, cum, nev, round(p, 3), game["clock"]])
                self.status[oid] = status
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
