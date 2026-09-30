import json
import os

import pytest

from nhl.contracts import GoalieState, MarketType, Selection
from nhl.data import moneypuck, nhl_api
from nhl.data.odds import parse_goalie_reports_csv, parse_odds_csv
from nhl.data.pit import HistoricalStore, LeakageError, assert_visible
from nhl.data.quality import evaluate_slate
from nhl.data.snapshots import RawSnapshotStore, SnapshotIntegrityError
from nhl.timeutil import parse_ts


def test_raw_store_is_write_once_and_verified(tmp_path):
    store = RawSnapshotStore(tmp_path)
    e1 = store.put("nhl_api", "schedule/2026-10-07", b'{"a":1}', "2026-10-07T12:00:00Z")
    e2 = store.put("nhl_api", "schedule/2026-10-07", b'{"a":1}', "2026-10-07T13:00:00Z")
    assert e1.sha256 == e2.sha256
    assert len(list(store.entries())) == 2  # every fetch is logged
    assert store.latest("nhl_api", "schedule/2026-10-07", as_of="2026-10-07T12:30:00Z").fetched_at == e1.fetched_at
    blob = store._blob_path(e1.sha256)
    assert not os.access(blob, os.W_OK) or os.geteuid() == 0  # read-only (root bypasses perms)
    os.chmod(blob, 0o644)
    import gzip
    blob.write_bytes(gzip.compress(b'{"a":2}'))
    with pytest.raises(SnapshotIntegrityError):
        store.get_bytes(e1)


def test_client_stores_before_parsing(tmp_path, fixtures):
    store = RawSnapshotStore(tmp_path)
    payload = (fixtures / "nhl_schedule.json").read_bytes()
    client = nhl_api.NHLApiClient(store, transport=lambda url: payload)
    entry = client.fetch_schedule("2026-10-07", fetched_at=parse_ts("2026-09-01T00:00:00Z"))
    games = nhl_api.parse_schedule(store.get_json(entry), entry)
    assert [g.game_id for g in games] == ["2026020001", "2026020002"]
    assert games[1].away == "UTA"
    assert games[0].available_at == parse_ts("2026-09-01T00:00:00Z")


def test_situation_codes():
    sit = nhl_api.parse_situation_code("1451")
    assert nhl_api.strength_label(sit, for_home=True) == "PP"
    assert nhl_api.strength_label(sit, for_home=False) == "SH"
    en = nhl_api.parse_situation_code("0651")
    assert nhl_api.strength_label(en, for_home=True) == "EN_FOR"
    assert nhl_api.strength_label(en, for_home=False) == "EXTRA_ATTACKER"


def test_pbp_result_shootout(fixtures):
    payload = json.loads((fixtures / "nhl_pbp_shootout.json").read_text())
    events = nhl_api.parse_pbp_events(payload)
    assert events[1]["strength"] == "PP"
    res = nhl_api.parse_result(payload, parse_ts("2026-10-07T23:00:00Z"))
    assert res.end_type == "SO" and res.winner == "HOME"
    assert (res.home_goals, res.away_goals) == (2, 2)
    assert (res.p1_home_goals, res.p1_away_goals) == (1, 0)
    assert res.available_at == parse_ts("2026-10-08T03:00:00Z")


def test_boxscore_and_shifts(fixtures):
    box = nhl_api.parse_boxscore_goalies(json.loads((fixtures / "nhl_boxscore.json").read_text()), parse_ts("2026-10-07T23:00:00Z"))
    starters = {g.team: g.goalie_id for g in box if g.started}
    assert starters == {"TOR": "8479361", "MTL": "8478470"}
    assert next(g for g in box if g.goalie_id == "8478470").shots_against == 33
    shifts = nhl_api.parse_shifts(json.loads((fixtures / "nhl_shifts.json").read_text()))
    assert len(shifts) == 2 and shifts[1]["start_sec"] == 1200 + 270


