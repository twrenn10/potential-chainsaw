"""NHL public API ingestion: schedule, play-by-play, boxscore (goalie starters), shifts.

Fetching always goes through ``RawSnapshotStore`` first; parsers only ever see
stored bytes. The transport is injectable so tests and offline replays never hit
the network.

Endpoints (public, undocumented, may change -- parsers are defensive):
* https://api-web.nhle.com/v1/schedule/{YYYY-MM-DD}
* https://api-web.nhle.com/v1/gamecenter/{game_id}/play-by-play
* https://api-web.nhle.com/v1/gamecenter/{game_id}/boxscore
* https://api.nhle.com/stats/rest/en/shiftcharts?cayenneExp=gameId={game_id}

Availability convention (conservative, see docs/leakage_audit.md):
* schedule entries: ``fetched_at`` of the snapshot they were parsed from;
* results / boxscore / pbp-derived stats: ``start_time + RESULT_LAG``. Games rarely
  exceed ~3h; using a later bound can only hide data, never leak it.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Callable

from nhl.contracts import Game, GameResult, GoalieGameStats, canonical_team
from nhl.contracts.ids import canonical_game_id, game_type_of
from nhl.timeutil import parse_ts, utcnow

from .snapshots import RawSnapshotStore, SnapshotEntry

WEB_BASE = "https://api-web.nhle.com/v1"
STATS_BASE = "https://api.nhle.com/stats/rest/en"
RESULT_LAG = timedelta(hours=4)
SOURCE = "nhl_api"

Transport = Callable[[str], bytes]


def http_transport(url: str, timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "nhl-desk/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed hosts
        return resp.read()


class NHLApiClient:
    def __init__(self, store: RawSnapshotStore, transport: Transport = http_transport) -> None:
        self.store = store
        self.transport = transport

    def _fetch(self, key: str, url: str, fetched_at: datetime | None = None) -> SnapshotEntry:
        payload = self.transport(url)
        json.loads(payload)  # reject non-JSON before it lands in the store
        return self.store.put(SOURCE, key, payload, fetched_at or utcnow(), {"url": url})

    def fetch_schedule(self, date: str, fetched_at: datetime | None = None) -> SnapshotEntry:
        return self._fetch(f"schedule/{date}", f"{WEB_BASE}/schedule/{date}", fetched_at)

    def fetch_pbp(self, game_id: str, fetched_at: datetime | None = None) -> SnapshotEntry:
        gid = canonical_game_id(game_id)
        return self._fetch(f"pbp/{gid}", f"{WEB_BASE}/gamecenter/{gid}/play-by-play", fetched_at)

    def fetch_boxscore(self, game_id: str, fetched_at: datetime | None = None) -> SnapshotEntry:
        gid = canonical_game_id(game_id)
        return self._fetch(f"boxscore/{gid}", f"{WEB_BASE}/gamecenter/{gid}/boxscore", fetched_at)

    def fetch_shifts(self, game_id: str, fetched_at: datetime | None = None) -> SnapshotEntry:
        gid = canonical_game_id(game_id)
        url = f"{STATS_BASE}/shiftcharts?cayenneExp=gameId={gid}"
        return self._fetch(f"shifts/{gid}", url, fetched_at)


# --------------------------------------------------------------------------- parsers


def _mmss(value: str | None) -> int:
    if not value:
        return 0
    minutes, seconds = value.split(":")
    return int(minutes) * 60 + int(seconds)


def parse_situation_code(code: str | None) -> dict[str, int] | None:
    """``situationCode`` digits: away goalie in net, away skaters, home skaters, home goalie in net."""

    if not code or len(code) != 4 or not code.isdigit():
        return None
    return {
        "away_goalie_in": int(code[0]),
        "away_skaters": int(code[1]),
        "home_skaters": int(code[2]),
        "home_goalie_in": int(code[3]),
    }


def strength_label(sit: dict[str, int] | None, for_home: bool) -> str:
    """Strength from one team's perspective: 5v5, PP, SH, 4v4, 3v3, EN_FOR, EN_AGAINST, ..."""

    if sit is None:
        return "UNKNOWN"
    own = sit["home_skaters"] if for_home else sit["away_skaters"]
    opp = sit["away_skaters"] if for_home else sit["home_skaters"]
    own_goalie = sit["home_goalie_in"] if for_home else sit["away_goalie_in"]
    opp_goalie = sit["away_goalie_in"] if for_home else sit["home_goalie_in"]
    if not opp_goalie:
        return "EN_FOR"  # shooting at an empty net
    if not own_goalie:
        return "EXTRA_ATTACKER"
    if own == opp:
        return f"{own}v{opp}"
    return "PP" if own > opp else "SH"


def parse_schedule(payload: dict[str, Any], entry: SnapshotEntry) -> list[Game]:
    games: list[Game] = []
    days = payload.get("gameWeek") or [{"games": payload.get("games", [])}]
    for day in days:
        for g in day.get("games", []):
            gid = str(g["id"])
            games.append(
                Game(
                    game_id=gid,
                    season=int(str(g.get("season", gid[:4]))[:4]),
                    game_type=game_type_of(gid),
                    start_time=parse_ts(g["startTimeUTC"]),
                    home=canonical_team(g["homeTeam"]["abbrev"]),
                    away=canonical_team(g["awayTeam"]["abbrev"]),
                    venue=(g.get("venue") or {}).get("default", ""),
                    available_at=entry.fetched_at,
                )
            )
    return sorted(games, key=lambda x: (x.start_time, x.game_id))


