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
from nhl.contracts import Game, PredictionMode, stable_digest
from nhl.data.pit import HistoricalStore, LeakageError, PointInTimeView
from nhl.data.quality import HealthReport, evaluate_slate
from nhl.desk.candidates import build_artifacts, ratings_fingerprint
from nhl.contracts.schemas import PRICEABLE_STATUSES
from nhl.features.league_constants import LeagueConstants, fit_league_constants
from nhl.features.priors import RosterSlot, goalie_priors, roster_as_of, team_priors
from nhl.features.ratings import Ratings, fit_ratings, league_baselines
from nhl.ledger.predictions import PredictionArtifact
from nhl.pricing.engine import PricedGame, game_seed, price_slate
from nhl.timeutil import utcnow


def roster_fingerprints(roster: list[RosterSlot]) -> dict[str, str]:
    by_team: dict[str, list[str]] = {}
    for s in roster:
        at = s.available_at.strftime("%Y-%m-%dT%H:%M:%SZ") if s.available_at else "UNDATED"
        by_team.setdefault(s.team, []).append(f"{s.player_id}:{s.position}:{s.proj_5v5_min}:{at}")
    return {t: stable_digest(sorted(v), 16) for t, v in by_team.items()}


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
    simulated_clock: bool = False,
    strict: bool = True,
) -> SlateRun:
    """``games`` are the slate's game ids as the caller knows them; the schedule is
    re-read from the view so the prices use the version known at ``as_of``.
    ``constants``: walk-forward league constants for ``season`` (fit here if absent)."""

    view = store.view(as_of, strict=strict)
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
    roster, roster_unattested = roster_as_of(rosters.get(season, []), as_of, strict=view.strict)
    extra_blocks = ["UNATTESTED_ROSTER"] if roster_unattested and view.data_origin != "SYNTHETIC" else []
    tp = team_priors(view, roster, season, league["rate_5v5"])
    ratings = fit_ratings(view, season, tp, goalie_priors(view, season), league)
    health = evaluate_slate(view, [g.game_id for g in slate], teams_with_prior=set(tp))
    # Seed from the information state (parameters, constants, slate), not the clock, so an
    # unchanged state reproduces identical prices (common random numbers across reprices).
    info_seed = game_seed(seed, "|".join([ratings_fingerprint(ratings), constants.constants_id]))
    priced, errors = price_slate(view, slate, ratings, n_sims=n_sims, seed=info_seed, sim_cfg=sim_cfg)
    artifacts = build_artifacts(
        view, priced, view.odds_snapshots({g.game_id for g in slate}), health, mode, created_at=created_at or utcnow(),
        extra_provenance_blocks=extra_blocks, constants=constants, roster_fingerprints=roster_fingerprints(roster),
        parameter_fingerprint=ratings_fingerprint(ratings), simulated_clock=simulated_clock,
    )
    return SlateRun(view, ratings, health, priced, errors, artifacts, constants, skipped)
