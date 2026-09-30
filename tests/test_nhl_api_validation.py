import copy
import json

import pytest

from nhl.data import nhl_api
from nhl.data.provenance import CaptureMode
from nhl.data.snapshots import SnapshotEntry
from nhl.data.validation import ParseError, strict_float, strict_int, FieldError
from nhl.timeutil import parse_ts

START = parse_ts("2026-10-07T23:00:00Z")


def entry(fetched: str = "2026-09-01T00:00:00Z", key: str = "k") -> SnapshotEntry:
    return SnapshotEntry("nhl_api:abc", "nhl_api", key, parse_ts(fetched), "abc", 1, {})


def load(fixtures, name):
    return json.loads((fixtures / name).read_text())


def test_strict_numbers():
    assert strict_int("3", "x") == 3 and strict_int(3.0, "x") == 3
    for bad in ("2.7", "", "x", True, -1, None):
        with pytest.raises(FieldError):
            strict_int(bad, "x")
    with pytest.raises(FieldError):
        strict_float("nan", "x")


def test_schedule_rejects_bad_records_individually(fixtures):
    payload = load(fixtures, "nhl_schedule.json")
    good = payload["gameWeek"][0]["games"][0]
    bad = []
    for mutate in (
        lambda g: g.update(season=20252026),                      # season inconsistent with id
        lambda g: g.update(startTimeUTC="2026-10-07T23:00:00"),   # no UTC offset
        lambda g: g["homeTeam"].update(abbrev="XXX"),             # unknown team
        lambda g: g.update(gameScheduleState="TBD"),              # no start time
        lambda g: g.update(gameState="WEIRD"),
        lambda g: g.update(gameType=3),                           # type inconsistent with id
        lambda g: g.pop("startTimeUTC"),
    ):
        g = copy.deepcopy(good)
        g["id"] = 2026020100 + len(bad)
        mutate(g)
        bad.append(g)
    ppd = copy.deepcopy(good)
    ppd.update(id=2026020200, gameScheduleState="PPD")
    payload["gameWeek"][0]["games"] += bad + [ppd]
    res = nhl_api.parse_schedule_checked(payload, entry())
    assert len(res.rejections) == len(bad)
    assert {g.game_id for g in res.records} == {"2026020001", "2026020002", "2026020200"}
    assert next(g for g in res.records if g.game_id == "2026020200").status == "PPD"
    with pytest.raises(ParseError):
        nhl_api.parse_schedule(payload, entry())


def test_schedule_duplicate_ids_are_ambiguous(fixtures):
    payload = load(fixtures, "nhl_schedule.json")
    payload["gameWeek"][0]["games"].append(copy.deepcopy(payload["gameWeek"][0]["games"][0]))
    res = nhl_api.parse_schedule_checked(payload, entry())
    assert [g.game_id for g in res.records] == ["2026020002"]