def test_moneypuck_parsers_and_availability(fixtures):
    teams = moneypuck.parse_team_games((fixtures / "moneypuck_teams.csv").read_bytes())
    assert {t.situation for t in teams} == {"5on5", "all"}
    assert teams[0].available_at == parse_ts("2025-10-11T12:00:00Z")
    goalies = moneypuck.parse_goalie_games((fixtures / "moneypuck_goalies.csv").read_bytes())
    started = {g.goalie_id for g in goalies if g.started}
    assert started == {"8479361", "8478470"}
    with pytest.raises(moneypuck.SchemaError):
        moneypuck.parse_team_games(b"gameId,team\n1,TOR\n")


def test_odds_parse_dedupes_and_pit_latest(fixtures):
    odds, dupes = parse_odds_csv((fixtures / "odds_sample.csv").read_bytes(), "odds:x")
    assert dupes == 1 and len(odds) == 7
    store = HistoricalStore(odds=odds)
    view = store.view("2026-10-07T22:45:00Z")
    ml = {s.selection: s.price_american for s in view.odds("2026020001", MarketType.ML)}
    assert ml == {Selection.HOME: -145, Selection.AWAY: 125}  # the 23:30 in-game price is invisible
    early = store.view("2026-10-07T15:00:00Z").odds("2026020001", MarketType.ML)
    assert {s.price_american for s in early} == {-135, 115}


def test_goalie_reports_latest(tmp_path):
    csv = (
        "available_at,game_id,team,goalie_id,state,source\n"
        "2026-10-07T15:00:00Z,2026020001,TOR,8479361,PROJECTED,dfo\n"
        "2026-10-07T21:00:00Z,2026020001,TOR,8479361,CONFIRMED,beat\n"
    ).encode()
    reps = parse_goalie_reports_csv(csv)
    store = HistoricalStore(goalie_reports=reps)
    assert store.view("2026-10-07T16:00:00Z").goalie_reports("2026020001")[0].state is GoalieState.PROJECTED
    assert store.view("2026-10-07T22:00:00Z").goalie_reports("2026020001")[0].state is GoalieState.CONFIRMED


def test_pit_hides_future_results_and_assert_visible(fixtures):
    payload = json.loads((fixtures / "nhl_pbp_shootout.json").read_text())
    res = nhl_api.parse_result(payload, parse_ts("2026-10-07T23:00:00Z"))
    store = HistoricalStore(results=[res])
    assert store.view("2026-10-07T23:30:00Z").results() == []
    assert len(store.view("2026-10-08T03:00:00Z").results()) == 1
    with pytest.raises(LeakageError):
        assert_visible([res], parse_ts("2026-10-07T23:30:00Z"))


def test_quality_gates(fixtures):
    from nhl.data.snapshots import SnapshotEntry

    entry = SnapshotEntry("nhl_api:x", "nhl_api", "schedule/2026-10-07", parse_ts("2026-09-01T00:00:00Z"), "x", 1, {})
    games = nhl_api.parse_schedule(json.loads((fixtures / "nhl_schedule.json").read_text()), entry)
    odds, _ = parse_odds_csv((fixtures / "odds_sample.csv").read_bytes())
    store = HistoricalStore(games=games, odds=odds)
    rep = evaluate_slate(store.view("2026-10-07T22:45:00Z"), ["2026020001"], teams_with_prior={"TOR", "MTL"})
    names = {(c.name, c.status) for c in rep.checks}
    assert ("PREGAME", "PASS") in names and ("ODDS_FRESH", "PASS") in names
    assert ("GOALIE_INPUTS", "FAIL") in names  # no reports, no start history -> hard fail
    assert rep.hard_fail and rep.game_blocks("2026020001") == ["GOALIE_INPUTS"]
    late = evaluate_slate(store.view("2026-10-07T23:05:00Z"), ["2026020001"], teams_with_prior={"TOR", "MTL"})
    assert "PREGAME" in late.game_blocks("2026020001")
