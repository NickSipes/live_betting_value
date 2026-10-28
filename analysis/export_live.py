#!/usr/bin/env python3
"""Export the priors the live poller needs, entering next week:
live/priors.json (incl. each player's team) and live/def_priors.json.
Weights are owned by analysis/retrain_markets.py; the watchlist is built by the
poller itself (refresh_watchlist) from these priors and the ESPN schedule.
Run from analysis/: ../.venv/bin/python export_live.py
"""
import json, os, collections, csv, gzip

exec(open(os.path.join(os.path.dirname(__file__), "v2_model.py")).read())

LIVE = f"{ROOT}/live"

# defense form for the live veto: yds allowed per game over the last 3 games
# PLAYED (a bye just pushes the window back a week), per market, team level
form = {"rush": collections.defaultdict(dict), "rec": collections.defaultdict(dict)}
with gzip.open(f"{ROOT}/data/pbp_2026.csv.gz", "rt") as fh:
    for r in csv.DictReader(fh):
        if r["defteam"]:
            wk = int(r["week"])
            for kind, col in (("rush", "rushing_yards"), ("rec", "receiving_yards")):
                try:
                    form[kind][r["defteam"]][wk] = form[kind][r["defteam"]].get(wk, 0.0) + float(r[col])
                except ValueError:
                    pass
dform = {}
for kind in ("rush", "rec"):
    dform[kind] = {}
    for team, weeks in form[kind].items():
        last3 = sorted(weeks)[-3:]
        dform[kind][team] = round(sum(weeks[w] for w in last3) / len(last3), 1)
json.dump(dform, open(f"{LIVE}/def_form.json", "w"), indent=1)
for kind in ("rush", "rec"):
    print(f"def form top-3 {kind}: {sorted(dform[kind], key=dform[kind].get)[:3]}")

# upcoming week = first week in the schedule with unplayed games
w_next = min(int(r["week"]) for r in csv.DictReader(open(f"{ROOT}/data/games.csv"))
             if r["season"] == "2026" and r["result"] in ("", "NA"))
pri, dpri = {}, {}
for kind, evs in events.items():
    tot, gms, nev = collections.defaultdict(float), collections.defaultdict(set), collections.defaultdict(int)
    dy, dg = collections.defaultdict(float), collections.defaultdict(set)
    for e in evs:
        gms[e[4]].add(e[1]); nev[e[4]] += 1
        if e[6]:
            tot[e[4]] += e[5]
            dy[(e[2], pos_of[e[4]])] += e[5]
        dg[e[2]].add(e[1])
    for p in gms:
        g = len(gms[p])
        ypg = tot[p] / g
        if g < 2 or ypg < 20:
            continue
        pri.setdefault(name_of[p], {})[kind] = {
            "ypg": round(ypg, 1), "ype": round(tot[p] / nev[p], 2) if nev[p] else 0,
            "epg": round(nev[p] / g, 1), "pos": pos_of[p]}
    for (dt, pos), v in dy.items():
        dpri[f"{kind}|{dt}|{pos}"] = round(v / len(dg[dt]), 1)

team_by_name = {r["full_name"]: r["team"] for r in csv.DictReader(open(f"{ROOT}/data/roster_2026.csv")) if r["gsis_id"]}
for name in pri:   # the poller's watchlist builder needs each player's team
    if name in team_by_name:
        pri[name]["team"] = team_by_name[name]

json.dump(pri, open(f"{LIVE}/priors.json", "w"), indent=1)
json.dump(dpri, open(f"{LIVE}/def_priors.json", "w"), indent=1)
print(f"exported priors for {len(pri)} players, {len(dpri)} defense cells (entering week {w_next})")
