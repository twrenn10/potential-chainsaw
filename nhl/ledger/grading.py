"""Settlement of priced outcomes (predictions and bets) against final results."""

from __future__ import annotations

from nhl.contracts import BetResult, GameResult, MarketType, Selection


def settle(
    result: GameResult,
    market: MarketType,
    selection: Selection,
    line: float | None = None,
    team_is_home: bool | None = None,
) -> BetResult:
    if market is MarketType.ML:
        return BetResult.WIN if result.winner == selection.value else BetResult.LOSS
    if market is MarketType.REG_3WAY:
        h, a = result.reg_home_goals, result.reg_away_goals
        actual = "HOME" if h > a else "AWAY" if a > h else "DRAW"
        return BetResult.WIN if actual == selection.value else BetResult.LOSS
    if line is None:
        raise ValueError(f"{market.value} needs line")
    home, away = result.settlement_goals()
    if market is MarketType.PUCK_LINE:
        margin = (home - away) if selection is Selection.HOME else (away - home)
        return _cmp(margin + line, 0.0)
    if market is MarketType.TOTAL:
        return _ou(home + away, line, selection)
    if market is MarketType.TEAM_TOTAL:
        if team_is_home is None:
            raise ValueError("TEAM_TOTAL needs team_is_home")
        return _ou(home if team_is_home else away, line, selection)
    raise ValueError(market)


def _cmp(value: float, zero: float) -> BetResult:
    if value > zero:
        return BetResult.WIN
    if value < zero:
        return BetResult.LOSS
    return BetResult.PUSH


def _ou(goals: int, line: float, selection: Selection) -> BetResult:
    r = _cmp(goals - line, 0.0)
    if selection is Selection.UNDER and r is not BetResult.PUSH:
        return BetResult.LOSS if r is BetResult.WIN else BetResult.WIN
    return r


def profit_units(result: BetResult, decimal_odds: float, stake: float = 1.0) -> float:
    if result is BetResult.WIN:
        return stake * (decimal_odds - 1.0)
    if result is BetResult.LOSS:
        return -stake
    return 0.0
