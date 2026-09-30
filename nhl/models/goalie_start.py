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
from datetime import timedelta

from nhl.config import load
from nhl.contracts import Game, GoalieState
from nhl.data.pit import PointInTimeView


@dataclass(frozen=True)
class StarterDistribution:
    team: str
    state: GoalieState  # best information state visible
    probs: tuple[tuple[str, float], ...]  # (goalie_id, p), sorted desc, sums to 1
    flags: tuple[str, ...] = ()  # GOALIE_SOURCE_CONFLICT, GOALIE_REPORT_STALE, GOALIE_SCRATCHED:<id>, ...
    reports: tuple[str, ...] = ()  # lineage: every report considered, "source|goalie|state|available_at"

    @property
    def confirmed(self) -> bool:
        return self.state is GoalieState.CONFIRMED and "GOALIE_SOURCE_CONFLICT" not in self.flags

    def fingerprint(self) -> str:
        from nhl.contracts import stable_digest

        return stable_digest([self.team, self.state.value, repr(self.probs), repr(self.flags), repr(self.reports)], 16)

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


P_NAMED = {
    GoalieState.CONFIRMED: "p_start_confirmed",
    GoalieState.EXPECTED: "p_start_expected",
    GoalieState.PROBABLE: "p_start_probable",
    GoalieState.PROJECTED: "p_start_projected",
}


def starter_distribution(view: PointInTimeView, game: Game, team: str) -> StarterDistribution:
    """Starter mixture from ONLY the reports visible at ``view.as_of``.

    Lineage rules (docs/goalie_information_lineage.md):
    * each source's LATEST visible report is its current call;
    * a goalie whose latest report from any source is SCRATCHED gets p = 0;
    * a report older than ``stale_report_hours`` before puck drop is stale: flagged,
      not used to name a starter;
    * the strongest state wins; if sources at that state name different goalies the
      claim is a CONFLICT: flagged, never "confirmed", and the named mass is split
      across the named goalies by the workload model;
    * UNKNOWN never becomes confirmed; with no usable claim the workload model decides.
    """

    cfg = load("goalie_start")
    weights = workload_weights(view, game, team, cfg["unknown_model"])
    history = [r for r in view.goalie_report_history(game.game_id) if r.team == team]
    stale_before = game.start_time - timedelta(hours=float(cfg["stale_report_hours"]["value"]))
    flags: list[str] = []
    lineage = tuple(f"{r.source}|{r.goalie_id}|{r.state.value}|{r.available_at.strftime('%Y-%m-%dT%H:%M:%SZ')}" for r in history)

    latest_by_source: dict[str, object] = {}
    latest_by_goalie: dict[str, object] = {}
    for r in history:  # chronological -> last write wins, deterministic tie order from the sort
        latest_by_source[r.source] = r
        latest_by_goalie[r.goalie_id] = r
    scratched = sorted(g for g, r in latest_by_goalie.items() if r.state is GoalieState.SCRATCHED)
    flags.extend(f"GOALIE_SCRATCHED:{g}" for g in scratched)
    for g in scratched:
        weights.pop(g, None)

    claims = []
    for src in sorted(latest_by_source):
        r = latest_by_source[src]
        if r.state.rank == 0 or r.goalie_id in scratched:
            continue
        if r.available_at < stale_before:
            flags.append(f"GOALIE_REPORT_STALE:{src}")
            continue
        claims.append(r)

    if claims:
        top_rank = max(r.state.rank for r in claims)
        top = [r for r in claims if r.state.rank == top_rank]
        named = sorted({r.goalie_id for r in top})
        state = top[0].state
        if len(named) > 1:
            flags.append("GOALIE_SOURCE_CONFLICT")
        if len({r.goalie_id for r in claims}) > len(named):
            flags.append("GOALIE_SOURCE_DISAGREEMENT")
        p_named = float(cfg[P_NAMED[state]])
        wn = {g: weights.get(g, 0.0) for g in named}
        wn_tot = sum(wn.values())
        probs = {g: p_named * (wn[g] / wn_tot if wn_tot > 0 else 1.0 / len(named)) for g in named}
        others = {g: w for g, w in weights.items() if g not in named}
        tot = sum(others.values())
        if tot > 0:
            for g, w in others.items():
                probs[g] = (1 - p_named) * w / tot
        else:
            probs = {g: p / p_named for g, p in probs.items()}  # no known alternative: unavoidable
    else:
        if not weights:
            raise ValueError(f"{team}: no usable goalie report and no start history visible at {view.as_of}")
        tot = sum(weights.values())
        probs = {g: w / tot for g, w in weights.items()}
        state = GoalieState.UNKNOWN
    ordered = tuple(sorted(((g, round(p, 6)) for g, p in probs.items()), key=lambda gp: (-gp[1], gp[0])))
    return StarterDistribution(team=team, state=state, probs=ordered, flags=tuple(sorted(set(flags))), reports=lineage)
