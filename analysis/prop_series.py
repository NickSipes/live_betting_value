#!/usr/bin/env python3
"""Pull a historical in-game prop line series from the-odds-api.

Usage: prop_series.py <away> <home> <start_iso> <end_iso> <market> <out_csv>
Walks 5-min snapshots, saves raw JSON to data/props/<event_id>/, writes one
CSV row per (snapshot, book, player, over/under).
"""
import json, os, sys, time, csv, urllib.request, urllib.parse

API = "https://api.the-odds-api.com/v4/historical/sports/americanfootball_nfl"
KEY = open(os.path.join(os.path.dirname(__file__), "..", ".env")).read().split("ODDS_API_KEY=")[1].strip()
BOOKS = "draftkings,fanduel"  # same credit cost as one book (priced per 10 books)

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8.4"})
    try:
        r = urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:
        print("FAILED URL:", url, "\nBODY:", e.read()[:300], file=sys.stderr)
        raise
    with r:
        used = r.headers.get("x-requests-used")
        return json.load(r), used

def main(away, home, start, end, market, out_csv):
    ev, _ = get(f"{API}/events?apiKey={KEY}&date={start}")
    event = next(e for e in ev["data"] if home in e["home_team"] and away in e["away_team"])
    eid = event["id"]
    print("event:", eid, event["away_team"], "@", event["home_team"], event["commence_time"])

    raw_dir = os.path.join(os.path.dirname(__file__), "..", "data", "props", eid)
    os.makedirs(raw_dir, exist_ok=True)
    rows, ts, used = [], start, "?"
    while ts and ts <= end:
        cache = os.path.join(raw_dir, f"{market}_{ts.replace(':','')}.json")
        if os.path.exists(cache):
            d = json.load(open(cache))
        else:
            try:
                d, used = get(f"{API}/events/{eid}/odds?apiKey={KEY}&date={ts}"
                              f"&bookmakers={BOOKS}&markets={market}&oddsFormat=american")
            except urllib.error.HTTPError as e:
                if e.code == 404:  # event expired = game over
                    print("event expired at", ts)
                    break
                raise
            json.dump(d, open(cache, "w"))
            time.sleep(0.15)
        snap = d.get("timestamp")
        for b in (d.get("data") or {}).get("bookmakers", []):
            for m in b.get("markets", []):
                for o in m.get("outcomes", []):
                    rows.append([snap, b["key"], o.get("description"), o["name"], o.get("point"), o["price"]])
        ts = d.get("next_timestamp")
    with open(out_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["snapshot", "book", "player", "side", "point", "price"])
        w.writerows(rows)
    print(f"{len(rows)} rows -> {out_csv}; credits used this month: {used}")

if __name__ == "__main__":
    main(*sys.argv[1:7])
