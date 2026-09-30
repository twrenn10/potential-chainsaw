from datetime import timedelta

import pytest

from nhl.config import load
from nhl.contracts import (
    Eligibility,
    EvidenceLane,
    Game,
    GoalieGameStats,
    GoalieReport,
    GoalieState,
    Lane,
)
from nhl.data.pit import HistoricalStore
from nhl.governance.lanes import decide, eligibility, evidence_lane
from nhl.governance.overrides import Override, OverrideLog, effective_status
from nhl.market.novig import american_to_decimal, no_vig
from nhl.models.goalie_start import starter_distribution
from nhl.pricing.markets import ev_per_unit
from nhl.timeutil import parse_ts

T0 = parse_ts("2026-11-10T00:00:00Z")


# 12-13. model edge vs execution edge
def test_positive_probability_edge_can_have_negative_ev():
    # Market -110/-110: no-vig 50%. Model 51% => +1pt probability edge ...
    nv = no_vig(["HOME", "AWAY"], [american_to_decimal(-110)] * 2)
    model_p = 0.51
    assert model_p - nv.fair("HOME") == pytest.approx(0.01)
    # ... but at the available -110 execution price EV is negative: the vig eats the edge.
    ev = ev_per_unit(model_p, 0.0, 1 - model_p, american_to_decimal(-110))
    assert ev < 0
    # The same probability edge at a better execution price is positive EV.
    assert ev_per_unit(model_p, 0.0, 1 - model_p, american_to_decimal(+105)) > 0


def test_vig_changes_execution_attractiveness():
    for price_home, price_away in ((-105, -105), (-120, 100)):
        nv = no_vig(["HOME", "AWAY"], [american_to_decimal(price_home), american_to_decimal(price_away)])
        assert sum(nv.no_vig) == pytest.approx(1.0)
    fair_book = no_vig(["HOME", "AWAY"], [2.0, 2.0], min_overround=0.0)
    assert fair_book.overround == pytest.approx(0.0)
    p = 0.52
    assert ev_per_unit(p, 0, 1 - p, 2.0) > ev_per_unit(p, 0, 1 - p, american_to_decimal(-110))


def test_disagreement_is_not_a_recommendation():
    # Large probability edge but negative EV: shadow lane never MODEL_PLUS.
    d = decide("ML", 0.10, "LIVE", [], 1.0, True, ev_per_unit=-0.01, evidence=EvidenceLane.SHADOW_FORWARD)
    assert d.shadow_lane is not Lane.MODEL_PLUS and d.lane is Lane.UNVALIDATED


def _actionable_cfg():
    cfg = load("governance")
    cfg.update(phase="2", actionable_enabled=True)
    return cfg


# 26. governance blocks prevent actionable even with positive EV and an enabled config
@pytest.mark.parametrize("kw,block", [
    ({"odds_age_minutes": 45.0}, "HARD_BLOCK:STALE_PRICE"),
    ({"provenance_blocks": ["NON_CAUSAL_INPUTS"]}, "HARD_BLOCK:NON_CAUSAL_INPUTS"),
    ({"data_origin": "SYNTHETIC"}, "HARD_BLOCK:SYNTHETIC_SOURCE"),
    ({"data_origin": "FIXTURE"}, "HARD_BLOCK:FIXTURE_SOURCE"),
    ({"health_blocks": ["GOALIE_INPUTS"]}, "HARD_BLOCK:GOALIE_INPUTS"),
])
def test_blocks_prevent_actionable_despite_positive_ev(kw, block):
    args = dict(market="ML", edge=0.2, data_origin="LIVE", health_blocks=[], odds_age_minutes=1.0, goalies_confirmed=True,
                validated_markets=frozenset({"ML"}), cfg=_actionable_cfg(), ev_per_unit=0.15, evidence=EvidenceLane.SHADOW_FORWARD)
    args.update(kw)
    d = decide(**args)
    assert d.lane is Lane.BLOCKED and block in d.reasons
    # Sanity: without the defect the same row would be actionable under that hypothetical config.
    ok = decide(**{**args, **{k: v for k, v in dict(odds_age_minutes=1.0, provenance_blocks=(), data_origin="LIVE", health_blocks=[]).items()}})
    assert ok.lane is Lane.ACTIONABLE


