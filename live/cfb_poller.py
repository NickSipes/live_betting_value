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

Usage: python3 cfb_poller.py [--poll 180] [--once]
Writes docs/cfb_state.json; appends flags to live/cfb_log.csv.
"""
import json, os, sys, time, csv, re, datetime, subprocess, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
KEY = os.environ.get("ODDS_API_KEY") or \
    open(os.path.join(HERE, "..", ".env")).read().split("ODDS_API_KEY=")[1].strip()
ODDS = "https://api.the-odds-api.com/v4/sports/americanfootball_ncaaf/odds"
ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard?groups=80&limit=250"
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
        self.hist_path = f"{HERE}/cfb_history.json"
        self.hist = json.load(open(self.hist_path)) if os.path.exists(self.hist_path) else {}
        self.status = {}

    def cycle(self):
        rows = []
        sb = get(ESPN)
        live = {}
        for ev in sb.get("events", []):
            st = ev["status"]
            if st["type"]["state"] != "in":
                continue
            comp = ev["competitions"][0]
            t = {c["homeAway"]: c for c in comp["competitors"]}
            live[ev["id"]] = {
                "home": t["home"]["team"]["displayName"], "away": t["away"]["team"]["displayName"],
                "habbr": t["home"]["team"]["abbreviation"], "aabbr": t["away"]["team"]["abbreviation"],
                "hs": int(t["home"]["score"]), "as": int(t["away"]["score"]),
                "gc": clock_elapsed(st["period"], st.get("displayClock")),
                "clock": f'Q{st["period"]} {st.get("displayClock", "")}',
                "pregame": parse_pregame(comp.get("odds"), t["home"]["team"]["abbreviation"]),
            }
        if not live:
            return rows
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
            if h["pregame"] is None:
                h["pregame"] = g["pregame"] if g["pregame"] is not None else pickcenter_pregame(gid)
            S0 = h["pregame"]
            margin = g["hs"] - g["as"]
            rem = max(0.0, 1 - g["gc"] / 3600)
            fair = None if S0 is None else -(margin + (-S0) * rem)
            edge = None if (fair is None or L is None) else round(L - fair, 1)
            now_gc = g["gc"]
            old = next(((gc, sp, mg) for gc, sp, mg in reversed(h["snaps"])
                        if gc <= now_gc - 600 and sp is not None), None)
            dline = dmarg = None
            if old and L is not None:
                dline, dmarg = round(L - old[1], 1), margin - old[2]
            h["snaps"].append((now_gc, L, margin))
            h["snaps"] = h["snaps"][-200:]
            swing = dline is not None and abs(dline) >= SWING_PTS and abs(dline + dmarg) >= SWING_PTS / 2
            status = "EDGE" if (edge is not None and abs(edge) >= EDGE_FLAG) else \
                     "SWING" if swing else "watch"
            lbl = f'{g["aabbr"]} @ {g["habbr"]}'
            rows.append({"id": gid, "game": lbl, "clock": g["clock"],
                         "score": f'{g["as"]}-{g["hs"]}', "pregame": S0, "line": L,
                         "fair": None if fair is None else round(fair, 1), "edge": edge,
                         "dline10": dline, "status": status,
                         "side": None if edge is None else (g["habbr"] if edge > 0 else g["aabbr"])})
            if status != self.status.get(gid) and status != "watch":
                notify(f"CFB {status}: {lbl}",
                       f'live {L} vs fair {fair:.1f} | {g["as"]}-{g["hs"]} {g["clock"]}')
                with open(f"{HERE}/cfb_log.csv", "a") as lf:
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
                      open(f"{HERE}/../docs/cfb_state.json", "w"))
            json.dump(self.hist, open(self.hist_path, "w"))
            print(f'{datetime.datetime.now():%H:%M:%S} {len(rows)} live CFB games, '
                  f'{sum(1 for r in rows if r["status"]!="watch")} flagged')
            if once:
                break
            time.sleep(self.poll)

if __name__ == "__main__":
    poll = int(sys.argv[sys.argv.index("--poll") + 1]) if "--poll" in sys.argv else 180
    CFB(poll).run(once="--once" in sys.argv)
