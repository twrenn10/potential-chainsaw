"""MoneyPuck CSV ingestion (team game-by-game, goalie game-by-game, skater seasons).

Leakage rules enforced here (see docs/leakage_audit.md):
* Season-summary files are END-OF-SEASON aggregates. They are only visible after
  the season closes (``available_at`` = July 1 after the season), so they can feed
  *next* season's priors but never same-season predictions.
* Game-by-game rows become visible the next day at 12:00 UTC (MoneyPuck refreshes
  overnight). Same-night data is never assumed available.
* MoneyPuck's xG model is trained on multi-season data that may postdate a backtest
  date, and files do not state the model version. Every xG-bearing row therefore gets
  ``provenance.versioned_derived``: causal only if captured contemporaneously or
  covered by an attestation in ``nhl/config/moneypuck_vintages.json``; otherwise
  NON-CAUSAL (invisible to strict walk-forward views). The causal alternative is our
  own walk-forward xG (``nhl.features.xg``).

Column semantics flagged ``VERIFY`` below must be checked against a live file before
the first real backtest.
"""

from __future__ import annotations

import csv
import io
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from nhl.config import load
from nhl.contracts import GoalieGameStats, PlayerSeason, TeamGameStats
from nhl.contracts.ids import canonical_game_id, canonical_player_id, canonical_team

from . import provenance as prov
from .snapshots import RawSnapshotStore, SnapshotEntry
from .validation import FieldError, ParseResult, strict_float, strict_int

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


SITUATIONS = {"5on5", "5on4", "4on5", "all", "other"}
SUMMARY_CAPTURE_GRACE = timedelta(days=92)  # July 1 -> Oct 1: captured before the next season


def game_row_available_at(game_date: str) -> datetime:
    """``gameDate`` is YYYYMMDD (local). Visible next day 12:00 UTC."""

    try:
        d = datetime.strptime(game_date, "%Y%m%d").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError) as exc:
        raise FieldError(f"gameDate invalid: {game_date!r}") from exc
    return d + timedelta(days=1, hours=12)


def _check_game(gid_raw: str, season_raw: str | None, game_date: str) -> str:
    gid = canonical_game_id(gid_raw)
    if season_raw is not None and str(season_raw).strip() and strict_int(season_raw, "season", 1900) != int(gid[:4]):
        raise FieldError(f"season {season_raw} inconsistent with gameId {gid}")
    try:
        d = datetime.strptime(game_date, "%Y%m%d") if game_date.isdigit() and len(game_date) == 8 else None
    except ValueError:
        d = None
    if d is None:
        raise FieldError(f"gameDate invalid: {game_date!r}")
    # Regular season/playoff games of season S are played between Sep S and Jul S+1.
    if not (datetime(int(gid[:4]), 9, 1) <= d <= datetime(int(gid[:4]) + 1, 7, 31)):
        raise FieldError(f"gameDate {game_date} outside season of {gid}")
    return gid


def _vintage_provenance(entry: SnapshotEntry | None, bound: datetime, season: int, attestations: list[dict] | None,
                        grace: timedelta = timedelta(hours=36)) -> prov.Provenance | None:
    if entry is None:
        return None  # unattested: governance blocks non-synthetic use
    atts = attestations if attestations is not None else load("moneypuck_vintages")["attestations"]
    return prov.versioned_derived(SOURCE, entry.snapshot_id, entry.fetched_at, bound, season, atts, grace)


def _dupes(out: ParseResult, keyf, what: str) -> None:
    counts = Counter(keyf(r) for r in out.records)
    bad = {k for k, n in counts.items() if n > 1}
    if bad:
        for k in sorted(bad):
            out.reject("moneypuck", str(k), f"duplicate {what} row: ambiguous")
        out.records = [r for r in out.records if keyf(r) not in bad]


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


