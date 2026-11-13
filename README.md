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
| `live/layers.py` | Swiss cheese layers for pregame picks: each system's named vetoes, and a ledger per system (`docs/layers_<system>.json`) that grades what every layer drops |
| `analysis/cfb_top25_lines.py` | CFB Top-25 line check: margin rating + common opponents vs the posted spread (`--backtest`); the <=4-week rule runs live as the NCAAF Pregame Picks "Game Spreads" section (`live/cfb_poller.py` `shared_pick`) |
| `analysis/ol_out.py` | Good Matchups over backtest at closing prices, and RB rushing props when OL regulars are out (snap counts), 2024-26 |

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
- Team rate inputs are shrunk (2026-11-08): the model's pass share, yards per pass attempt and yards per carry
  come from the live box score, and at 3:49 of Q1 in CIN @ ATL one 16-yard carry made the Bengals' yards per
  carry 16.0 against a training mean of 3.8 (sd 1.6): +1.4 logits, calls on Chase, Gesicki and Higgins at
  0.70-0.75 that fell to 0.41 one carry later. Both `analysis/v2_model.py` and the poller now pull the three
  rates toward league norms with 10 pseudo-plays (`team_rates`); the same calls rescore 0.47-0.49. On the shipped
  rec weights this moves weeks-6+ AUC .533 -> .522 (noise); a pooled refit on the shrunk features was worse (.48),
  so the weights stay. The rec calibration is refit on every held-out week 6+ quote (6,163 quotes, 267
  player-games, `analysis/fit_calib.py` now reads `data/week7/oos_preds.json` for both markets) instead of the
  week-7 archive alone: raw P>=0.6 first flags go 104-98, raw >=0.8 16-4; calibrated 0.60 now needs raw 0.79
  and tops out at 0.76. The old table had a 0.38 -> 0.69 step between raw 0.78 and 0.82 fit on one week.
