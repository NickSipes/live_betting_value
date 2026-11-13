#!/usr/bin/env python3
"""Game-script factor for the live model's yardage baseline: how much of a player's normal rest-of-game production
(season ypg x time left) he gets, given his team's lead and the clock, relative to all game states at that clock.

nflverse pbp 2020-2025 (2026 left out: it holds the held-out weeks), regular season, players with 3+ earlier games
and 20+ ypg. Ratio of sums per (lead bin, clock checkpoint), divided by the all-states ratio at that checkpoint so
the factor only carries the lead (the model already has elapsed). Writes live/lead_factor.json; the poller and
analysis/v2_model.py look it up with lead_factor() (bin on lead, linear in elapsed).

Run: python3 analysis/lead_factor.py"""
import bisect, collections, csv, gzip, json, os

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
BINS = [(-99, -15), (-14, -9), (-8, -4), (-3, 3), (4, 8), (9, 14), (15, 99)]
EFS = [0.15, 0.25, 0.35, 0.45, 0.55, 0.65, 0.75, 0.85]
COLS = {"rec": ("receiver_player_id", "receiving_yards"), "rush": ("rusher_player_id", "rushing_yards")}

def lead_factor(table, kind, diff, ef):
    """Factor for this market at this lead and elapsed fraction; 1.0 without a table."""
    t = table.get(kind) if table else None
    if not t:
        return 1.0
    row = next(f for (lo, hi), f in zip(t["bins"], t["f"]) if lo <= diff <= hi)
    efs = t["efs"]
    if ef <= efs[0]:
        return row[0]
    if ef >= efs[-1]:
        return row[-1]
    i = bisect.bisect_right(efs, ef)
    a = (ef - efs[i - 1]) / (efs[i] - efs[i - 1])
    return row[i - 1] + a * (row[i] - row[i - 1])

def build():
    got = {k: collections.defaultdict(float) for k in COLS}   # (bin, ef) -> yards after
    exp = {k: collections.defaultdict(float) for k in COLS}   # (bin, ef) -> ypg x time left
    for yr in range(2020, 2026):
        games = collections.defaultdict(list)
        for r in csv.DictReader(gzip.open(f"{ROOT}/data/pbp_{yr}.csv.gz", "rt")):
            if r["season_type"] == "REG" and r["posteam"] and r["game_seconds_remaining"] not in ("", "NA"):
                games[r["game_id"]].append(r)
        week = {g: int(pl[0]["week"]) for g, pl in games.items()}
        yds = {k: collections.defaultdict(lambda: collections.defaultdict(float)) for k in COLS}   # pid -> week -> yds
        for g, pl in games.items():
            for kind, (idc, yc) in COLS.items():
                for r in pl:
                    if r[idc]:
                        yds[kind][r[idc]][week[g]] += float(r[yc]) if r[yc] not in ("", "NA") else 0.0
        for g, pl in games.items():
            wk = week[g]
            el = [3600 - float(r["game_seconds_remaining"]) for r in pl]
            for ef in EFS:
                t = ef * 3600
                i = bisect.bisect_right(el, t) - 1
                if i < 0:
                    continue
                last = pl[i]
                sd = float(last["score_differential"] or 0)
                for kind, (idc, yc) in COLS.items():
                    after = collections.defaultdict(float)
                    players = {}
                    for r, e in zip(pl, el):
                        if r[idc]:
                            players[r[idc]] = r["posteam"]
                            if e > t and r[yc] not in ("", "NA") and e <= 3600:
                                after[r[idc]] += float(r[yc])
                    for pid, team in players.items():
                        prior = [v for w, v in yds[kind][pid].items() if w < wk]
                        if len(prior) < 3 or sum(prior) / len(prior) < 20:
                            continue
                        d = sd if last["posteam"] == team else -sd
                        b = next(j for j, (lo, hi) in enumerate(BINS) if lo <= d <= hi)
                        got[kind][(b, ef)] += after[pid]
                        exp[kind][(b, ef)] += sum(prior) / len(prior) * (1 - ef)
    out = {}
    for kind in COLS:
        f = []
        for b in range(len(BINS)):
            row = []
            for ef in EFS:
                allr = sum(got[kind][(j, ef)] for j in range(len(BINS))) / sum(exp[kind][(j, ef)] for j in range(len(BINS)))
                row.append(round(got[kind][(b, ef)] / exp[kind][(b, ef)] / allr, 3) if exp[kind][(b, ef)] else 1.0)
            f.append(row)
        out[kind] = {"bins": BINS, "efs": EFS, "f": f}
    return out

if __name__ == "__main__":
    t = build()
    json.dump(t, open(f"{ROOT}/live/lead_factor.json", "w"), indent=1)
    for kind, v in t.items():
        print(f"\n{kind}: factor by lead (rows) x elapsed (cols)\n{'':12s}" + "".join(f"{e:6.2f}" for e in v["efs"]))
        for (lo, hi), row in zip(v["bins"], v["f"]):
            print(f"{f'{lo}..{hi}':12s}" + "".join(f"{x:6.2f}" for x in row))
    assert lead_factor(t, "rec", 0, 0.5) == lead_factor(t, "rec", 2, 0.5)
    assert abs(lead_factor(t, "rec", 20, 0.15) - t["rec"]["f"][-1][0]) < 1e-9 and lead_factor(None, "rec", 20, 0.5) == 1.0
    print("\nwrote live/lead_factor.json")