def test_schedule_provenance_live_vs_backfill(fixtures):
    payload = load(fixtures, "nhl_schedule.json")
    live = nhl_api.parse_schedule(payload, entry())
    assert all(g.provenance.causal and g.provenance.rule == "LIVE_CAPTURE" for g in live)
    # Fetched after the games started, with no documented override -> non-causal.
    late = nhl_api.parse_schedule_checked(payload, entry("2026-12-01T00:00:00Z"), CaptureMode.BACKFILL, overrides={"schedule": {}})
    assert all(not g.provenance.causal for g in late.records)
    ov = {"schedule": {"2026": {"override_id": "SCHED-2026", "available_at": "2026-07-01T00:00:00Z", "evidence": "x"}}}
    over = nhl_api.parse_schedule(payload, entry("2026-12-01T00:00:00Z"), CaptureMode.BACKFILL, ov)
    assert all(g.available_at == parse_ts("2026-07-01T00:00:00Z") and g.provenance.override_id == "SCHED-2026" for g in over)


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (lambda p: p["plays"][0].update(situationCode="1771"), "impossible skaters"),
        (lambda p: p["plays"][0].update(situationCode="15x1"), "malformed"),
        (lambda p: p["plays"][0]["details"].update(eventOwnerTeamId=99), "not in game"),
        (lambda p: p["plays"][0].update(timeInPeriod="21:00"), "exceeds"),
        (lambda p: p["plays"][0].update(periodDescriptor={"number": 4, "periodType": "REG"}), "impossible period"),
        (lambda p: p["plays"][2]["details"].pop("duration"), "duration"),
        (lambda p: p["plays"].append(copy.deepcopy(p["plays"][0])), "duplicate eventId"),
    ],
)
def test_pbp_rejections(fixtures, mutate, reason):
    payload = load(fixtures, "nhl_pbp_shootout.json")
    mutate(payload)
    res = nhl_api.parse_pbp_checked(payload)
    assert res.rejections and reason in res.rejections[0].reason
    result, ev = nhl_api.parse_result_checked(payload, START, entry())
    assert result is None  # never produce a result from an incomplete/invalid event stream


def test_result_cross_checks_reported_score(fixtures):
    payload = load(fixtures, "nhl_pbp_shootout.json")
    ok, ev = nhl_api.parse_result_checked(payload, START, entry("2026-10-08T02:00:00Z"))
    assert ok.end_type == "SO" and not ev.warnings
    assert ok.provenance.rule == "EVENT_FACT" and ok.available_at == parse_ts("2026-10-08T02:00:00Z")
    # Backfilled today: still available at the historical event bound.
    old, _ = nhl_api.parse_result_checked(payload, START, entry("2027-06-01T00:00:00Z"))
    assert old.available_at == parse_ts("2026-10-08T03:00:00Z")
    bad = copy.deepcopy(payload)
    bad["homeTeam"]["score"] = 4
    res, ev = nhl_api.parse_result_checked(bad, START, entry())
    assert res is None and "reconstructed" in ev.rejections[-1].reason
    noscore = copy.deepcopy(payload)
    noscore["homeTeam"].pop("score")
    assert nhl_api.parse_result_checked(noscore, START, entry())[0] is None
    sog = copy.deepcopy(payload)
    sog["homeTeam"]["sog"] = 30
    res, ev = nhl_api.parse_result_checked(sog, START, entry())
    assert res is not None and ev.warnings  # data inconsistency surfaced, not hidden
    live = copy.deepcopy(payload)
    live["gameState"] = "LIVE"
    assert nhl_api.parse_result_checked(live, START, entry())[0] is None


def test_boxscore_rejections(fixtures):
    payload = load(fixtures, "nhl_boxscore.json")
    two = copy.deepcopy(payload)
    two["playerByGameStats"]["awayTeam"]["goalies"][1]["starter"] = True
    res = nhl_api.parse_boxscore_goalies_checked(two, START, entry())
    assert any("exactly one starter" in r.reason for r in res.rejections)
    inc = copy.deepcopy(payload)
    inc["playerByGameStats"]["homeTeam"]["goalies"][0]["saveShotsAgainst"] = "29/30"
    res = nhl_api.parse_boxscore_goalies_checked(inc, START, entry())
    assert any("inconsistent" in r.reason for r in res.rejections)
    clean = nhl_api.parse_boxscore_goalies_checked(payload, START, entry())
    assert not clean.rejections and all(g.provenance.causal for g in clean.records)


def test_shift_rejections(fixtures):
    payload = load(fixtures, "nhl_shifts.json")
    payload["data"].append({"gameId": 2026020001, "playerId": 1, "teamAbbrev": "TOR", "period": 1,
                            "startTime": "10:00", "endTime": "09:00", "typeCode": 517})
    res = nhl_api.parse_shifts_checked(payload)
    assert len(res.records) == 2 and "ends before" in res.rejections[0].reason
