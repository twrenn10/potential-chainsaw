# NHL Desk: Phase 2 report

Branch `claude/nhl-pricing-engine-e9tmet`. Phase 2 has two parts:
- **Part A** (2026-09-30, commits `4b48aea`…`09d2ad5`): real-data ingestion hardening, temporal provenance, leakage gaps 3A–3D.
- **Part B** (commits `7dc7e1f`…HEAD): Priorities 4–14.

> **Verdict: LIVE FORWARD VALIDATION IS BLOCKED.** The architecture and the fixture-based lifecycle are complete. No live NHL, MoneyPuck, odds or goalie-report payload has ever been captured from this environment (`nhl capture-*` → `403 Forbidden` at the egress proxy), and no odds provider is configured.
>
> Status by layer:
> - Parser implementation: **complete**.
> - Fixture validation: **complete**.
> - Live-source validation: **blocked**.
> - Genuine CLV validation: **externally blocked** (no odds provider).

---

## Part A (summary; details in temporal_provenance.md and leakage_audit.md)
- Checked parsers for NHL API schedule/pbp/boxscore/shifts/rosters and MoneyPuck. They reject bad records and never coerce.
- Named provenance rules per information class; strict causal views by default.
- MoneyPuck vintage governance, plus an in-house walk-forward xG model.
- Walk-forward league constants, persisted write-once.
- Timestamped roster snapshots; schedule versions and postponements.
- Raw capture/replay.
- Artifact hashing fix for `-0.0`; NaN/inf rejected.
- Override registries included in the configuration fingerprint.

## Part B, priority by priority

### P4: Market data and no-vig close (odds_and_close_methodology.md)
- **Observation contract (market-v2):** provider, `source_ts` ≤ `observed_at` ≤ fetch time, game, book, market, period, selection, line, team/participant, price in its original format (exact decimal kept), market status, max stake, provenance.
- **Structurally distinct markets:** ML, REG_3WAY, PUCK_LINE, TOTAL, TEAM_TOTAL, GOALIE_SAVES, PLAYER_SOG. Keys are deterministic and include non-default periods and participants.
- **No-vig:** explicit `NoVigResult` (raw implied, overround, method, exponent, inputs) for 2-way and 3-way markets. Malformed markets are rejected, never normalised.
- **Close rule `close-v1`:** the last valid complete state strictly before actual puck drop, with a labelled scheduled-time fallback. Suspended, incomplete, non-contemporaneous, malformed or stale closes are `UNAVAILABLE`, never substituted.
- Closes are persisted append-only and hash-chained, available or not.
- The provider-neutral interface means a vendor adapter only emits market-v2.

### P5: Model edge vs betting edge
Artifact schema v2 keeps the chain distinct:
- `model_probability` → `model_fair_price`/`model_fair_decimal`;
- `raw_implied_probability` → `no_vig_probability` (+ `novig_method`, `market_overround`);
- `probability_edge`, `fair_price_edge`;
- `execution_price`/`execution_decimal` → push-aware `ev_per_unit`;
- `status`/`eligibility`/`block_reasons`.

There is no ambiguous `edge` field. Shadow `MODEL_PLUS` needs positive EV. Tests prove that a positive probability edge can be negative EV at -110, that vig changes attractiveness, and that stale or blocked rows cannot promote despite positive EV.

### P6: Goalie lineage (goalie_information_lineage.md)
- States: CONFIRMED / EXPECTED / PROBABLE / PROJECTED / SCRATCHED / UNKNOWN.
- Each source's latest call is used. Conflicts are flagged and never treated as confirmed. Stale reports are flagged and ignored. Scratches zero out that goalie.
- Every report considered is hashed into `goalie_fingerprint`. The mixture is deterministic, with common random numbers across a game's goalie pairs.
- A later confirmation creates a new artifact and never mutates the old one (tested).

### P7: Forward runner (forward_testing_protocol.md)
- Scheduled invocations: price / close / settle / verify / export, with an append-only `cycles.jsonl`.
- It uses the same `run_slate` as walk-forward and replay, with `ReplayStateSource` for production.
- `append_if_changed` appends only when the state fingerprint changes. Identical reruns and clock-only cycles append nothing.

