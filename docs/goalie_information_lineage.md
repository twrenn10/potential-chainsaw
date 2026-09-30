# Goalie information lineage

Code: `nhl/contracts/enums.py::GoalieState`, `nhl/data/odds.py::parse_goalie_reports_checked`, `nhl/models/goalie_start.py::starter_distribution`, `nhl/pricing/engine.py`, `nhl/desk/candidates.py`. Config: `nhl/config/goalie_start.json`.

## States
| State | Meaning | P(named goalie starts) |
|---|---|---|
| `CONFIRMED` | team/beat confirmation | 0.985 (never 1.0 from a report) |
| `EXPECTED` | strong report short of confirmation | 0.93 |
| `PROBABLE` | coach/source indicates likely starter | 0.90 |
| `PROJECTED` | aggregator projection | 0.88 |
| `SCRATCHED` | this goalie will **not** start (replaced/scratched/injured) | 0 |
| `UNKNOWN` | no information | workload model |

The probabilities are priors, not fitted values. Refitting them needs timestamped historical reports, which is an open blocker.

## Report event record
Each report is a `GoalieReport` with:
- `game_id`, `team`, `goalie_id`, `state`, `source`, and optional `confidence` (validated to [0, 1]);
- `available_at`;
- `provenance`: `published_at` = source timestamp, `fetched_at`, the rule, and causality.

Reports published at or after puck drop are rejected. A backfilled report without a publication time is non-causal (see temporal_provenance.md).

## Mixture construction at `as_of` (`starter_distribution`)
Only reports with `available_at ≤ as_of` are considered (strict view).
1. **Workload model** gives base weights from visible history: recent start share, second night of a back-to-back, rest.
2. Each **source's latest report** is its current call. Per goalie, the latest report from any source decides `SCRATCHED` (p = 0; weights removed; flag `GOALIE_SCRATCHED:<id>`).
3. A report available more than `stale_report_hours` (30, PROVISIONAL) before puck drop is **stale**. It is flagged `GOALIE_REPORT_STALE:<source>` and not used to name a starter.
4. The strongest remaining state wins. If sources at that state name **different goalies**, it is flagged `GOALIE_SOURCE_CONFLICT`. The named mass is split across the named goalies by workload weight, and the state is **never treated as confirmed**. Lower-ranked contradicting claims are flagged `GOALIE_SOURCE_DISAGREEMENT`.
5. The residual mass (1 − p_named) is spread over other team goalies by workload weight. With no usable claim, the workload model alone decides (`UNKNOWN`). **UNKNOWN never becomes confirmed.**
6. Components below 3% are dropped and renormalised. The game is priced across every home × away goalie pair, weighted by the product of probabilities. For example, A 70% / B 30% prices each pair and mixes the resulting outcome distributions.

**Determinism:** the result is a pure function of the visible reports and history (sorted inputs, fixed tie order). Each game has its own random stream, and all goalie pairs of a game share common random numbers. A goalie change in one game cannot move another game's price.

## In artifacts
- `goalie_state`, e.g. `HOME:CONFIRMED(8479361@0.985)|AWAY:PROBABLE*CONFLICT(8478470@0.450)`.
- `goalie_fingerprint`: a hash of both teams' state, probabilities, flags and the full report lineage (`source|goalie|state|available_at` for every report considered).
- `reason_codes` carry `SOFT:HOME:GOALIE_SOURCE_CONFLICT` and similar.
- `confirmed` requires CONFIRMED and no conflict. Any future ACTIONABLE lane requires both goalies confirmed.

## Repricing
A later report changes the goalie fingerprint and usually the prices. The forward runner then appends a **new** artifact through `append_if_changed`. The earlier artifact is never touched: the store is append-only, triggers block edits, and the hash chain detects tampering. If the new information does not change the state fingerprint, nothing is appended.

Tests:
- `test_goalie_state_transitions_and_determinism`
- `test_unknown_never_becomes_confirmed_and_scratch_is_respected`
- `test_conflicting_sources_are_flagged_not_confirmed`
- `test_stale_reports_are_flagged_and_ignored`
- `test_forward_lifecycle` (a later confirmation reprices only that game; earlier artifacts are byte-identical)