- Game Spreads rule on 2021-2024 (`analysis/cfb_shared_seasons.py`, 2026-11-08; scores, ranks and kickoffs from ESPN's weekly
  scoreboards, lines from the-odds-api historical snapshots taken before each game day's kickoffs, FanDuel else DraftKings,
  159 snapshots / 1,590 credits; 2025 re-run through the same code gives 27-13, matching the earlier backtest): **no edge.**
  Ranked games, 2+ shared opponents met <=4 weeks apart, line 4+ from the implied spread: 24-22, 25-30, 27-24, 15-25 for
  2021-24, 118-114 (51%) pooled with 2025; 6+ gap 92-93, 8+ gap 70-74, 3+ shared opponents 66-65. Both teams ranked
  30-23 (57%) is 2025 plus noise (2-6 in 2024); home-side bets 65-49 vs away 53-65 is not a pattern worth a rule. One shared
  opponent 78-86. 2025's 26-14 was the best of twelve variants tried on one season; the 2026 forward test is 7-6 through
  week 10 (2-4 on 2026-11-07). The rule is a describer, not a pick.
  Vetoes for the worst disagreements (2026-11-12, `veto_table` in the same script, after Ole Miss +3.5 lost to Georgia by
  35 on the week's biggest gap, 33): fifteen candidates on the 232 rule bets of 2021-25, shown as the bets a veto drops |
  keeps. Bet against a top-5 team 9-15 | 109-99; gap over 20 6-9 | 112-105; implied 14+ from the rating's spread 26-33 |
  92-81. Rating leaning the other way (56-56), the line's underdog made a favorite (16-16) and the 4-week filter moving
  the number 7+ (17-13) carry nothing. No veto lifts the kept bets past 53%: they trim the ugliest misses, not the rule.
  Stacked as layers (top-5 opponent, gap over 20, implied 14+ from the rating, away bets; any one drops the bet): kept
  50-27 (65%; 6-7, 11-5, 15-6, 9-7, 9-2 by season), dropped 68-87. Judged on 2021-23 (kept 32-18) the stack went 18-9 on
  2024-25, but the four layers were picked from fifteen with all five seasons in view, and the away layer alone does
  most of it (home bets 65-49). 2026 week 10: kept 0-2, dropped 2-2. Live from 2026-11-12 as `SPREAD_LAYERS` in
  `live/cfb_poller.py`; every candidate and the layers that caught it are graded in `docs/layers_cfb_game_spreads.json`.

## Pregame pick layers (Swiss cheese)

Every pregame pick system works the same way: its model proposes each candidate it likes, and a stack of named
veto layers decides which ones are bet. A candidate any layer catches is not bet, but it is still written to the
system's ledger with the layers that caught it and graded at the final, so each layer has its own forward record
(`record` in the ledger: `kept` and one `[wins, losses]` per layer; a pick two layers caught counts for both).
A layer stays while the picks it drops lose, and comes out when they don't.

| system | model proposes | layers | ledger |
|---|---|---|---|
| `cfb_game_spreads` | `shared_pick` in `live/cfb_poller.py`: ranked game, 2+ shared opponents <=4 weeks apart, line 4+ from what they imply | `SPREAD_LAYERS`: top5, gap20, rating14, away | `docs/layers_cfb_game_spreads.json` |
| `nfl_rb_unders` | `live/poller.py`: RB rushing line 20.5-41.5, usual QB starts, bottom-third scoring team | `RB_UNDER_LAYERS`: explosive | `docs/layers_nfl_rb_unders.json` (caught picks only, ungraded; `docs/rb_skips.json` is still the graded log) |

To add a layer, add a `(key, why, test)` row to the system's list: `test(pick, ctx)` is true when it catches the pick.
To add a system, give it a list and a `layers.Ledger(name, LAYERS, DOCS)`, call `layers.vetoes` on each candidate,
`put` it in the ledger and `grade` it at the final (`live/layers.py` has the whole interface and a self-check).

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
- Good Matchups over + OL starters out (`analysis/ol_out.py`, 2024-26 closing lines at real prices, 2026-11-12; needs
  nflverse `data/snap_counts_{season}.csv.gz`): **the Good Matchups over has no edge.** Top-N season ypg vs a bottom-10
  defense, over at the closing line: 424-443 (48.9%, roi -6.9%); 2024 -1%, 2025 -11%, 2026 -13%. RB rushing is the drain
  (125-162, -16.5%); receiving is vig-level (299-281, -2.1%). The live ledger agrees (10-12 through 2026 week 9). The
  book already prices the matchup: lines sit at 0.86x season ypg on these. Stale season averages make it worse, not
  better: recent form down (last 3 < 0.8x ypg) 78-104 (-17.4%), and RB rush overs with an OL regular out 32-50 (-25.9%).
  OL regular out (85%+ snaps in 75%+ of earlier games, now under 50%) and RB rushing unders: 0 out 51%, 1 out 53%
  (roi +0.6%), 2+ out 46-31 (59.7%, +13.4%, z 1.7) but 2025 went 21-20 — a lead, not a rule. Lines barely move for it
  (line minus ypg -1.9 vs -2.5). No OL quality data (nflverse contracts stop at 2022), so an All-Pro out counts the
  same as a backup out. Colts after Quenton Nelson (wk 8-9): Taylor 21 carries 3.3 ypc, 29% stuffed (18% before), but
  the other RBs ran 6.5 ypc on 19 — two games, unreadable.
  Winners vs losers inside the Good Matchups over (same 2024-26 bets; 26 conditions, so ~1 hits |t|>2 by luck): no
  slice is positive in all three seasons. Negative every season: the over at plus money (no-vig < 48%) 6-30 (-61%),
  last 3 < 0.8x ypg (-17%), away (-11%; home -2%), RB rushing (-17%). Filtering to recent form 0.8x+ and a bottom-half
  scoring defense leaves 211-198 (-2%): the filters only shrink the loss to the vig. Finals vs line: median -0.5, mean
  +4.5 (winners beat by a median 19.5, losers miss by 14.5); the line sits at the median, so the over needs a real edge.
- Other losses through 2026 week 9 (2026-11-12, ledger `data/fly/docs/calls.json`, autopsy `analysis/week_calls.py`):
  live calls 29-22 (57%, z ~1.0 vs a coin), 5-6 since the week 8 calibration refit. 12 of 22 losses got zero yards after
  the call, and week 8-9 losers had their team up 8-21 with a 38% pass share after. On the 6,163 held-out quotes, though,
  up 8+ hits *better* (58% at raw p>=0.6 vs 47% within 7), so no 8+ veto (14+ is vetoed: see below). Raw p 0.51-0.68 calibrates
  to 0.47; only raw 0.79+ reaches 0.6. Rush calibration tops out at 0.47, so rush never triggers. RB unders 2-2: both
  losses were one long run (McGowan 36 of 45 on 6 carries, Hampton 32 of 60). The explosive-rate veto went 1-1 (Hall 61
  over 38.5 saved, Allen 33 under 34.5 missed). WR-mate unders 0-2: the WR1 soaked up the targets (Lamb 16 targets for 195
  with Pickens out, Cooper 11 for 58), the opposite of the 7-18 backtest. Two bets; keep the forward test running.
- Leads and live receiving calls (2026-11-12): **the book prices the lead; the model doesn't.** Play-by-play 2020-26
  (WR/TE/RB with 40+ prior ypg): rest-of-game receiving as a share of normal (ypg x time left) is 0.74x up 8-13 at
  half, 0.61x up 14+, 0.43x up 14+ at end Q3, 0.23x mid Q4; trailing teams run 1.03-1.54x. On held-out live quotes the
  book's second-half line needs 0.54x normal up 8-13 and 0.48x up 14+ (0.84x within 7, 1.03x down 8+), and receivers
  beat those shadings 41-51% at half / mid Q3 in every bucket, so the market is roughly fair on game script. The model
  isn't: `price_ratio` divides by an unadjusted ypg x time-left baseline, so a lead-shaded line reads as a discount
  (weight -0.29), and the lead terms net to ~0 (-0.003 logit/pt at half, +0.005 late). Week 9 live calls with the team up
  8+: 1-4 (price_ratio 0.29-0.54); ledger total up 8+: 4-5. Held-out raw p>=0.6: up 8-13 hit 65%, up 14+ 44% (23
  player-games). Fix to test: lead- and clock-adjusted baseline in `price_ratio`, retrain, compare held-out.
- Retrain with a lead-adjusted baseline (`analysis/lead_factor.py`, 2026-11-12): **no ship; the receiving model has no
  held-out signal to fix.** Factor table from 2020-25 pbp (rest-of-game yards vs ypg x time left by lead x clock,
  relative to all states at that clock; rec up 15+ at 85% elapsed 0.25x, down 9-14 1.55x; rush the mirror image).
  Swapped into `price_ratio` and refit on the deployed recipe (pooled wk 2-5, 999 quotes): rec held-out wk 6-9 (9,325
  quotes, 360 player-games) AUC .473 current vs .472 adjusted (bootstrap gain -.011 to +.008), flags at P>=0.6 54% vs
  52%. Walk-forward on rec (train all earlier weeks, test wk 6/7/8/9): full 19 features .485 / .482, compact 7
  (elapsed, ypg, score terms, needed, price_ratio, discount) .509 / .500; flags at P>=0.6 99-98, 94-98, 90-94, 93-96.
  The book prices game state and remaining volume well enough that these inputs don't beat it. The earlier "64% at
  P>=0.6 on wk 6-7" was 7-game noise. The live 29-22 is within luck (z ~1).
- Lead veto on live receiving calls (2026-11-12, `live/poller.py` `LEAD_VETO`, from week 10): no rec over call while the
  player's team leads by 14+. Replay of the deployed model + calibration + gates on held-out wk 6-9 quotes: 21 calls,
  15-6; up 14+ 2-3, everything else 13-3 (up 8-13 3-0, so the cut is 14, not 8). Backed by the pbp mechanism (up 14+
  is the worst bucket for beating the book's shaded line at every checkpoint) and held-out quotes (up 14+ 44%). Small
  sample: vetoed calls are kept as `blocked` (`why: "leading"`) in history.json for grading. Rushing is not vetoed.
- NFL live lead call, forward through 2026 week 9 (2026-11-12, `data/fly/docs/archive/nfl_w{7,8,9}.json`): one call,
  BUF @ MIN, MIN +13.5 (+210) at Q4 6:17 down 20-6, lost 23-6 (0-1). The 23 leads the filters stopped, graded at the
  first FanDuel quote after the lead on the trailing side: 8-15 (side_rule 4-9, not_top 3-4, late 1-1, seen 0-1), so
  the blocks saved ~6.9 units. Leaders covered 16 of 24 incl. the call (z ~1.6); the 2024-25 backtest's top-vs-top
  subset went the other way (27-15 trailing), so this is noise until the rule has its own forward sample.
- Pregame unders re-checked (2026-11-12, all closing props 2024-26 at real prices via `prop_search.build`):
  **RB under holds; WR-mate under doesn't need the mate.** RB rushing under, layer by layer: every RB -1%; line
  20.5-41.5 +15/+7/+9% by season; + usual QB 200-136 (+21/+6/+12%); + bottom-third offense 58-28 (+25/+32/+26%);
  + 10+ run rate <=12% 47-18. Neighbors move smoothly (20.5-30.5 +17%, 30.5-41.5 +9%, 10.5-20.5 -4%, 41.5+ -10%/-7%;
  offense thirds +3 / +13 / +28%; veto cut 0.10-0.14 all +17 to +37%). The layers were added on these same games, so
  the honest number is the core's 2025 (+6%, the season after it was found): expect single digits, not +28%.
  WR-mate under: `wr_mate_out.py` calls a teammate "out" if he drew no target (hindsight). With a pregame definition
  (60+ ypg WR teammate inactive, from snap counts) top-20 WR unders go 34-23 (+3/+16/+35%), but the same WRs with
  that teammate *playing* go 132-97 (+9%, positive every season) and with no such teammate 202-209 (-7%). The mate
  being out adds ~4 points over noise, and the 57 bets come from 18 team-seasons (MIA 2025 x11, HOU 2024 x9, NYG 2025
  x9). The teammate-played under (+9%, z ~2.3) is a new lead found looking, not a rule.
  Dropped 2026-11-12: the poller no longer makes the WR teammate-out under or publishes `docs/wr_unders.json`, and the
  page's WR Unders section is gone. The Good Matchups over is still skipped when that teammate is out (over 23-34).
  The RB under feed now pulls only the rushing market (1 credit a game instead of 2).
- Good Matchups rescue attempts (2026-11-12): **no rule found.** Flipped to the under: -4% overall (RB rushing +3%,
  receiving -8%). Positive every season only in thin slices found by looking: the over at plus money (no-vig < 48%)
  under 30-6 (+11/+38/+65%), but across *all* props that price signal is flat (under 76-56 at over < 48%, -3%; the
  under costs ~-129), so the 30-6 is a carve-out of noise; RB rushing with recent form < 0.8x ypg under 51-20 (+25/+24%,
  one 2026 bet). Control: the same top-N players *not* facing a bottom-10 defense win the receiving under +4% every
  season (547-448), the Good Matchups receiving under -8%: the matchup is priced in, no direction left to bet.
- Good Matchups filter search (2026-11-12): 72 conditions + pairs (1,666 filters with 40+ bets) found on 2024, tested on
  2025-26. Best 2024 z 3.77 vs 3.79 at the 90th percentile of shuffled margins (luck); 4 of the top 25 made money on
  2025-26. Named factors: away -8/-10/-22%, underdog, bottom-third offense, bottom-third record and implied team total
  < 22 lose every season, but cutting them leaves the rest around -2% (home 214-202). The one survivor, home & implied
  team total 22.5-25.2 (75-42; 2025-26 36-26 +10%), is a spike: home 20-22.5 -15%, 25.2-27 -26%, 27+ +3%, widening to
  21-26 drops 2025 to -15%, and half its edge is RB receiving 26-4. Paper-track it if anything; it is not a rule yet.
- Good Matchups, 4 picks a week (2026-11-12, live from week 10, `live/poller.py` `GM_PICKS`, `gm_score`, `gm_top`):
  every eligible matchup gets a point for home, favored, offense outside the bottom third in scoring, record > .400
  and last-3 form >= 0.8x ypg; the top 4 (ties: worst defense) are bet, alerted and shown. The rest are not picks: off
  the page and out of calls.json. A pick locks at its kickoff; open slots go to later games. Backtest of 7 rankings,
  top 3 a week 2024-26: this one 60-45 (+15/+5/+1%), ypg x defense 57-48 (+3%), best player 49-56, worst defense first
  (the old page order) 45-60 (-19%); random 3 -8% (5-95% -20 to +7%). Top 4 this way: 82-58 (+18/+7/+4%, +11%). Every
  candidate goes in `docs/layers_nfl_good_matchups.json` (layer `not_picked`), graded at the final, so the rule gets a
  forward record against the matchups it skipped. A Good Matchup the poller already bet at kickoff (a pregame call,
  live or in this week's calls.json) counts as a pick even unmarked, so a restart or rule change can't make a fifth; this
  is how Likely (week 10, bet by the old code before the deploy) is pick #1. Points and marks freeze at kickoff (ESPN
  drops the spread once a game is live), and nothing is ranked until the scoring ranks load after a restart.
