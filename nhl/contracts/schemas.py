"""Canonical record contracts.

Every record that can influence a prediction carries ``available_at``: the earliest
UTC instant at which the information was actually knowable. ``nhl.data.pit`` uses
that field (never the event date) to decide visibility at an ``as_of``.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from nhl.timeutil import fmt_ts, parse_ts

if TYPE_CHECKING:  # pragma: no cover
    from nhl.data.provenance import Provenance

from .enums import GoalieState, MarketStatus, MarketType, OddsFormat, Period, Selection
from .ids import canonical_game_id, canonical_player_id, canonical_team


GAME_STATUSES = frozenset({"SCHEDULED", "LIVE", "FINAL", "PPD", "SUSP", "CNCL"})
PRICEABLE_STATUSES = frozenset({"SCHEDULED"})


def _to_row(obj: Any) -> dict[str, Any]:
    row = asdict(obj)
    row.pop("provenance", None)
    for key, value in row.items():
        if isinstance(value, datetime):
            row[key] = fmt_ts(value)
        elif hasattr(value, "value"):
            row[key] = value.value
    return row


@dataclass(frozen=True)
class Game:
    game_id: str
    season: int
    game_type: str
    start_time: datetime  # scheduled puck drop, UTC
    home: str
    away: str
    venue: str = ""
    available_at: datetime | None = None  # when the schedule entry was known
    status: str = "SCHEDULED"  # SCHEDULED | LIVE | FINAL | PPD | SUSP | CNCL
    provenance: "Provenance | None" = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.status not in GAME_STATUSES:
            raise ValueError(f"bad game status {self.status!r}")
        object.__setattr__(self, "game_id", canonical_game_id(self.game_id))
        object.__setattr__(self, "home", canonical_team(self.home))
        object.__setattr__(self, "away", canonical_team(self.away))
        object.__setattr__(self, "start_time", parse_ts(self.start_time))
        if self.available_at is not None:
            object.__setattr__(self, "available_at", parse_ts(self.available_at))
        if self.home == self.away:
            raise ValueError("home and away must differ")

    def to_row(self) -> dict[str, Any]:
        return _to_row(self)


@dataclass(frozen=True)
class GameResult:
    """Final result. ``available_at`` is game end, never puck drop."""

    game_id: str
    home_goals: int  # includes OT goals; excludes shootout
    away_goals: int
    reg_home_goals: int
    reg_away_goals: int
    p1_home_goals: int
    p1_away_goals: int
    end_type: str  # REG | OT | SO
    shootout_winner: str | None  # HOME | AWAY | None
    available_at: datetime
    provenance: "Provenance | None" = field(default=None, compare=False, repr=False)
    actual_start: datetime | None = None  # actual puck drop when a source provides it

    def __post_init__(self) -> None:
        object.__setattr__(self, "game_id", canonical_game_id(self.game_id))
        object.__setattr__(self, "available_at", parse_ts(self.available_at))
        if self.actual_start is not None:
            object.__setattr__(self, "actual_start", parse_ts(self.actual_start))
        if self.end_type not in {"REG", "OT", "SO"}:
            raise ValueError(f"bad end_type {self.end_type}")
        if self.end_type == "SO" and self.shootout_winner not in {"HOME", "AWAY"}:
            raise ValueError("shootout result needs shootout_winner")
        if self.end_type == "REG" and self.reg_home_goals == self.reg_away_goals:
            raise ValueError("regulation result cannot be tied")

    @property
    def winner(self) -> str:
        if self.end_type == "SO":
            return str(self.shootout_winner)
        return "HOME" if self.home_goals > self.away_goals else "AWAY"

    def settlement_goals(self) -> tuple[int, int]:
        """Sportsbook convention: shootout winner is credited one goal."""

        home, away = self.home_goals, self.away_goals
        if self.end_type == "SO":
            if self.shootout_winner == "HOME":
                home += 1
            else:
                away += 1
        return home, away

    def to_row(self) -> dict[str, Any]:
        return _to_row(self)


@dataclass(frozen=True)
class TeamGameStats:
    """One team's per-game, per-strength stats (MoneyPuck game-by-game grain)."""

    game_id: str
    team: str
    opponent: str
    is_home: bool
    game_date: str
    situation: str  # 5on5 | 5on4 | 4on5 | all | other
    toi_sec: float
    xg_for: float
    xg_against: float
    goals_for: int
    goals_against: int
    sog_for: int
    sog_against: int
    penalties_for: int  # penalties taken by this team
    penalties_against: int  # penalties drawn
    available_at: datetime
    source: str = "moneypuck"
    provenance: "Provenance | None" = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "game_id", canonical_game_id(self.game_id))
        object.__setattr__(self, "team", canonical_team(self.team))
        object.__setattr__(self, "opponent", canonical_team(self.opponent))
        object.__setattr__(self, "available_at", parse_ts(self.available_at))