### P8: Lanes (governance_gates.md)
- Evidence lanes: DEV_SYNTHETIC / HISTORICAL_RESEARCH / STRICT_WALK_FORWARD / SHADOW_FORWARD / FORWARD_DEGRADED.
- Eligibility states, and the action lane with hard blocks.
- ACTIONABLE needs SHADOW_FORWARD evidence, passed gates, a phase change, positive EV and confirmed goalies. It is refused in code today.
- Manual overrides are demotion-only and logged. Promotion requests are logged and refused.

### P9: Market-by-market evaluation
- Per market and per evidence lane: counts, Brier, log loss (model vs close), calibration intercept/slope, ECE, average predicted vs hit rate, average no-vig, model-minus-market, blend-weight CI, close availability.
- Execution (paper, positive-EV rows): CLV mean/median/CI, positive-CLV rate, average execution decimal, average EV, realised ROI (secondary).
- Props are reported as NOT_PRICED.

### P10: Gates (`gate_version 2026.09.30-v2`, all thresholds **PROVISIONAL**)
- Criteria: sample size, calibration slope/intercept, ECE, log loss vs close, blend-weight CI, CLV CI, close availability, mean health, provenance, eligible lanes only, chain/leakage/determinism checks.
- ROI is never a criterion. Interpretation flags cover ROI vs CLV and calibration without market advantage.
- The version was bumped from v1 to *add* criteria; no v1 threshold was loosened.

### P11: Desk exports (`nhl/desk/reports.py`)
- CURRENT_SLATE, PREDICTION_HISTORY, CLV_REPORT, GRADING_REPORT, GOVERNANCE_REPORT (with overrides), DATA_FRESHNESS (odds, schedule, goalie reports, rosters, team/player/event inputs), and a manifest.
- Byte-identical across reruns (tested).

### P12: CLI (`nhl --help`)
- Commands: capture-schedule/data/rosters/goalies/odds, replay, fit-params, price-slate, forward-cycle, capture-close, ingest-results, grade, clv, verify, evaluate-gates, export-desk, backtest, leakage-audit, forward-drill.
- Unsafe modes need explicit flags: `--synthetic`, `--simulated-clock --at`, `--overwrite`. Non-causal forward pricing is refused.

### P13: Tests
**128 passed** (Part A end: 89; all 89 still pass). No existing assertion was weakened. Two mechanical updates:
- the artifact test helper uses the v2 field names;
- the raw-SQL tamper test updates `probability_edge` instead of the removed `edge` column, so it still exercises the trigger rather than failing on a missing column.

| File | Tests | Covers (mandate numbering) |
|---|---|---|
| test_market_contract.py | 15 | 1–11: contract, malformed rejection, keys, American/decimal, 2-way/3-way no-vig, incomplete, close selection, actual cutoff, scheduled fallback, stale/suspended close, close store |
| test_chain_and_lineage.py | 16 | 12–14, 16–17, 24–26, 30: edge vs EV, vig, stale/blocked cannot promote, goalie transitions/determinism/conflicts/staleness/scratch, lanes, overrides, config fingerprint |
| test_forward_lifecycle.py | 3 | 15, 18–23, 27–28: later confirmation immutability, idempotence, meaningful reprice, no duplicates, chain across reprices, grading, CLV, deterministic exports, post-drop refusal, old artifact unaffected by later roster/market changes |
| test_real_format_lifecycle.py | 1 | Definition of done on real-format fixtures via replay |
| test_cli_safety.py | 4 | Unsafe flags, overwrite refusal, leakage-audit CLI |
| test_backtest.py (existing) | 5 | 29: full vs truncated leakage equivalence; synthetic blocked; postponement; unattested real-origin blocked |
| earlier suites | 84 | Part A and Phase 1 |

### P14: No model tuning. Model changes and their justification
Three **clock-dependence defects** were found by the forward-lifecycle tests. They were fixed as implementation errors (incorrect causal/timing behaviour), not for performance:

| # | Defect | Evidence | Fix | Tests |
|---|---|---|---|---|
| 1 | Monte Carlo seed derived from `as_of` | A clock-only rerun re-appended all 65 artifacts | Seed derived from parameter fingerprint + constants id | `test_forward_lifecycle` |
| 2 | Rating time-decay anchored to `as_of` | Parameters drifted hourly with no new data | Decay anchored to the newest visible data time | same |
| 3 | One RNG stream per slate | A goalie confirmation in one game repriced 4 other games | Per-game streams with common random numbers across goalie pairs | same |

**Before → after on the synthetic 2025-26 walk-forward** (1,188 games; changes are Monte Carlo/decay-anchor noise, and no metric was targeted):

