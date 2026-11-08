# Live Betting Value — NFL Player Props

Finding +EV live over bets on player yardage props: books time-decay lines
during quiet stretches; we model whether the price has sagged below the
player's realistic remaining production.

## Current state (2026 week 7)

- **v2 model** (`analysis/v2_model.py`): logistic P(final > quoted line) from
  live game state + price features (needed yards, price vs pace, discount from
  pregame). Held-out wk6-7: 64% over wins at P>=0.6 vs 56.7% sample base;
  mid/late-game flags 10/12. Sample is burst-biased — validation on 7 unflagged
  wk6 games in `data/props_series/` is the honest check.
- **Live monitor** (`live/poller.py`): polls the-odds-api (2 credits/game/poll)
  + ESPN live boxscores, scores every quoted player, serves a dashboard at
  localhost:8766, sends macOS + browser notifications at NEAR (P>=0.5) and
  TRIGGER (P>=0.6), logs paper trades to `live/paper_log.csv`.

```bash
# game day:
cd live && python3 poller.py --poll 300        # then open localhost:8766
# refresh model weights/priors after new data:
cd analysis && ../.venv/bin/python export_live.py
```

## Pipeline

| script | what |
|---|---|
| `fetch_data.sh` | nflverse games.csv + play-by-play (re-fetchable, gitignored) |
| `analysis/prop_series.py` | walk historical 5-min odds snapshots for one game/market |
| `analysis/pull_instances.py` / `pull_flags.py` / `pull_validation.py` | batch pulls |
| `analysis/find_blanked.py` | find stalled-star instances (blanked/dry, TOP_N + OUT env) |
| `analysis/blanked_report.py` | per-instance charts w/ line overlays -> reports/ |
| `analysis/burst_model.py` | v1: P(25+/20+ yds in next 10 game-min), no price |
| `analysis/v2_model.py` | v2: price-aware over model (train wk<=5, test wk6+) |
| `analysis/export_live.py` | train v2 + write live/{model_weights,priors,def_priors}.json |
| `live/poller.py` + `live/web/` | live monitor + dashboard |
| `analysis/cfb_top25_lines.py` | CFB Top-25 line check: margin rating + common opponents vs the posted spread (`--backtest`); the <=4-week rule runs live as the NCAAF Pregame Picks "Game Spreads" section (`live/cfb_poller.py` `shared_pick`) |

## Findings log

- ~55% of NFL games finish one-score, but conditional on trailing 9+ a live
  +8.5 ticket is ~50% — spread markets look efficient (2024-26 data).
- CFB 2026 (570 FBS games, `analysis/cfb_spread_model.py`): leads leak 0.31 of the margin
  per fraction remaining, possession worth 0.73x linear EP, both significant; momentum
  events add nothing out of sample (same as NFL). Fair-line EDGE flags on 16 archived
  live games covered 9/19 game-sides at |edge|>=4 — no edge shown yet.
- Prop lines decay 13-46 yds during droughts; decay matches mean remaining
  production — droughts alone are NOT value (8.8% burst rate vs 9.8% base).
- v1 burst model: AUC 0.66; drought + top-decile = 46% burst rate, but flags
  bet blind at quoted lines went 7/13 (breakeven).
- Beating your own prop line does not carry (`analysis/gm_beat_history.py`, 823 cohort bets 2024-26):
  players 3/3 over their last three lines go 52% at the next closing line, mean beat 20+ goes 51%,
  corr(last-3 beat, next beat) = +0.03. The book moves the next line only ~0.9 yds per 10 yds beaten,
  and that is right: single-game beats are noise. Lines sit ~10 yds under season yds/game by design
  (right-skewed finals), so a big ypg-minus-line gap is normal, not an edge (gap 20+: 56%, p=0.27).
  Only the cold end shows anything: 0/3 over the last three goes 36% (18-32), a fade not an over.
