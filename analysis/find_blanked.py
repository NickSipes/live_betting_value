#!/usr/bin/env python3
"""Find 2026 instances of a top-10 player at position vs a bottom-10 defense
at that position where production stalls, two flavors per kind:

Receiving (WR/TE, receiving yards):
  blanked - >= BLANK_GC_MIN game-clock minutes without a catch
  dry     - >= DRY_REC_TGT targets while still <= DRY_REC_YDS receiving yards

Rushing (RB, rushing yards):
  blanked - some >= BLANK_GC_MIN game-clock window adding <= STALL_YDS rushing yards
  dry     - >= DRY_RUSH_ATT carries while still <= DRY_RUSH_YDS rushing yards

Stretches are measured on the game clock (qtr + quarter_seconds_remaining), not
wall time. Events keep their wall-clock time_of_day for joining odds snapshots.
Ranks use season-to-date data entering each week (so weeks >= MIN_WEEK only).
Writes data/blanked_instances.json; prints a table.
"""
import csv, gzip, json, collections, os

ROOT = os.path.join(os.path.dirname(__file__), "..")
TOP_N = int(os.environ.get("TOP_N", 10))        # player rank cutoff; big = all players
OUT = os.environ.get("OUT", f"{ROOT}/data/blanked_instances.json")
MIN_WEEK = 4
BLANK_GC_MIN = 20   # game-clock minutes (a 60-min game; 20 = a third of it)
STALL_YDS = 10
DRY_REC_TGT, DRY_REC_YDS = 4, 25
DRY_RUSH_ATT, DRY_RUSH_YDS = 8, 30
POST_YDS = 30       # yards required AFTER the window/trigger: silence must be
                    # followed by output, or there was no over to cash

def elapsed(qtr, qsr):
    """Game seconds elapsed; OT quarters are 600s."""
    full = min(qtr - 1, 4) * 900 + max(qtr - 5, 0) * 600
    qlen = 900 if qtr <= 4 else 600
    return full + (qlen - qsr)

pos_of, name_of = {}, {}
for r in csv.DictReader(open(f"{ROOT}/data/roster_2026.csv")):
    if r["gsis_id"]:
        pos_of[r["gsis_id"]] = r["position"]
        name_of[r["gsis_id"]] = r["full_name"]

# events: kind -> list of (week, game_id, defteam, pid, yds, counts, tod, gc)
events = {"rec": [], "rush": []}
def_games = collections.defaultdict(set)
game_len = collections.defaultdict(int)   # max game-clock seconds seen per game
game_meta = {}

with gzip.open(f"{ROOT}/data/pbp_2026.csv.gz", "rt") as f:
    for p in csv.DictReader(f):
        gid, wk = p["game_id"], int(p["week"])
        gc = None
        if p["qtr"] not in ("", "NA") and p.get("quarter_seconds_remaining") not in ("", "NA", None):
            gc = elapsed(int(float(p["qtr"])), float(p["quarter_seconds_remaining"]))
            game_len[gid] = max(game_len[gid], gc)
        if p.get("defteam"):
            def_games[p["defteam"]].add((wk, gid))
        game_meta.setdefault(gid, {"week": wk, "home": p["home_team"], "away": p["away_team"]})
        if gc is None:
            continue
        rid = p.get("receiver_player_id")
        if rid and p.get("passer_player_name") and pos_of.get(rid) in ("WR", "TE"):
            y = p.get("receiving_yards")
            y = float(y) if y not in ("", "NA") else 0.0
            events["rec"].append((wk, gid, p["defteam"], rid, y, p.get("complete_pass") == "1",
                                  p.get("time_of_day"), gc))
        uid = p.get("rusher_player_id")
        if uid and p.get("rush_attempt") == "1" and pos_of.get(uid) == "RB":
            y = p.get("rushing_yards")
            y = float(y) if y not in ("", "NA") else 0.0
            events["rush"].append((wk, gid, p["defteam"], uid, y, True, p.get("time_of_day"), gc))

POSITIONS = {"rec": ("WR", "TE"), "rush": ("RB",)}
DRY = {"rec": (DRY_REC_TGT, DRY_REC_YDS), "rush": (DRY_RUSH_ATT, DRY_RUSH_YDS)}
max_week = max(t[0] for k in events for t in events[k])
out = []

