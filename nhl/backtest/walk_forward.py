"""Strict walk-forward backtest.

For each slate day D (US/Eastern date):
  as_of = earliest puck drop on D - lead
  view  = store.view(as_of)          # only data available at as_of
  priors, ratings <- view            # refit from scratch every day
  goalie mixtures, simulation, pricing <- view
  artifacts(mode=BACKTEST) appended to the immutable store

Nothing computed on day D can see results, stats, reports or odds stamped after
``as_of``. Evaluation against closing lines/results happens afterwards in
``nhl.backtest.evaluate`` and never feeds back into predictions.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from nhl.config import load
from nhl.contracts import Game, PredictionMode
from nhl.data.pit import HistoricalStore
from nhl.features.league_constants import LeagueConstantsStore, fit_league_constants
from nhl.features.priors import RosterSlot
from nhl.ledger.predictions import PredictionStore
from nhl.pipeline import run_slate

ET = ZoneInfo("America/New_York")


@dataclass
class BacktestConfig:
    season: int
    start: date
    end: date
    lead_minutes: int = 60
    n_sims: int | None = None
    seed: int = 20260925


def slate_days(games: list[Game]) -> dict[date, list[Game]]:
    days: dict[date, list[Game]] = defaultdict(list)
    for g in games:
        days[g.start_time.astimezone(ET).date()].append(g)
    return dict(sorted(days.items()))


def run_walk_forward(
    store: HistoricalStore,
    rosters: dict[int, list[RosterSlot]],
    cfg: BacktestConfig,
    predictions: PredictionStore,
    progress: bool = False,
    constants_store: LeagueConstantsStore | None = None,
) -> dict:
    n_sims = cfg.n_sims or int(load("simulator")["backtest_n_sims"])
    # Constants for the season come only from seasons completed before it (gap 3B).
    constants = fit_league_constants(store, cfg.season)
    constants_path = str(constants_store.save(constants)) if constants_store else ""
    # Slate days from EVERY schedule version (a game rescheduled later still belongs to
    # its originally announced day as known then); run_slate re-reads the schedule as
    # known at as_of and skips postponed / not-yet-known games.
    season_games = [g for g in store.games if g.season == cfg.season]
    summary = {"days": 0, "games_priced": 0, "games_skipped": 0, "artifacts": 0, "skipped_identical": 0,
               "errors": [], "skipped_status": [], "constants_id": constants.constants_id, "constants_path": constants_path,
               "constants_train_seasons": list(constants.train_seasons)}
    for day, games in slate_days(season_games).items():
        if day < cfg.start or day > cfg.end:
            continue
        as_of: datetime = min(g.start_time for g in games) - timedelta(minutes=cfg.lead_minutes)
        on_day = {g.game_id: g for g in games}
        known_today = [g for g in store.view(as_of).games() if g.game_id in on_day and g.start_time.astimezone(ET).date() == day]
        run = run_slate(store, rosters, cfg.season, known_today, as_of, PredictionMode.BACKTEST, n_sims=n_sims,
                        seed=str(cfg.seed), constants=constants)
        priced, errors, artifacts = run.priced, run.errors, run.artifacts
        summary["errors"].extend(f"{day}:{gid}:{msg}" for gid, msg in errors)
        summary["skipped_status"].extend(f"{day}:{gid}:{why}" for gid, why in (run.skipped or []))
        res = predictions.append(artifacts)
        summary["days"] += 1
        summary["games_priced"] += len(priced)
        summary["games_skipped"] += len(errors)
        summary["artifacts"] += res["inserted"]
        summary["skipped_identical"] += res["skipped_identical"]
        if progress and summary["days"] % 20 == 0:
            print(f"  {day}: {summary['games_priced']} games, {summary['artifacts']} artifacts", flush=True)
    return summary
