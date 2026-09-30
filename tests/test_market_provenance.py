from nhl.data.odds import parse_goalie_reports_checked, parse_odds_checked
from nhl.data.provenance import CaptureMode
from nhl.data.snapshots import SnapshotEntry
from nhl.timeutil import parse_ts

HEAD = "snapshot_ts,book,game_id,market,selection,line,team,price_american,max_stake,source\n"


def entry(fetched):
    return SnapshotEntry("odds:x", "odds", "f", parse_ts(fetched), "x", 1, {})


def test_odds_backfill_keeps_observation_time_and_rejects_impossible_rows():
    rows = (
        "2026-10-07T14:00:00Z,BookA,2026020001,ML,HOME,,,-135,,vendor\n"
        "2026-10-07T14:00:00Z,BookA,2026020001,ML,AWAY,,,115,,vendor\n"
        "2026-10-07T14:00:00Z,BookA,2026020001,ML,AWAY,,,115,,vendor\n"      # harmless duplicate
        "2026-10-07T15:00:00Z,BookA,2026020001,ML,HOME,,,-140,,vendor\n"
        "2026-10-07T15:00:00Z,BookA,2026020001,ML,HOME,,,-150,,vendor\n"     # conflicting -> both dropped
        "2026-10-07T16:00:00,BookA,2026020001,ML,HOME,,,-140,,vendor\n"      # no UTC offset
        "2026-10-07T17:00:00Z,BookA,2026020001,ML,HOME,,,-140.5,,vendor\n"   # non-integer price
        "2030-01-01T00:00:00Z,BookA,2026020001,ML,HOME,,,-140,,vendor\n"     # observed after fetch
    )
    res = parse_odds_checked((HEAD + rows).encode(), entry("2027-01-01T00:00:00Z"))
    assert res.dupes == 1
    assert len(res.records) == 2 and len(res.rejections) == 4
    assert all(r.provenance.rule == "SOURCE_PUBLISHED" and r.available_at == r.snapshot_ts for r in res.records)


def test_goalie_reports_need_pregame_publication_time_for_backfill():
    starts = {"2026020001": parse_ts("2026-10-07T23:00:00Z")}
    csv = ("available_at,game_id,team,goalie_id,state,source\n"
           "2026-10-07T21:30:00Z,2026020001,TOR,8479361,CONFIRMED,beat\n"
           ",2026020001,MTL,8478470,PROJECTED,dfo\n"
           "2026-10-07T23:05:00Z,2026020001,TOR,8479361,CONFIRMED,beat\n"
           "2026-10-07T20:00:00Z,2026029999,TOR,8479361,CONFIRMED,beat\n").encode()
    back = parse_goalie_reports_checked(csv, entry("2027-01-01T00:00:00Z"), starts, CaptureMode.BACKFILL)
    assert len(back.rejections) == 2
    by_team = {r.team: r for r in back.records}
    assert by_team["TOR"].provenance.causal and by_team["TOR"].available_at == parse_ts("2026-10-07T21:30:00Z")
    assert not by_team["MTL"].provenance.causal  # no publication time, captured after the fact
    live = parse_goalie_reports_checked(csv, entry("2026-10-07T22:00:00Z"), starts, CaptureMode.LIVE)
    assert {r.team: r for r in live.records}["MTL"].provenance.causal