@dataclass(frozen=True)
class GoalieGameStats:
    game_id: str
    team: str
    goalie_id: str
    started: bool
    toi_sec: float
    shots_against: int
    goals_against: int
    xg_against: float
    available_at: datetime
    provenance: "Provenance | None" = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "game_id", canonical_game_id(self.game_id))
        object.__setattr__(self, "team", canonical_team(self.team))
        object.__setattr__(self, "goalie_id", canonical_player_id(self.goalie_id))
        object.__setattr__(self, "available_at", parse_ts(self.available_at))

    @property
    def gsax(self) -> float:
        return self.xg_against - self.goals_against


@dataclass(frozen=True)
class GoalieReport:
    """A starter report as observed at ``available_at`` (e.g. DailyFaceoff, beat writer)."""

    game_id: str
    team: str
    goalie_id: str
    state: GoalieState
    source: str
    available_at: datetime
    provenance: "Provenance | None" = field(default=None, compare=False, repr=False)
    confidence: float | None = None  # source-stated confidence in [0, 1], if any

    def __post_init__(self) -> None:
        object.__setattr__(self, "game_id", canonical_game_id(self.game_id))
        object.__setattr__(self, "team", canonical_team(self.team))
        object.__setattr__(self, "goalie_id", canonical_player_id(self.goalie_id))
        object.__setattr__(self, "state", GoalieState(self.state))
        object.__setattr__(self, "available_at", parse_ts(self.available_at))


@dataclass(frozen=True)
class OddsSnapshot:
    """One market observation: one book's price for one outcome at one instant.

    Timestamps (all UTC):
    * ``snapshot_ts`` -- OBSERVATION time: when the price was seen (by the provider or
      us). It is the instant from which this price is known to exist, so it doubles
      as ``available_at``.
    * ``source_ts`` -- the provider's own last-change time for the price, if given
      (must be <= ``snapshot_ts``).
    * fetch time lives in ``provenance.fetched_at`` (must be >= ``snapshot_ts``).

    Price: ``price_american`` is always present (display/legacy). When the source
    quoted DECIMAL, the exact value is kept in ``price_decimal`` and all probability
    maths uses ``decimal``; nothing is re-rounded silently.
    """

    snapshot_ts: datetime
    book: str
    game_id: str
    market: MarketType
    selection: Selection
    price_american: int
    line: float | None = None
    team: str | None = None  # TEAM_TOTAL only
    max_stake: float | None = None  # limit observed, when the feed provides one
    source: str = ""
    snapshot_id: str = ""  # raw snapshot the row was parsed from
    provenance: "Provenance | None" = field(default=None, compare=False, repr=False)
    period: Period | None = None  # defaults to the market's settlement period
    participant: str | None = None  # NHL player id for props
    odds_format: OddsFormat = OddsFormat.AMERICAN
    price_decimal: float | None = None
    source_ts: datetime | None = None
    market_status: MarketStatus = MarketStatus.OPEN
    provider: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "snapshot_ts", parse_ts(self.snapshot_ts))
        object.__setattr__(self, "game_id", canonical_game_id(self.game_id))
        object.__setattr__(self, "market", MarketType(self.market))
        object.__setattr__(self, "selection", Selection(self.selection))
        object.__setattr__(self, "odds_format", OddsFormat(self.odds_format))
        object.__setattr__(self, "market_status", MarketStatus(self.market_status))
        object.__setattr__(self, "period", Period(self.period) if self.period else self.market.default_period)
        if self.team:
            object.__setattr__(self, "team", canonical_team(self.team))
        if self.participant:
            object.__setattr__(self, "participant", canonical_player_id(self.participant))
        if self.source_ts is not None:
            object.__setattr__(self, "source_ts", parse_ts(self.source_ts))
            if self.source_ts > self.snapshot_ts:
                raise ValueError("source_ts after observation time")
        if not self.book.strip():
            raise ValueError("book required")
        validate_selection(self.market, self.selection)
        validate_line(self.market, self.selection, self.line)
        if (self.market is MarketType.TEAM_TOTAL) != bool(self.team):
            raise ValueError("team is required for TEAM_TOTAL and only for TEAM_TOTAL")
        if self.market.is_prop != bool(self.participant):
            raise ValueError("participant is required for props and only for props")
        validate_price(self.price_american, self.price_decimal, self.odds_format)

    @property
    def available_at(self) -> datetime:
        return self.snapshot_ts

    @property
    def decimal(self) -> float:
        if self.price_decimal is not None:
            return float(self.price_decimal)
        p = float(self.price_american)
        return 1.0 + (p / 100.0 if p > 0 else 100.0 / -p)

    def to_row(self) -> dict[str, Any]:
        return _to_row(self)