for kind, evs in events.items():
    for w in range(MIN_WEEK, max_week + 1):
        pyds = collections.defaultdict(float)
        for e in evs:
            if e[0] < w and e[5]:
                pyds[e[3]] += e[4]
        top10 = {}
        for pos in POSITIONS[kind]:
            ranked = sorted((p for p in pyds if pos_of[p] == pos), key=lambda p: -pyds[p])[:TOP_N]
            for i, p in enumerate(ranked):
                top10[p] = (pos, i + 1)
        dyds = collections.defaultdict(float)
        for e in evs:
            if e[0] < w and e[5]:
                dyds[(e[2], pos_of[e[3]])] += e[4]
        worst10 = {}
        for pos in POSITIONS[kind]:
            rows = []
            for dt in def_games:
                n = len([g for g in def_games[dt] if g[0] < w])
                if n >= 2:
                    rows.append((dt, dyds[(dt, pos)] / n))
            rows.sort(key=lambda r: -r[1])
            for i, (dt, ypg) in enumerate(rows[:10]):
                worst10[(dt, pos)] = (i + 1, round(ypg, 1))

        week_ev = collections.defaultdict(list)
        for e in evs:
            if e[0] == w:
                week_ev[(e[1], e[3])].append(e)
        for (gid, pid), ev_list in week_ev.items():
            if pid not in top10:
                continue
            pos, prank = top10[pid]
            dt = ev_list[0][2]
            if (dt, pos) not in worst10:
                continue
            drank, dypg = worst10[(dt, pos)]
            ev_list.sort(key=lambda e: e[7])
            cum, elist = 0, []
            for e in ev_list:
                gained = e[4] if e[5] else 0.0
                cum += gained
                elist.append({"tod": e[6], "gc": e[7], "yds": int(e[4]),
                              "complete": e[5], "cum": int(cum)})
            glen = game_len[gid]

            tags, extra = [], {}
            # blanked/stalled: longest game-clock window adding <= thresh yards.
            # points: (gc, cum BEFORE event, cum AFTER); window i->j gains
            # before_j - after_i, so the play ending a silence isn't counted in it.
            # ponytail: O(n^2) over ~25 events per game, fine
            prog = [(0, 0, 0)]
            for e in elist:
                gained = e["yds"] if e["complete"] else 0
                prog.append((e["gc"], e["cum"] - gained, e["cum"]))
            prog.append((glen, cum, cum))
            thresh = 0 if kind == "rec" else STALL_YDS
            best = (0, None, None, 0)
            for i in range(len(prog)):
                for j in range(i + 1, len(prog)):
                    post = cum - prog[j][1]   # yards from the gap-ending play onward
                    if prog[j][1] - prog[i][2] <= thresh and post >= POST_YDS:
                        dur = (prog[j][0] - prog[i][0]) / 60
                        if dur > best[0]:
                            best = (dur, prog[i][0], prog[j][0], post)
            if best[0] >= BLANK_GC_MIN:
                tags.append("blanked")
                extra.update(gap_start=best[1], gap_end=best[2], gap_min=round(best[0]),
                             post_yds=round(best[3]))
            dn, dy = DRY[kind]
            for n, e in enumerate(elist, 1):
                if n >= dn and e["cum"] <= dy:
                    if cum - e["cum"] >= POST_YDS:
                        tags.append("dry")
                        extra.update(dry_gc=e["gc"], dry_n=n, dry_yds=e["cum"],
                                     dry_post=round(cum - e["cum"]))
                    break
            if not tags:
                continue
            out.append({
                "kind": kind, "tags": tags, "game_id": gid, "week": w,
                "player": name_of[pid], "pos": pos, "pos_rank": prank,
                "yds_entering": round(pyds[pid]),
                "defteam": dt, "def_rank_vs_pos": drank, "def_ypg_allowed": dypg,
                "final_yds": int(cum), "n_events": len(elist), "events": elist,
                "game_len": glen,
                "home": game_meta[gid]["home"], "away": game_meta[gid]["away"], **extra,
            })

out.sort(key=lambda r: (r["kind"], r["week"], -r["yds_entering"]))
json.dump(out, open(OUT, "w"), indent=1)
print(f"{len(out)} instances (weeks {MIN_WEEK}-{max_week})")
for r in out:
    unit = "tgt" if r["kind"] == "rec" else "att"
    gap = f"{r['gap_min']}gc-min" if "blanked" in r["tags"] else ""
    print(f"{r['kind']:4s} wk{r['week']} {r['player']:22s} {r['pos']}#{r['pos_rank']:<2d} "
          f"vs {r['defteam']:3s} (#{r['def_rank_vs_pos']} worst, {r['def_ypg_allowed']} ypg) "
          f"{','.join(r['tags']):11s} {gap:9s} {r['n_events']:2d} {unit}, {r['final_yds']:3d} yds final")
