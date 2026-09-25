"""Rink scorekeeper effects for recorded shots (Phase 1D input; not used by game pricing).

Shots on goal are counted by the home arena's scorers, and counting differs by rink.
SOG and goalie-saves props must be priced on rink-adjusted counts. The estimate
compares SOG recorded in a rink (both teams) with those same teams' SOG in their
*road* games, and shrinks toward 1.0 with ``k_games`` pseudo-games.
"""

from __future__ import annotations

from collections import defaultdict

from nhl.contracts import TeamGameStats


def rink_sog_factors(stats: list[TeamGameStats], k_games: float = 20.0) -> dict[str, float]:
    """Home-rink team -> multiplicative factor on recorded SOG (1.0 = neutral)."""

    rows = [s for s in stats if s.situation == "all"]
    road_rate: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])  # team -> [sog, games]
    for s in rows:
        if not s.is_home:
            road_rate[s.team][0] += s.sog_for
            road_rate[s.team][1] += 1
    obs: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])  # rink -> [observed, expected, games]
    for s in rows:
        rink = s.team if s.is_home else s.opponent
        rr = road_rate.get(s.team)
        if not rr or rr[1] == 0:
            continue
        obs[rink][0] += s.sog_for
        obs[rink][1] += rr[0] / rr[1]
        obs[rink][2] += 0.5  # two rows per game
    out = {}
    for rink, (o, e, n) in sorted(obs.items()):
        raw = o / e if e > 0 else 1.0
        out[rink] = (raw * n + 1.0 * k_games) / (n + k_games)
    return out
