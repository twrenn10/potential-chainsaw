# Leakage audit (Phase 1)

Principle: every record carries `available_at` = the earliest instant it was actually knowable. Model and feature code reads history only through `nhl.data.pit.PointInTimeView`, which returns records with `available_at <= as_of`. Evaluation data (results, closing lines) is read only by `nhl.backtest.evaluate`, after predictions are stored.

**Enforcement test:** `tests/test_backtest.py::test_future_data_cannot_change_predictions` runs a slate twice. The first run uses the full store. The second uses a store physically truncated to what was knowable at `as_of`. The two sets of artifacts must be identical. Any future-data path fails this test.

| # | Channel | Rule | Status |
|---|---|---|---|
| 1 | Game results | `available_at = puck drop + 4h`. That is later than any real finish, so the bound can hide data but never leak it. | Enforced |
| 2 | MoneyPuck game-by-game rows | Visible the next day at 12:00 UTC. Same-night data is never assumed available. | Enforced |
| 3 | MoneyPuck season summaries | Visible July 1 after the season. They feed the next season's priors only. | Enforced + tested |
| 4 | Player priors | Only seasons with lag ≥ 1 are used, via the view. | Enforced + tested |
| 5 | Goalie priors | Only goalie games from seasons strictly before the target season. | Enforced |
| 6 | In-season ratings | Refit from scratch every slate from the view. Exponential decay only reweights visible rows. | Enforced |
| 7 | Goalie starters | A report counts only once its `available_at` has passed. Otherwise the starter comes from the workload model on visible history. Games are priced as a mixture, and a starter is never assumed confirmed. | Enforced + tested |
| 8 | Odds | Only snapshots with `snapshot_ts <= as_of` are used. The close is used only in evaluation. | Enforced + tested |
| 9 | Schedule / rest / travel | Known in advance. For historical backfill, `parse_schedule(..., historical_available_at=...)` must be set explicitly. | Enforced; postponements are a residual |
| 10 | Prediction timing | `as_of < puck_drop` for every artifact. FORWARD rows also require the wall-clock write time to be before puck drop. | Enforced (DB layer) + tested |
| 11 | Artifact rewrite | Append-only triggers, a hash chain, and refusal to rewrite an existing `prediction_id`. | Enforced + tested |
| 12 | Validation gates | Predetermined in `validation_gates.json` and versioned by `gate_version`. | Process |
| **R1** | **MoneyPuck xG model** | MoneyPuck's xG is trained on multi-season data that may postdate a backtest date. | **Open (residual).** Fit our own walk-forward xG before any ACTIONABLE promotion. |
| **R2** | **Simulator league constants** | Pull timing, empty-net, 3v3, PP and score-effect rates are general-league defaults. Refitting them on the full history and then backtesting on the same seasons would leak. | **Open.** Refit only on seasons before each test window. |
| **R3** | **Rosters for priors** | `RosterSlot` has no `available_at` yet. Mid-season trades and injuries would be invisible, or leak if taken from an end-of-season roster. | **Open.** Add timestamped roster snapshots from the NHL API roster endpoint. |
| **R4** | **Postponed games** | A historical schedule backfill shows the final dates, not the originally announced ones. | Accept, and document per season. |
| R5 | Synthetic data | Results come from the same simulator family as the model, so the data is circular. | By design. Synthetic data is always BLOCKED and gates are ineligible. |
