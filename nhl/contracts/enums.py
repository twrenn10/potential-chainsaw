"""Shared enums for the NHL engine.

Mirrors the role of ``super_engine.data_contracts.enums`` in tournament_v2, but the
market set and lanes are NHL-native.
"""

from __future__ import annotations

from enum import Enum


class MarketType(str, Enum):
    """Canonical markets. Each is structurally distinct; never collapse them.

    Game markets are priced by the engine. Player/goalie props are accepted, keyed,
    de-vigged and closed at the data layer but NOT priced (research-only, Phase 1D).
    """

    ML = "ML"  # full game incl. OT + shootout
    REG_3WAY = "REG_3WAY"  # 60-minute result: HOME / DRAW / AWAY
    PUCK_LINE = "PUCK_LINE"  # full game incl. OT; shootout winner credited +1 goal
    TOTAL = "TOTAL"  # full game incl. OT; shootout winner credited +1 goal
    TEAM_TOTAL = "TEAM_TOTAL"  # same settlement convention as TOTAL
    GOALIE_SAVES = "GOALIE_SAVES"  # prop: one goalie's saves over/under
    PLAYER_SOG = "PLAYER_SOG"  # prop: one skater's shots on goal over/under

    @property
    def is_two_way(self) -> bool:
        return self is not MarketType.REG_3WAY

    @property
    def has_line(self) -> bool:
        return self in {MarketType.PUCK_LINE, MarketType.TOTAL, MarketType.TEAM_TOTAL,
                        MarketType.GOALIE_SAVES, MarketType.PLAYER_SOG}

    @property
    def is_prop(self) -> bool:
        return self in {MarketType.GOALIE_SAVES, MarketType.PLAYER_SOG}

    @property
    def priced(self) -> bool:
        """Whether the game engine produces model probabilities for this market."""

        return not self.is_prop

    @property
    def default_period(self) -> "Period":
        return Period.REGULATION if self is MarketType.REG_3WAY else Period.FULL_GAME


class Period(str, Enum):
    """Settlement period. FULL_GAME includes OT (and shootout per market rules)."""

    FULL_GAME = "FULL_GAME"
    REGULATION = "REGULATION"
    P1 = "P1"


class OddsFormat(str, Enum):
    AMERICAN = "AMERICAN"
    DECIMAL = "DECIMAL"


class MarketStatus(str, Enum):
    OPEN = "OPEN"
    SUSPENDED = "SUSPENDED"
    CLOSED = "CLOSED"


class Selection(str, Enum):
    HOME = "HOME"
    AWAY = "AWAY"
    DRAW = "DRAW"
    OVER = "OVER"
    UNDER = "UNDER"


class Lane(str, Enum):
    """Promotion lanes. Order is least to most trusted.

    BLOCKED rows can never be promoted downstream (tournament_v2 rule). In Phase 1
    everything that is not BLOCKED is UNVALIDATED; see ``nhl.governance.lanes``.
    """

    BLOCKED = "BLOCKED"
    UNVALIDATED = "UNVALIDATED"
    PASS = "PASS"
    WATCH = "WATCH"
    MODEL_PLUS = "MODEL_PLUS"
    ACTIONABLE = "ACTIONABLE"


class GoalieState(str, Enum):
    """Starter information state for one team at prediction time.

    Probable/projected is never equivalent to confirmed (tournament_v2
    starter-certainty rule).
    """

    CONFIRMED = "CONFIRMED"
    PROJECTED = "PROJECTED"
    UNKNOWN = "UNKNOWN"


class LineupState(str, Enum):
    CONFIRMED = "CONFIRMED"  # morning skate / warmup lines observed
    PROJECTED = "PROJECTED"
    UNKNOWN = "UNKNOWN"


class BetResult(str, Enum):
    WIN = "WIN"
    LOSS = "LOSS"
    PUSH = "PUSH"
    VOID = "VOID"
    PENDING = "PENDING"


class PredictionMode(str, Enum):
    """FORWARD rows are the only evidence that counts for promotion gates in 1E."""

    FORWARD = "FORWARD"
    BACKTEST = "BACKTEST"


class DataOrigin(str, Enum):
    """Provenance of the data a prediction was built on.

    SYNTHETIC mirrors tournament_v2's DEV_BOOTSTRAP_SOURCE: always BLOCKED.
    """

    LIVE = "LIVE"
    HISTORICAL = "HISTORICAL"
    SYNTHETIC = "SYNTHETIC"
