"""One-slate orchestration shared by the daily desk and the walk-forward backtest.

Stages (tournament_v2 order, NHL internals):
point-in-time view -> priors -> joint ratings -> data-quality gates -> goalie
mixtures + game-state simulation -> model vs no-vig market -> governance lanes ->
prediction artifacts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from nhl.config import load
from nhl.contracts import Game, PredictionMode
from nhl.data.pit import HistoricalStore, LeakageError, PointInTimeView
from nhl.data.quality import HealthReport, evaluate_slate
from nhl.desk.candidates import build_artifacts
from nhl.features.priors import RosterSlot, goalie_priors, team_priors
from nhl.features.ratings import Ratings, fit_ratings, league_baselines
from nhl.ledger.predictions import PredictionArtifact
from nhl.pricing.engine import PricedGame, game_seed, price_slate
from nhl.timeutil import utcnow


@dataclass
class SlateRun:
    view: PointInTimeView
    ratings: Ratings
    health: HealthReport
    priced: list[PricedGame]
    errors: list[tuple[str, str]]
    artifacts: list[PredictionArtifact]


def run_slate(
    store: HistoricalStore,
    rosters: dict[int, list[RosterSlot]],
    season: int,
    games: list[Game],
    as_of: datetime,
    mode: PredictionMode,
    n_sims: int | None = None,
    seed: str = "nhl",
    created_at: datetime | None = None,
) -> SlateRun:
    if any(g.start_time <= as_of for g in games):
        raise LeakageError("as_of must be before every puck drop on the slate")
    league = league_baselines(load("simulator"))
    view = store.view(as_of)
    tp = team_priors(view, rosters.get(season, []), season, league["rate_5v5"])
    ratings = fit_ratings(view, season, tp, goalie_priors(view, season), league)
    health = evaluate_slate(view, [g.game_id for g in games], teams_with_prior=set(tp))
    priced, errors = price_slate(view, games, ratings, n_sims=n_sims, seed=game_seed(seed, as_of.isoformat()))
    artifacts = build_artifacts(
        view, priced, view.odds_snapshots({g.game_id for g in games}), health, mode, created_at=created_at or utcnow()
    )
    return SlateRun(view, ratings, health, priced, errors, artifacts)
