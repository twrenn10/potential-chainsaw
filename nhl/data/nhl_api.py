"""NHL public API ingestion: schedule, play-by-play, boxscore (goalie starters), shifts.

Fetching always goes through ``RawSnapshotStore`` first; parsers only ever see
stored bytes. The transport is injectable so tests and offline replays never hit
the network.

Endpoints (public, undocumented, may change -- parsers are defensive):
* https://api-web.nhle.com/v1/schedule/{YYYY-MM-DD}
* https://api-web.nhle.com/v1/gamecenter/{game_id}/play-by-play
* https://api-web.nhle.com/v1/gamecenter/{game_id}/boxscore
* https://api.nhle.com/stats/rest/en/shiftcharts?cayenneExp=gameId={game_id}

* https://api-web.nhle.com/v1/roster/{TEAM}/{SSSSSSSS}

Every parser validates each record, rejects bad ones with a reason (never coerces),
and attaches ``Provenance`` built by ``nhl.data.provenance`` rules:
* schedule: LIVE capture = fetched_at; BACKFILL only via documented override;
* results / pbp / boxscore: immutable event facts, available at
  ``min(fetched_at, puck drop + RESULT_LAG)``;
* rosters: published reports (live capture only, unless a source time exists).
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Callable

from nhl.contracts import Game, GameResult, GoalieGameStats, canonical_team
from nhl.contracts.ids import canonical_game_id, canonical_player_id, game_type_of
from nhl.timeutil import utcnow

from . import provenance as prov
from .snapshots import RawSnapshotStore, SnapshotEntry
from .validation import FieldError, ParseResult, mmss, req, strict_int, strict_utc

WEB_BASE = "https://api-web.nhle.com/v1"
STATS_BASE = "https://api.nhle.com/stats/rest/en"
RESULT_LAG = timedelta(hours=4)
SOURCE = "nhl_api"
SOURCE_VERSION = "api-web/v1"

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

    def fetch_roster(self, team: str, season: int, fetched_at: datetime | None = None) -> SnapshotEntry:
        t = canonical_team(team)
        return self._fetch(f"roster/{t}/{season}", f"{WEB_BASE}/roster/{t}/{season}{season + 1}", fetched_at)

    def fetch_shifts(self, game_id: str, fetched_at: datetime | None = None) -> SnapshotEntry:
        gid = canonical_game_id(game_id)
        url = f"{STATS_BASE}/shiftcharts?cayenneExp=gameId={gid}"
        return self._fetch(f"shifts/{gid}", url, fetched_at)


# --------------------------------------------------------------------------- parsers

SCHEDULE_STATE = {"OK": None, "PPD": "PPD", "SUSP": "SUSP", "CNCL": "CNCL"}  # TBD -> rejected (no start time)
GAME_STATE = {"FUT": "SCHEDULED", "PRE": "SCHEDULED", "LIVE": "LIVE", "CRIT": "LIVE", "FINAL": "FINAL", "OFF": "FINAL"}
SHOT_EVENTS = {"shot-on-goal", "missed-shot", "blocked-shot", "goal"}
VALIDATED_EVENTS = SHOT_EVENTS | {"penalty"}


def _mmss(value: str | None) -> int:
    """Lenient clock parse for already-validated fields."""

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


def validate_situation_code(code: Any, period_type: str) -> dict[str, int]:
    sit = parse_situation_code(code if isinstance(code, str) else None)
    if sit is None:
        raise FieldError(f"situationCode malformed: {code!r}")
    if sit["away_goalie_in"] > 1 or sit["home_goalie_in"] > 1:
        raise FieldError(f"situationCode goalie digit not 0/1: {code}")
    if period_type == "SO":
        return sit  # shootout codes (e.g. 1010 / 0101) do not describe skater strength
    for side in ("away", "home"):
        sk = sit[f"{side}_skaters"]
        # 3..5 skaters with goalie in; up to 6 with goalie pulled.
        if not (3 <= sk <= (5 if sit[f"{side}_goalie_in"] else 6)):
            raise FieldError(f"situationCode impossible skaters for {side}: {code}")
    return sit


def strength_label(sit: dict[str, int] | None, for_home: bool) -> str:
    """Strength from one team's perspective: 5v5, PP, SH, 4v4, 3v3, EN_FOR, EXTRA_ATTACKER."""

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


def _period_rules(game_type: str, number: int, ptype: str) -> int:
    """Validate the period descriptor; return max clock seconds for the period."""

    if ptype == "REG" and 1 <= number <= 3:
        return 1200
    if ptype == "OT" and number >= 4:
        if game_type == "PLAYOFF":
            return 1200
        if number == 4:
            return 300
    if ptype == "SO" and number == 5 and game_type != "PLAYOFF":
        return 0
    raise FieldError(f"impossible period {number}/{ptype} for {game_type}")


