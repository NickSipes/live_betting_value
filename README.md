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
