import csv, collections
rows=[r for r in csv.DictReader(open("data/games.csv")) if r["season"] in ("2024","2025","2026") and r["result"] not in ("","NA")]
for season in ("2024","2025","2026"):
    g=[r for r in rows if r["season"]==season]
    n=len(g)
    margins=[abs(int(r["result"])) for r in g]
    one_score=sum(1 for m in margins if m<=8)
    print(f"\n=== {season} season ({n} completed games) ===")
    print(f"Final margin <=8 (one-score): {one_score}/{n} = {one_score/n:.1%}")
    for lo,hi,label in [(0,3,"spread 0-3"),(3.5,8,"spread 3.5-8"),(8.5,99,"spread >8")]:
        sub=[abs(int(r["result"])) for r in g if r["spread_line"] not in ("","NA") and lo<=abs(float(r["spread_line"]))<=hi]
        if sub:
            os=sum(1 for m in sub if m<=8)
            print(f"  pregame {label:14s}: n={len(sub):3d}  margin<=8: {os/len(sub):.1%}")
# distribution of margins, all close-spread games 2024-2026
close=[abs(int(r["result"])) for r in rows if r["spread_line"] not in ("","NA") and abs(float(r["spread_line"]))<=8]
print(f"\n=== Margin distribution, pregame spread <=8, 2024-2026 (n={len(close)}) ===")
dist=collections.Counter()
for m in close:
    b = "0-3" if m<=3 else "4-8" if m<=8 else "9-16" if m<=16 else "17+"
    dist[b]+=1
for b in ("0-3","4-8","9-16","17+"):
    print(f"  margin {b:5s}: {dist[b]:4d}  {dist[b]/len(close):.1%}")