def _season_token_ok(token: Any, gid: str) -> bool:
    t = str(token)
    return len(t) == 8 and t.isdigit() and t[:4] == gid[:4] and int(t[4:]) == int(t[:4]) + 1


def parse_schedule_checked(
    payload: dict[str, Any],
    entry: SnapshotEntry,
    mode: prov.CaptureMode = prov.CaptureMode.LIVE,
    overrides: dict | None = None,
) -> ParseResult[Game]:
    out: ParseResult[Game] = ParseResult()
    days = payload.get("gameWeek")
    if days is None:
        days = [{"games": payload.get("games", [])}]
    for day in days:
        for g in day.get("games", []) or []:
            key = str(g.get("id", "?"))
            try:
                gid = canonical_game_id(req(g, "id"))
                if not _season_token_ok(req(g, "season"), gid):
                    raise FieldError(f"season {g.get('season')!r} inconsistent with id")
                if strict_int(req(g, "gameType"), "gameType", 1, 3) != int(gid[4:6]):
                    raise FieldError("gameType inconsistent with id")
                sched_state = req(g, "gameScheduleState") if "gameScheduleState" in g else "OK"
                if sched_state not in SCHEDULE_STATE:
                    raise FieldError(f"unsupported gameScheduleState {sched_state!r}")
                game_state = req(g, "gameState") if "gameState" in g else "FUT"
                if game_state not in GAME_STATE:
                    raise FieldError(f"unknown gameState {game_state!r}")
                status = SCHEDULE_STATE[sched_state] or GAME_STATE[game_state]
                start = strict_utc(req(g, "startTimeUTC"), "startTimeUTC")
                home = canonical_team(req(g, "homeTeam", "abbrev"))
                away = canonical_team(req(g, "awayTeam", "abbrev"))
                p = prov.schedule(SOURCE, entry.snapshot_id, entry.fetched_at, int(gid[:4]), start, mode, overrides)
                out.records.append(
                    Game(
                        game_id=gid, season=int(gid[:4]), game_type=game_type_of(gid), start_time=start,
                        home=home, away=away, venue=(g.get("venue") or {}).get("default", ""),
                        available_at=p.available_at, status=status, provenance=p,
                    )
                )
            except (FieldError, ValueError, KeyError, TypeError) as exc:
                out.reject("schedule", key, str(exc))
    ids = [g.game_id for g in out.records]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:  # same game twice in one snapshot is ambiguous: drop all copies
        for d in sorted(dupes):
            out.reject("schedule", d, "duplicate game id within snapshot")
        out.records = [g for g in out.records if g.game_id not in dupes]
    out.records.sort(key=lambda x: (x.start_time, x.game_id))
    return out


def parse_schedule(
    payload: dict[str, Any],
    entry: SnapshotEntry,
    mode: prov.CaptureMode = prov.CaptureMode.LIVE,
    overrides: dict | None = None,
) -> list[Game]:
    return parse_schedule_checked(payload, entry, mode, overrides).require_clean()


def _game_header(payload: dict[str, Any]) -> tuple[str, str, int, dict[int, str], str]:
    gid = canonical_game_id(req(payload, "id"))
    home_id = strict_int(req(payload, "homeTeam", "id"), "homeTeam.id", 1)
    away_id = strict_int(req(payload, "awayTeam", "id"), "awayTeam.id", 1)
    if home_id == away_id:
        raise FieldError("home and away team ids equal")
    teams = {home_id: canonical_team(req(payload, "homeTeam", "abbrev")),
             away_id: canonical_team(req(payload, "awayTeam", "abbrev"))}
    return gid, game_type_of(gid), home_id, teams, str(payload.get("gameState", ""))


