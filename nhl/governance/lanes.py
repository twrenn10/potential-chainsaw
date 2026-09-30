"""Market-specific promotion lanes.

Rules carried over from tournament_v2:
* hard-block reasons route a row to BLOCKED, and BLOCKED rows are never promoted;
* synthetic/dev data (DEV_BOOTSTRAP_SOURCE there, SYNTHETIC here) is always BLOCKED;
* unknown calibration never reaches the top lane.

Phase 1 ceiling: every non-blocked row is UNVALIDATED, whatever its edge. The lane
the row *would* get from thresholds is recorded as ``shadow_lane`` (capped at
MODEL_PLUS) purely for later evaluation. ``ACTIONABLE`` is refused in code while
phase == "1", even if config says otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from nhl.config import load
from nhl.contracts import DataOrigin, Eligibility, EvidenceLane, Lane, PredictionMode

PHASE1_CEILING = Lane.UNVALIDATED


class GovernanceError(RuntimeError):
    pass


@dataclass
class LaneDecision:
    lane: Lane
    shadow_lane: Lane
    reasons: list[str] = field(default_factory=list)


def shadow_lane(market: str, edge: float, cfg: dict, ev_per_unit: float | None = None) -> Lane:
    """Informational lane from the PROBABILITY edge. A model-market disagreement is not
    a recommendation: MODEL_PLUS additionally requires positive EV at the available
    execution price when that price is supplied."""

    t = cfg["lanes_by_market"].get(market)
    if t is None:
        return Lane.PASS  # unpriced / research-only market
    if edge >= t["model_plus_min_edge"] and (ev_per_unit is None or ev_per_unit > 0):
        return Lane.MODEL_PLUS
    if edge >= t["watch_min_edge"]:
        return Lane.WATCH
    return Lane.PASS


def decide(
    market: str,
    edge: float,
    data_origin: str,
    health_blocks: list[str],
    odds_age_minutes: float,
    goalies_confirmed: bool,
    validated_markets: frozenset[str] = frozenset(),
    cfg: dict | None = None,
    provenance_blocks: list[str] | tuple[str, ...] = (),
    ev_per_unit: float | None = None,
    evidence: EvidenceLane | None = None,
) -> LaneDecision:
    cfg = cfg or load("governance")
    reasons: list[str] = []
    if data_origin == DataOrigin.SYNTHETIC.value:
        reasons.append("HARD_BLOCK:SYNTHETIC_SOURCE")
    reasons.extend(f"HARD_BLOCK:{b}" for b in health_blocks)
    # Temporal provenance: non-causal, unattested or non-strict inputs can never be promoted.
    reasons.extend(f"HARD_BLOCK:{b}" for b in provenance_blocks)
    if odds_age_minutes > cfg["max_odds_age_minutes"]:
        reasons.append("HARD_BLOCK:STALE_PRICE")
    if data_origin == DataOrigin.FIXTURE.value:
        reasons.append("HARD_BLOCK:FIXTURE_SOURCE")
    if evidence is EvidenceLane.DEV_SYNTHETIC and not any(r.startswith("HARD_BLOCK:") for r in reasons):
        reasons.append("HARD_BLOCK:DEV_EVIDENCE")
    shadow = shadow_lane(market, edge, cfg, ev_per_unit)
    if any(r.startswith("HARD_BLOCK:") for r in reasons):
        return LaneDecision(Lane.BLOCKED, shadow, reasons)

    if not goalies_confirmed:
        reasons.append("GOALIE_UNCONFIRMED_PRICED_AS_MIXTURE")
    if cfg["phase"] == "1" or market not in validated_markets:
        reasons.append("PHASE1_UNVALIDATED" if cfg["phase"] == "1" else "MARKET_NOT_VALIDATED")
        return LaneDecision(PHASE1_CEILING, shadow, reasons)

    # Post-Phase-1 path (1E): validated markets may use thresholds; ACTIONABLE also
    # needs confirmed goalies and the explicit config switch.
    lane = shadow
    t = cfg["lanes_by_market"][market]
    if (
        cfg.get("actionable_enabled")
        and goalies_confirmed
        and evidence is EvidenceLane.SHADOW_FORWARD
        and ev_per_unit is not None and ev_per_unit > 0
        and edge >= t["actionable_min_edge"]
    ):
        lane = Lane.ACTIONABLE
    return LaneDecision(assert_allowed(lane, cfg), shadow, reasons)


def assert_allowed(lane: Lane, cfg: dict | None = None) -> Lane:
    cfg = cfg or load("governance")
    if lane is Lane.ACTIONABLE and (cfg["phase"] == "1" or not cfg.get("actionable_enabled")):
        raise GovernanceError("ACTIONABLE lane is disabled in Phase 1")
    return lane


DEV_ORIGINS = {DataOrigin.SYNTHETIC.value, DataOrigin.FIXTURE.value}


def evidence_lane(data_origin: str, mode: str, strict_view: bool, provenance_blocks: list[str],
                  simulated_clock: bool = False) -> EvidenceLane:
    if data_origin in DEV_ORIGINS or simulated_clock:
        return EvidenceLane.DEV_SYNTHETIC
    clean = strict_view and not provenance_blocks
    if mode == PredictionMode.BACKTEST.value:
        return EvidenceLane.STRICT_WALK_FORWARD if clean else EvidenceLane.HISTORICAL_RESEARCH
    return EvidenceLane.SHADOW_FORWARD if clean else EvidenceLane.FORWARD_DEGRADED


def eligibility(evidence: EvidenceLane, lane: Lane) -> Eligibility:
    if evidence is EvidenceLane.DEV_SYNTHETIC:
        return Eligibility.INELIGIBLE_DEV
    if lane is Lane.BLOCKED:
        return Eligibility.INELIGIBLE_BLOCKED
    if evidence in (EvidenceLane.HISTORICAL_RESEARCH, EvidenceLane.FORWARD_DEGRADED):
        return Eligibility.RESEARCH_ONLY
    if lane is Lane.ACTIONABLE:
        return Eligibility.ACTIONABLE_ELIGIBLE
    return Eligibility.EVALUATION_ONLY
