#!/usr/bin/env python3
"""Train v2 on everything pulled so far and export what the live poller needs:
live/model_weights.json, live/priors.json (entering next week), live/def_priors.json.
Run from analysis/: ../.venv/bin/python export_live.py
"""
import json, os, collections, datetime
from zoneinfo import ZoneInfo

exec(open(os.path.join(os.path.dirname(__file__), "v2_model.py")).read())

# deployment weights: refit on ALL pulled data (train + test splits combined)
Xall = np.vstack([Xtr2, Xte2])
yall = np.concatenate([ytr2, yte2])
mua, sda = Xall.mean(0), Xall.std(0) + 1e-9
wa, ba = fit_logistic((Xall - mua) / sda, yall, l2=3e-3)

LIVE = f"{ROOT}/live"
json.dump({"feats": V2_FEATS, "mu": mua.tolist(), "sd": sda.tolist(),
           "w": wa.tolist(), "b": float(ba)}, open(f"{LIVE}/model_weights.json", "w"), indent=1)

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

json.dump(pri, open(f"{LIVE}/priors.json", "w"), indent=1)
json.dump(dpri, open(f"{LIVE}/def_priors.json", "w"), indent=1)
print(f"exported weights + priors for {len(pri)} players, {len(dpri)} defense cells (entering week {w_next})")

# ---- upcoming-week watchlist: top-N player vs bottom-10 defense -------------
TOP_N_WL = {"WR": 20, "TE": 10, "RB": 15}
team_by_name = {r["full_name"]: r["team"] for r in csv.DictReader(open(f"{ROOT}/data/roster_2026.csv")) if r["gsis_id"]}

ranks = {}   # (kind,pos) -> name -> rank
for kind in ("rec", "rush"):
    bypos = collections.defaultdict(list)
    for name, kinds in pri.items():
        if kind in kinds:
            bypos[kinds[kind]["pos"]].append((name, kinds[kind]["ypg"]))
    for pos, rows in bypos.items():
        for i, (name, _) in enumerate(sorted(rows, key=lambda r: -r[1])):
            ranks[(kind, pos, name)] = i + 1
dranks = {}   # (kind,pos) -> team -> rank (1 = worst)
bypos = collections.defaultdict(list)
for k, v in dpri.items():
    kind, team, pos = k.split("|")
    bypos[(kind, pos)].append((team, v))
for kp, rows in bypos.items():
    for i, (team, _) in enumerate(sorted(rows, key=lambda r: -r[1])):
        dranks[(*kp, team)] = i + 1

sched = {}
for r in csv.DictReader(open(f"{ROOT}/data/games.csv")):
    if r["season"] == "2026" and r["week"] == str(w_next):
        lbl = f'{r["away_team"]} @ {r["home_team"]}'
        kick = f'{r["weekday"][:3]} {r["gametime"]} ET'
        kick_iso = datetime.datetime.fromisoformat(f'{r["gameday"]}T{r["gametime"]}') \
            .replace(tzinfo=ZoneInfo("America/New_York")).isoformat()
        sched[r["home_team"]] = (r["away_team"], lbl, kick, kick_iso)
        sched[r["away_team"]] = (r["home_team"], lbl, kick, kick_iso)

watch = []
for name, kinds in pri.items():
    team = team_by_name.get(name)
    if team not in sched:
        continue
    opp, lbl, kick, kick_iso = sched[team]
    for kind, st in kinds.items():
        pos = st["pos"]
        prank = ranks.get((kind, pos, name), 999)
        drank = dranks.get((kind, pos, opp), 999)
        if prank <= TOP_N_WL[pos] and drank <= 10:
            watch.append({"player": name, "pos": pos, "rank": prank, "ypg": st["ypg"],
                          "market": kind, "team": team, "opp": opp, "def_rank": drank,
                          "def_ypg": dpri[f"{kind}|{opp}|{pos}"], "game": lbl, "kick": kick, "kick_iso": kick_iso})
watch.sort(key=lambda w: (w["kick_iso"], w["def_rank"], w["rank"]))
json.dump({"week": w_next, "entries": watch}, open(f"{ROOT}/docs/watchlist.json", "w"), indent=1)
print(f"watchlist: {len(watch)} players for week {w_next}")
