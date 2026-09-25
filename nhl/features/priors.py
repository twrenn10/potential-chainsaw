"""Player-based preseason priors.

Last season's *team* numbers go stale in the offseason (trades, signings, coaching,
relocation). Priors are therefore rebuilt from the players on the projected roster:

1. Player talent = TOI-weighted blend of up to 3 prior seasons (weights 3/2/1),
   shrunk toward 0 (league average) with ``k_minutes`` pseudo-minutes.
2. Team prior = roster-TOI-weighted mean of player talent, then multiplied by
   ``team_retention`` (< 1) because on-ice rates also contain system/teammate
   effects that do not transfer with the player.
3. Converted to log-multipliers on league-average 5v5 xG rates, matching the
   parameterisation in ``nhl.features.ratings``.

Goalie priors: GSAx per unit xGA over prior seasons, shrunk with ``k_goalie_xga``
pseudo-xGA toward 0. Goalie talent is noisy; the prior is deliberately heavy.

All inputs come through a ``PointInTimeView`` so a same-season summary can never
leak into that season's priors.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

from nhl.contracts import PlayerSeason
from nhl.data.pit import PointInTimeView, assert_visible

SEASON_WEIGHTS = (3.0, 2.0, 1.0)


@dataclass(frozen=True)
class RosterSlot:
    team: str
    player_id: str
    position: str  # F | D | G
    proj_5v5_min: float  # projected 5v5 minutes per game


@dataclass(frozen=True)
class TeamPrior:
    team: str
    att: float  # log-multiplier on xG for
    dfn: float  # log-multiplier on xG against (+ = worse defence)
    coverage: float  # share of projected TOI with player history
    n_players: int


@dataclass(frozen=True)
class GoaliePrior:
    goalie_id: str
    g: float  # log save-skill: goals = xGA * exp(-g)
    xga_seen: float


def player_talent(
    history: list[PlayerSeason], target_season: int, k_minutes: float = 800.0
) -> dict[str, tuple[float, float, float]]:
    """player_id -> (xgf60_rel, xga60_rel, weighted minutes)."""

    acc: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
    for ps in history:
        lag = target_season - ps.season
        if lag < 1 or lag > len(SEASON_WEIGHTS):
            continue
        w = SEASON_WEIGHTS[lag - 1] * ps.toi_5v5_min
        a = acc[ps.player_id]
        a[0] += w * ps.on_ice_xgf60_rel
        a[1] += w * ps.on_ice_xga60_rel
        a[2] += w
    out = {}
    for pid, (sf, sa, w) in acc.items():
        denom = w + k_minutes * SEASON_WEIGHTS[0]
        out[pid] = (sf / denom, sa / denom, w / SEASON_WEIGHTS[0])
    return out


def team_priors(
    view: PointInTimeView,
    roster: list[RosterSlot],
    target_season: int,
    league_xg60: float,
    team_retention: float = 0.6,
) -> dict[str, TeamPrior]:
    history = view.player_seasons()
    assert_visible(history, view.as_of, "player_season")
    talent = player_talent(history, target_season)
    by_team: dict[str, list[RosterSlot]] = defaultdict(list)
    for slot in roster:
        if slot.position != "G":
            by_team[slot.team].append(slot)
    out = {}
    for team, slots in sorted(by_team.items()):
        tot = sum(s.proj_5v5_min for s in slots) or 1.0
        f = a = covered = 0.0
        for s in slots:
            t = talent.get(s.player_id)
            if t is None:
                continue  # unknown players contribute league average (0)
            share = s.proj_5v5_min / tot
            f += share * t[0]
            a += share * t[1]
            covered += share
        f *= team_retention
        a *= team_retention
        out[team] = TeamPrior(
            team=team,
            att=math.log(max(0.2, (league_xg60 + f) / league_xg60)),
            dfn=math.log(max(0.2, (league_xg60 + a) / league_xg60)),
            coverage=round(covered, 4),
            n_players=len(slots),
        )
    return out


def goalie_priors(view: PointInTimeView, target_season: int, k_goalie_xga: float = 150.0) -> dict[str, GoaliePrior]:
    """From goalie game rows of prior seasons only (strictly < target_season)."""

    xga: dict[str, float] = defaultdict(float)
    ga: dict[str, float] = defaultdict(float)
    for gs in view.goalie_stats():
        season = int(gs.game_id[:4])
        lag = target_season - season
        if lag < 1 or lag > 3 or math.isnan(gs.xg_against):
            continue
        w = SEASON_WEIGHTS[lag - 1] / SEASON_WEIGHTS[0]
        xga[gs.goalie_id] += w * gs.xg_against
        ga[gs.goalie_id] += w * gs.goals_against
    out = {}
    for gid in sorted(xga):
        # MAP of log(xGA/GA) with a pseudo-count prior centred on league average.
        g = math.log((xga[gid] + k_goalie_xga) / (ga[gid] + k_goalie_xga))
        out[gid] = GoaliePrior(goalie_id=gid, g=g, xga_seen=xga[gid])
    return out
