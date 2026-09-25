"""Shared enums for the NHL engine.

Mirrors the role of ``super_engine.data_contracts.enums`` in tournament_v2, but the
market set and lanes are NHL-native.
"""

from __future__ import annotations

from enum import Enum


class MarketType(str, Enum):
    """Canonical game markets priced in Phase 1B."""

    ML = "ML"  # full game incl. OT + shootout
    REG_3WAY = "REG_3WAY"  # 60-minute result: HOME / DRAW / AWAY
    PUCK_LINE = "PUCK_LINE"  # full game incl. OT; shootout winner credited +1 goal
    TOTAL = "TOTAL"  # full game incl. OT; shootout winner credited +1 goal
    TEAM_TOTAL = "TEAM_TOTAL"  # same settlement convention as TOTAL

    @property
    def is_two_way(self) -> bool:
        return self is not MarketType.REG_3WAY

    @property
    def has_line(self) -> bool:
        return self in {MarketType.PUCK_LINE, MarketType.TOTAL, MarketType.TEAM_TOTAL}


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
