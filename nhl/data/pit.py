"""Point-in-time access. The ONLY way model/feature code reads historical data.

``HistoricalStore`` holds everything ingested. ``view(as_of)`` returns a
``PointInTimeView`` whose accessors can only return records with
``available_at <= as_of``. ``assert_visible`` is used by feature builders as a
belt-and-braces check that nothing reached them another way.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Protocol, TypeVar

from nhl.contracts import (
    Game,
    GameResult,
    GoalieGameStats,
    GoalieReport,
    MarketType,
    OddsSnapshot,
    PlayerSeason,
    TeamGameStats,
)
from nhl.timeutil import fmt_ts, parse_ts


class LeakageError(RuntimeError):
    """Raised when a record from the future reaches a point-in-time computation."""


class _Timed(Protocol):
    @property
    def available_at(self) -> datetime | None: ...


T = TypeVar("T", bound=_Timed)


def assert_visible(records: Iterable[_Timed], as_of: datetime, what: str = "record") -> None:
    cutoff = parse_ts(as_of)
    for rec in records:
        avail = rec.available_at
        if avail is not None and avail > cutoff:
            raise LeakageError(f"{what} available_at={fmt_ts(avail)} > as_of={fmt_ts(cutoff)}: {rec!r}"[:400])


@dataclass
class HistoricalStore:
    games: list[Game] = field(default_factory=list)
    results: list[GameResult] = field(default_factory=list)
    team_stats: list[TeamGameStats] = field(default_factory=list)
    goalie_stats: list[GoalieGameStats] = field(default_factory=list)
    goalie_reports: list[GoalieReport] = field(default_factory=list)
    odds: list[OddsSnapshot] = field(default_factory=list)
    player_seasons: list[PlayerSeason] = field(default_factory=list)
    data_origin: str = "HISTORICAL"

    def game(self, game_id: str) -> Game:
        """Latest known version (post-hoc use only: evaluation, grading)."""

        versions = [g for g in self.games if g.game_id == game_id]
        if not versions:
            raise KeyError(game_id)
        return max(versions, key=lambda g: (g.available_at is not None, g.available_at or g.start_time))

    def latest_games(self) -> dict[str, Game]:
        best: dict[str, Game] = {}
        key = lambda g: (g.available_at is not None, g.available_at or g.start_time)  # noqa: E731
        for g in self.games:
            if g.game_id not in best or key(g) >= key(best[g.game_id]):
                best[g.game_id] = g
        return dict(sorted(best.items()))

    def view(self, as_of: str | datetime, strict: bool = True) -> "PointInTimeView":
        return PointInTimeView(self, parse_ts(as_of), strict=strict)


class PointInTimeView:
    """``strict=True`` (default): records whose provenance is NON-CAUSAL are invisible.
    ``strict=False`` is for diagnostics only; any non-causal record it returns sets
    ``non_causal_used`` and governance hard-blocks the resulting predictions.

    Records with no provenance at all set ``unattested_used`` (legacy/synthetic
    paths); governance hard-blocks those for non-synthetic data origins.
    """

    def __init__(self, store: HistoricalStore, as_of: datetime, strict: bool = True) -> None:
        self._store = store
        self.as_of = as_of
        self.strict = strict
        self.data_origin = store.data_origin
        self.non_causal_used = False
        self.non_causal_hidden = 0
        self.unattested_used = False

    def _visible(self, rows: Iterable[T]) -> list[T]:
        out = []
        for r in rows:
            if r.available_at is not None and r.available_at > self.as_of:
                continue
            prov = getattr(r, "provenance", None)
            if prov is None:
                self.unattested_used = True
            elif not prov.causal:
                if self.strict:
                    self.non_causal_hidden += 1
                    continue
                self.non_causal_used = True
            out.append(r)
        return out

    def provenance_blocks(self) -> list[str]:
        blocks = []
        if self.non_causal_used:
            blocks.append("NON_CAUSAL_INPUTS")
        if self.unattested_used and self.data_origin != "SYNTHETIC":
            blocks.append("UNATTESTED_PROVENANCE")
        return blocks

    # Schedule: the LATEST version of each game known at as_of (reschedules and
    # postponements are new versions, never edits). Results are separate.
    def games(self) -> list[Game]:
        latest: dict[str, Game] = {}
        for g in self._visible(self._store.games):
            cur = latest.get(g.game_id)
            if cur is None or (g.available_at or self.as_of) >= (cur.available_at or self.as_of):
                latest[g.game_id] = g
        return sorted(latest.values(), key=lambda g: (g.start_time, g.game_id))

    def results(self) -> list[GameResult]:
        return self._visible(self._store.results)

    def team_stats(self) -> list[TeamGameStats]:
        return self._visible(self._store.team_stats)

    def goalie_stats(self) -> list[GoalieGameStats]:
        return self._visible(self._store.goalie_stats)

    def player_seasons(self) -> list[PlayerSeason]:
        return self._visible(self._store.player_seasons)

    def goalie_reports(self, game_id: str) -> list[GoalieReport]:
        """Latest visible report per (team, goalie) for a game."""

        latest: dict[tuple[str, str], GoalieReport] = {}
        for rep in self._visible(r for r in self._store.goalie_reports if r.game_id == game_id):
            key = (rep.team, rep.goalie_id)
            if key not in latest or rep.available_at >= latest[key].available_at:
                latest[key] = rep
        return sorted(latest.values(), key=lambda r: (r.team, r.goalie_id))

    def goalie_report_history(self, game_id: str) -> list[GoalieReport]:
        """Every visible starter report for a game (all sources), chronological."""

        rows = self._visible(r for r in self._store.goalie_reports if r.game_id == game_id)
        return sorted(rows, key=lambda r: (r.available_at, r.team, r.source, r.goalie_id, r.state.value))

    def odds(self, game_id: str, market: MarketType | None = None) -> list[OddsSnapshot]:
        """Latest visible price per (book, market, team, line, selection)."""

        latest: dict[tuple, OddsSnapshot] = {}
        for s in self._visible(o for o in self._store.odds if o.game_id == game_id):
            if market is not None and s.market is not market:
                continue
            key = (s.book, s.market, s.team, s.line, s.selection)
            if key not in latest or s.snapshot_ts >= latest[key].snapshot_ts:
                latest[key] = s
        return sorted(latest.values(), key=lambda s: (s.market.value, s.team or "", s.line or 0.0, s.selection.value, s.book))

    def odds_snapshots(self, game_ids: set[str]) -> list[OddsSnapshot]:
        """Every visible odds observation (full history up to as_of) for these games."""

        return self._visible(s for s in self._store.odds if s.game_id in game_ids)

    def team_games_played(self) -> dict[str, list[str]]:
        """Game ids with visible results per team, chronological."""

        games = {g.game_id: g for g in self.games()}
        start = {gid: g.start_time for gid, g in games.items()}
        by_team: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
        for r in self.results():
            g = games.get(r.game_id)
            if g is None:
                continue
            by_team[g.home].append((start[g.game_id], g.game_id))
            by_team[g.away].append((start[g.game_id], g.game_id))
        return {t: [gid for _, gid in sorted(v)] for t, v in by_team.items()}
