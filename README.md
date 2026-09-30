# NHL Desk (`nhl-model`)

This is a point-in-time NHL pricing engine with promotion lanes gated by governance. It carries over the sport-agnostic architecture of `twrenn10/tournament_v2`: deterministic stages, hard blocks that are never promoted, market-vs-model comparison, CLV/calibration tracking, grading, and summary/manifest exports. The internals are NHL-native.

> **Nothing is actionable.** Every row is `UNVALIDATED` or `BLOCKED` until the predetermined gates in `nhl/config/validation_gates.json` pass on SHADOW_FORWARD evidence. **LIVE FORWARD VALIDATION IS BLOCKED** (no network access to NHL sources from this environment; no odds provider).

## Quick start

```bash
pip install -e ".[dev]"
python3 -m pytest -q                                   # 128 tests
nhl --help                                             # full operational CLI (safe by default)
nhl backtest --synthetic --out out/demo --overwrite     # synthetic walk-forward + evaluation (~5 min)
nhl forward-drill --synthetic --out out/drill           # simulated-clock shadow-forward lifecycle -> desk reports
nhl leakage-audit --synthetic
nhl capture-schedule --date 2026-10-07 --store store/   # live NHL API (needs network access)
nhl price-slate --forward-root fwd/ --store store/ --date 2026-10-07 --season 2026
```

## Layout

| Path | Role |
|---|---|
| `nhl/contracts/` | Canonical ids (games, teams, players, markets), enums, validated record schemas with `available_at` |
| `nhl/data/` | Raw immutable snapshots, checked parsers (NHL API / MoneyPuck / odds / goalie reports / rosters), provenance rules, strict `PointInTimeView`, capture + replay, quality gates |
| `nhl/features/` | Player-based priors, joint MAP ratings, walk-forward league constants, walk-forward xG, rest/travel, rink SOG factors |
| `nhl/models/` | `goalie_start.py` starter mixture, `game_state.py` simulator |
| `nhl/pricing/` | Slate pricing engine, market probabilities (ML, 3-way, puck line, totals, team totals) |
| `nhl/market/` | Odds conversion, explicit no-vig, complete-group quotes, close-v1 selection, provider interface |
| `nhl/governance/` | Evidence lanes, eligibility, action lanes, ACTIONABLE refusal, logged demotion-only overrides |
| `nhl/ledger/` | Immutable artifacts (schema v2), close store, settlement/CLV join, grading, paper bet ledger |
| `nhl/backtest/` | Strict walk-forward loop, metrics, evaluation against the no-vig close, gates |
| `nhl/desk/` | Artifact builder (pricing chain) and deterministic desk reports |
| `nhl/forward/` | Shadow-forward runner (scheduled, append-only, same pricing path) |
| `nhl/config/*.json` | Simulator baselines, goalie-start priors, governance, validation gates (hashed into every artifact) |

## Docs
- [Phase 2 report](docs/PHASE2_REPORT.md): ingestion, provenance, leakage gaps, market/close, pricing chain, goalie lineage, forward runner, lanes, gates, desk, CLI, verdict
- [Temporal provenance rules](docs/temporal_provenance.md)
- [Odds and close methodology](docs/odds_and_close_methodology.md)
- [Goalie information lineage](docs/goalie_information_lineage.md)
- [Forward-testing protocol](docs/forward_testing_protocol.md)
- [Governance lanes and gates](docs/governance_gates.md)
- [Phase 1 report](docs/PHASE1_REPORT.md): architecture map, MLB reuse, NHL-specific design, test results, pricing sample, blockers
- [Leakage audit](docs/leakage_audit.md)
- [Data-source inventory](docs/data_source_inventory.md)
- [Samples](docs/samples/) from the synthetic run (clearly labeled SYNTHETIC)