def parse_team_games_checked(
    payload: bytes,
    entry: SnapshotEntry | None = None,
    attestations: list[dict] | None = None,
    situations: tuple[str, ...] = ("5on5", "5on4", "4on5", "all"),
) -> ParseResult[TeamGameStats]:
    out: ParseResult[TeamGameStats] = ParseResult()
    for i, r in enumerate(_reader(payload, TEAM_REQUIRED)):
        key = f"{r.get('gameId')}:{r.get('playerTeam')}:{r.get('situation')}"
        try:
            if r["situation"] not in SITUATIONS:
                raise FieldError(f"unknown situation {r['situation']!r}")
            if r["situation"] not in situations:
                continue
            gid = _check_game(r["gameId"], r.get("season"), r["gameDate"])
            team, opp = canonical_team(r["playerTeam"]), canonical_team(r["opposingTeam"])
            if team == opp:
                raise FieldError("playerTeam == opposingTeam")
            ha = r["home_or_away"].strip().upper()
            if ha not in {"HOME", "AWAY"}:
                raise FieldError(f"home_or_away {r['home_or_away']!r}")
            toi = strict_float(r["iceTime"], "iceTime", 0.0, 200 * 60)
            if toi == 0 and r["situation"] in {"5on5", "all"}:
                raise FieldError("zero ice time for 5on5/all")
            bound = game_row_available_at(r["gameDate"])
            p = _vintage_provenance(entry, bound, int(gid[:4]), attestations)
            out.records.append(
                TeamGameStats(
                    game_id=gid, team=team, opponent=opp, is_home=ha == "HOME", game_date=r["gameDate"],
                    situation=r["situation"], toi_sec=toi,
                    xg_for=strict_float(r["xGoalsFor"], "xGoalsFor", 0.0, 30.0),
                    xg_against=strict_float(r["xGoalsAgainst"], "xGoalsAgainst", 0.0, 30.0),
                    goals_for=strict_int(r["goalsFor"], "goalsFor", 0, 20),
                    goals_against=strict_int(r["goalsAgainst"], "goalsAgainst", 0, 20),
                    sog_for=strict_int(r["shotsOnGoalFor"], "shotsOnGoalFor", 0, 120),
                    sog_against=strict_int(r["shotsOnGoalAgainst"], "shotsOnGoalAgainst", 0, 120),
                    # VERIFY against a live file: "penaltiesFor" = penalties taken by playerTeam.
                    penalties_for=strict_int(r["penaltiesFor"], "penaltiesFor", 0, 40),
                    penalties_against=strict_int(r["penaltiesAgainst"], "penaltiesAgainst", 0, 40),
                    available_at=p.available_at if p else bound, provenance=p,
                )
            )
        except (FieldError, ValueError, KeyError) as exc:
            out.reject("moneypuck_teams", key or f"row{i}", str(exc))
    _dupes(out, lambda x: (x.game_id, x.team, x.situation), "team-game-situation")
    out.records.sort(key=lambda x: (x.game_date, x.game_id, x.team, x.situation))
    return out


def parse_team_games(payload: bytes, situations: tuple[str, ...] = ("5on5", "5on4", "4on5", "all"),
                     entry: SnapshotEntry | None = None, attestations: list[dict] | None = None) -> list[TeamGameStats]:
    return parse_team_games_checked(payload, entry, attestations, situations).require_clean()


def parse_goalie_games_checked(
    payload: bytes, entry: SnapshotEntry | None = None, attestations: list[dict] | None = None
) -> ParseResult[GoalieGameStats]:
    """Situation 'all' rows. ``started`` is INFERRED as max ice time per team-game;
    prefer NHL boxscore ``starter`` when both exist (see ``merge_goalie_sources``).
    A tie for max ice time is ambiguous and rejects that team-game."""

    out: ParseResult[GoalieGameStats] = ParseResult()
    rows = []
    for r in _reader(payload, GOALIE_REQUIRED):
        if r["situation"] != "all":
            continue
        key = f"{r.get('gameId')}:{r.get('playerTeam')}:{r.get('playerId')}"
        try:
            gid = _check_game(r["gameId"], r.get("season"), r["gameDate"])
            toi = strict_float(r["icetime"], "icetime", 0.0, 200 * 60)
            ga = strict_int(r["goals"], "goals", 0, 20)
            shots = strict_int(r["ongoal"], "ongoal", 0, 120)
            if ga > shots:
                raise FieldError("goals > shots on goal")
            rows.append((key, gid, canonical_team(r["playerTeam"]), canonical_player_id(r["playerId"]), toi, ga, shots,
                         strict_float(r["xGoals"], "xGoals", 0.0, 30.0), r["gameDate"]))
        except (FieldError, ValueError, KeyError) as exc:
            out.reject("moneypuck_goalies", key, str(exc))
    by_tg: dict[tuple[str, str], list] = defaultdict(list)
    for row in rows:
        by_tg[(row[1], row[2])].append(row)
    for (gid, team), grp in sorted(by_tg.items()):
        top = max(r[4] for r in grp)
        if sum(1 for r in grp if r[4] == top) > 1:
            out.reject("moneypuck_goalies", f"{gid}:{team}", "tied ice time: starter ambiguous")
            continue
        for key, _, _, pid, toi, ga, shots, xga, gdate in grp:
            bound = game_row_available_at(gdate)
            p = _vintage_provenance(entry, bound, int(gid[:4]), attestations)
            out.records.append(GoalieGameStats(
                game_id=gid, team=team, goalie_id=pid, started=toi == top, toi_sec=toi, shots_against=shots,
                goals_against=ga, xg_against=xga, available_at=p.available_at if p else bound, provenance=p))
    _dupes(out, lambda x: (x.game_id, x.goalie_id), "goalie-game")
    out.records.sort(key=lambda x: (x.game_id, x.team, x.goalie_id))
    return out


