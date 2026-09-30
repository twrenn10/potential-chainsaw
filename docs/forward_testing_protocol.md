# Forward-testing protocol (shadow forward)

The runner (`nhl/forward/runner.py`) is a set of **scheduled, idempotent invocations**, not a daemon. Every step appends state and never edits it. Pricing uses the same `nhl.pipeline.run_slate` as walk-forward research and replay.

## Daily schedule (times US/Eastern; one command per step)
| When | Command | Effect |
|---|---|---|
| 09:00 | `nhl capture-schedule`, `nhl capture-rosters`, `nhl capture-odds`, `nhl capture-goalies` | Raw snapshots with fetch times |
| 09:05 | `nhl price-slate` | Morning baseline: artifacts for every not-yet-started game |
| every 30–60 min until last puck drop | `nhl forward-cycle --provider-dir …` | Re-capture odds/goalies, then reprice. **Append only if the state fingerprint changed** |
| at T-60 and T-10 per game | `nhl forward-cycle` | Final pregame artifacts |
| after last puck drop | `nhl capture-close` | close-v1 selections (available or unavailable) persisted |
| next morning | `nhl ingest-results` (= `capture-data`) | Final play-by-play/boxscores give results (event facts) |
| next morning | `nhl grade`, `nhl clv` | Derived reports from the settlement join |
| next morning | `nhl verify --forward-root F` | Prediction and close hash chains |
| next morning | `nhl evaluate-gates`, `nhl export-desk` | Market-by-market evaluation; deterministic desk reports |
| weekly | `nhl leakage-audit` (on the research store) | Full vs truncated equivalence |

All forward state lives under `--forward-root`:
- `predictions.sqlite` (artifacts);
- `closes.sqlite`;
- `overrides.sqlite`;
- `league_constants/season=S/*.json` (write-once);
- `cycles.jsonl` (append-only run log).

Raw inputs live under `--store`.

## Repricing semantics
- Each artifact's `state_fingerprint` hashes every price-relevant field: model version, parameter/config/constants fingerprints, goalie and roster fingerprints, market key, book, execution price, raw and no-vig probabilities, model probabilities, lanes, eligibility and reasons. It excludes the clock and the quote age.
- `append_if_changed` appends an artifact only when the fingerprint differs from the latest stored artifact for the same (game, outcome, book, mode). Identical reruns are no-ops (`skipped_identical`), and clock-only cycles report `unchanged_state`.
- Outputs are a function of information, not the wall clock. The simulation seed derives from the parameter/constants fingerprint, rating decay is anchored to the newest visible data, and each game has its own random stream. (Three clock-dependence defects were found and fixed in Phase 2; see PHASE2_REPORT.md.)

## Reconstructing an old prediction
Given a `prediction_id`, the artifact row holds:
- game, market, period, selection, line, team/participant, book;
- `created_at` / `as_of` / `puck_drop`;
- model version and `parameter_fingerprint`, `constants_id` (with the persisted constants file), `default_constants`, `config_hash`;
- `data_snapshot_id`, `roster_fingerprint`, `goalie_state` + `goalie_fingerprint`, `market_snapshot_ref` (exact observations);
- model probability and fair odds, raw and no-vig probabilities, probability edge, execution price, EV;
- evidence lane, action lane, eligibility and every block reason.

The result, settlement, close and CLV come from the settlement join (`GRADING_REPORT.csv`, `CLV_REPORT.csv`) and the close store.

Replaying the raw snapshot store (`ReplayStateSource`) up to `as_of` reproduces the inputs. No hidden mutable state is needed.

## Clocks
Production uses the wall clock. `--simulated-clock --at TS` exists for drills only. Every artifact it creates is `DEV_SYNTHETIC` with `HARD_BLOCK:SIMULATED_CLOCK`, so a drill can never be counted as forward evidence.

## What counts as shadow-forward evidence
All of the following must hold:
- `evidence_lane = SHADOW_FORWARD`: mode FORWARD, wall clock, strict view, and no provenance blocks;
- a data origin that is not synthetic or fixture;
- `created_at < puck_drop`, which is enforced at write time.

Artifacts with provenance defects are `FORWARD_DEGRADED` and research-only.

## Current status
The code path is operational and exercised end to end on real-format fixtures (`tests/test_real_format_lifecycle.py`) and on synthetic drills (`nhl forward-drill`). **LIVE FORWARD VALIDATION IS BLOCKED:** the environment cannot reach the NHL API or MoneyPuck (`403 Forbidden`), and no live odds or goalie-report provider exists.
