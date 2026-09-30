import json

from nhl.data.ingest import capture_day, replay, replay_rosters
from nhl.data.nhl_api import NHLApiClient
from nhl.data.snapshots import RawSnapshotStore
from nhl.timeutil import parse_ts


def fake_transport(fixtures, final: bool, bad_pbp: bool = False):
    sched = json.loads((fixtures / "nhl_schedule.json").read_text())
    if final:
        sched["gameWeek"][0]["games"][0]["gameState"] = "OFF"
    pbp = json.loads((fixtures / "nhl_pbp_shootout.json").read_text())
    if bad_pbp:
        pbp["plays"][0]["situationCode"] = "9999"
    pages = {
        "schedule/2026-10-07": sched,
        "gamecenter/2026020001/play-by-play": pbp,
        "gamecenter/2026020001/boxscore": json.loads((fixtures / "nhl_boxscore.json").read_text()),
        "roster/TOR/20262027": json.loads((fixtures / "nhl_roster.json").read_text()),
    }

    def transport(url: str) -> bytes:
        for k, v in pages.items():
            if url.endswith(k):
                return json.dumps(v).encode()
        raise AssertionError(url)

    return transport


def test_capture_then_replay_is_deterministic_and_provenanced(fixtures, tmp_path):
    from datetime import date

    raw = RawSnapshotStore(tmp_path)
    pre = NHLApiClient(raw, fake_transport(fixtures, final=False))
    # Schedule captured live in the morning, final data captured that night.
    pre.fetch_schedule("2026-10-07", fetched_at=parse_ts("2026-10-07T15:00:00Z"))
    pre.fetch_roster("TOR", 2026, fetched_at=parse_ts("2026-10-01T12:00:00Z"))
    post = NHLApiClient(raw, fake_transport(fixtures, final=True))
    entries = capture_day(post, date(2026, 10, 7), fetched_at=parse_ts("2026-10-08T03:30:00Z"))
    assert len(entries) == 3
    store, rep = replay(raw)
    store2, _ = replay(raw)
    assert [g.to_row() for g in store.games] == [g.to_row() for g in store2.games]
    assert len(store.results) == 1 and store.results[0].provenance.rule == "EVENT_FACT"
    # Two schedule captures -> the game has two versions (SCHEDULED at 15:00, FINAL later).
    versions = [g for g in store.games if g.game_id == "2026020001"]
    assert [v.status for v in versions] == ["SCHEDULED", "FINAL"]
    assert store.view("2026-10-07T20:00:00Z").games()[0].status == "SCHEDULED"
    assert rep.counts["results"] == 1 and rep.counts["goalie_games"] == 3
    rosters = replay_rosters(raw)
    assert len(rosters[2026]) == 4 and all(s.provenance.causal for s in rosters[2026])
    paths = rep.write(tmp_path / "report")
    assert json.loads(open(paths["summary"]).read())["n_rejections"] == 0


def test_replay_surfaces_rejections_and_withholds_result(fixtures, tmp_path):
    raw = RawSnapshotStore(tmp_path)
    client = NHLApiClient(raw, fake_transport(fixtures, final=True, bad_pbp=True))
    client.fetch_schedule("2026-10-07", fetched_at=parse_ts("2026-10-07T15:00:00Z"))
    client.fetch_pbp("2026020001", fetched_at=parse_ts("2026-10-08T03:30:00Z"))
    store, rep = replay(raw)
    assert store.results == []
    reasons = [r.reason for r in rep.rejections]
    assert any("situationCode" in r for r in reasons) and any("result withheld" in r for r in reasons)
    anomalies = open(rep.write(tmp_path / "r")["anomalies"]).read()
    assert "result withheld" in anomalies
