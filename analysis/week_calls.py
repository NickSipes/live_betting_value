#!/usr/bin/env python3
"""Settled-call autopsy for one week: what each call saw, and what happened after.

Inputs (pulled from the server into data/week{N}/): calls.json (ledger).
For every live 70% call it rebuilds, from the ESPN play log of that game:
  - at the call: game minute, yards needed, targets/carries so far, team score diff
  - after the call: the player's targets/carries and yards, his team's pass share,
    how the score moved, and how much game clock was left
Writes data/week{N}/autopsy.json and prints the comparison tables.

Usage: python3 analysis/week_calls.py 7
"""
import json, os, sys, statistics as st
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "live"))
sys.argv = sys.argv[:2]
import poller as P   # reuses the play-log parser and name matching the live system uses

WEEK = int(sys.argv[1]) if len(sys.argv) > 1 else 7
D = os.path.join(os.path.dirname(__file__), "..", "data", f"week{WEEK}")
ledger = json.load(open(f"{D}/calls.json"))
FEATS = ledger["feats"]
calls = [c for c in ledger["calls"] if c["week"] == WEEK]

logs, box = {}, {}
for c in calls:
    eid = c["espn_id"]
    if eid not in logs:
        logs[eid] = P.espn_timeline(eid)
        box[eid] = P.Monitor.espn_players(None, eid)[0]

rows = []
for c in calls:
    pplays, tplays = logs[c["espn_id"]]
    team = box[c["espn_id"]].get(c["player"], {}).get("team")
    mine = [q for q in pplays if q[2] == c["market"] and P.same_player(q[3], c["player"])]
    gc = c.get("gc") or 0
    after = [q for q in mine if q[1] > gc]
    t_after = [q for q in tplays if q[2] == team and q[1] > gc]
    pass_share = lambda ts: (sum(q[3] == "pass" for q in ts) / len(ts)) if ts else None
    x = dict(zip(FEATS, c["x"])) if c.get("x") else {}
    end_gc = max(q[1] for q in tplays) if tplays else 3600
    rows.append({
        "player": c["player"], "market": c["market"], "type": c.get("type", "live"), "result": c["result"],
        "game": c["game"], "line": c["line"], "final": c["final"], "margin": round(c["final"] - c["line"], 1),
        "call_min": round(gc / 60, 1), "yds_at_call": c["cum"], "needed": round(c["line"] - c["cum"], 1),
        "p": c.get("p"), "events_at_call": x.get("ev_sofar"), "drought": x.get("drought"),
        "season_ypg": x.get("ypg"), "price_ratio": x.get("price_ratio"), "discount": x.get("discount"),
        "diff_at_call": x.get("score_diff"), "pace_at_call": x.get("pace"),
        "pass_share_at_call": x.get("pass_share"),
        "events_after": len(after), "yds_after": sum(q[4] for q in after if q[5]),
        "min_left": round((end_gc - gc) / 60, 1),
        "events_after_per_min": round(len(after) / max((end_gc - gc) / 60, 1), 3),
        "pass_share_after": round(pass_share(t_after), 3) if t_after else None,
        "team_plays_after_per_min": round(len(t_after) / max((end_gc - gc) / 60, 1), 3),
    })
json.dump(rows, open(f"{D}/autopsy.json", "w"), indent=1)

live = [r for r in rows if r["type"] == "live"]
W = [r for r in live if r["result"] == "win"]
Lz = [r for r in live if r["result"] == "loss"]
def med(rs, k):
    v = [r[k] for r in rs if r[k] is not None]
    return round(st.median(v), 2) if v else None
print(f"week {WEEK}: {len(live)} live calls ({len(W)}-{len(Lz)}), "
      f"{len(rows) - len(live)} pregame ({sum(r['result']=='win' for r in rows if r['type']=='pregame')} wins)\n")
print(f"{'median':28s} {'wins':>8s} {'losses':>8s}")
for k in ["call_min", "p", "needed", "yds_at_call", "events_at_call", "season_ypg", "price_ratio", "discount",
          "diff_at_call", "pass_share_at_call", "pace_at_call", "min_left", "events_after", "events_after_per_min",
          "yds_after", "pass_share_after", "team_plays_after_per_min"]:
    print(f"{k:28s} {str(med(W, k)):>8s} {str(med(Lz, k)):>8s}")