- CFB Top-25 lines (`analysis/cfb_top25_lines.py`, 2025-26 weeks 5+, 764 priced games): a ridge margin
  rating (prior-season prior, HFA ~3.8) disagrees with the line a lot but does not beat it: 51% overall,
  52% on ranked games at |edge|>=4, 13-19 at >=8 — the biggest disagreements are the rating being wrong,
  not the market. Ranked teams covered 56% straight up (108-85, positive both seasons) with no conditioning.
  Common opponents (>=2 shared, bet the side they favor by 4+): 72-59 in 2025, 9-12 in 2026 — not a thing yet.
  Strength of the shared opponents: weighting by it or capping margins at 21 changes nothing; keeping only
  shared opponents in the top half of the rating goes 39-30 on all games and 20-9 on ranked games in 2025 at a
  4+ gap, 1-4 / 1-1 in 2026 — one-season result. Against-the-spread margins vs shared opponents: 50% at any gap,
  59-67% only at 6+ (2025-driven). Shared opponent met within 4 weeks by both teams: all games 69-45 / 9-9,
  ranked 26-14 / 5-2 at a 4+ gap — the only cut where 2026 leans the same way; unranked alone 43-31 / 4-7.
  Turnovers in the shared games (cache from the halftime study): crediting 4 pts per turnover leaves the hit
  rate where it was (ranked 21-10 / 4-1 on fewer bets); dropping shared games with a 2+ turnover swing or 14+
  points off a team's norm cuts the sample to a third with no gain. Rule stays unadjusted; turnovers are context. Twelve variants tried on the same
  2025 games now; forward test the <=4-week rule, don't bet the records.
  Home field by conference (`--hfa`, FBS-vs-FBS, pooled 2025-26): power conferences ~4-5 (SEC 4.8, Big Ten 4.8,
  Big 12 4.4, ACC 3.8, se ~1), Group of Five 2.5-3.7, Sun Belt 0.9 — SEC vs Big 12 is within noise; power vs
  Sun Belt is real. Fitting it per conference (`--conf`) does not change the backtest.
- Pregame shared-opponent lean as a gate on the live CFB bets (2026-11-07; lean = the <=4-week rule's side: the line 4+
  from what two or more shared opponents imply, rated on earlier weeks only): **no change to the live rule.** The lean
  rarely exists when the live bets fire: two shared opponents met within four weeks takes half a season, so it is on
  0 of 56 2026 lead bets (weeks 3-8), 28 of 144 in 2025 (all week 9+), and 17 of 89 first EDGE flags on the 82 archived
  2026 games (weeks 8-10, `data/fly/docs/archive/cfb_w*.json`). Bet side agrees with the lean / disagrees / no lean:

  | live bet | season | agree | disagree | no lean |
  |---|---|---|---|---|
  | EDGE flag, first per game-side (`fair_spread`, 0.31 / 0.73) | 2026 | 7-1 | 2-7 | 39-33 |
  | EDGE flag | 2025 | no live snapshots | | |
  | trailing team +pts, flagged top vs top (`lead_spread_value.py`) | 2025 | 2-4 | 7-10 | 31-40 |
  | trailing team +pts, every 11+ lead | 2025 | 3-5 | 7-13 | 48-68 |
  | leader -pts, college variant (only the trailing team ranked) | 2025 | 1-0 | 1-0 | 8-3 |
  | trailing team +pts, 11+ leads graded from the archive's quotes | 2026 | 2-1 | 0-4 | 14-21 |
  | leader -pts, same games | 2026 | 4-0 | 1-2 | 21-14 |

  2026 leans the lean's way everywhere (EDGE 7-1 vs 2-7, leader 4-0 vs 1-2) on 17 and 7 bets; 2025 runs the other way
  (trailing bets with the lean 3-5, against it 7-13). One season, under 20 bets per cut: not a gate. Re-run when the
  2026 archive reaches December, the weeks the lean exists; EDGE flags themselves are 48-41 on the 82 games.
