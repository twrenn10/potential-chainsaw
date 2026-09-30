# Project Agent Instructions

## Repo overview
- `nhl/` is the NHL Desk package. Stage order: `data` -> `features` -> `models` -> `pricing` -> `market` -> `governance` -> `ledger` -> `desk`; `backtest` drives the same stages walk-forward.
- Architecture patterns are mirrored from `twrenn10/tournament_v2` (`super_engine/`): dataclass contracts with validation, stable digest keys, hard-block reasons that can never be promoted, deterministic CSV/JSON writers, summary + manifest + anomalies per builder. MLB statistical assumptions are NOT reused.

## Non-negotiable rules
- Point-in-time only: every input record has `available_at`; model code reads data only through a strict `nhl.data.pit.PointInTimeView`.
- Every ingested record gets a `Provenance` from a named rule in `nhl.data.provenance`; never set `available_at` by hand. Backfill overrides and vintage attestations go only in `nhl/config/backfill_overrides.json` / `moneypuck_vintages.json` with evidence.
- Parsers use the `*_checked` validators: reject with a reason, never coerce.
- League constants for season S come only from `fit_league_constants` (seasons < S).
- No confirmed goalie is assumed unless a CONFIRMED report is visible at `as_of`; games are priced as a starter mixture.
- No independent full-game Poisson in production pricing; use `nhl.models.game_state`.
- No hand-multiplied factor chains; effects come from the joint fit in `nhl.features.ratings`.
- Phase 1: nothing leaves `UNVALIDATED`/`BLOCKED`. `ACTIONABLE` is refused in code.
- Prediction artifacts are append-only (`nhl.ledger.predictions`); never edit or delete rows.
- No letter confidence grades.
- Validation gates in `nhl/config/validation_gates.json` are predetermined; changing them requires a new `gate_version`.

## Validation
- `python3 -m pytest -q` before every commit.
- Keep outputs deterministic for a fixed seed + inputs.
