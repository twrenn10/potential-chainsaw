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
from nhl.contracts import DataOrigin, Lane

PHASE1_CEILING = Lane.UNVALIDATED


class GovernanceError(RuntimeError):
    pass


@dataclass
class LaneDecision:
    lane: Lane
    shadow_lane: Lane
    reasons: list[str] = field(default_factory=list)


def shadow_lane(market: str, edge: float, cfg: dict) -> Lane:
    t = cfg["lanes_by_market"][market]
    if edge >= t["model_plus_min_edge"]:
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
    shadow = shadow_lane(market, edge, cfg)
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
        and edge >= t["actionable_min_edge"]
    ):
        lane = Lane.ACTIONABLE
    return LaneDecision(assert_allowed(lane, cfg), shadow, reasons)


def assert_allowed(lane: Lane, cfg: dict | None = None) -> Lane:
    cfg = cfg or load("governance")
    if lane is Lane.ACTIONABLE and (cfg["phase"] == "1" or not cfg.get("actionable_enabled")):
        raise GovernanceError("ACTIONABLE lane is disabled in Phase 1")
    return lane
