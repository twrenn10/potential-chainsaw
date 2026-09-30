# NHL Desk: Phase 1 build report (2026-09-25)

Branch `claude/nhl-pricing-engine-e9tmet`. This report covers stages 1–15 of the build order: scaffolding, ingestion, priors, the goalie mixture, the game-state simulator, pricing, immutable artifacts, walk-forward backtesting, closing-line evaluation, CLV and grading.

**Status: every recommendation is `UNVALIDATED` or `BLOCKED`. The `ACTIONABLE` lane is refused in code.**

---

## 1. Architecture map

```
                      RAW IMMUTABLE SNAPSHOTS  (nhl/data/snapshots.py)
      content-addressed, write-once, hash-verified; append-only fetch manifest
                                   │
   ┌───────────────┬──────────────┼────────────────┬──────────────────┐
 nhl_api.py      moneypuck.py    odds.py (odds)   odds.py (goalie reports)
 schedule/pbp/   team/goalie/    timestamped       timestamped starter
 boxscore/shifts skater CSVs     book prices       reports
   └───────────────┴──────────────┼────────────────┴──────────────────┘
                                   ▼
      HistoricalStore ──view(as_of)──► PointInTimeView   (nhl/data/pit.py)
      every record has available_at; the view is the only accessor
                                   │
          ┌────────────────────────┼─────────────────────────────┐
          ▼                        ▼                             ▼
  data/quality.py          features/priors.py             features/rest_travel.py
  slate health gates       player-based team priors,      rest, B2B, 3-in-4,
  (hard-fail → BLOCKED)    shrunk goalie priors           travel, time zones
                                   │
                                   ▼
                         features/ratings.py
     joint MAP Poisson fits: 5v5 xG (home, B2B, att/def) · PP/PK · penalties
     goalie goals|xG (league-centred) · league goals-per-xG calibration
                                   │
          ┌────────────────────────┴───────────────┐
          ▼                                        ▼
  models/goalie_start.py                  models/game_state.py
  P(start) per goalie:                    dt-step simulator: 5v5/4v4/PP/SH,
  confirmed / projected /                 score effects, pulled goalie 6v5,
  workload model                          empty net, 3v3 OT, shootout
          └──────────── pricing/engine.py ─────────┘
            goalie-pair mixture → batched simulation
                                   │
                                   ▼
                         pricing/markets.py
          ML · regulation 3-way · puck line · total · team total
          (push-aware; shootout +1 settlement convention)
                                   │
   market/novig.py + market/snapshots.py ─► desk/candidates.py
   de-vig, complete-group quotes at as_of    model vs no-vig edge, EV, fair price
                                   │
                                   ▼
                          governance/lanes.py
          BLOCKED (synthetic / health / stale) or UNVALIDATED (Phase 1 ceiling)
          + shadow lane (PASS/WATCH/MODEL_PLUS, informational only)
                                   │
                                   ▼
                    ledger/predictions.py  (IMMUTABLE)
      append-only SQLite · triggers · SHA-256 hash chain · pre-puck-drop rule
          │                              │                          │
          ▼                              ▼                          ▼
    desk/board.py                 backtest/walk_forward.py   ledger/bets.py
    board CSV, summary,           daily PIT refit loop,      paper placements,
    manifest, anomalies, text     then backtest/evaluate.py  settlements, limits
                                  (vs no-vig close,          ledger/grading.py
                                  calibration, CLV, gates)   ledger/clv.py
```

`nhl/pipeline.py::run_slate` is the single orchestration used by both the daily desk and the walk-forward loop, so the backtest exercises exactly the production path.

## 2. What was reused from MLB (`twrenn10/tournament_v2`)

Only sport-agnostic patterns were reused. No statistical assumptions were copied.

