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
- WR1 with a real WR2 out (60+ rec yds/game teammate, no target in the game) goes 7-18 (28%, p=0.03) at the
  closing over, 29-38 with a 45 ypg teammate (`analysis/wr_mate_out.py`, 2024-26). The book raises the line
  ~6 yds expecting the volume; the WR1 produces the same relative to their season average as usual
  (-8.6 vs -9.2), so the raise is the edge. Line level alone is not it (WR1s at the same line level with
  mates playing: 50%). 25 bets: a hypothesis for the under, so it runs as a small forward test: from 2026 week 9
  the poller skips the WR over and makes a pregame under (`rule: wr_mate_under`) when a top-20 WR's 60+ ypg WR
  teammate is out (injury report, or no receiving line posted for him once the game's market is up). `live/test_wr_mate.py`.
- v2 with price as input is the first thing that beats the sample base rate.
  Not yet proven out-of-distribution: that's the forward test.

## Costs (the-odds-api, 20K/mo plan)

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
