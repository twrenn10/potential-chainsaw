import json
from datetime import timedelta

from nhl.contracts import RosterSlot
from nhl.data import nhl_api
from nhl.data.provenance import CaptureMode
from nhl.data.snapshots import SnapshotEntry
from nhl.features.priors import roster_as_of
from nhl.timeutil import parse_ts


def entry(fetched):
    return SnapshotEntry("nhl_api:r", "nhl_api", "roster/TOR/2026", parse_ts(fetched), "r", 1, {})


def test_roster_parser_live_vs_backfill(fixtures):
    payload = json.loads((fixtures / "nhl_roster.json").read_text())
    live = nhl_api.parse_roster_checked(payload, "TOR", 2026, entry("2026-10-01T15:00:00Z"))
    assert not live.rejections and len(live.records) == 4
    assert all(s.provenance.causal and s.available_at == parse_ts("2026-10-01T15:00:00Z") for s in live.records)
    # Fetched after the season ended: declared LIVE is overridden to backfill -> non-causal.
    late = nhl_api.parse_roster_checked(payload, "TOR", 2025, entry("2026-10-01T15:00:00Z"), CaptureMode.LIVE)
    assert all(not s.provenance.causal for s in late.records)
    bad = json.loads(json.dumps(payload))
    bad["defensemen"][0]["positionCode"] = "C"
    bad["forwards"].append(dict(payload["forwards"][0]))
    res = nhl_api.parse_roster_checked(bad, "TOR", 2026, entry("2026-10-01T15:00:00Z"))
    assert len(res.rejections) == 2 and len(res.records) == 3


def test_roster_as_of_uses_latest_known_snapshot():
    t1, t2 = parse_ts("2026-10-01T00:00:00Z"), parse_ts("2026-12-01T00:00:00Z")
    snap1 = [RosterSlot("TOR", "1", "F", 15, t1), RosterSlot("TOR", "2", "F", 15, t1)]
    snap2 = [RosterSlot("TOR", "1", "F", 15, t2), RosterSlot("TOR", "3", "F", 15, t2)]  # trade: 2 -> 3
    before, un = roster_as_of(snap1 + snap2, t2 - timedelta(days=1))
    assert [s.player_id for s in before] == ["1", "2"] and not un
    after, _ = roster_as_of(snap1 + snap2, t2)
    assert [s.player_id for s in after] == ["1", "3"]
    undated, un = roster_as_of([RosterSlot("TOR", "9", "F", 15)], t2)
    assert un and [s.player_id for s in undated] == ["9"]
