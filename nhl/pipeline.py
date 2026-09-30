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
from nhl.contracts.schemas import PRICEABLE_STATUSES
from nhl.features.league_constants import LeagueConstants, fit_league_constants
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
    constants: LeagueConstants | None = None
    skipped: list[tuple[str, str]] | None = None


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
    constants: LeagueConstants | None = None,
) -> SlateRun:
    """``games`` are the slate's game ids as the caller knows them; the schedule is
    re-read from the view so the prices use the version known at ``as_of``.
    ``constants``: walk-forward league constants for ``season`` (fit here if absent)."""

    view = store.view(as_of)
    known = {g.game_id: g for g in view.games()}
    skipped: list[tuple[str, str]] = []
    slate: list[Game] = []
    for g in games:
        k = known.get(g.game_id)
        if k is None:
            skipped.append((g.game_id, "NOT_IN_SCHEDULE_AT_AS_OF"))
        elif k.status not in PRICEABLE_STATUSES:
            skipped.append((g.game_id, f"STATUS_{k.status}"))
        elif k.start_time <= as_of:
            raise LeakageError("as_of must be before every puck drop on the slate")
        else:
            slate.append(k)
    if constants is None:
        constants = fit_league_constants(store, season)
    elif constants.target_season != season:
        raise LeakageError(f"constants fit for {constants.target_season} used for season {season}")
    sim_cfg = constants.apply(load("simulator"))
    league = league_baselines(sim_cfg)
    tp = team_priors(view, rosters.get(season, []), season, league["rate_5v5"])
    ratings = fit_ratings(view, season, tp, goalie_priors(view, season), league)
    health = evaluate_slate(view, [g.game_id for g in slate], teams_with_prior=set(tp))
    priced, errors = price_slate(view, slate, ratings, n_sims=n_sims, seed=game_seed(seed, as_of.isoformat()), sim_cfg=sim_cfg)
    artifacts = build_artifacts(
        view, priced, view.odds_snapshots({g.game_id for g in slate}), health, mode, created_at=created_at or utcnow(),
        constants_id=constants.constants_id,
    )
    return SlateRun(view, ratings, health, priced, errors, artifacts, constants, skipped)