def parse_pbp_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    gid = str(payload["id"])
    home_id = payload["homeTeam"]["id"]
    teams = {
        payload["homeTeam"]["id"]: canonical_team(payload["homeTeam"]["abbrev"]),
        payload["awayTeam"]["id"]: canonical_team(payload["awayTeam"]["abbrev"]),
    }
    out: list[dict[str, Any]] = []
    for play in payload.get("plays", []):
        pd = play.get("periodDescriptor", {})
        period = int(pd.get("number", 0))
        details = play.get("details", {}) or {}
        owner = details.get("eventOwnerTeamId")
        sit = parse_situation_code(play.get("situationCode"))
        is_home = owner == home_id if owner is not None else None
        out.append(
            {
                "game_id": gid,
                "event_id": play.get("eventId"),
                "period": period,
                "period_type": pd.get("periodType", "REG"),
                "game_seconds": (period - 1) * 1200 + _mmss(play.get("timeInPeriod")),
                "event_type": play.get("typeDescKey", ""),
                "team": teams.get(owner, ""),
                "is_home": is_home,
                "strength": strength_label(sit, bool(is_home)) if is_home is not None else "UNKNOWN",
                "situation_code": play.get("situationCode", ""),
                "shooter_id": details.get("shootingPlayerId") or details.get("scoringPlayerId"),
                "goalie_id": details.get("goalieInNetId"),
                "x": details.get("xCoord"),
                "y": details.get("yCoord"),
                "shot_type": details.get("shotType", ""),
                "penalty_minutes": details.get("duration"),
            }
        )
    return out


def parse_result(payload: dict[str, Any], start_time: datetime) -> GameResult | None:
    """Final result from play-by-play. Returns None unless the game is final."""

    if payload.get("gameState") not in {"FINAL", "OFF"}:
        return None
    events = parse_pbp_events(payload)
    goals = [e for e in events if e["event_type"] == "goal"]

    def count(pred: Callable[[dict[str, Any]], bool], home: bool) -> int:
        return sum(1 for e in goals if pred(e) and e["is_home"] is home)

    reg = lambda e: e["period_type"] == "REG"  # noqa: E731
    ot = lambda e: e["period_type"] == "OT"  # noqa: E731
    so = lambda e: e["period_type"] == "SO"  # noqa: E731
    p1 = lambda e: e["period"] == 1  # noqa: E731
    reg_h, reg_a = count(reg, True), count(reg, False)
    ot_h, ot_a = count(ot, True), count(ot, False)
    so_h, so_a = count(so, True), count(so, False)
    if reg_h != reg_a:
        end_type, so_winner = "REG", None
    elif ot_h != ot_a:
        end_type, so_winner = "OT", None
    else:
        end_type = "SO"
        if so_h == so_a:
            raise ValueError(f"{payload['id']}: tied after shootout events; incomplete payload")
        so_winner = "HOME" if so_h > so_a else "AWAY"
    return GameResult(
        game_id=str(payload["id"]),
        home_goals=reg_h + ot_h,
        away_goals=reg_a + ot_a,
        reg_home_goals=reg_h,
        reg_away_goals=reg_a,
        p1_home_goals=count(p1, True),
        p1_away_goals=count(p1, False),
        end_type=end_type,
        shootout_winner=so_winner,
        available_at=parse_ts(start_time) + RESULT_LAG,
    )


def parse_boxscore_goalies(payload: dict[str, Any], start_time: datetime) -> list[GoalieGameStats]:
    gid = str(payload["id"])
    by_game = payload.get("playerByGameStats", {})
    out: list[GoalieGameStats] = []
    for side in ("homeTeam", "awayTeam"):
        team = canonical_team(payload[side]["abbrev"])
        for g in by_game.get(side, {}).get("goalies", []):
            shots = g.get("shotsAgainst")
            if shots is None and g.get("saveShotsAgainst"):
                shots = int(str(g["saveShotsAgainst"]).split("/")[1])
            out.append(
                GoalieGameStats(
                    game_id=gid,
                    team=team,
                    goalie_id=str(g["playerId"]),
                    started=bool(g.get("starter", False)),
                    toi_sec=float(_mmss(g.get("toi"))),
                    shots_against=int(shots or 0),
                    goals_against=int(g.get("goalsAgainst", 0)),
                    xg_against=float("nan"),  # joined from MoneyPuck; NHL API has no xG
                    available_at=parse_ts(start_time) + RESULT_LAG,
                )
            )
    return out


def parse_shifts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for s in payload.get("data", []):
        if s.get("typeCode") not in (None, 517):  # 517 = shift; 505 = goal marker rows
            continue
        period = int(s["period"])
        rows.append(
            {
                "game_id": str(s["gameId"]),
                "player_id": str(s["playerId"]),
                "team": canonical_team(s["teamAbbrev"]),
                "period": period,
                "start_sec": (period - 1) * 1200 + _mmss(s.get("startTime")),
                "end_sec": (period - 1) * 1200 + _mmss(s.get("endTime")),
            }
        )
    return sorted(rows, key=lambda r: (r["start_sec"], r["team"], r["player_id"]))
