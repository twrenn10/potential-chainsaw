from dataclasses import replace
from datetime import timedelta

import pytest

from nhl.contracts import Game, GameResult, TeamGameStats
from nhl.data import provenance as P
from nhl.data.pit import HistoricalStore
from nhl.governance.lanes import decide
from nhl.timeutil import parse_ts

T0 = parse_ts("2024-11-01T23:00:00Z")
TODAY = parse_ts("2026-09-30T12:00:00Z")


def test_event_fact_backfill_keeps_historical_availability():
    # A final score fetched today was still knowable at game end.
    prov = P.event_fact("nhl_api", "s", TODAY, T0 + timedelta(hours=4))
    assert prov.causal and prov.available_at == T0 + timedelta(hours=4)
    live = P.event_fact("nhl_api", "s", T0 + timedelta(hours=3), T0 + timedelta(hours=4))
    assert live.available_at == T0 + timedelta(hours=3)


def test_report_backfill_without_timestamp_is_non_causal():
    live = P.published_report("dfo", "s", T0 - timedelta(hours=2), T0, None, P.CaptureMode.LIVE)
    assert live.causal and live.rule == "LIVE_CAPTURE"
    # Declared LIVE but fetched after puck drop -> treated as backfill.
    late = P.published_report("dfo", "s", T0 + timedelta(hours=1), T0, None, P.CaptureMode.LIVE)
    assert not late.causal and late.rule == "NO_HISTORICAL_TIMESTAMP"
    stamped = P.published_report("dfo", "s", TODAY, T0, T0 - timedelta(hours=3), P.CaptureMode.BACKFILL)
    assert stamped.causal and stamped.available_at == T0 - timedelta(hours=3)
    with pytest.raises(P.ProvenanceError):
        P.published_report("dfo", "s", T0, T0, T0 + timedelta(hours=1), P.CaptureMode.LIVE)


def test_schedule_backfill_requires_documented_override():
    assert not P.schedule("nhl_api", "s", TODAY, 2024, T0, P.CaptureMode.BACKFILL, overrides={"schedule": {}}).causal
    ov = {"schedule": {"2024": {"override_id": "SCHED-2024", "available_at": "2024-07-01T00:00:00Z", "evidence": "x"}}}
    prov = P.schedule("nhl_api", "s", TODAY, 2024, T0, P.CaptureMode.BACKFILL, overrides=ov)
    assert prov.causal and prov.override_id == "SCHED-2024" and prov.rule == "BACKFILL_OVERRIDE"


def test_moneypuck_vintage_rules():
    bound = T0 + timedelta(days=1)
    contemporaneous = P.versioned_derived("moneypuck", "s", bound + timedelta(hours=2), bound, 2024, [])
    assert contemporaneous.causal and contemporaneous.rule == "VINTAGE_CONTEMPORANEOUS"
    backfilled = P.versioned_derived("moneypuck", "s", TODAY, bound, 2024, [])
    assert not backfilled.causal and backfilled.rule == "VINTAGE_UNVERIFIED"
    att = [{"attestation_id": "A1", "model_id": "mp-xg-2023", "trained_through_season": 2023, "published_at": "2024-09-01T00:00:00Z"}]
    assert P.versioned_derived("moneypuck", "s", TODAY, bound, 2024, att).causal
    # A model trained through the row's own season is in-sample: still non-causal.
    att_bad = [{**att[0], "trained_through_season": 2024}]
    assert not P.versioned_derived("moneypuck", "s", TODAY, bound, 2024, att_bad).causal


def test_walk_forward_model_rejects_in_sample_scoring():
    with pytest.raises(P.ProvenanceError):
        P.walk_forward_model("nhl_xg", "s", TODAY, T0, "m", 2024, 2024)
    assert P.walk_forward_model("nhl_xg", "s", TODAY, T0, "m", 2023, 2024).available_at == T0


def _stat(causal: bool) -> TeamGameStats:
    bound = T0 + timedelta(days=1)
    prov = P.versioned_derived("moneypuck", "s", TODAY if not causal else bound, bound, 2024, [])
    return TeamGameStats("2024020001", "TOR", "MTL", True, "20241101", "5on5", 2900, 2.4, 1.9, 2, 1, 25, 22, 0, 0,
                         available_at=prov.available_at, provenance=prov)


def test_strict_view_hides_non_causal_and_flags_permissive_use():
    store = HistoricalStore(team_stats=[_stat(False)], data_origin="HISTORICAL")
    strict = store.view(TODAY)
    assert strict.team_stats() == [] and strict.non_causal_hidden == 1 and strict.provenance_blocks() == []
    loose = store.view(TODAY, strict=False)
    assert len(loose.team_stats()) == 1 and loose.provenance_blocks() == ["NON_CAUSAL_INPUTS"]
    assert decide("ML", 0.1, "HISTORICAL", [], 1.0, True, provenance_blocks=loose.provenance_blocks()).lane.value == "BLOCKED"


def test_unattested_records_block_non_synthetic_origins():
    res = GameResult("2024020001", 3, 2, 3, 2, 1, 0, "REG", None, T0 + timedelta(hours=4))
    v = HistoricalStore(results=[res], data_origin="HISTORICAL").view(TODAY)
    v.results()
    assert v.provenance_blocks() == ["UNATTESTED_PROVENANCE"]
    s = HistoricalStore(results=[res], data_origin="SYNTHETIC").view(TODAY)
    s.results()
    assert s.provenance_blocks() == []  # synthetic is blocked by its own hard block


def test_schedule_versions_latest_known_wins():
    v1 = Game("2024020001", 2024, "REGULAR", T0, "TOR", "MTL", available_at="2024-07-01T00:00:00Z")
    ppd = replace(v1, status="PPD", available_at=T0 - timedelta(hours=5))
    v2 = replace(v1, start_time=T0 + timedelta(days=40), status="SCHEDULED", available_at=T0 + timedelta(days=3))
    store = HistoricalStore(games=[v1, ppd, v2])
    assert store.view(T0 - timedelta(days=1)).games()[0].status == "SCHEDULED"
    assert store.view(T0 - timedelta(hours=1)).games()[0].status == "PPD"
    after = store.view(T0 + timedelta(days=5)).games()
    assert len(after) == 1 and after[0].start_time == T0 + timedelta(days=40)
    with pytest.raises(ValueError):
        replace(v1, status="WEIRD")
