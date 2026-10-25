#!/usr/bin/env python3
"""Render data/blanked_instances.json as reports/blanked_2026.html, overlaying
DK/FD prop-line series from data/props_series/ when present.
Game-clock x-axis. Pure server-side SVG, native <title> tooltips, no JS."""
import csv, gzip, json, os, html, bisect, datetime

ROOT = os.path.join(os.path.dirname(__file__), "..")
inst = json.load(open(f"{ROOT}/data/blanked_instances.json"))
MARKET = {"rec": "player_reception_yds", "rush": "player_rush_yds"}

W, H = 920, 260
M = {"t": 20, "r": 70, "b": 46, "l": 38}   # extra bottom room for the event rug
PW, PH = W - M["l"] - M["r"], H - M["t"] - M["b"]

def ts(x):
    return datetime.datetime.fromisoformat(x.replace("Z", "+00:00")).timestamp()

# wall-clock -> game-clock map per instance game, from every timestamped play
def elapsed(qtr, qsr):
    full = min(qtr - 1, 4) * 900 + max(qtr - 5, 0) * 600
    return full + ((900 if qtr <= 4 else 600) - qsr)

gids = {r["game_id"] for r in inst}
wallmap = {g: [] for g in gids}
with gzip.open(f"{ROOT}/data/pbp_2026.csv.gz", "rt") as f:
    for p in csv.DictReader(f):
        g = p["game_id"]
        if g in gids and p.get("time_of_day") and p["qtr"] not in ("", "NA") \
                and p.get("quarter_seconds_remaining") not in ("", "NA", None):
            wallmap[g].append((ts(p["time_of_day"]),
                               elapsed(int(float(p["qtr"])), float(p["quarter_seconds_remaining"]))))
for g in wallmap:
    wallmap[g].sort()

def to_gc(gid, t, glen):
    pts = wallmap[gid]
    i = bisect.bisect_right(pts, (t, float("inf"))) - 1
    if i < 0:
        return 0
    return min(pts[i][1], glen)

# book line series per (gid, market, player): book -> [(gc, point, over, under)]
def load_series(r):
    path = f"{ROOT}/data/props_series/{r['game_id']}_{MARKET[r['kind']]}.csv"
    if not os.path.exists(path):
        return {}
    rows = [x for x in csv.DictReader(open(path))]
    mine = [x for x in rows if x["player"] == r["player"]]
    if not mine:  # fallback: surname + first initial
        sur = r["player"].split()[-1].lower()
        ini = r["player"][0].lower()
        mine = [x for x in rows if x["player"].split()[-1].lower() == sur
                and x["player"][0].lower() == ini]
        if mine:
            print(f"note: fuzzy name match {r['player']} -> {mine[0]['player']}")
    out = {}
    glen = r["game_len"]
    for b in ("draftkings", "fanduel"):
        pts = {}
        for x in mine:
            if x["book"] != b or x["side"] != "Over" or not x["point"]:
                continue
            pts[x["snapshot"]] = float(x["point"])
        series = [(to_gc(r["game_id"], ts(k), glen), v) for k, v in sorted(pts.items())]
        dedup = []
        for gc, v in series:   # keep last quote per game-clock position
            if dedup and dedup[-1][0] == gc:
                dedup[-1] = (gc, v)
            else:
                dedup.append((gc, v))
        if dedup:
            out[b] = dedup
    return out