def test_actionable_requires_shadow_forward_evidence_and_phase():
    cfg = _actionable_cfg()
    strict = decide("ML", 0.2, "HISTORICAL", [], 1.0, True, frozenset({"ML"}), cfg, ev_per_unit=0.1,
                    evidence=EvidenceLane.STRICT_WALK_FORWARD)
    assert strict.lane is not Lane.ACTIONABLE
    # The shipped config (phase 1) never yields ACTIONABLE.
    shipped = decide("ML", 0.2, "LIVE", [], 1.0, True, frozenset({"ML"}), ev_per_unit=0.1, evidence=EvidenceLane.SHADOW_FORWARD)
    assert shipped.lane is Lane.UNVALIDATED


# 24-25. synthetic / non-causal lanes
def test_evidence_lanes():
    assert evidence_lane("SYNTHETIC", "FORWARD", True, []) is EvidenceLane.DEV_SYNTHETIC
    assert evidence_lane("LIVE", "FORWARD", True, [], simulated_clock=True) is EvidenceLane.DEV_SYNTHETIC
    assert evidence_lane("HISTORICAL", "BACKTEST", True, ["NON_CAUSAL_INPUTS"]) is EvidenceLane.HISTORICAL_RESEARCH
    assert evidence_lane("HISTORICAL", "BACKTEST", False, []) is EvidenceLane.HISTORICAL_RESEARCH
    assert evidence_lane("HISTORICAL", "BACKTEST", True, []) is EvidenceLane.STRICT_WALK_FORWARD
    assert evidence_lane("LIVE", "FORWARD", True, []) is EvidenceLane.SHADOW_FORWARD
    assert evidence_lane("LIVE", "FORWARD", True, ["UNATTESTED_ROSTER"]) is EvidenceLane.FORWARD_DEGRADED
    assert eligibility(EvidenceLane.DEV_SYNTHETIC, Lane.UNVALIDATED) is Eligibility.INELIGIBLE_DEV
    assert eligibility(EvidenceLane.HISTORICAL_RESEARCH, Lane.UNVALIDATED) is Eligibility.RESEARCH_ONLY
    assert eligibility(EvidenceLane.STRICT_WALK_FORWARD, Lane.UNVALIDATED) is Eligibility.EVALUATION_ONLY


def test_manual_override_is_explicit_logged_and_cannot_promote(tmp_path):
    log = OverrideLog(tmp_path / "o.sqlite")
    with pytest.raises(Exception):
        log.record(Override("P:1", "DEMOTE", "ops", "", "2026-10-07T20:00:00Z"))
    d = log.record(Override("P:1", "DEMOTE", "ops", "line error at book", "2026-10-07T20:00:00Z"))
    assert d["effective"]
    p = log.record(Override("P:2", "PROMOTE_REQUEST", "ops", "looks good", "2026-10-07T20:01:00Z"), gates_passed_for_market=True)
    assert not p["effective"] and "Phase 1" in p["refusal"]
    rows = log.rows()
    assert effective_status("UNVALIDATED", [r for r in rows if r["prediction_id"] == "P:1"]) == "BLOCKED(OVERRIDE)"
    assert effective_status("UNVALIDATED", [r for r in rows if r["prediction_id"] == "P:2"]) == "UNVALIDATED"


