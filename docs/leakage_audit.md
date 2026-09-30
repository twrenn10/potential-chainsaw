# Leakage audit (updated in Phase 2)

Principle: every record carries `available_at` and, in Phase 2, a `Provenance` stating the rule that produced it (see [temporal_provenance.md](temporal_provenance.md)). Model and feature code reads history only through a **strict** `nhl.data.pit.PointInTimeView`. Evaluation data is read only by `nhl.backtest.evaluate`, after predictions are stored.

**Enforcement test:** `tests/test_backtest.py::test_future_data_cannot_change_predictions`. Artifacts built from the full store are byte-identical to those built from a store physically truncated at `as_of`. Since Phase 2 this also covers league constants and roster snapshots.

| # | Channel | Rule | Status |
|---|---|---|---|
| 1 | Game results | `EVENT_FACT`: `min(fetched_at, puck drop + 4h)`. Produced only from complete, score-cross-checked event streams | Enforced + tested |
| 2 | MoneyPuck game rows | Next day 12:00 UTC **and** vintage rule (see 3A) | Enforced + tested |
| 3 | MoneyPuck season summaries | July 1 after the season **and** vintage rule | Enforced |
| 4 | Player priors | Seasons with lag ≥ 1 only, via strict view | Enforced + tested |
| 5 | Goalie priors | Seasons strictly before the target season | Enforced |
| 6 | In-season ratings | Refit every slate from the strict view | Enforced |
| 7 | Goalie starters | Reports need a pregame `published_at` or a live pregame capture; otherwise non-causal. Priced as a mixture | Enforced + tested |
| 8 | Odds | `SOURCE_PUBLISHED` at `snapshot_ts`. Rows observed after fetch, or with conflicting prices at one instant, are rejected. The close is used only in evaluation | Enforced + tested |
| 9 | Schedule | Live capture = `fetched_at`. Backfill only through a documented override, else non-causal. Versions are kept | Enforced + tested |
| 10 | Prediction timing | `as_of < puck_drop`. FORWARD rows also need wall-clock write time before puck drop | Enforced + tested |
| 11 | Artifact rewrite | Append-only triggers, a hash chain, and refusal to rewrite. Phase 2: signed-zero normalisation, non-finite values rejected | Enforced + tested |
| 12 | Validation gates | Predetermined, `gate_version` | Process |
| **3A** | **MoneyPuck xG vintage** (was R1) | Backfilled third-party xG is `VINTAGE_UNVERIFIED` (non-causal, invisible to strict views) unless captured contemporaneously or attested. Causal replacement: in-house walk-forward xG (`nhl/features/xg.py`), fit only on prior seasons | **Closed in code.** The attestation registry is empty; the in-house xG is untested on real payloads |
| **3B** | **League constants** (was R2) | `fit_league_constants`: seasons completed before S, strict view at Sep 1 of S, rolling window, per-constant FIT/DEFAULT provenance, persisted write-once with content id stamped into artifacts. The pipeline refuses constants from another season | **Closed** for rate_5v5, rate_pp, penalty_rate, rate_3v3, shootout_home_prob. Pull timing, empty-net, 6v5, SH, 4v4 and score-effect constants remain labelled static defaults (not fit from any season, so no leakage, but unvalidated) |
| **3C** | **Rosters** (was R3) | `RosterSlot.available_at` + provenance. Priors use the latest snapshot at `as_of`. Undated rosters are hard-blocked for real data. The roster endpoint is live-capture only | **Closed in code.** Historical rosters cannot be reconstructed from the endpoint; a transaction-level source is needed for pre-2026 backtests |
| **3D** | **Postponements** (was R4) | Schedule versions; views return the version known at `as_of`; PPD/SUSP/CNCL are not priced; slates are built from every version | **Closed** for live-captured schedules. Remains a residual inside seasons whose schedule is backfilled through an override |
| R5 | Synthetic data | Circular by design | Always BLOCKED; gates ineligible |