| tournament_v2 pattern | NHL implementation |
|---|---|
| `super_engine/data_contracts`: dataclass contracts validated in `__post_init__`; `str` enums | `nhl/contracts/{enums,schemas}.py`, adding `available_at` on every record |
| `build_market_key` / `build_bet_id` stable SHA-1 digests | `nhl/contracts/ids.py::build_market_key`, `make_prediction_id`, bet ids |
| Stage order: ingest → dedupe → signal → reference market → edge → context → confidence → decision → exports | `pipeline.run_slate`: PIT view → priors/ratings → health gates → simulate/price → no-vig reference → edge → lanes → artifacts → desk |
| `ingest/dedupe_snapshots.py` duplicate flagging | Exact-duplicate drop and count in `data/odds.py` |
| `select_reference_markets`: latest per selection, deterministic sort | `market/snapshots.py::quotes_at` (complete groups only, leg-skew limit) |
| `ContextRecord.hard_block_reasons` route to `BLOCKED`, never promoted | `governance/lanes.py` `HARD_BLOCK:*` reasons; the bet ledger refuses BLOCKED |
| `DEV_BOOTSTRAP_SOURCE` → always BLOCKED | `DataOrigin.SYNTHETIC` → `HARD_BLOCK:SYNTHETIC_SOURCE`; gates ineligible |
| `UNKNOWN calibration → no PLAY_NOW`; `CALIBRATION_QUEUE` | Phase 1 ceiling `UNVALIDATED`; later phases only for markets that passed gates |
| Starter-certainty contract: probable ≠ confirmed; certainty is an explicit field | `GoalieState` CONFIRMED / PROJECTED / UNKNOWN, priced as a mixture |
| Builder outputs: CSV + summary JSON + manifest JSON + anomalies CSV | `desk/board.py` (manifest carries file hashes, snapshot ids, config hashes) |
| `validate_pipeline_determinism.py` (run twice, compare) | `tests/test_backtest.py::test_desk_export_is_deterministic` |
| `grading/compute_clv.py`, `grade_bets.py`, stable grading ids | `ledger/clv.py` (price-based CLV, edge at bet vs close), `ledger/grading.py`, `ledger/bets.py` |
| `portfolio/correlation_controls.py` | Deferred to Phase 1D (correlated SOG/saves exposure) |
| Market data tiers A–D, CLV attribution bands | Deferred; the `max_stake` / limit fields are captured now |

What was deliberately **not** reused:
- Points-based edge (`projection − line`). NHL edge is in probability space against a de-vigged market.
- `confidence_score` and `play_confidence_tier`. They were replaced by calibration metrics. There are no letter grades.
- MLB periods (`game`/`f5`) and baseball run environments.

## 3. What was made NHL-specific

- **Game-state simulator** instead of independent full-game Poisson draws. Manpower state from minor-penalty timers (a PP goal ends the minor). Score effects that are stronger in the 3rd period. Pulled-goalie rules (trailing by 1 with ≤150 s left, by 2 with ≤210 s) with 6v5 attack and empty-net hazards. 5 minutes of 3v3 sudden-death OT, then a shootout.
- **Joint effect estimation.** Home, back-to-back, attack and defence are fit together in one penalized Poisson model on 5v5 xG. PP/PK and penalty drawing/taking have their own joint fits. Goaltending enters only as goals given xG, relative to a league intercept, so team defence and the goalie are not double counted. A league goals-per-xG factor (5v5 and PP) is fit from data. It was added after the synthetic walk-forward exposed a totals bias (ECE 0.054 → 0.010).
- **Player-based preseason priors.** A roster-TOI-weighted, shrunk 3-season player talent estimate, not last season's team numbers. The ARI→UTA relocation is aliased, but ratings are rebuilt from players.
- **Goalie start probabilities.** Confirmed 0.985, projected 0.88, otherwise a workload model (recent start share, second night of a back-to-back, rest). Prices are mixed across goalie pairs.
- **Market set and settlement.** ML includes OT/SO. The 3-way is regulation only. Puck line and totals include OT, with the shootout winner credited +1. Pricing is push-aware for integer lines.
- **Rink scorekeeper SOG factors** (`features/rink.py`). Built for Phase 1D and not wired into game pricing.

## 4. Data-source inventory
See [data_source_inventory.md](data_source_inventory.md). The NHL API and MoneyPuck are blocked by this environment's network policy, so ingestion is fixture-tested and the end-to-end run is synthetic.

## 5. Leakage audit
See [leakage_audit.md](leakage_audit.md). The central guarantee is tested directly: predictions are byte-identical with the future present in the store versus physically removed. Residual items R1–R4 are open.

## 6. Test results

`python3 -m pytest -q` → **37 passed** (about 14 s). Coverage by area:

| Area | Tests |
|---|---|
| Contracts / ids | team aliases and relocation, game-id validation, naive-timestamp rejection, market-key rules, odds validation, shootout settlement |
| Ingestion | write-once raw store + integrity check, store-before-parse client, situation codes, PBP result incl. shootout, boxscore starters, shifts, MoneyPuck parsers and availability, odds dedupe, PIT latest-price, goalie-report visibility, quality gates |
| Models / pricing | B2B/travel, league-average plausibility, pulled-goalie effects, determinism, pricing identities (puck-line complement, pushes, ML ≥ regulation), mixture linearity, de-vig, goalie states and B2B backup, rating recovery without same-season summaries, slate pricing, per-state goal counters |
| Governance / ledger | append-only + hash chain + tamper detection, forward-after-puck-drop refusal, Phase 1 lanes + ACTIONABLE refusal (even with config flipped), grading conventions, CLV, bet-ledger rules |
| Backtest | **future-data invariance**, walk-forward + evaluate + gates, deterministic desk export |

