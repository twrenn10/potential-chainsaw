# Governance lanes and validation gates

## Three separate dimensions on every artifact
1. **Evidence lane** (`evidence_lane`): what kind of evidence the prediction is.

   | Lane | Definition | Can be evidence? | Can be actionable? |
   |---|---|---|---|
   | `DEV_SYNTHETIC` | Synthetic or fixture origin, or a simulated clock | Never | Never |
   | `HISTORICAL_RESEARCH` | Real historical data, but a non-strict view or provenance defects | Research only | Never |
   | `STRICT_WALK_FORWARD` | Backtest where every input met the causal `as_of` standard | Model evaluation | No |
   | `SHADOW_FORWARD` | Real-time, pre-event, contemporaneous data, clean provenance | **Key Phase 2 validation lane** | Only after gates |
   | `FORWARD_DEGRADED` | Real-time but with provenance or data defects | Audit only | Never |

2. **Action lane** (`status`): `BLOCKED` / `UNVALIDATED` / (post-gate) `PASS` / `WATCH` / `MODEL_PLUS` / `ACTIONABLE`. `shadow_lane` is the informational lane the thresholds would give. `MODEL_PLUS` needs both a probability edge **and** positive EV at the execution price.

3. **Eligibility** (`eligibility`): `INELIGIBLE_DEV`, `INELIGIBLE_BLOCKED`, `RESEARCH_ONLY`, `EVALUATION_ONLY`, `ACTIONABLE_ELIGIBLE`.

## Hard blocks (always `BLOCKED`, never promotable)
- `SYNTHETIC_SOURCE`, `FIXTURE_SOURCE`, `SIMULATED_CLOCK`, `DEV_EVIDENCE`;
- `NON_CAUSAL_INPUTS`, `UNATTESTED_PROVENANCE`, `UNATTESTED_ROSTER`, `NON_STRICT_VIEW`;
- `STALE_PRICE` (quote older than `max_odds_age_minutes`);
- any data-health FAIL (`GOALIE_INPUTS`, `TEAM_RATING_INPUTS`, `PREGAME`, `SCHEDULE_KNOWN`).

## ACTIONABLE requires every one of these
- `phase != "1"` **and** `actionable_enabled` in `governance.json` (currently phase 1 and false; `assert_allowed` refuses otherwise);
- the market's validation gates passed;
- `evidence_lane == SHADOW_FORWARD`;
- no hard block;
- both goalies confirmed (no conflict);
- positive EV at the execution price;
- probability edge ≥ `actionable_min_edge`.

**No manual promotion exists.** `OverrideLog` records `DEMOTE` actions, which are effective, logged, reasoned and shown as `BLOCKED(OVERRIDE)`. It also records `PROMOTE_REQUEST` actions, which are logged and refused while gates have not passed and the phase forbids ACTIONABLE. Overrides never modify artifacts.

## Validation gates (`validation_gates.json`, `gate_version 2026.09.30-v2`)
Version 2 bumped from `2026.09.25-v1` to **add** criteria. No v1 threshold was loosened. **Every threshold is PROVISIONAL**: an operational choice, not scientifically established. ROI is **never** a criterion.

Evaluated per market, using only artifacts in eligible evidence lanes (`STRICT_WALK_FORWARD`, `SHADOW_FORWARD`). Promotion evidence must come from `SHADOW_FORWARD`.

| Criterion | Threshold (PROVISIONAL) |
|---|---|
| Sample | ≥ 1000 scored games; ≥ 300 CLV samples |
| Log loss vs no-vig close | Δ ≤ 0 |
| Calibration slope | in [0.85, 1.15] |
| Calibration intercept | \|a\| ≤ 0.10 |
| ECE | ≤ 0.025 |
| Information beyond market | Blend-weight 95% CI lower bound > 0 |
| CLV | Mean CLV 95% CI lower bound > 0 (positive-EV rows) |
| Market completeness | Close availability ≥ 95% |
| Data completeness | Mean health ≥ 0.95 |
| Provenance | No non-causal/unattested/non-strict artifacts; no synthetic/fixture origin; no dev/research lanes mixed in |
| Integrity / reproducibility | `ARTIFACT_CHAIN_VERIFIED`, `LEAKAGE_EQUIVALENCE`, `DETERMINISTIC_RERUN` must be demonstrated |
| Unpriced markets | `GOALIE_SAVES`, `PLAYER_SOG` always `NOT_PRICED:RESEARCH_ONLY_MARKET` |

## Interpretation rules (reported, never used to pass)
- Positive realised ROI with negative CLV: `WARNING:POSITIVE_ROI_WITH_NEGATIVE_CLV`.
- Negative short-term ROI with a CLV CI above zero: `NOTE:NEGATIVE_ROI_WITH_STABLE_POSITIVE_CLV` (do not reject on ROI).
- Calibrated but not beating the close: `NOTE:WELL_CALIBRATED_WITHOUT_MARKET_ADVANTAGE` (not a betting edge).

## Current result
Every gate fails. The only evaluated data is synthetic (`NOT_ELIGIBLE:SYNTHETIC_DATA`, `NO_STRICT_OR_SHADOW_FORWARD_EVIDENCE`), and even on synthetic data the model does not beat the synthetic close. That is expected, because that close is built near the truth.
