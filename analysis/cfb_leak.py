"""Fit the CFB lead-leak coefficient used by live/cfb_poller.py fair_spread.

Same form as the NFL fit (analysis/momentum.py), at every drive end:
  final - margin = -k * margin * rem + c * exp * rem
where margin = home - away now, rem = fraction of game left, exp = -(pregame home spread).
Data: ESPN scoreboard + summary (free) for completed FBS games, cached in data/cfb_drives.json.

Usage: python3 analysis/cfb_leak.py [season] [--refresh]"""
import json, os, sys, urllib.request
from concurrent.futures import ThreadPoolExecutor
import numpy as np

SEASON = next((int(a) for a in sys.argv[1:] if a.isdigit()), 2026)
CACHE = os.path.join(os.path.dirname(__file__), "..", "data", f"cfb_drives_{SEASON}.json")
B = "https://site.api.espn.com/apis/site/v2/sports/football/college-football"

def get(u):
    return json.load(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=20))

def game(eid):
    """(pregame home spread, final home margin, [(gc, margin)] at each drive end) or None."""
    try:
        s = get(f"{B}/summary?event={eid}")
        pc = s.get("pickcenter") or []
        sp = pc[0].get("spread") if pc else None
        comp = {c["homeAway"]: int(c["score"]) for c in s["header"]["competitions"][0]["competitors"]}
        ends = []
        for d in s["drives"]["previous"]:
            p = d["plays"][-1] if d.get("plays") else None
            if not p or p["period"]["number"] > 4:
                continue
            m, sec = p["clock"]["displayValue"].split(":")
            gc = (p["period"]["number"] - 1) * 900 + 900 - (int(m) * 60 + int(sec))
            ends.append((gc, p["homeScore"] - p["awayScore"]))
        return None if sp is None else (float(sp), comp["home"] - comp["away"], ends)
    except Exception:
        return None

def pull():
    ids = set()
    for wk in range(1, 17):
        try:
            sb = get(f"{B}/scoreboard?groups=80&limit=300&seasontype=2&week={wk}&dates={SEASON}")
        except Exception:
            continue
        ids |= {e["id"] for e in sb.get("events", []) if e["status"]["type"]["state"] == "post"}
    with ThreadPoolExecutor(16) as ex:
        return {i: g for i, g in zip(ids, ex.map(game, sorted(ids))) if g}

def main():
    if "--refresh" in sys.argv or not os.path.exists(CACHE):
        json.dump(pull(), open(CACHE, "w"))
    games = json.load(open(CACHE))
    X, y = [], []
    for sp, final, ends in games.values():
        for gc, m in ends:
            rem = max(0.0, 1 - gc / 3600)
            if rem > 0.05:
                X.append((-m * rem, -sp * rem)); y.append(final - m)
    X, y = np.array(X), np.array(y)
    (k, c), *_ = np.linalg.lstsq(X, y, rcond=None)
    # bootstrap by game would be cleaner; ponytail: row bootstrap, rows within a game correlate so CI is a bit tight
    rng = np.random.default_rng(7)
    bs = [np.linalg.lstsq(X[i], y[i], rcond=None)[0][0] for i in (rng.integers(0, len(y), len(y)) for _ in range(500))]
    print(f"{SEASON}: {len(games)} games, {len(y)} drive ends")
    print(f"k (lead leak) = {k:.3f}  [95% {np.percentile(bs, 2.5):.3f}, {np.percentile(bs, 97.5):.3f}]   NFL: 0.28")
    print(f"c (pregame weight) = {c:.3f}   NFL: 1.0")

if __name__ == "__main__":
    main()