def parse_pbp_checked(payload: dict[str, Any]) -> ParseResult[dict[str, Any]]:
    out: ParseResult[dict[str, Any]] = ParseResult()
    try:
        gid, gtype, home_id, teams, _ = _game_header(payload)
    except (FieldError, ValueError) as exc:
        out.reject("pbp", str(payload.get("id", "?")), f"header: {exc}")
        return out
    seen_ids: set[Any] = set()
    for play in payload.get("plays", []) or []:
        key = f"{gid}#{play.get('eventId', '?')}"
        etype = play.get("typeDescKey", "")
        try:
            ev_id = req(play, "eventId")
            if ev_id in seen_ids:
                raise FieldError("duplicate eventId")
            seen_ids.add(ev_id)
            number = strict_int(req(play, "periodDescriptor", "number"), "period", 1, 20)
            ptype = req(play, "periodDescriptor", "periodType")
            max_sec = _period_rules(gtype, number, ptype)
            clock = mmss(req(play, "timeInPeriod"), "timeInPeriod", max_sec) if ptype != "SO" else 0
            details = play.get("details") or {}
            owner = details.get("eventOwnerTeamId")
            sit = None
            if etype in VALIDATED_EVENTS:
                if owner not in teams:
                    raise FieldError(f"{etype}: eventOwnerTeamId {owner!r} not in game")
                sit = validate_situation_code(req(play, "situationCode"), ptype)
            elif owner is not None and owner not in teams:
                raise FieldError(f"eventOwnerTeamId {owner!r} not in game")
            if etype == "penalty":
                strict_int(req(details, "duration"), "penalty duration", 0, 10)
            is_home = (owner == home_id) if owner in teams else None
            base_seconds = (number - 1) * 1200 if number <= 4 or gtype == "PLAYOFF" else 3900
            out.records.append(
                {
                    "game_id": gid,
                    "event_id": ev_id,
                    "period": number,
                    "period_type": ptype,
                    "game_seconds": base_seconds + clock,
                    "event_type": etype,
                    "team": teams.get(owner, ""),
                    "is_home": is_home,
                    "strength": strength_label(sit, bool(is_home)) if (sit and is_home is not None and ptype != "SO") else "UNKNOWN",
                    "situation_code": play.get("situationCode", ""),
                    "shooter_id": details.get("shootingPlayerId") or details.get("scoringPlayerId"),
                    "goalie_id": details.get("goalieInNetId"),
                    "x": details.get("xCoord"),
                    "y": details.get("yCoord"),
                    "shot_type": details.get("shotType", ""),
                    "penalty_minutes": details.get("duration"),
                }
            )
        except (FieldError, ValueError, TypeError) as exc:
            out.reject("pbp", key, str(exc))
    return out


def parse_pbp_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return parse_pbp_checked(payload).require_clean()


def parse_result_checked(
    payload: dict[str, Any], start_time: datetime, entry: SnapshotEntry | None = None
) -> tuple[GameResult | None, ParseResult[dict[str, Any]]]:
    """Final result from play-by-play, cross-checked against the payload's own score.

    Returns ``(None, ...)`` unless the game is final AND every goal event parsed AND
    the reconstructed score matches the reported score. Nothing is guessed.
    """

    events = parse_pbp_checked(payload)
    gid = str(payload.get("id", "?"))
    if payload.get("gameState") not in {"FINAL", "OFF"}:
        return None, events
    if any(r.reason for r in events.rejections):
        events.reject("result", gid, "play-by-play has rejected events; result withheld")
        return None, events
    goals = [e for e in events.records if e["event_type"] == "goal"]

    def count(pred: Callable[[dict[str, Any]], bool], home: bool) -> int:
        return sum(1 for e in goals if pred(e) and e["is_home"] is home)

    reg_h, reg_a = count(lambda e: e["period_type"] == "REG", True), count(lambda e: e["period_type"] == "REG", False)
    ot_h, ot_a = count(lambda e: e["period_type"] == "OT", True), count(lambda e: e["period_type"] == "OT", False)
    so_h, so_a = count(lambda e: e["period_type"] == "SO", True), count(lambda e: e["period_type"] == "SO", False)
    if ot_h + ot_a > 1:
        events.reject("result", gid, "more than one overtime goal in a sudden-death game")
        return None, events
    if reg_h != reg_a:
        end_type, so_winner = "REG", None
    elif ot_h != ot_a:
        end_type, so_winner = "OT", None
    else:
        end_type = "SO"
        if so_h == so_a:
            events.reject("result", gid, "tied after regulation, OT and shootout events: incomplete payload")
            return None, events
        so_winner = "HOME" if so_h > so_a else "AWAY"
    fin_h, fin_a = reg_h + ot_h + (1 if so_winner == "HOME" else 0), reg_a + ot_a + (1 if so_winner == "AWAY" else 0)
    for side, val in (("homeTeam", fin_h), ("awayTeam", fin_a)):
        reported = (payload.get(side) or {}).get("score")
        if reported is None:
            events.reject("result", gid, f"{side}.score missing; cannot cross-check")
            return None, events
        if strict_int(reported, f"{side}.score") != val:
            events.reject("result", gid, f"{side}.score {reported} != reconstructed {val}")
            return None, events
    for side, is_home in (("homeTeam", True), ("awayTeam", False)):
        sog = (payload.get(side) or {}).get("sog")
        if sog is not None:
            n = sum(1 for e in events.records if e["event_type"] in {"shot-on-goal", "goal"} and e["is_home"] is is_home and e["period_type"] != "SO")
            if strict_int(sog, f"{side}.sog") != n:
                events.warn("result", gid, f"{side}.sog {sog} != event count {n}")
    bound = start_time + RESULT_LAG
    p = prov.event_fact(SOURCE, entry.snapshot_id, entry.fetched_at, bound, SOURCE_VERSION, start_time) if entry else None
    result = GameResult(
        game_id=gid, home_goals=reg_h + ot_h, away_goals=reg_a + ot_a, reg_home_goals=reg_h, reg_away_goals=reg_a,
        p1_home_goals=count(lambda e: e["period"] == 1, True), p1_away_goals=count(lambda e: e["period"] == 1, False),
        end_type=end_type, shootout_winner=so_winner, available_at=p.available_at if p else bound, provenance=p,
    )
    return result, events


