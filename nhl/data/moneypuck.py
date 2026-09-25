"""MoneyPuck CSV ingestion (team game-by-game, goalie game-by-game, skater seasons).

Leakage rules enforced here (see docs/leakage_audit.md):
* Season-summary files are END-OF-SEASON aggregates. They are only visible after
  the season closes (``available_at`` = July 1 after the season), so they can feed
  *next* season's priors but never same-season predictions.
* Game-by-game rows become visible the next day at 12:00 UTC (MoneyPuck refreshes
  overnight). Same-night data is never assumed available.
* MoneyPuck's xG model is trained on multi-season data that may postdate a backtest
  date. That is a known, documented residual leakage; the mitigation is to refit our
  own xG walk-forward before any ACTIONABLE promotion (Phase 1E blocker).

Column semantics flagged ``VERIFY`` below must be checked against a live file before
the first real backtest.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from nhl.contracts import GoalieGameStats, PlayerSeason, TeamGameStats

from .snapshots import RawSnapshotStore, SnapshotEntry

SOURCE = "moneypuck"
TEAM_REQUIRED = (
    "gameId", "playerTeam", "opposingTeam", "home_or_away", "gameDate", "situation",
    "iceTime", "xGoalsFor", "xGoalsAgainst", "goalsFor", "goalsAgainst",
    "shotsOnGoalFor", "shotsOnGoalAgainst", "penaltiesFor", "penaltiesAgainst",
)
GOALIE_REQUIRED = ("playerId", "gameId", "playerTeam", "gameDate", "situation", "icetime", "xGoals", "goals", "ongoal")
SKATER_REQUIRED = ("playerId", "season", "team", "position", "situation", "icetime", "onIce_xGoalsFor", "onIce_xGoalsAgainst")


class SchemaError(ValueError):
    pass


def game_row_available_at(game_date: str) -> datetime:
    """``gameDate`` is YYYYMMDD (local). Visible next day 12:00 UTC."""

    d = datetime.strptime(game_date, "%Y%m%d").replace(tzinfo=timezone.utc)
    return d + timedelta(days=1, hours=12)


def season_summary_available_at(season: int) -> datetime:
    return datetime(season + 1, 7, 1, tzinfo=timezone.utc)


def _reader(payload: bytes, required: tuple[str, ...]) -> csv.DictReader:
    reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig")))
    missing = [c for c in required if c not in (reader.fieldnames or [])]
    if missing:
        raise SchemaError(f"missing columns: {missing}")
    return reader


def store_csv(store: RawSnapshotStore, key: str, payload: bytes, fetched_at: datetime, url: str = "") -> SnapshotEntry:
    return store.put(SOURCE, key, payload, fetched_at, {"url": url})


def parse_team_games(payload: bytes, situations: tuple[str, ...] = ("5on5", "5on4", "4on5", "all")) -> list[TeamGameStats]:
    out: list[TeamGameStats] = []
    for r in _reader(payload, TEAM_REQUIRED):
        if r["situation"] not in situations:
            continue
        out.append(
            TeamGameStats(
                game_id=r["gameId"],
                team=r["playerTeam"],
                opponent=r["opposingTeam"],
                is_home=r["home_or_away"].upper() == "HOME",
                game_date=r["gameDate"],
                situation=r["situation"],
                toi_sec=float(r["iceTime"]),
                xg_for=float(r["xGoalsFor"]),
                xg_against=float(r["xGoalsAgainst"]),
                goals_for=int(float(r["goalsFor"])),
                goals_against=int(float(r["goalsAgainst"])),
                sog_for=int(float(r["shotsOnGoalFor"])),
                sog_against=int(float(r["shotsOnGoalAgainst"])),
                # VERIFY: MoneyPuck "penaltiesFor" = penalties taken by playerTeam.
                penalties_for=int(float(r["penaltiesFor"])),
                penalties_against=int(float(r["penaltiesAgainst"])),
                available_at=game_row_available_at(r["gameDate"]),
            )
        )
    return sorted(out, key=lambda x: (x.game_date, x.game_id, x.team, x.situation))


def parse_goalie_games(payload: bytes) -> list[GoalieGameStats]:
    """Situation 'all' rows. ``started`` is INFERRED as max ice time per team-game;
    prefer NHL boxscore ``starter`` when both exist (see ``merge_goalie_sources``)."""

    rows = [r for r in _reader(payload, GOALIE_REQUIRED) if r["situation"] == "all"]
    top: dict[tuple[str, str], float] = defaultdict(float)
    for r in rows:
        key = (r["gameId"], r["playerTeam"])
        top[key] = max(top[key], float(r["icetime"]))
    out = [
        GoalieGameStats(
            game_id=r["gameId"],
            team=r["playerTeam"],
            goalie_id=r["playerId"],
            started=float(r["icetime"]) >= top[(r["gameId"], r["playerTeam"])],
            toi_sec=float(r["icetime"]),
            shots_against=int(float(r["ongoal"])),
            goals_against=int(float(r["goals"])),
            xg_against=float(r["xGoals"]),
            available_at=game_row_available_at(r["gameDate"]),
        )
        for r in rows
    ]
    return sorted(out, key=lambda x: (x.game_id, x.team, x.goalie_id))


def merge_goalie_sources(moneypuck: list[GoalieGameStats], boxscore: list[GoalieGameStats]) -> list[GoalieGameStats]:
    """Boxscore owns ``started``; MoneyPuck owns ``xg_against``. Later availability wins."""

    box = {(g.game_id, g.goalie_id): g for g in boxscore}
    out = []
    for g in moneypuck:
        b = box.get((g.game_id, g.goalie_id))
        if b is None:
            out.append(g)
            continue
        out.append(
            GoalieGameStats(
                game_id=g.game_id, team=g.team, goalie_id=g.goalie_id, started=b.started,
                toi_sec=g.toi_sec, shots_against=g.shots_against, goals_against=g.goals_against,
                xg_against=g.xg_against, available_at=max(g.available_at, b.available_at),
            )
        )
    return out


def parse_skater_seasons(payload: bytes, league_xgf60: float, league_xga60: float) -> list[PlayerSeason]:
    """5v5 on-ice rates relative to league average. Visible only after season end."""

    out = []
    for r in _reader(payload, SKATER_REQUIRED):
        if r["situation"] != "5on5":
            continue
        minutes = float(r["icetime"]) / 60.0
        if minutes <= 0:
            continue
        pos = "D" if r["position"].upper().startswith("D") else "F"
        season = int(r["season"])
        out.append(
            PlayerSeason(
                player_id=r["playerId"],
                season=season,
                team=r["team"],
                position=pos,
                toi_5v5_min=minutes,
                on_ice_xgf60_rel=float(r["onIce_xGoalsFor"]) / minutes * 60 - league_xgf60,
                on_ice_xga60_rel=float(r["onIce_xGoalsAgainst"]) / minutes * 60 - league_xga60,
                available_at=season_summary_available_at(season),
            )
        )
    return sorted(out, key=lambda p: (p.season, p.team, p.player_id))