- WR1 with a real WR2 out (60+ rec yds/game teammate, no target in the game) goes 7-18 (28%, p=0.03) at the
  closing over, 29-38 with a 45 ypg teammate (`analysis/wr_mate_out.py`, 2024-26). The book raises the line
  ~6 yds expecting the volume; the WR1 produces the same relative to their season average as usual
  (-8.6 vs -9.2), so the raise is the edge. Line level alone is not it (WR1s at the same line level with
  mates playing: 50%). 25 bets: a hypothesis for the under, so it runs as a small forward test: from 2026 week 9
  the poller skips the WR over and makes a pregame under (`rule: wr_mate_under`) when a top-20 WR's 60+ ypg WR
  teammate is out (injury report, or no receiving line posted for him once the game's market is up). `live/test_wr_mate.py`.
- RB unders (the live `rb_under` rule, 2024-26, 99 bets 65-34): the under cashes through yards per carry and missing
  10+ yd runs, not fewer carries (carries fall ~0.7 vs the player's last 3 for bottom-third teams and everyone else alike;
  ypc falls 4.07->3.51 vs 4.29->4.09). Lines sit only ~2.5 yds under last-3 pace for bottom-third RBs vs ~5.7 for the rest.
  Backfield competition (own/RB2 carry share, RB2 ypc) and OL continuity (nflverse snap counts, usual 5 from the last 3
  games) separate nothing. The player's own share of recent yards from 10+ runs runs backwards (low share 21-7, high 23-17).
  The team's season 10+ yd run rate is the one cut with the mechanism and the same sign every season: bottom third at
  <=12% 53-22, above 12% 12-12 (8-8, 4-4; no 2026 bet above it). From 2026 week 9 the poller vetoes RB unders above 12%
  (`RB_EXPL`, `live/team_expl.json` from `export_live.py`) and logs the skipped bets to `docs/rb_skips.json`; every pick
  records `team_expl`. 24 removed bets is still noise: the forward test is what decides it.
- v2 with price as input is the first thing that beats the sample base rate.
  Not yet proven out-of-distribution: that's the forward test.
- Game Spreads rule on 2021-2024 (`analysis/cfb_shared_seasons.py`, 2026-11-08; scores, ranks and kickoffs from ESPN's weekly
  scoreboards, lines from the-odds-api historical snapshots taken before each game day's kickoffs, FanDuel else DraftKings,
  159 snapshots / 1,590 credits; 2025 re-run through the same code gives 27-13, matching the earlier backtest): **no edge.**
  Ranked games, 2+ shared opponents met <=4 weeks apart, line 4+ from the implied spread: 24-22, 25-30, 27-24, 15-25 for
  2021-24, 118-114 (51%) pooled with 2025; 6+ gap 92-93, 8+ gap 70-74, 3+ shared opponents 66-65. Both teams ranked
  30-23 (57%) is 2025 plus noise (2-6 in 2024); home-side bets 65-49 vs away 53-65 is not a pattern worth a rule. One shared
  opponent 78-86. 2025's 26-14 was the best of twelve variants tried on one season; the 2026 forward test is 7-6 through
  week 10 (2-4 on 2026-11-07). The rule is a describer, not a pick.

## Costs (the-odds-api, 100K/mo plan)

Historical: 10 credits per market/region per snapshot request; a full game
~450/market. Live: 1 credit per 2 books per market -> 2/game/poll; a full
Sunday at 5-min polling ~1.2K. Used so far: ~14K.

## Data sources

- nflverse: games, play-by-play, rosters (free)
- the-odds-api: historical + live odds (key in .env, gitignored)
- ESPN unofficial API: live boxscores/scoreboard (free, used by poller)

## Hosting (Fly.io)

One always-on machine runs the dashboard and all three pollers (`live/start.sh`);
state lives on the `lbv_data` volume at `/data`. Deploy changes with `fly deploy`.
Locally, the same pollers still run unchanged (defaults write to `docs/` and `live/`).