## 7. First historical pricing sample (SYNTHETIC)

> **This is a synthetic league, not real NHL data.** It proves the mechanics work. It does not show that the model is valid.

`python3 -m nhl.cli demo --out out/demo` generates two synthetic seasons, walk-forwards the 2025-26 season (Oct 20 → Apr 15), evaluates, and writes a desk for 2026-01-20. The run takes about 3.5 minutes. Samples are in [`docs/samples/`](samples/).

Walk-forward: **1,188 games, 15,444 immutable artifacts, 0 pricing errors, hash chain verified, 0 missing closes.**

| Market | n | log loss model | log loss close | Δ (95% CI) | ECE | calib. slope | blend w (95% CI) |
|---|---|---|---|---|---|---|---|
| ML | 1188 | 0.6892 | 0.6871 | +0.0021 (−0.0031, +0.0069) | 0.016 | 0.94 | 0.24 (−0.35, 0.88) |
| Puck line | 1188 | 0.6065 | 0.6068 | −0.0003 (−0.0057, +0.0043) | 0.010 | 0.95 | 0.52 (0.04, 1.12) |
| Total | 1188 | 0.6925 | 0.6893 | +0.0032 (−0.0021, +0.0086) | 0.010 | 0.33 | 0.18 (−0.36, 0.70) |
| Team total | 2376 | 0.6680 | 0.6663 | +0.0017 (−0.0020, +0.0051) | 0.009 | 0.77 | 0.31 (−0.10, 0.74) |
| Reg 3-way | 1188 | 1.0696 | 1.0694 | +0.0002 | – | – | – |

Every gate fails (`NOT_ELIGIBLE:SYNTHETIC_DATA`, and for most markets also `LOGLOSS_WORSE_THAN_CLOSE` / `NO_INFORMATION_BEYOND_MARKET`). That is the intended behavior. The synthetic close is the truth plus small noise, so a model with one season of data should not beat it, and the gate catches that.

Sample desk rows (2026-01-20, as_of 22:00Z):

```
BUF @ NSH  (2026-01-20 23:00Z)  P(OT)=0.230  E[total]=6.13
  goalies: HOME:UNKNOWN(8470610@0.717)|AWAY:CONFIRMED(8470582@0.985)
  TOTAL UNDER +6.5   mkt -117  nv 0.515  model 0.553  fair -124  edge +3.8%  [BLOCKED | shadow MODEL_PLUS]
  ML HOME            mkt -107  nv 0.495  model 0.521  fair -109  edge +2.6%  [BLOCKED | shadow WATCH]
```

Every artifact row carries the requested audit fields: `prediction_id, created_at, as_of, game_id, market, selection, sportsbook, market_price, no_vig_probability, model_probability, fair_price, edge, goalie_state, lineup_state, model_version, feature_version, data_snapshot_id, status`. It also carries `puck_drop, mode, data_origin, line, team, market_key, market_observed_at, model_p_win/p_push, ev_per_unit, config_hash, health_score, shadow_lane, reason_codes`, plus `prev_hash/row_hash` in the store.

## 8. Remaining blockers before forward testing (opening week)

1. **Network access.** Allow `api-web.nhle.com`, `api.nhle.com` and `moneypuck.com` (and the odds/goalie vendors) in the environment, or run on an unrestricted host. Then run the ingestion against live payloads and **verify field names** (`situationCode` digit order, boxscore goalie fields, MoneyPuck `penaltiesFor` semantics).
2. **Historical timestamped odds, including closes.** Phase 1C cannot run on real data without them. This needs a paid source. Load it into the canonical odds CSV.
3. **Timestamped goalie reports.** Needed for honest historical mixtures. Without them the backtest treats every starter as UNKNOWN, which is conservative. Forward tracking needs a live source (check ToS).
4. **Timestamped roster snapshots** for priors (leakage item R3), plus projected TOI per player.
5. **Refit simulator league constants walk-forward** (leakage item R2) from 2019–2025 play-by-play: pull timing, empty-net rate, 3v3 rate, PP conversion, score effects.
6. **Scheduler.** Daily FORWARD runs at OPEN / 9AM / NOON / 3PM / T-60, close capture, a nightly grading job, and chain verification. The components exist; only the cron/automation is missing.
7. **Per-book settlement rules** for puck line and totals (OT/SO inclusion) must be confirmed and stored per book.
8. Before any ACTIONABLE lane (Phase 1E, not needed to *start* forward tracking): an in-house walk-forward xG model (R1), real-data gates passing at `gate_version` 2026.09.25-v1, and lineup ingestion (`lineup_state` is always UNKNOWN today).
9. Phase 1D items stay deferred until the game-engine gate passes: SOG and goalie-saves models (rink factors are ready), prop settlement rules, and correlated-exposure caps.