def parse_goalie_games(payload: bytes, entry: SnapshotEntry | None = None, attestations: list[dict] | None = None) -> list[GoalieGameStats]:
    return parse_goalie_games_checked(payload, entry, attestations).require_clean()


def merge_goalie_sources(moneypuck: list[GoalieGameStats], boxscore: list[GoalieGameStats]) -> list[GoalieGameStats]:
    """Boxscore owns ``started`` (an event fact); MoneyPuck owns ``xg_against``. The merged
    record keeps the MoneyPuck provenance (its xG decides causality) and the later
    availability of the two."""

    box = {(g.game_id, g.goalie_id): g for g in boxscore}
    out = []
    for g in moneypuck:
        b = box.get((g.game_id, g.goalie_id))
        if b is None:
            out.append(g)
            continue
        out.append(replace(g, started=b.started, available_at=max(g.available_at, b.available_at)))
    return out


def parse_skater_seasons_checked(
    payload: bytes, league_xgf60: float, league_xga60: float,
    entry: SnapshotEntry | None = None, attestations: list[dict] | None = None,
) -> ParseResult[PlayerSeason]:
    """5v5 on-ice rates relative to league average. Visible only after season end."""

    out: ParseResult[PlayerSeason] = ParseResult()
    for r in _reader(payload, SKATER_REQUIRED):
        if r["situation"] != "5on5":
            continue
        key = f"{r.get('playerId')}:{r.get('season')}"
        try:
            minutes = strict_float(r["icetime"], "icetime", 0.0) / 60.0
            if minutes <= 0:
                raise FieldError("zero 5v5 ice time")
            pos = r["position"].strip().upper()
            if pos not in {"C", "L", "R", "D", "F"}:
                raise FieldError(f"position {r['position']!r}")
            season = strict_int(r["season"], "season", 1900, 2100)
            bound = season_summary_available_at(season)
            p = _vintage_provenance(entry, bound, season + 1, attestations, SUMMARY_CAPTURE_GRACE)
            out.records.append(
                PlayerSeason(
                    player_id=canonical_player_id(r["playerId"]), season=season, team=canonical_team(r["team"]),
                    position="D" if pos == "D" else "F", toi_5v5_min=minutes,
                    on_ice_xgf60_rel=strict_float(r["onIce_xGoalsFor"], "onIce_xGoalsFor") / minutes * 60 - league_xgf60,
                    on_ice_xga60_rel=strict_float(r["onIce_xGoalsAgainst"], "onIce_xGoalsAgainst") / minutes * 60 - league_xga60,
                    available_at=p.available_at if p else bound, provenance=p,
                )
            )
        except (FieldError, ValueError, KeyError) as exc:
            out.reject("moneypuck_skaters", key, str(exc))
    # A traded player has one row per team; the (player, season, team) triple must be unique.
    _dupes(out, lambda x: (x.player_id, x.season, x.team), "player-season-team")
    out.records.sort(key=lambda x: (x.season, x.team, x.player_id))
    return out


def parse_skater_seasons(payload: bytes, league_xgf60: float, league_xga60: float,
                         entry: SnapshotEntry | None = None, attestations: list[dict] | None = None) -> list[PlayerSeason]:
    return parse_skater_seasons_checked(payload, league_xgf60, league_xga60, entry, attestations).require_clean()
