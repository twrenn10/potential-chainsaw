import json
from datetime import datetime, timedelta, timezone

import pytest

from nhl.data.ingest import replay
from nhl.data.live_quality import source_quality_rows
from nhl.data.nhl_api import HttpPayload, NHLApiClient
from nhl.data.odds import parse_market_observations_checked
from nhl.data.snapshots import RawSnapshotStore
from nhl.forward.evidence import build_forward_cohort, first_slate_audit, validate_forward_artifact
from nhl.forward.scheduler import build_plan
from nhl.market.providers import TheOddsApiProvider

UTC = timezone.utc
NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)


def test_http_capture_preserves_metadata_and_malformed_bytes(tmp_path):
    raw = RawSnapshotStore(tmp_path)
    client = NHLApiClient(raw, transport=lambda _: HttpPayload(b"not-json", 200, {"etag": "abc"}))
    with pytest.raises(json.JSONDecodeError):
        client.fetch_schedule("2026-10-01", NOW)
    entry = raw.latest("nhl_api", "schedule/2026-10-01")
    assert entry is not None and raw.get_bytes(entry) == b"not-json"
    assert entry.meta["http_status"] == 200 and entry.meta["response_headers"]["etag"] == "abc"
    assert list(raw.failures())[0]["error_class"] == "MALFORMED_PAYLOAD"


def test_failed_refresh_is_visible_and_does_not_delete_prior_snapshot(tmp_path):
    raw = RawSnapshotStore(tmp_path)
    prior = raw.put("odds", "feed/2026-10-01", b"prior", NOW - timedelta(minutes=5))
    raw.record_failure("odds", "feed/2026-10-01", NOW, "HTTP_ERROR", "unavailable", status=503, retryable=True)
    assert raw.get_bytes(prior) == b"prior"
    row = next(r for r in source_quality_rows(raw, NOW) if r["source"] == "odds")
    assert row["source"] == "odds" and row["error_status"] == "ERROR" and row["anomaly_count"] == 1


def test_stale_source_is_reported(tmp_path):
    raw = RawSnapshotStore(tmp_path)
    raw.put("odds", "feed/day", b"x", NOW - timedelta(minutes=31))
    row = next(r for r in source_quality_rows(raw, NOW) if r["source"] == "odds")
    assert row["stale"] is True and row["stale_threshold_minutes"] == 30


def test_moneypuck_wrong_dataset_is_structured_rejection(tmp_path):
    raw = RawSnapshotStore(tmp_path)
    raw.put("moneypuck", "teams/aggregate", b"team,season\nTOR,2025\n", NOW)
    store, report = replay(raw)
    assert not store.team_stats
    assert any("schema mismatch" in r.reason for r in report.rejections)


def test_odds_adapter_normalizes_provider_timestamps_and_ids(tmp_path):
    provider = object.__new__(TheOddsApiProvider)
    payload = json.dumps([{
        "id": "evt1", "home_team": "Toronto", "away_team": "Montreal",
        "bookmakers": [{"key": "book", "last_update": "2026-10-01T11:59:00Z", "markets": [
            {"key": "h2h", "last_update": "2026-10-01T11:59:30Z", "outcomes": [
                {"name": "Toronto", "price": 1.8}, {"name": "Montreal", "price": 2.1}
            ]}
        ]}]
    }]).encode()
    csv_payload, rejects = provider.normalize(payload, NOW, {"evt1": "2026020001"})
    text = csv_payload.decode()
    assert not rejects and "the_odds_api" in text and "2026020001" in text and "2026-10-01T11:59:30Z" in text
    entry = RawSnapshotStore(tmp_path).put("odds", "feed/day", csv_payload, NOW)
    parsed = parse_market_observations_checked(csv_payload, entry)
    assert len(parsed.records) == 2 and not parsed.rejections


def _artifact(**overrides):
    row = {
        "mode": "FORWARD", "data_origin": "LIVE", "evidence_lane": "SHADOW_FORWARD",
        "created_at": "2026-10-01T10:00:00Z", "as_of": "2026-10-01T10:00:00Z",
        "puck_drop": "2026-10-01T23:00:00Z", "market_snapshot_ref": "snap:1",
        "market_observed_at": "2026-10-01T09:59:00Z", "block_reasons": "[]",
        "eligibility": "EVALUATION_ONLY", "market": "ML", "provider": "feed",
        "model_version": "m1", "config_hash": "c1", "parameter_fingerprint": "p1",
        "game_id": "2026020001", "market_id": "ML", "sportsbook": "book",
        "goalie_fingerprint": "g1", "roster_fingerprint": "r1", "data_snapshot_id": "d1",
        "constants_id": "k1", "model_probability": 0.55,
    }
    row.update(overrides)
    return row


def test_forward_evidence_excludes_simulated_and_mixed_lanes():
    valid = _artifact()
    dev = _artifact(evidence_lane="DEV_SYNTHETIC", data_origin="SYNTHETIC")
    assert validate_forward_artifact(valid)[0]
    cohort = build_forward_cohort([valid, dev])
    assert cohort["n_artifacts"] == 1 and cohort["sample_counts"] == {"ML": 1}
    assert cohort["excluded"]["NOT_SHADOW_FORWARD"] == 1


def test_audit_flags_only_unexplained_reprices():
    first = _artifact()
    explained = _artifact(created_at="2026-10-01T10:30:00Z", as_of="2026-10-01T10:30:00Z",
                          market_snapshot_ref="snap:2", model_probability=0.56)
    audit = first_slate_audit([first, explained])
    assert audit[0]["reprices"] == 1 and audit[0]["unexplained_reprice"] is False


def test_scheduler_increases_cadence_near_close():
    cfg = {"morning_hours_before_first": 9, "pregame_start_hours_before": 6, "pregame_minutes": 60,
           "near_close_window_minutes": 120, "near_close_minutes": 15, "postgame_hours_after_last": 4}
    plan = build_plan([NOW + timedelta(hours=10)], cfg)
    refresh = [x for x in plan if x.action == "refresh_and_price"]
    gaps = [(b.due_at - a.due_at).total_seconds() / 60 for a, b in zip(refresh, refresh[1:])]
    assert 60 in gaps and 15 in gaps