def _market_cfg() -> dict:
    from nhl.config import load

    return load("market")


def validate_selection(market: MarketType, selection: Selection) -> None:
    allowed = {
        MarketType.ML: {Selection.HOME, Selection.AWAY},
        MarketType.REG_3WAY: {Selection.HOME, Selection.DRAW, Selection.AWAY},
        MarketType.PUCK_LINE: {Selection.HOME, Selection.AWAY},
    }.get(market, {Selection.OVER, Selection.UNDER})
    if selection not in allowed:
        raise ValueError(f"selection {selection.value} impossible for {market.value}")


def validate_line(market: MarketType, selection: Selection, line: float | None) -> None:
    if not market.has_line:
        if line is not None:
            raise ValueError(f"{market.value} takes no line")
        return
    if line is None:
        raise ValueError(f"{market.value} snapshot needs line")
    rule = _market_cfg()["lines"][market.value]
    x = float(line)
    if not math.isfinite(x) or abs(x / rule["step"] - round(x / rule["step"])) > 1e-9:
        raise ValueError(f"{market.value} line {line} not a multiple of {rule['step']}")
    if market is MarketType.PUCK_LINE:
        if not rule["min_abs"] <= abs(x) <= rule["max_abs"]:
            raise ValueError(f"impossible puck line {line}")
    elif not rule["min"] <= x <= rule["max"]:
        raise ValueError(f"impossible {market.value} line {line}")


def validate_price(american: int, decimal: float | None, fmt: "OddsFormat") -> None:
    lim = _market_cfg()["price_limits"]
    if isinstance(american, bool) or int(american) != american:
        raise ValueError(f"American price must be an integer: {american!r}")
    a = int(american)
    if -100 < a < 100 or abs(a) > lim["american_abs_max"]:
        raise ValueError(f"invalid American price {a}")
    if fmt is OddsFormat.DECIMAL:
        if decimal is None or not math.isfinite(decimal) or not lim["decimal_min"] <= decimal <= lim["decimal_max"]:
            raise ValueError(f"invalid decimal price {decimal!r}")
    elif decimal is not None:
        raise ValueError("price_decimal is only set for DECIMAL-format sources")


@dataclass(frozen=True)
class PlayerSeason:
    """Player-season impact summary used for preseason priors.

    Rates are on-ice per 60 at 5v5, already relative to league average (+ is good
    for xgf, + is bad for xga). ``available_at`` must be after the season ended.
    """

    player_id: str
    season: int
    team: str
    position: str  # F | D | G
    toi_5v5_min: float
    on_ice_xgf60_rel: float
    on_ice_xga60_rel: float
    available_at: datetime
    extras: dict[str, float] = field(default_factory=dict)
    provenance: "Provenance | None" = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "player_id", canonical_player_id(self.player_id))
        object.__setattr__(self, "team", canonical_team(self.team))
        object.__setattr__(self, "available_at", parse_ts(self.available_at))
        if self.position not in {"F", "D", "G"}:
            raise ValueError(f"bad position {self.position}")


@dataclass(frozen=True)
class RosterSlot:
    """One player on one team's roster snapshot.

    ``available_at`` = when this roster state was knowable (snapshot capture or
    source publication). Slots without it are UNATTESTED and are hard-blocked for
    non-synthetic data. A team's roster at ``as_of`` is its latest snapshot with
    ``available_at <= as_of`` (see ``nhl.features.priors.roster_as_of``).
    """

    team: str
    player_id: str
    position: str  # F | D | G
    proj_5v5_min: float  # projected 5v5 minutes per game
    available_at: datetime | None = None
    provenance: "Provenance | None" = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "team", canonical_team(self.team))
        object.__setattr__(self, "player_id", canonical_player_id(self.player_id))
        if self.position not in {"F", "D", "G"}:
            raise ValueError(f"bad position {self.position}")
        if not (0.0 <= self.proj_5v5_min <= 40.0):
            raise ValueError(f"implausible projected minutes {self.proj_5v5_min}")
        if self.available_at is not None:
            object.__setattr__(self, "available_at", parse_ts(self.available_at))