def gc_label(sec):
    q = int(sec // 900) + 1 if sec < 3600 else 5
    rem = (900 - (sec - (q - 1) * 900)) if q <= 4 else (600 - (sec - 3600))
    return f"{'OT' if q > 4 else f'Q{q}'} {int(rem // 60)}:{int(rem % 60):02d}"

def chart(r, series):
    rec = r["kind"] == "rec"
    glen = r["game_len"]
    linemax = max((v for s in series.values() for _, v in s), default=0)
    ymax = max(80, r["final_yds"] + 20, linemax + 15)
    X = lambda g: M["l"] + g / glen * PW
    Y = lambda v: M["t"] + PH - v / ymax * PH
    rug_y = M["t"] + PH + 14
    s = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{html.escape(r["player"])} cumulative {"receiving" if rec else "rushing"} yards and live lines">']
    if "blanked" in r["tags"]:
        a, b = X(r["gap_start"]), X(r["gap_end"])
        s.append(f'<rect x="{a:.1f}" y="{M["t"]}" width="{b-a:.1f}" height="{PH}" fill="var(--gap-band)"/>')
        what = "no catches" if rec else "&le;10 rush yds"
        s.append(f'<text class="qlbl" x="{(a+b)/2:.1f}" y="{M["t"]+26}" text-anchor="middle">{what} — {r["gap_min"]} game min · +{r["post_yds"]} after</text>')
    qends = [0, 900, 1800, 2700, 3600] + ([glen] if glen > 3600 else [])
    for i in range(len(qends) - 1):
        a, b = X(qends[i]), X(min(qends[i + 1], glen))
        if i % 2 == 0:
            s.append(f'<rect x="{a:.1f}" y="{M["t"]}" width="{b-a:.1f}" height="{PH}" fill="var(--band)"/>')
        lbl = "OT" if i >= 4 else f"Q{i+1}"
        s.append(f'<text class="qlbl" x="{(a+b)/2:.1f}" y="{M["t"]+12}" text-anchor="middle">{lbl}</text>')
        s.append(f'<text class="tick" x="{a:.1f}" y="{M["t"]+PH+32}" text-anchor="middle">{qends[i]//60}&prime;</text>')
    step = 20 if ymax <= 120 else 40
    v = 0
    while v <= ymax:
        s.append(f'<line x1="{M["l"]}" x2="{M["l"]+PW}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="var(--grid)"/>')
        s.append(f'<text class="tick" x="{M["l"]-8}" y="{Y(v)+4:.1f}" text-anchor="end">{v}</text>')
        v += step
    s.append(f'<line x1="{M["l"]}" x2="{M["l"]+PW}" y1="{Y(0):.1f}" y2="{Y(0):.1f}" stroke="var(--baseline)"/>')
    # book line series (step)
    for b, cvar in (("draftkings", "--s-dk"), ("fanduel", "--s-fd")):
        pts = series.get(b)
        if not pts:
            continue
        d = f'M{X(pts[0][0]):.1f},{Y(pts[0][1]):.1f}'
        for gc, v in pts[1:]:
            d += f'H{X(gc):.1f}V{Y(v):.1f}'
        d += f'H{X(min(pts[-1][0] + 150, glen)):.1f}'
        s.append(f'<path d="{d}" fill="none" stroke="var({cvar})" stroke-width="2" stroke-linejoin="round" opacity="0.9"/>')
    # cumulative yards step line
    d = f'M{X(0):.1f},{Y(0):.1f}'
    for e in r["events"]:
        if e["complete"]:
            d += f'H{X(e["gc"]):.1f}V{Y(e["cum"]):.1f}'
    d += f'H{X(glen):.1f}'
    s.append(f'<path d="{d}" fill="none" stroke="var(--s-actual)" stroke-width="2.5" stroke-linejoin="round"/>')
    if "dry" in r["tags"]:
        dx = X(r["dry_gc"])
        unit = "tgt" if rec else "att"
        s.append(f'<line x1="{dx:.1f}" x2="{dx:.1f}" y1="{M["t"]}" y2="{M["t"]+PH}" stroke="var(--ink-muted)" stroke-dasharray="4,3"/>')
        s.append(f'<text class="qlbl" x="{dx+5:.1f}" y="{M["t"]+PH-8}">{r["dry_n"]} {unit}, {r["dry_yds"]} yds · +{r["dry_post"]} after</text>')
    for e in r["events"]:
        cx, when = X(e["gc"]), gc_label(e["gc"])
        if rec:
            if e["complete"]:
                s.append(f'<circle cx="{cx:.1f}" cy="{Y(e["cum"]):.1f}" r="4.5" fill="var(--s-actual)" stroke="var(--surface-1)" stroke-width="2"><title>{when} — catch +{e["yds"]} (total {e["cum"]})</title></circle>')
            else:
                s.append(f'<circle cx="{cx:.1f}" cy="{Y(e["cum"]):.1f}" r="4" fill="none" stroke="var(--s-actual)" stroke-width="1.5" opacity="0.7"><title>{when} — incomplete target</title></circle>')
        elif e["yds"] >= 10:
            s.append(f'<circle cx="{cx:.1f}" cy="{Y(e["cum"]):.1f}" r="4.5" fill="var(--s-actual)" stroke="var(--surface-1)" stroke-width="2"><title>{when} — run +{e["yds"]} (total {e["cum"]})</title></circle>')
    unit = "target" if rec else "carry"
    for e in r["events"]:
        cx, when = X(e["gc"]), gc_label(e["gc"])
        solid = e["complete"] and e["yds"] > 0
        tip = (f"catch +{e['yds']}" if e["complete"] else "incomplete") if rec else f"run {e['yds']:+d}"
        s.append(f'<line x1="{cx:.1f}" x2="{cx:.1f}" y1="{rug_y-7}" y2="{rug_y}" stroke="var(--s-actual)" stroke-width="2" opacity="{"1" if solid else "0.45"}"><title>{when} — {unit}: {tip}</title></line>')
    s.append(f'<text class="tick" x="{M["l"]-8}" y="{rug_y-1}" text-anchor="end">{unit}s</text>')
    # end labels, collision-spaced
    labels = [(Y(r["final_yds"]), f'{r["final_yds"]} yds', "--s-actual")]
    for b, cvar, tag in (("draftkings", "--s-dk", "DK"), ("fanduel", "--s-fd", "FD")):
        if series.get(b):
            labels.append((Y(series[b][-1][1]), f'{tag} {series[b][-1][1]:g}', cvar))
    labels.sort()
    for i in range(1, len(labels)):
        if labels[i][0] - labels[i-1][0] < 15:
            labels[i] = (labels[i-1][0] + 15, labels[i][1], labels[i][2])
    for y, txt, cvar in labels:
        s.append(f'<text class="dlabel" x="{M["l"]+PW+10}" y="{y+4:.1f}" fill="var({cvar})">{txt}</text>')
    s.append('</svg>')
    return "".join(s)

def card(r):
    series = load_series(r)
    unit = "targets" if r["kind"] == "rec" else "carries"
    tags = " ".join(f'<span class="tag">{t}</span>' for t in r["tags"])
    nodata = "" if series else ' <span class="tag" style="opacity:.6">no line data</span>'
    head = (f'{html.escape(r["player"])} <span class="pos">{r["pos"]} #{r["pos_rank"]}</span> {tags}{nodata}'
            f' <span class="meta">wk{r["week"]} · {r["away"]} @ {r["home"]} · vs {r["defteam"]} '
            f'(#{r["def_rank_vs_pos"]} worst vs {r["pos"]}, {r["def_ypg_allowed"]} ypg allowed) · '
            f'{r["yds_entering"]} yds entering · {r["n_events"]} {unit} → {r["final_yds"]} yds</span>')
    return f'<div class="card"><h2>{head}</h2>{chart(r, series)}</div>'

sections = []
for kind, title in (("rec", "Receiving yards (top-10 WR/TE)"), ("rush", "Rushing yards (top-10 RB)")):
    cs = [card(r) for r in inst if r["kind"] == kind]
    sections.append(f'<h1 class="sect">{title} — {len(cs)} instances</h1>{"".join(cs)}')

page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stalled Stars 2026</title>
<style>
  .viz-root {{
    color-scheme: light;
    --surface-1:#fcfcfb; --page:#f9f9f7; --ink-1:#0b0b0b; --ink-2:#52514e; --ink-muted:#898781;
    --grid:#e1e0d9; --baseline:#c3c2b7; --s-actual:#1baf7a; --s-dk:#2a78d6; --s-fd:#eb6834;
    --band:rgba(11,11,11,0.03); --gap-band:rgba(11,11,11,0.05); --border:rgba(11,11,11,0.10);
  }}
  @media (prefers-color-scheme: dark) {{
    :root:where(:not([data-theme="light"])) .viz-root {{
      color-scheme: dark;
      --surface-1:#1a1a19; --page:#0d0d0d; --ink-1:#f0efec; --ink-2:#c3c2b7;
      --grid:#2c2c2a; --baseline:#383835; --s-actual:#199e70; --s-dk:#3987e5; --s-fd:#d95926;
      --band:rgba(255,255,255,0.03); --gap-band:rgba(255,255,255,0.05); --border:rgba(255,255,255,0.10);
    }}
  }}
  :root[data-theme="dark"] .viz-root {{
    color-scheme: dark;
    --surface-1:#1a1a19; --page:#0d0d0d; --ink-1:#f0efec; --ink-2:#c3c2b7;
    --grid:#2c2c2a; --baseline:#383835; --s-actual:#199e70; --s-dk:#3987e5; --s-fd:#d95926;
    --band:rgba(255,255,255,0.03); --gap-band:rgba(255,255,255,0.05); --border:rgba(255,255,255,0.10);
  }}
  body {{ margin:0; background:var(--page); font-family:system-ui,-apple-system,"Segoe UI",sans-serif; }}
  .viz-root {{ max-width:980px; margin:0 auto; padding:24px 16px; }}
  h1 {{ font-size:18px; color:var(--ink-1); margin:0 0 4px; }}
  h1.sect {{ font-size:16px; margin:22px 0 10px; }}
  .sub {{ font-size:13px; color:var(--ink-2); margin:0 0 6px; line-height:1.5; }}
  .legend {{ display:flex; gap:18px; font-size:12.5px; color:var(--ink-2); margin:0 0 10px; flex-wrap:wrap; }}
  .legend span {{ display:inline-flex; align-items:center; gap:6px; }}
  .sw {{ width:14px; height:3px; border-radius:2px; display:inline-block; }}
  .card {{ background:var(--surface-1); border:1px solid var(--border); border-radius:12px; padding:16px 18px; margin-bottom:16px; }}
  h2 {{ font-size:14px; color:var(--ink-1); margin:0 0 8px; font-weight:600; }}
  .pos {{ color:var(--ink-2); font-weight:500; }}
  .tag {{ font-size:11px; color:var(--ink-2); border:1px solid var(--ink-muted); border-radius:9px; padding:1px 7px; font-weight:500; vertical-align:1px; }}
  .meta {{ display:block; font-size:12px; color:var(--ink-2); font-weight:400; margin-top:3px; }}
  svg {{ display:block; width:100%; height:auto; }}
  svg text {{ font-family:inherit; }}
  .tick, .qlbl {{ font-size:11px; fill:var(--ink-muted); }}
  .dlabel {{ font-size:12px; font-weight:600; }}
</style></head><body>
<div class="viz-root">
  <h1>Stalled stars vs live lines, 2026 season</h1>
  <p class="sub">Top-10 player vs a bottom-10 defense at their position, where a scoreless stretch
  (<strong>blanked</strong>: 20+ game-minutes shaded; <strong>dry</strong>: 4+ targets &le;25 yds / 8+ carries
  &le;30 yds, dashed line) was followed by 30+ yards. X-axis is the game clock; book lines are 5-minute
  the-odds-api snapshots mapped onto it (flat through halftime). Rug: every target/carry (faded = incomplete
  or stuffed). Ranks use data entering that week.</p>
  <div class="legend">
    <span><span class="sw" style="background:var(--s-dk)"></span>DraftKings O/U line</span>
    <span><span class="sw" style="background:var(--s-fd)"></span>FanDuel O/U line</span>
    <span><span class="sw" style="background:var(--s-actual)"></span>cumulative yards</span>
  </div>
  {"".join(sections)}
</div>
</body></html>"""
open(f"{ROOT}/reports/blanked_2026.html", "w").write(page)
print("wrote reports/blanked_2026.html,", len(inst), "charts")
