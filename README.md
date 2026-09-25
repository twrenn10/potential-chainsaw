# NHL Desk (`nhl-model`)

This is a point-in-time NHL pricing engine with promotion lanes gated by governance. It carries over the sport-agnostic architecture of `twrenn10/tournament_v2`: deterministic stages, hard blocks that are never promoted, market-vs-model comparison, CLV/calibration tracking, grading, and summary/manifest exports. The internals are NHL-native.

> **Phase 1: nothing is actionable.** Every row is `UNVALIDATED` or `BLOCKED` until the predetermined walk-forward gates in `nhl/config/validation_gates.json` pass on real data.

## Quick start

```bash
pip install -e ".[dev]"
python3 -m pytest -q                         # 37 tests
python3 -m nhl.cli demo --out out/demo        # synthetic end-to-end walk-forward (~3.5 min)
python3 -m nhl.cli verify --db out/demo/predictions.sqlite
python3 -m nhl.cli fetch-schedule --date 2026-10-07   # live NHL API (needs network access)
```

## Layout

| Path | Role |
|---|---|
| `nhl/contracts/` | Canonical ids (games, teams, players, markets), enums, validated record schemas with `available_at` |
| `nhl/data/` | Raw immutable snapshots, NHL API / MoneyPuck / odds / goalie-report ingestion, `PointInTimeView`, quality gates |
| `nhl/features/` | Player-based priors, joint MAP ratings, rest/travel, rink SOG factors |
| `nhl/models/` | `goalie_start.py` starter mixture, `game_state.py` simulator |
| `nhl/pricing/` | Slate pricing engine, market probabilities (ML, 3-way, puck line, totals, team totals) |
| `nhl/market/` | American/decimal conversion, de-vig, quotes at `as_of`, open/close/movement marks |
| `nhl/governance/` | Lane decisions, Phase 1 ceiling, ACTIONABLE refusal |
| `nhl/ledger/` | Immutable prediction artifacts, grading, CLV, paper bet ledger |
| `nhl/backtest/` | Strict walk-forward loop, metrics, evaluation against the no-vig close, gates |
| `nhl/desk/` | Candidate artifacts and daily board exports |
| `nhl/config/*.json` | Simulator baselines, goalie-start priors, governance, validation gates (hashed into every artifact) |

## Docs
- [Phase 1 report](docs/PHASE1_REPORT.md): architecture map, MLB reuse, NHL-specific design, test results, pricing sample, blockers
- [Leakage audit](docs/leakage_audit.md)
- [Data-source inventory](docs/data_sources.md)
- [Samples](docs/samples/) from the synthetic run (clearly labeled SYNTHETIC)
