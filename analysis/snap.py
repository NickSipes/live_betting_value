import csv, gzip

games={}
for r in csv.DictReader(open("data/games.csv")):
    if r["season"] in ("2024","2025","2026") and r["result"] not in ("","NA") and r["spread_line"] not in ("","NA"):
        games[r["game_id"]]={"spread":abs(float(r["spread_line"])),"result":int(r["result"])}

# score at end of Q2 and end of Q3 per game (last play seen in that quarter)
snaps={}  # gid -> {2:(hs,as), 3:(hs,as)}
for y in ("2024","2025","2026"):
    with gzip.open(f"data/pbp_{y}.csv.gz","rt") as f:
        for p in csv.DictReader(f):
            gid=p["game_id"]
            if gid not in games: continue
            try:
                qtr=int(float(p["qtr"])); hs=int(float(p["total_home_score"])); as_=int(float(p["total_away_score"]))
            except ValueError: continue
            if qtr in (2,3): snaps.setdefault(gid,{})[qtr]=(hs,as_)

def covered(gid,side):
    m = games[gid]["result"] if side=="away" else -games[gid]["result"]
    return m<=8

print("Bettable snapshots, pregame spread <=8, take trailing team +8.5")
print("(breakeven at -110 vig = 52.4%)\n")
for q,label in [(2,"AT HALFTIME"),(3,"END OF Q3")]:
    print(f"--- {label}, trailing by: ---")
    for lo,hi,bl in [(9,10,"9-10"),(11,13,"11-13"),(14,16,"14-16"),(17,99,"17+"),(9,99,"9+ (all)")]:
        n=w=0
        for gid,s in snaps.items():
            if games[gid]["spread"]>8 or q not in s: continue
            hs,as_=s[q]
            for side,d in (("home",as_-hs),("away",hs-as_)):
                if lo<=d<=hi:
                    n+=1; w+=covered(gid,side)
        if n: print(f"  down {bl:9s}: {w:3d}/{n:3d} = {w/n:.1%}")
    print()