def parse_result(payload: dict[str, Any], start_time: datetime, entry: SnapshotEntry | None = None) -> GameResult | None:
    result, events = parse_result_checked(payload, start_time, entry)
    events.require_clean()
    return result


def parse_boxscore_goalies_checked(
    payload: dict[str, Any], start_time: datetime, entry: SnapshotEntry | None = None
) -> ParseResult[GoalieGameStats]:
    out: ParseResult[GoalieGameStats] = ParseResult()
    try:
        gid, _, _, _, _ = _game_header(payload)
    except (FieldError, ValueError) as exc:
        out.reject("boxscore", str(payload.get("id", "?")), f"header: {exc}")
        return out
    bound = start_time + RESULT_LAG
    p = prov.event_fact(SOURCE, entry.snapshot_id, entry.fetched_at, bound, SOURCE_VERSION, start_time) if entry else None
    by_game = payload.get("playerByGameStats") or {}
    for side in ("homeTeam", "awayTeam"):
        team = canonical_team(payload[side]["abbrev"])
        goalies = (by_game.get(side) or {}).get("goalies", [])
        starters = [g for g in goalies if g.get("starter") is True]
        if goalies and len(starters) != 1:
            out.reject("boxscore", f"{gid}:{team}", f"expected exactly one starter, got {len(starters)}")
            continue
        for g in goalies:
            key = f"{gid}:{team}:{g.get('playerId', '?')}"
            try:
                if not isinstance(g.get("starter"), bool):
                    raise FieldError("starter flag missing or not boolean")
                ga = strict_int(req(g, "goalsAgainst"), "goalsAgainst")
                if "saveShotsAgainst" in g:
                    saves_s, shots_s = str(g["saveShotsAgainst"]).split("/")
                    saves, shots = strict_int(saves_s, "saves"), strict_int(shots_s, "shots")
                    if saves > shots or shots - saves != ga:
                        raise FieldError(f"saveShotsAgainst {g['saveShotsAgainst']} inconsistent with goalsAgainst {ga}")
                else:
                    shots = strict_int(req(g, "shotsAgainst"), "shotsAgainst")
                    if ga > shots:
                        raise FieldError("goalsAgainst > shotsAgainst")
                out.records.append(
                    GoalieGameStats(
                        game_id=gid, team=team, goalie_id=canonical_player_id(req(g, "playerId")), started=g["starter"],
                        toi_sec=float(mmss(req(g, "toi"), "toi", 200 * 60)), shots_against=shots, goals_against=ga,
                        xg_against=float("nan"),  # NHL API has no xG; joined from an xG source
                        available_at=p.available_at if p else bound, provenance=p,
                    )
                )
            except (FieldError, ValueError, KeyError, TypeError) as exc:
                out.reject("boxscore", key, str(exc))
    return out


def parse_boxscore_goalies(payload: dict[str, Any], start_time: datetime, entry: SnapshotEntry | None = None) -> list[GoalieGameStats]:
    return parse_boxscore_goalies_checked(payload, start_time, entry).require_clean()


def parse_shifts_checked(payload: dict[str, Any]) -> ParseResult[dict[str, Any]]:
    out: ParseResult[dict[str, Any]] = ParseResult()
    for s in payload.get("data", []) or []:
        key = f"{s.get('gameId', '?')}:{s.get('playerId', '?')}:{s.get('period', '?')}:{s.get('startTime', '?')}"
        if s.get("typeCode") not in (None, 517):  # 517 = shift; 505 = goal marker rows
            continue
        try:
            period = strict_int(req(s, "period"), "period", 1, 20)
            start = mmss(req(s, "startTime"), "startTime", 1200)
            end = mmss(req(s, "endTime"), "endTime", 1200)
            if end < start:
                raise FieldError("shift ends before it starts")
            out.records.append(
                {
                    "game_id": canonical_game_id(req(s, "gameId")),
                    "player_id": canonical_player_id(req(s, "playerId")),
                    "team": canonical_team(req(s, "teamAbbrev")),
                    "period": period,
                    "start_sec": (period - 1) * 1200 + start,
                    "end_sec": (period - 1) * 1200 + end,
                }
            )
        except (FieldError, ValueError, TypeError) as exc:
            out.reject("shifts", key, str(exc))
    out.records.sort(key=lambda r: (r["start_sec"], r["team"], r["player_id"]))
    return out


def parse_shifts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return parse_shifts_checked(payload).require_clean()
