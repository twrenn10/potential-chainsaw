# NHL Desk: Phase 2 report (2026-09-30)

Branch `claude/nhl-pricing-engine-e9tmet`, 9 commits after Phase 1 (`4b48aea` … the documentation commit).

**Scope note:** the Phase 2 mandate received was truncated partway through item 3B ("Persist…"). This report covers Priorities 1, 2, 3A and 3B as specified. It also covers the two remaining Phase 1 leakage gaps, rosters (3C) and postponements (3D), which are presumed to be the rest of Priority 3. Anything after that point in the mandate has not been seen or implemented.

**Real-data status: live sources are still unreachable from this environment** (`nhl capture` → `403 Forbidden` at the egress proxy). Every parser is validated against source-format fixtures only. **No real-data validation has occurred**, and nothing in this report should be read as such.

Governance is unchanged or stricter. Everything is still `UNVALIDATED` or `BLOCKED`, `ACTIONABLE` is still refused in code, and the model was not re-tuned. The synthetic metrics below are identical to Phase 1's within simulation noise.

## Priority 1: real NHL data ingestion
| Item | Implementation |
|---|---|
| Validation primitives | `nhl/data/validation.py`: strict int/float/UTC/clock parsing, and `ParseResult` (records + rejections + warnings). No silent coercion (`"2.7"` is not an int, `NaN`/`inf` are not numbers, naive timestamps are rejected) |
| Schedules | id/season/gameType consistency, explicit UTC start, `gameScheduleState` (OK/PPD/SUSP/CNCL; TBD rejected) and `gameState` mapping, unknown teams rejected, duplicate ids in one snapshot treated as ambiguous and dropped |
| Play-by-play | Period/type/clock rules by game type, `situationCode` digit and skater sanity, event owner must be in the game, penalty duration, duplicate event ids |
| Results | Only from final games with fully valid event streams, where the reconstructed score (including the shootout +1) **matches the payload score**. More than one OT goal is rejected. SOG mismatches are surfaced as warnings |
| Goalies (boxscore) | Exactly one starter per team; `saveShotsAgainst` consistent with `goalsAgainst` |
| Shifts / rosters | Shift start ≤ end; roster position codes per group; duplicate players rejected |
| MoneyPuck | Strict numerics and ranges, season/date/id consistency, HOME/AWAY, team ≠ opponent, duplicate rows and tied-ice-time starters treated as ambiguous |
| Odds / goalie reports | UTC timestamps, integer prices, observation not after fetch, conflicting same-instant prices dropped, starter reports must be pregame |
| Capture / replay | `nhl/data/ingest.py`: `capture_day` (live job) and `replay` (deterministic rebuild of a provenanced `HistoricalStore` from raw snapshots, with `INGEST_summary.json` and `INGEST_anomalies.csv`). CLI: `nhl capture`, `nhl replay` |

Source identity is preserved through `snapshot_id` (content hash) on every record, `source_version` (`api-web/v1`, model ids, capture dates), and the raw fetch manifest (URL, fetch time).

## Priority 2: temporal provenance
See [temporal_provenance.md](temporal_provenance.md). Named rules per information class set `available_at` and `causal`. `PointInTimeView` is strict by default. Non-causal, unattested, undated-roster and non-strict use each produce a governance hard block and make evaluation gates ineligible. Backfill overrides live in documented registries, which are empty in this commit.