| Market | log loss (close) | log loss model before → after | ECE before → after | slope before → after |
|---|---|---|---|---|
| ML | 0.6871 | 0.6893 → 0.6884 | 0.017 → 0.021 | 0.92 → 1.04 |
| Puck line | 0.6068 | 0.6065 → 0.6075 | 0.010 → 0.027 | 0.95 → 0.95 |
| Total | 0.6893 | 0.6925 → 0.6933 | 0.009 → 0.013 | 0.33 → 0.23 |
| Team total | 0.6663 | 0.6680 → 0.6682 | 0.009 → 0.011 | 0.76 → 0.73 |

**Historical metrics changed only within noise, and no gate outcome changed:** every gate failed before and fails after.

The synthetic numbers carry no real-data meaning. The market in that data is built near the truth.

### Default constants
Status is persisted in `LeagueConstants.sources`, hashed into `constants_id` → `config_hash`, and listed per artifact in `default_constants`.
- **Structural defaults**, not estimable from ingested data (need event-level fits on prior seasons): `rate_sh`, `rate_6v5_attack`, `rate_empty_net`, `rate_4v4_mult`, `score_effect_beta`, `score_effect_beta_p3`, `pull_trailing_by_1_seconds`, `pull_trailing_by_2_seconds`.
- **Exposure default** in the synthetic sample: `rate_pp` (210 PP-hours < the 300-hour minimum from one prior season).
- Fitted: `rate_5v5`, `penalty_rate`, `rate_3v3`, `shootout_home_prob`.

## Final verification (at the code commit that generated the samples, `244132f`)
| Check | Result |
|---|---|
| Complete suite | 128 passed |
| Artifact hash chains | Backtest 15,444 rows verified; closes chain verified; drill predictions + closes verified |
| Deterministic replay | `test_capture_then_replay_is_deterministic_and_provenanced` |
| Deterministic desk exports | Drill export ×2 byte-identical; `test_forward_lifecycle`, `test_real_format_fixture_lifecycle` |
| Leakage equivalence | Backtest audit 2025-11-12 and 2026-02-03: EQUIVALENT; test suite |
| Old artifact unaffected by later roster/goalie/market changes | `test_old_artifact_unaffected_by_later_roster_and_market_changes`, `test_forward_lifecycle` |
| Synthetic cannot promote | All 15,444 backtest rows DEV_SYNTHETIC / BLOCKED; gates `NOT_ELIGIBLE:SYNTHETIC_DATA` |
| Non-causal cannot qualify as strict | `test_evidence_lanes`, `test_strict_view_hides_non_causal…`, gates `NOT_ELIGIBLE:NON_CAUSAL_OR_UNATTESTED_INPUTS` |
| Samples ↔ final fingerprints | `docs/samples/README.md` records the code commit and `all_config_hash()` including both registries |

## Blockers (ordered)
1. **Network:** `api-web.nhle.com`, `api.nhle.com` and `moneypuck.com` are unreachable (403). No live payload has been parsed, and the VERIFY fields remain unconfirmed.
2. **Odds provider:** no timestamped live/historical odds source with closing coverage. CLV and the close benchmark are fixture/synthetic only.
3. **Goalie-report provider** with publication timestamps.
4. **Actual puck-drop source:** every close uses the labelled scheduled fallback.
5. **Event-level fits** for the 8 structural default constants (prior seasons only). Real-data check of the in-house xG.
6. **Historical transaction-level rosters** for strict pre-2026 backtests. Forward tracking can start from live roster captures.
7. **Operational scheduler** (cron) for the protocol in forward_testing_protocol.md.
8. Props (SOG/saves) are unpriced (Phase 1D), and lineup ingestion is absent (`lineup_state=UNKNOWN`).

## Commits (Part B)
```
7dc7e1f Priority 4: market-data contract, explicit no-vig, closing-line rule
833ac97 Priorities 5, 6, 8-10: explicit pricing chain, goalie lineage, evidence lanes, market-by-market gates
599633b Priorities 7 and 11: forward runner, reprice dedupe, desk reports; fix clock-driven reprices
d02e8a4 Priority 12-13: safe operational CLI, leakage-audit module, real-format lifecycle drill
244132f forward-drill: exercise a late goalie scratch on a team that has a confirmed report
(+ this documentation/samples commit; final HEAD reported in the hand-off message)
```
