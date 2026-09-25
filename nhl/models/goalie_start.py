"""Probabilistic starting-goalie model.

Output for each team in a game: a probability distribution over that team's
goalies. Games are priced as a mixture over these distributions; a goalie is
never treated as certain unless a CONFIRMED report is visible at ``as_of`` (and
even then P < 1: late scratches happen).

Precedence at ``as_of``:
1. CONFIRMED report -> that goalie gets ``p_start_confirmed``.
2. PROJECTED report -> that goalie gets ``p_start_projected``.
3. Otherwise the workload model: logistic on recent start share, back-to-back
   (started night one of a B2B) and rest.
Residual mass is spread over other team goalies by the workload model.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from nhl.config import load
from nhl.contracts import Game, GoalieState
from nhl.data.pit import PointInTimeView


@dataclass(frozen=True)
class StarterDistribution:
    team: str
    state: GoalieState  # best information state visible
    probs: tuple[tuple[str, float], ...]  # (goalie_id, p), sorted desc, sums to 1

    @property
    def top(self) -> tuple[str, float]:
        return self.probs[0]

    def truncated(self, min_p: float = 0.03) -> tuple[tuple[str, float], ...]:
        kept = [(g, p) for g, p in self.probs if p >= min_p] or [self.probs[0]]
        tot = sum(p for _, p in kept)
        return tuple((g, p / tot) for g, p in kept)


def _logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def workload_weights(view: PointInTimeView, game: Game, team: str, cfg: dict | None = None) -> dict[str, float]:
    """Unnormalised start propensities for each team goalie from visible history."""

    cfg = cfg or load("goalie_start")["unknown_model"]
    start_of = {g.game_id: g.start_time for g in view.games()}
    history = sorted(
        (gs for gs in view.goalie_stats() if gs.team == team and gs.game_id in start_of and start_of[gs.game_id] < game.start_time),
        key=lambda gs: start_of[gs.game_id],
    )
    starts = [gs for gs in history if gs.started]
    if not starts:
        return {}
    window = starts[-int(cfg["recent_starts_window"]):]
    goalies = sorted({gs.goalie_id for gs in history[-60:]})
    last_start = starts[-1]
    last_game_time = start_of[last_start.game_id]
    b2b_second = (game.start_time - last_game_time).total_seconds() < 30 * 3600
    prior = float(cfg["share_prior_starts"])
    out = {}
    for gid in goalies:
        n = sum(1 for gs in window if gs.goalie_id == gid)
        share = (n + prior / max(len(goalies), 1)) / (len(window) + prior)
        own_last = max((start_of[gs.game_id] for gs in starts if gs.goalie_id == gid), default=None)
        rest_days = 3.0 if own_last is None else min((game.start_time - own_last).total_seconds() / 86400.0, 3.0)
        x = cfg["intercept"] + cfg["b_share"] * _logit(share) + cfg["b_rest"] * rest_days
        if b2b_second and last_start.goalie_id == gid:
            x += cfg["b_b2b_second"]
        out[gid] = _sigmoid(x)
    return out


def starter_distribution(view: PointInTimeView, game: Game, team: str) -> StarterDistribution:
    cfg = load("goalie_start")
    weights = workload_weights(view, game, team, cfg["unknown_model"])
    reports = [r for r in view.goalie_reports(game.game_id) if r.team == team]
    best = None
    for rep in reports:
        rank = {GoalieState.CONFIRMED: 2, GoalieState.PROJECTED: 1, GoalieState.UNKNOWN: 0}[rep.state]
        if best is None or (rank, rep.available_at) > best[0]:
            best = ((rank, rep.available_at), rep)

    if best is not None and best[1].state in (GoalieState.CONFIRMED, GoalieState.PROJECTED):
        rep = best[1]
        p_named = cfg["p_start_confirmed"] if rep.state is GoalieState.CONFIRMED else cfg["p_start_projected"]
        others = {g: w for g, w in weights.items() if g != rep.goalie_id}
        tot = sum(others.values())
        probs = {rep.goalie_id: p_named}
        if tot > 0:
            for g, w in others.items():
                probs[g] = (1 - p_named) * w / tot
        else:
            probs[rep.goalie_id] = 1.0  # no known alternative: unavoidable
        state = rep.state
    else:
        if not weights:
            raise ValueError(f"{team}: no goalie report and no start history visible at {view.as_of}")
        tot = sum(weights.values())
        probs = {g: w / tot for g, w in weights.items()}
        state = GoalieState.UNKNOWN
    ordered = tuple(sorted(((g, round(p, 6)) for g, p in probs.items()), key=lambda gp: (-gp[1], gp[0])))
    return StarterDistribution(team=team, state=state, probs=ordered)