## Priority 3: the four leakage gaps
| Gap | Resolution | Tests |
|---|---|---|
| **3A MoneyPuck vintage** | The files carry no model version, so vintage cannot be established from the data. Backfilled MoneyPuck xG is therefore `VINTAGE_UNVERIFIED` (non-causal) unless captured within 36 h or attested (`moneypuck_vintages.json`, currently empty). **Causal replacement:** walk-forward in-house xG from NHL play-by-play, fit only on prior seasons; scoring an in-sample season raises | `test_moneypuck_vintage_rules`, `test_vintage_backfill_is_non_causal_and_hidden_from_strict_views`, `test_xg_fits_only_prior_seasons…` |
| **3B League constants** | Rolling window of seasons completed before S, strict view at Sep 1 of S. Per-constant FIT/DEFAULT provenance and sample sizes. Persisted write-once as `league_constants/season=S/LC_<hash>.json`. `constants_id` is stamped into every artifact's `config_hash`. The pipeline refuses constants from another season | `test_constants_use_only_prior_completed_seasons` (mutating season-S data leaves S constants unchanged; mutating S-1 changes them), write-once/tamper tests, wrong-season refusal |
| **3C Rosters** | Dated `RosterSlot` snapshots; priors use the latest one at `as_of`; undated rosters are blocked for real data; roster endpoint is live-capture only | `test_roster_as_of_uses_latest_known_snapshot`, `test_roster_parser_live_vs_backfill` |
| **3D Postponements** | Schedule versions (dedupe by content); latest version known at `as_of`; PPD/SUSP/CNCL not priced; slates built from all versions | `test_schedule_versions_latest_known_wins`, `test_postponed_game_skipped_then_priced_on_new_date` |

Synthetic 2025 constants (`LC:67c7cb02a1bcdc94`, trained on 2024): rate_5v5 2.613 (FIT), penalty_rate 3.033 (FIT), rate_3v3 7.051 (FIT, 285 tied games), shootout_home_prob 0.493 (FIT, 88 shootouts), rate_pp **DEFAULT** (210 PP-hours < the 300-hour minimum). Eight simulator constants are still **static defaults** because they need event-level fits (pull timing, empty-net, 6v5, SH, 4v4, score effects).

## Integrity finding during Phase 2
The full-season rerun failed `verify_chain` at row 12,023. Cause: `ev_per_unit` rounded to `-0.0`, and SQLite stores it as `0.0`, so the recomputed hash differed. This was a latent Phase 1 bug. Nothing was edited; the chain check did its job. Fix: canonical hashing normalises signed zero, which leaves the hash unchanged for every other row, and `append` now rejects NaN/inf. The affected generated database was left untouched, and the run was regenerated and verified. Regression test: `test_negative_zero_and_nan_round_trip_safety`.

## Tests
`python3 -m pytest -q` → **89 passed** (Phase 1: 37). New suites: `test_provenance`, `test_nhl_api_validation`, `test_moneypuck_validation`, `test_market_provenance`, `test_xg`, `test_league_constants`, `test_rosters`, `test_ingest_replay`, plus new backtest integration tests (postponement; real-origin data without provenance is fully BLOCKED).

## Synthetic walk-forward rerun (mechanics only, SYNTHETIC)
1,188 games, 15,444 artifacts, **hash chain verified**, 0 missing closes, 0 provenance-blocked artifacts. ML log loss 0.6893 vs close 0.6871; ECE: ML 0.017, total 0.009; calibration slope: ML 0.92, puck line 0.95. All gates fail: `NOT_ELIGIBLE:SYNTHETIC_DATA`, and most also fail on log loss versus the close. Samples are in `docs/samples/`.

## Remaining blockers before real forward testing
1. **Network access** to `api-web.nhle.com`, `api.nhle.com` and `moneypuck.com`. Then run `nhl capture` / `nhl replay` on live payloads and confirm the VERIFY fields: `situationCode` digit order, penalty `eventOwnerTeamId` = penalized team, the MoneyPuck `penaltiesFor` meaning, rink-side x-coordinate convention for xG, and boxscore goalie fields.
2. **Timestamped odds history including closes** (paid vendor) in the canonical CSV.
3. **Timestamped goalie reports** (a source with publication times, or live pregame captures from opening night).
4. **Historical rosters:** transaction-level data for any pre-2026 strict backtest. Forward tracking can start from live roster captures.
5. **Event-level fits** for the eight default simulator constants (from play-by-play, walk-forward), and a real-data check of the in-house xG before it feeds ratings.
6. **A scheduler** for daily `capture` + forward runs at OPEN/9AM/NOON/3PM/T-60, close capture, grading and chain verification.
7. **The rest of the Phase 2 mandate** after "Persist…", which was not received.
