# Data guide

Everything collected for the live-betting project: in-game player prop lines (receiving and
rushing yards), live game spreads (NFL and college), the model's calls and how they settled,
plus the historical play-by-play and odds pulls the model was built from.

The live system runs on Fly.io (app `live-betting-value`) and writes to a volume mounted at
`/data`. `sh analysis/pull_fly.sh` copies that volume here into `data/fly/` (overwrites with the
server's current copy; the pull time is in `data/fly/PULLED_AT`). Re-run it after each slate.

## Conventions (apply everywhere)

- **Game ids** are ESPN event ids (strings, e.g. `"401873011"`). Prop keys are
  `"{espn_id}|{player full name}|{market}"`.
- **market**: `rec` = receiving yards, `rush` = rushing yards.
- **gc** = game-clock seconds elapsed, 0 at kickoff to 3600 at the end of regulation
  (overtime runs past 3600). Q2 starts at 900, Q3 at 1800, Q4 at 2700. All charts and snapshots
  use gc, never wall time.
- **clock** strings like `"Q2 13:17"` are the same moment in ESPN's display form.
- **Lines** come from the-odds-api, FanDuel preferred, DraftKings when FanDuel has none.
  Prop lines are the over/under yards number. A quote older than ~6 minutes is treated as
  suspended and recorded as `null`.
- **Spreads are the home team's spread**: negative = home favored (`-3.5` = home -3.5),
  positive = home underdog. `pregame`, live `L`, and `fair` all use this sign.
- **margin** = home score − away score. **score strings** `"as-hs"` (e.g. `"16-10"`) are
  away first, home second.
- **Timestamps**: `t`, `settled_t`, `ts` are Unix seconds; ISO strings are UTC.
- **Polling**: the live pollers run every ~180 s (props, NFL spreads) and ~120 s (CFB spreads),
  so consecutive snapshots are a few game-clock minutes apart. Snapshots with the same gc are
  repeated polls during stoppages or before kickoff; dedupe on gc if needed.
- **rebuilt / backfilled**: when the poller joined a game after kickoff, the missing part was
  reconstructed from the-odds-api *historical* snapshots (5-minute spacing) plus the ESPN
  play log. `"rebuilt": true` on a call means it was found in that reconstruction, not seen live.

## The model (for interpreting P and calls)

Logistic model, P(player finishes over the current live line). It scores only inside game-clock
600–3060 s (its training window); outside that, P is `null`. Statuses: P ≥ 0.50 first sets a
`signal`; P ≥ 0.70 first sets a `call` (the dashboard's "model pick"). NEAR = 0.55–0.69,
TRIGGER = ≥ 0.70. Deployed weights: `live/model_weights.json` (repo); priors: `live/priors.json`
and `live/def_priors.json` (repo).

From week 8 on (post week-7 review): scoring is per market. `rec` keeps the pooled v2 weights
(`live/model_weights_pooled_v2.json`; its retrain tested below random on week 7, AUC 0.396);
`rush` has its own model (`analysis/retrain_markets.py`, trained on weeks 2–7 including the
week-7 archive; week-7 out-of-sample AUC 0.605) — the pooled model's is_rush penalty had made
live rush calls impossible. P is isotonic-calibrated per market (`live/calib.json`, fitted by
`analysis/fit_calib.py`: rec on week-7 archive snapshots, raw 0.70 ≈ 0.36; rush on its week-7
out-of-sample predictions, plateau ≈ 0.58–0.60), and a call/TRIGGER additionally requires the
game-state gate: score_diff ≥ −3 and (3+ targets/carries or yards ≥ 60% of the line), and
never fires against a top-3 defense **by last-3-games form** (team yards allowed per game over
the 3 most recent games played — a bye pushes the window back; `live/def_form.json`, exported
weekly by `analysis/export_live.py`, season-to-date fallback if absent). The call/TRIGGER
threshold is calibrated P ≥ 0.60 (−110 breakeven is 0.524); NEAR = 0.55–0.59. Week-7 archive
P values are raw pooled-model scores, called at raw P ≥ 0.70; the form-based veto was applied
retroactively (entering-w7 form top-3: rush JAX/CHI/CAR, rec MIN/SEA/NYG), removing eight
rec calls into MIN/NYG that went 1-7 — week 7 live stands 22-15 after cleanup.
`docs/watchlist.json` is now written by the poller itself: it advances to the upcoming week
automatically once the current slate finishes (schedule from ESPN; player teams and ranks
from `live/priors.json`, which `analysis/export_live.py` exports).

The 19 model inputs, in order (`calls.json → feats`; every `x` array follows it):
`elapsed` (fraction of game), `ypg` (player season yds/game), `ype` (yds per target/carry),
`def_ypg` (opponent yds/game allowed at the position), `ev_sofar` (targets or carries so far),
`yds_sofar`, `drought` (minutes since the player last gained yards), `ev_last10`, `yds_last10`
(last 10 game minutes), `score_diff` (player's team minus opponent), `diff_x_late`
(score_diff × elapsed), `pass_share` (team pass-play share so far), `team_ypp`, `team_ypr`
(team yds per pass / per rush so far), `pace` (team plays per minute vs normal), `is_rush`,
`needed_rem` (yards still needed for the line), `price_ratio` (live line / pregame line),
`discount` (expected remaining yards / needed).

**Settlement**: a live call (model pick) **wins** the moment the player's yards pass the line
that was called; otherwise it is settled against the final box score. A pregame call is the
same over at the closing pregame line, made at kickoff for watchlist matchups.

## `data/fly/` — collected live (from the Fly volume)

### Player props

**`docs/archive/props_w{week}.json`** — the main prop dataset. Every prop the poller quoted
in a finished game, flagged or not (week 7: 204 props across 12 games, 78 reached a 50%
signal, 42 got a model pick, 8 got a pregame call). Dict keyed by prop key:

| field | meaning |
|---|---|
| `player`, `market`, `game` (`"AWY @ HOM"`), `logo` | identity; `logo` is the player's team |
| `l0` | closing pregame line (taken at kickoff) |
| `close` | last live line quoted |
| `final` | final yards from the ESPN box score |
| `pmax` | highest P the model gave |
| `snaps` | list of `[gc, yards_so_far, targets_or_carries_so_far, live_line, P]`; P is `null` outside 600–3060 s or when no line was posted |
| `signal` | first P ≥ 0.50: `{clock, gc, line, cum (yards at that moment), p, rebuilt, x, t}`, or `null` |
| `call` | first P ≥ 0.70 (model pick): same fields plus `score` (`"PIT 3 – NO 0"`), `result` (`win`/`loss`), `won_at`, `won_yds`, `settled_t`; or `null` |
| `pre` | pregame call: `{type:"pregame", src:"closing", line (closing line), gc:0, result, won_at, won_yds, settled_t}`, or absent |

**`docs/calls.json`** — season ledger of every settled call. `{feats: [...19 names], calls: [...]}`.
Each call: `week, espn_id, game, player, market, type` (`live` = model pick, `pregame`),
`pregame_line, line` (line called), `cum` (yards at the call), `p, gc, clock, score, final,
result, won_at, won_yds, settled, settled_t, rebuilt, x` (model inputs at the call; absent on
pregame calls).

**`live/history.json`** — working state for props in games not yet final (moves into the
archive at the final whistle). Keyed by prop key: `l0, l0_closing, last_change` (gc of the
player's last yardage gain), `snaps` (same layout), `meta`, `signal`, `call`, `pre`, `pmax`.

**`live/pregame_lines.json`** — closing pregame prop lines per game:
`{espn_id: {"Player Name|market": line}}`.

**`live/paper_log.csv`** (no header) — one row each time a prop entered TRIGGER:
`utc_time, player, market, line, yards, targets_or_carries, p, clock`. The 27 early rows
through 2026-10-25 14:40 UTC have p < 0.70 (an older version also logged NEAR); trust
`calls.json` for call outcomes.

**`docs/watchlist.json`** — this week's pregame matchups (top-20 WR / top-10 TE / top-15 RB
by season yds/game vs a bottom-10 defense at the position):
`{week, entries: [{player, pos, rank, ypg, market, team, opp, def_rank, def_ypg, game, kick, kick_iso}]}`.

### Game spreads

**`docs/archive/nfl_w{week}.json`**, **`docs/archive/cfb_w{week}.json`** — every tracked game.
Dict keyed by ESPN id:

| field | meaning |
|---|---|
| `meta` | `{game, habbr, aabbr, hlogo, alogo, hcolor, acolor}` |
| `pregame` | pregame home spread |
| `final` | `"as-hs"` |
| `snaps` | NFL: `[gc, live_home_spread, margin, situation]`, situation like `"DAL ball · PHI 35 · 2nd & 7"` or `null`. CFB week 8 (collected before the situation field existed): `[gc, live_home_spread, margin]`. Capped at the last 200 per game. |
| `events` | key plays: `[gc, label, side ("home"/"away"), play text, home_score_after, away_score_after, class]`; class is `score`, `to` (turnover), `mom` (momentum: explosive plays, 4th-down stops/conversions), or `inj` (injury) |

**`docs/nfl_games.json`**, **`docs/cfb_games.json`** — schedule and results registry for this
week and next. Keyed by ESPN id: `week, date` (kickoff UTC), `state` (`pre`/`in`/`post`),
`detail, aabbr, habbr, alogo, hlogo, hid, as, hs, pregame` (home spread), `has_hist`
(true = tracked live; a spread archive exists).

**`live/nfl_spread_log.csv`** (no header) — one row per EDGE/SWING flag:
`utc_time, game, clock, score ("as-hs"), pregame, live, fair, edge, status`.
`fair` = −(margin × (1 − 0.28 × rem) + possession value + (−pregame) × rem), rem = fraction of
game left, in home-spread sign; `edge = live − fair` (positive edge favors the home side).
EDGE = |edge| ≥ 4; SWING = line moved ≥ 4 in ~10 game minutes beyond the score change.
The 0.28 margin damping is the pooled 2025–26 drive-end fit from `analysis/momentum.py`
(leads give back ~14% at halftime); rows logged before 2026-10-27 used the undamped fair line.

**`live/hist_nfl_spreads.json`**, `hist_cfb_spreads.json` — working state for spreads in games
not yet final (same layout as the archive plus `poss`, `t`).

### Dashboard plumbing (not needed for analysis)
`docs/state.json`, `docs/nfl_spreads_state.json`, `docs/cfb_state.json` (current live board),
`docs/nfl_history.json`, `docs/nfl_spread_history.json`, `docs/cfb_history.json` (live chart
feeds), `live/backfilled*.json` (game ids already backfilled).

## Elsewhere in `data/` (local only, from before the live system)

| path | what it is |
|---|---|
| `pbp_2024.csv.gz`, `pbp_2025.csv.gz`, `pbp_2026.csv.gz` | nflverse play-by-play (one row per play; standard nflverse columns such as `game_id, week, posteam, defteam, game_seconds_remaining, receiver_player_id, receiving_yards, rusher_player_id, rushing_yards, pass, rush`). The model's training data. |
| `games.csv` | nflverse schedule/results with closing spread and total. Careful: nflverse `spread_line` is the expected home margin, so **positive = home favored** — the opposite sign of the live data's home spread. |
| `roster_2026.csv` | nflverse roster: `gsis_id, full_name, position, team` … joins play-by-play ids to names. |
| `props/{odds_event_id}/{market}_{snapshot}.json` | Raw the-odds-api historical event-odds responses (1,475 snapshots, weeks 4–7), FanDuel + DraftKings, `player_reception_yds` / `player_rush_yds`, mostly 5-minute spacing through games. `data.bookmakers[].markets[].outcomes[]` = `{name: Over/Under, description: player, price (American), point (line)}`. |
| `props_series/{season}_{week}_{AWY}_{HOM}_{market}.csv` | The same pulls flattened: `snapshot, book, player, side, point, price`. 30 selected game/market pairs (the drought cases studied), every player in each. |
| `blanked_instances.json`, `bowers_rec_yds.csv` | Early exploratory cuts (players blanked for long stretches; one player's line chart). |
| `week7/` | Working files from the week-7 call review (`autopsy*.json` = per-call usage after the call). |

Mac-only live files from Saturday 2026-10-24, before the move to Fly, in `live/` (repo root):
`cfb_spread_log.csv` and `cfb_log.csv` (CFB EDGE/SWING flags, same columns as the NFL spread
log) and `cfb_history.json` (CFB week 8 line histories; already merged into
`data/fly/docs/archive/cfb_w8.json`).

## Coverage and caveats

- Live props and NFL spreads: **week 7 only** so far (12 of 14 games). Not tracked: NE @ CHI
  (Thursday, before the system started) and DAL @ PHI (Monday night; lands in the archive at
  its final whistle — re-run the pull).
- CFB spreads: week 8 (14 games). CFB has no props.
- One week of calls is ~50 settled bets: treat any effect as a hypothesis.
- Prop P values before the 600 s / after the 3060 s mark are `null` by design, not missing data.
- Lines are FanDuel-first, DraftKings fallback, mixed within a game when FanDuel pulled a market.
