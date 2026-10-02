import csv
import io
import json
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from nhl.data.ingest import replay
from nhl.data.snapshots import RawSnapshotStore
from nhl.market.owls import (
    HTTPResponse,
    OwlsHTTPError,
    OwlsInsightClient,
    build_event_map,
    normalize_line_history,
    normalize_live_board,
)

UTC = timezone.utc
NOW = datetime(2026, 10, 2, 19, 45, 31, tzinfo=UTC)


def board(markets=None):
    markets = markets or [{"key": "h2h", "last_update": "2026-10-02T19:44:37.375Z",
                           "outcomes": [{"name": "Winnipeg Jets", "price": -115},
                                        {"name": "Boston Bruins", "price": 104}]}]
    event = {"eventId": "owls-1", "commence_time": "2026-10-03T00:07:00Z",
             "home_team": "Winnipeg Jets", "away_team": "Boston Bruins",
             "bookmakers": [{"key": "pinnacle", "markets": markets}]}
    return json.dumps({"success": True, "data": {"pinnacle": [event]}}).encode()


def rows(payload):
    return list(csv.DictReader(io.StringIO(payload.decode())))


@dataclass
class Game:
    game_id: str
    home: str
    away: str
    start_time: datetime


def test_market_mapping_and_american_price_are_exact():
    out, rejected = normalize_live_board(board(), NOW, {"owls-1": "2026010001"},
                                          verified_settlement={("pinnacle", "h2h")})
    parsed = rows(out)
    assert not rejected and [r["selection"] for r in parsed] == ["HOME", "AWAY"]
    assert [r["price"] for r in parsed] == ["-115", "104"] and all(r["odds_format"] == "AMERICAN" for r in parsed)
    assert all(r["observed_at"] == "2026-10-02T19:45:31Z" for r in parsed)
    assert all(r["source_ts"] == "2026-10-02T19:44:37Z" for r in parsed)


def test_unknown_and_unverified_settlement_are_rejected():
    markets = [{"key": "mystery", "outcomes": []}, {"key": "totals", "outcomes": [
        {"name": "Over", "price": -110, "point": 6}, {"name": "Under", "price": -102, "point": 6}]}]
    out, rejected = normalize_live_board(board(markets), NOW, {"owls-1": "2026010001"})
    assert not rows(out)
    assert any(x.startswith("UNKNOWN_MARKET") for x in rejected)
    assert any(x.startswith("UNVERIFIED_SETTLEMENT") for x in rejected)


def test_nil_price_and_non_opposite_spread_are_rejected():
    markets = [{"key": "spreads", "outcomes": [
        {"name": "Winnipeg Jets", "price": None, "point": -1.5},
        {"name": "Boston Bruins", "price": -255, "point": 2.5}]}]
    out, rejected = normalize_live_board(board(markets), NOW, {"owls-1": "2026010001"},
                                          verified_settlement={("pinnacle", "spreads")})
    assert not rows(out)
    assert any(x.startswith("MISSING_PRICE") for x in rejected)
    assert any(x.startswith("NON_OPPOSITE_SPREAD") for x in rejected)


def test_event_mapping_exact_missing_and_ambiguous():
    event = {"eventId": "e", "home_team": "Winnipeg Jets", "away_team": "Boston Bruins",
             "commence_time": "2026-10-03T00:07:00Z"}
    game = Game("2026010001", "WPG", "BOS", datetime(2026, 10, 3, 0, 0, tzinfo=UTC))
    assert build_event_map([event], [game])[0] == {"e": "2026010001"}
    assert build_event_map([event], [])[1] == ["UNMAPPED_EVENT:e:0"]
    assert build_event_map([event], [game, game])[1] == ["AMBIGUOUS_EVENT:e:2"]


def test_history_requires_per_row_timestamp():
    payload = json.dumps({"data": {"book": "pinnacle", "market": "h2h", "side": "home",
                                    "history": [{"price": -110},
                                                {"timestamp": "2026-10-02T02:32:31.350Z", "price": -121}]}}).encode()
    out, rejected = normalize_line_history(payload, NOW, "2026010001")
    assert len(rows(out)) == 1
    assert rejected == ["NO_HISTORICAL_TIMESTAMP:0"]
    assert rows(out)[0]["observed_at"] == "2026-10-02T02:32:31Z"


def test_429_honours_retry_after_and_stores_both_bodies(tmp_path):
    responses = iter([HTTPResponse(429, b'{"retry":true}', {"Retry-After": "2"}),
                      HTTPResponse(200, b'{"success":true}', {})])
    waits = []
    store = RawSnapshotStore(tmp_path)
    client = OwlsInsightClient(store, "secret", transport=lambda *_: next(responses), sleep=waits.append,
                               clock=lambda: NOW)
    client.capture("/api/v1/nhl/odds")
    assert waits == [2.0]
    assert [store.get_bytes(e) for e in store.entries("odds")] == [b'{"retry":true}', b'{"success":true}']


def test_401_fails_closed_after_raw_capture(tmp_path):
    store = RawSnapshotStore(tmp_path)
    client = OwlsInsightClient(store, "secret", transport=lambda *_: HTTPResponse(401, b"denied", {}), clock=lambda: NOW)
    with pytest.raises(OwlsHTTPError) as exc:
        client.capture("/api/v1/nhl/odds")
    assert exc.value.status == 401 and store.get_bytes(store.latest("odds", "owls/api_v1_nhl_odds")) == b"denied"


def test_api_key_never_enters_snapshot_or_exception(tmp_path):
    key = "never-store-this"
    seen = {}
    def transport(url, headers, timeout):
        seen.update(headers)
        return HTTPResponse(403, b"forbidden", {})
    store = RawSnapshotStore(tmp_path)
    with pytest.raises(OwlsHTTPError) as exc:
        OwlsInsightClient(store, key, transport=transport, clock=lambda: NOW).capture("/api/v1/nhl/odds")
    disk = "".join(p.read_text(errors="ignore") for p in tmp_path.rglob("*") if p.is_file())
    assert seen["Authorization"] == "Bearer " + key
    assert key not in disk and key not in str(exc.value)


def test_raw_owls_snapshot_is_not_misparsed_during_replay(tmp_path):
    store = RawSnapshotStore(tmp_path)
    store.put("odds", "owls/api_v1_nhl_odds", board(), NOW, {"provider": "owls", "http_status": 200})
    state, report = replay(store)
    assert not state.odds and report.counts["odds.raw_owls"] == 1


def test_normalization_is_deterministic():
    args = (board(), NOW, {"owls-1": "2026010001"})
    kwargs = {"verified_settlement": {("pinnacle", "h2h")}}
    assert normalize_live_board(*args, **kwargs) == normalize_live_board(*args, **kwargs)
