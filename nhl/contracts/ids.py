"""Canonical NHL identifiers.

* game_id   -- NHL 10-digit id ``SSSSTTNNNN`` (season start year, game type, number).
* team      -- current NHL tri-code; historical/alternate codes map via ``TEAM_ALIASES``.
* player_id -- NHL player id (MoneyPuck uses the same ids). Goalies are players.
* market_key / prediction ids -- stable digests, same pattern as tournament_v2.
"""

from __future__ import annotations

import re
from hashlib import sha1

from .enums import MarketType, Selection

TEAMS: frozenset[str] = frozenset(
    {
        "ANA", "BOS", "BUF", "CAR", "CBJ", "CGY", "CHI", "COL", "DAL", "DET", "EDM",
        "FLA", "LAK", "MIN", "MTL", "NJD", "NSH", "NYI", "NYR", "OTT", "PHI", "PIT",
        "SEA", "SJS", "STL", "TBL", "TOR", "UTA", "VAN", "VGK", "WPG", "WSH",
    }
)

# Alternate codes seen across NHL API eras, MoneyPuck and sportsbook feeds.
# ARI -> UTA is a franchise relocation: ratings must NOT carry over automatically,
# player-based priors handle the roster (see nhl.features.priors).
TEAM_ALIASES: dict[str, str] = {
    "L.A": "LAK", "LA": "LAK", "N.J": "NJD", "NJ": "NJD", "S.J": "SJS", "SJ": "SJS",
    "T.B": "TBL", "TB": "TBL", "WAS": "WSH", "MON": "MTL", "CLB": "CBJ", "NAS": "NSH",
    "VEG": "VGK", "UTAH": "UTA", "ARI": "UTA", "PHX": "UTA",
}

GAME_TYPES = {"01": "PRESEASON", "02": "REGULAR", "03": "PLAYOFF"}
_GAME_ID_RE = re.compile(r"^(\d{4})(0[123])(\d{4})$")


def canonical_team(code: str) -> str:
    token = (code or "").strip().upper()
    token = TEAM_ALIASES.get(token, token)
    if token not in TEAMS:
        raise ValueError(f"unknown team code: {code!r}")
    return token


def canonical_game_id(value: str | int) -> str:
    token = str(value).strip()
    if not _GAME_ID_RE.match(token):
        raise ValueError(f"invalid NHL game_id: {value!r}")
    return token


def season_of(game_id: str) -> int:
    """Season start year, e.g. 2026 for the 2026-27 season."""

    return int(canonical_game_id(game_id)[:4])


def game_type_of(game_id: str) -> str:
    return GAME_TYPES[canonical_game_id(game_id)[4:6]]


def canonical_player_id(value: str | int) -> str:
    token = str(value).strip()
    if not token.isdigit():
        raise ValueError(f"invalid NHL player id: {value!r}")
    return token


def stable_digest(parts: list[str], n: int = 12) -> str:
    return sha1("|".join(parts).encode("utf-8")).hexdigest()[:n]


def format_line(line: float | None) -> str:
    return "" if line is None else f"{float(line):+.1f}"


def build_market_key(
    game_id: str,
    market: MarketType,
    selection: Selection,
    line: float | None = None,
    team: str | None = None,
) -> str:
    """Book-agnostic key for one priced outcome (e.g. ``2026020001:TOTAL:OVER:+6.5``).

    ``team`` is required for TEAM_TOTAL so home/away team totals stay distinct.
    """

    canonical_game_id(game_id)
    if market.has_line and line is None:
        raise ValueError(f"{market.value} requires a line")
    if market is MarketType.TEAM_TOTAL and not team:
        raise ValueError("TEAM_TOTAL requires team")
    parts = [game_id, market.value]
    if team:
        parts.append(canonical_team(team))
    parts.append(selection.value)
    if line is not None:
        parts.append(format_line(line))
    return ":".join(parts)