# 14, 16, 17. goalie state machine, determinism, conflicts, staleness
def _goalie_store(reports):
    games, stats = [], []
    for i in range(8):
        gid = f"20260200{i + 1:02d}"
        games.append(Game(gid, 2026, "REGULAR", T0 - timedelta(days=2 * (8 - i)), "TOR", "MTL"))
        stats.append(GoalieGameStats(gid, "TOR", "8000001" if i % 4 else "8000002", True, 3600, 30, 3, 3.0,
                                     T0 - timedelta(days=2 * (8 - i)) + timedelta(hours=4)))
    target = Game("2026020099", 2026, "REGULAR", T0, "TOR", "MTL")
    return HistoricalStore(games=games + [target], goalie_stats=stats, goalie_reports=reports), target


def rep(gid, state, src, hours_before, goalie="8000002"):
    return GoalieReport("2026020099", "TOR", goalie, state, src, T0 - timedelta(hours=hours_before))


def test_goalie_state_transitions_and_determinism():
    reports = [rep("x", GoalieState.PROJECTED, "dfo", 10), rep("x", GoalieState.PROBABLE, "coach", 6),
               rep("x", GoalieState.CONFIRMED, "beat", 1.5)]
    store, g = _goalie_store(reports)
    states = [starter_distribution(store.view(T0 - timedelta(hours=h)), g, "TOR") for h in (12, 8, 3, 1)]
    assert [d.state for d in states] == [GoalieState.UNKNOWN, GoalieState.PROJECTED, GoalieState.PROBABLE, GoalieState.CONFIRMED]
    assert states[0].top[0] == "8000001"  # workload model before any report
    assert states[-1].top == ("8000002", 0.985) and states[-1].confirmed
    again = starter_distribution(store.view(T0 - timedelta(hours=1)), g, "TOR")
    assert again == states[-1] and again.fingerprint() == states[-1].fingerprint()
    assert abs(sum(p for _, p in states[1].probs) - 1) < 1e-5


def test_unknown_never_becomes_confirmed_and_scratch_is_respected():
    store, g = _goalie_store([rep("x", GoalieState.UNKNOWN, "dfo", 2)])
    d = starter_distribution(store.view(T0 - timedelta(hours=1)), g, "TOR")
    assert d.state is GoalieState.UNKNOWN and not d.confirmed
    store, g = _goalie_store([rep("x", GoalieState.CONFIRMED, "beat", 3), rep("x", GoalieState.SCRATCHED, "team", 0.5)])
    before = starter_distribution(store.view(T0 - timedelta(hours=1)), g, "TOR")
    after = starter_distribution(store.view(T0 - timedelta(minutes=10)), g, "TOR")
    assert before.top[0] == "8000002" and after.top[0] == "8000001"
    assert "GOALIE_SCRATCHED:8000002" in after.flags and dict(after.probs).get("8000002") is None


def test_conflicting_sources_are_flagged_not_confirmed():
    store, g = _goalie_store([rep("x", GoalieState.CONFIRMED, "beat", 2, "8000002"),
                              rep("x", GoalieState.CONFIRMED, "dfo", 2, "8000001")])
    d = starter_distribution(store.view(T0 - timedelta(hours=1)), g, "TOR")
    assert "GOALIE_SOURCE_CONFLICT" in d.flags and not d.confirmed
    assert {gid for gid, _ in d.probs} == {"8000001", "8000002"}


def test_stale_reports_are_flagged_and_ignored():
    store, g = _goalie_store([rep("x", GoalieState.CONFIRMED, "beat", 48, "8000002")])
    d = starter_distribution(store.view(T0 - timedelta(hours=1)), g, "TOR")
    assert d.state is GoalieState.UNKNOWN and "GOALIE_REPORT_STALE:beat" in d.flags


# 30. configuration fingerprint covers registries
def test_config_fingerprint_changes_with_registry(monkeypatch):
    import nhl.config as cfgmod
    from nhl.desk.candidates import all_config_hash

    base = all_config_hash()
    real = cfgmod._raw

    def patched(name):
        txt = real(name)
        return txt.replace('"schedule": {}', '"schedule": {"2026": {"override_id": "X"}}') if name == "backfill_overrides" else txt

    monkeypatch.setattr(cfgmod, "_raw", patched)
    assert all_config_hash() != base
