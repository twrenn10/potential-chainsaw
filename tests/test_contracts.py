from datetime import datetime

import pytest

from nhl.contracts import (
    Game,
    GameResult,
    MarketType,
    OddsSnapshot,
    Selection,
    build_market_key,
    canonical_game_id,
    canonical_team,
    season_of,
)
from nhl.timeutil import parse_ts


def test_team_aliases_and_relocation():
    assert canonical_team("l.a") == "LAK"
    assert canonical_team("ARI") == "UTA"
    with pytest.raises(ValueError):
        canonical_team("XXX")


def test_game_id_validation():
    assert season_of("2026020001") == 2026
    with pytest.raises(ValueError):
        canonical_game_id("2026990001")


def test_naive_timestamp_rejected():
    with pytest.raises(ValueError):
        parse_ts(datetime(2026, 10, 7, 23, 0))
    with pytest.raises(ValueError):
        Game("2026020001", 2026, "REGULAR", "2026-10-07T23:00:00", "TOR", "MTL")


def test_market_key_requires_line_and_team():
    assert build_market_key("2026020001", MarketType.TOTAL, Selection.OVER, 6.5) == "2026020001:TOTAL:OVER:+6.5"
    with pytest.raises(ValueError):
        build_market_key("2026020001", MarketType.TOTAL, Selection.OVER)
    with pytest.raises(ValueError):
        build_market_key("2026020001", MarketType.TEAM_TOTAL, Selection.OVER, 3.5)
    assert "TOR" in build_market_key("2026020001", MarketType.TEAM_TOTAL, Selection.OVER, 3.5, team="TOR")


def test_odds_snapshot_validation():
    with pytest.raises(ValueError):
        OddsSnapshot("2026-10-07T20:00:00Z", "bk", "2026020001", MarketType.ML, Selection.HOME, 50)
    with pytest.raises(ValueError):
        OddsSnapshot("2026-10-07T20:00:00Z", "bk", "2026020001", MarketType.TOTAL, Selection.OVER, -110)


def test_shootout_settlement_credits_one_goal():
    r = GameResult("2026020001", 2, 2, 2, 2, 0, 1, "SO", "HOME", "2026-10-08T02:00:00Z")
    assert r.winner == "HOME"
    assert r.settlement_goals() == (3, 2)
    with pytest.raises(ValueError):
        GameResult("2026020001", 2, 2, 2, 2, 0, 0, "REG", None, "2026-10-08T02:00:00Z")
