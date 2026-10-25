import csv, gzip, collections

# load games: pregame spread + final result
games={}
for r in csv.DictReader(open("data/games.csv")):
    if r["season"] in ("2024","2025","2026") and r["result"] not in ("","NA") and r["spread_line"] not in ("","NA"):
        games[r["game_id"]]={"spread":abs(float(r["spread_line"])),"result":int(r["result"]),
                             "home":r["home_team"],"away":r["away_team"],"season":r["season"]}

# per game, per side: max deficit, and half when deficit first reached >=9
trail={}  # (game_id, side) -> {"max":int, "first9_half":1/2}
for y in ("2024","2025","2026"):
    with gzip.open(f"data/pbp_{y}.csv.gz","rt") as f:
        rd=csv.DictReader(f)
        for p in rd:
            gid=p["game_id"]
            if gid not in games: continue
            try:
                hs=int(float(p["total_home_score"])); as_=int(float(p["total_away_score"]))
                qtr=int(float(p["qtr"]))
            except (ValueError,KeyError): continue
            half=1 if qtr<=2 else 2
            for side,deficit in (("home",as_-hs),("away",hs-as_)):
                if deficit<=0: continue
                k=(gid,side)
                t=trail.setdefault(k,{"max":0,"first9_half":None})
                if deficit>t["max"]: t["max"]=deficit
                if deficit>=9 and t["first9_half"] is None: t["first9_half"]=half

# evaluate hypothetical +8.5 live bet on the trailing team
# bet wins if that team loses by <=8 or wins
def covered(g,side):
    m = g["result"] if side=="away" else -g["result"]  # m = how much this side LOST by (positive = lost)
    return m<=8

print("Hypothetical live bet: take trailing team at +8.5 once they trail by 9+ in-game")
print("(cover = lose by <=8 or win; breakeven at -110 = 52.4%)\n")
for cond,label in [(lambda g:g["spread"]<=8,"pregame spread <=8"),(lambda g:g["spread"]>8,"pregame spread >8")]:
    print(f"--- {label} ---")
    for hf,hl in [(1,"first trailed 9+ in 1st half"),(2,"first trailed 9+ in 2nd half")]:
        for lo,hi,bl in [(9,12,"max deficit 9-12"),(13,16,"max deficit 13-16"),(17,99,"max deficit 17+"),(9,99,"any (9+)")]:
            n=w=0
            for (gid,side),t in trail.items():
                g=games[gid]
                if not cond(g) or t["first9_half"]!=hf or not (lo<=t["max"]<=hi): continue
                n+=1; w+=covered(g,side)
            if n: print(f"  {hl}, {bl:18s}: {w:3d}/{n:3d} = {w/n:.1%}")
    print()
# by season, the headline cut: spread<=8, trailed 9+ anytime
print("--- by season: pregame spread <=8, trailed 9+ anytime ---")
for season in ("2024","2025","2026"):
    n=w=0
    for (gid,side),t in trail.items():
        g=games[gid]
        if g["season"]!=season or g["spread"]>8 or t["first9_half"] is None: continue
        n+=1; w+=covered(g,side)
    print(f"  {season}: {w}/{n} = {w/n:.1%}")
