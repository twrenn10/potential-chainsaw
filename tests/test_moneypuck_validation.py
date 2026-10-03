import pytest

from nhl.data import moneypuck
from nhl.data.pit import HistoricalStore
from nhl.data.snapshots import SnapshotEntry
from nhl.data.validation import ParseError
from nhl.timeutil import parse_ts

HEADER = "team,season,name,gameId,playerTeam,opposingTeam,home_or_away,gameDate,position,situation,iceTime,xGoalsFor,xGoalsAgainst,goalsFor,goalsAgainst,shotsOnGoalFor,shotsOnGoalAgainst,penaltiesFor,penaltiesAgainst\n"
GOOD = "TOR,2025,TOR,2025020010,TOR,MTL,HOME,20251010,Team Level,5on5,2900,2.4,1.9,2,1,24,20,0,0\n"


def entry(fetched: str) -> SnapshotEntry:
    return SnapshotEntry("moneypuck:x", "moneypuck", "teams", parse_ts(fetched), "x", 1, {})


@pytest.mark.parametrize(
    "row,reason",
    [
        (GOOD.replace(",2,1,24", ",2.7,1,24"), "not an integer"),
        (GOOD.replace("2900", "-5"), "< 0"),
        (GOOD.replace("2900", "nan"), "non-finite"),
        (GOOD.replace("HOME", "NEUTRAL"), "home_or_away"),
        (GOOD.replace("20251010", "20251310"), "gameDate"),
        (GOOD.replace("20251010", "20230101"), "outside season"),
        (GOOD.replace("TOR,2025,TOR", "TOR,2024,TOR"), "inconsistent"),
        (GOOD.replace(",MTL,", ",TOR,"), "playerTeam == opposingTeam"),
        (GOOD.replace("5on5", "6on5"), "unknown situation"),
        (GOOD.replace(",2.4,", ",,"), "missing"),
    ],
)
def test_team_rows_rejected_not_coerced(row, reason):
    res = moneypuck.parse_team_games_checked((HEADER + GOOD.replace("2025020010", "2025020011") + row).encode())
    assert len(res.records) == 1 and reason in res.rejections[0].reason


def test_duplicate_rows_are_ambiguous():
    res = moneypuck.parse_team_games_checked((HEADER + GOOD + GOOD).encode())
    assert res.records == [] and "duplicate" in res.rejections[0].reason
    with pytest.raises(ParseError):
        moneypuck.parse_team_games((HEADER + GOOD + GOOD).encode())


def test_vintage_backfill_is_non_causal_and_hidden_from_strict_views():
    payload = (HEADER + GOOD).encode()
    live = moneypuck.parse_team_games(payload, entry=entry("2025-10-11T08:00:00Z"), attestations=[])
    assert live[0].provenance.causal and live[0].provenance.rule == "VINTAGE_CONTEMPORANEOUS"
    back = moneypuck.parse_team_games(payload, entry=entry("2026-09-30T00:00:00Z"), attestations=[])
    assert not back[0].provenance.causal and back[0].provenance.non_causal_reason
    store = HistoricalStore(team_stats=back, data_origin="HISTORICAL")
    assert store.view("2026-01-01T00:00:00Z").team_stats() == []
    att = [{"attestation_id": "A", "model_id": "mp-2024", "trained_through_season": 2024, "published_at": "2025-09-01T00:00:00Z"}]
    attested = moneypuck.parse_team_games(payload, entry=entry("2026-09-30T00:00:00Z"), attestations=att)
    assert attested[0].provenance.causal and attested[0].provenance.source_version == "mp-2024"


def test_goalie_tied_icetime_is_ambiguous(fixtures):
    csv = (fixtures / "moneypuck_goalies.csv").read_text().replace("8481519,2025,C Goalie,2025020010,MTL,TOR,AWAY,20251010,G,all,1200",
                                                                    "8481519,2025,C Goalie,2025020010,MTL,TOR,AWAY,20251010,G,all,2400")
    res = moneypuck.parse_goalie_games_checked(csv.encode())
    assert {g.team for g in res.records} == {"TOR"} and "ambiguous" in res.rejections[0].reason


def test_goalie_goals_exceeding_shots_rejected(fixtures):
    csv = (fixtures / "moneypuck_goalies.csv").read_text().replace("2.6,2,29", "2.6,19,18")
    res = moneypuck.parse_goalie_games_checked(csv.encode())
    assert any("goals > shots" in r.reason for r in res.rejections)


def test_non_causal_moneypuck_does_not_hide_causal_boxscore_starts(fixtures):
    """A VINTAGE_UNVERIFIED MoneyPuck row merged with a causal boxscore row must not make
    the starter fact invisible to strict views."""

    import json

    from nhl.data import nhl_api
    from nhl.data.pit import HistoricalStore

    box_payload = json.loads((fixtures / "nhl_boxscore.json").read_text())
    box_payload["id"] = 2025020010
    box = nhl_api.parse_boxscore_goalies_checked(
        box_payload, parse_ts("2025-10-10T23:00:00Z"),
        SnapshotEntry("nhl_api:b", "nhl_api", "boxscore/2025020010", parse_ts("2025-10-11T03:30:00Z"), "b", 1, {}),
    ).records
    mp_csv = (fixtures / "moneypuck_goalies.csv").read_text().replace("8479361", "8479361").encode()
    mp = moneypuck.parse_goalie_games(mp_csv, entry=entry("2026-09-30T00:00:00Z"), attestations=[])
    assert all(not g.provenance.causal for g in mp)
    merged = moneypuck.merge_goalie_sources(mp, box)
    store = HistoricalStore(goalie_stats=merged, data_origin="HISTORICAL")
    visible = store.view("2026-01-01T00:00:00Z").goalie_stats()
    starters = {g.goalie_id for g in visible if g.started}
    assert {"8479361", "8478470"} <= starters
