from dataclasses import replace

import pytest

from nhl.data.pit import HistoricalStore
from nhl.features.league_constants import (
    ConstantsIntegrityError,
    LeagueConstantsStore,
    fit_league_constants,
)


def _copy(store: HistoricalStore, **kw) -> HistoricalStore:
    base = dict(games=store.games, results=store.results, team_stats=store.team_stats, goalie_stats=store.goalie_stats,
                goalie_reports=store.goalie_reports, odds=store.odds, player_seasons=store.player_seasons,
                data_origin=store.data_origin)
    base.update(kw)
    return HistoricalStore(**base)


def test_constants_use_only_prior_completed_seasons(league):
    c25 = fit_league_constants(league.store, 2025)
    assert c25.train_seasons == (2024,)
    assert c25.sources["rate_5v5"] == "FIT" and c25.sources["rate_3v3"] == "FIT"
    assert c25.sources["rate_empty_net"].startswith("DEFAULT:")
    # Rewrite every 2025 result and stat: the 2025 constants must not move.
    mutated = _copy(
        league.store,
        results=[replace(r, end_type="SO", shootout_winner="HOME", home_goals=r.away_goals, reg_home_goals=r.reg_away_goals)
                 if r.game_id.startswith("2025") else r for r in league.store.results],
        team_stats=[replace(s, xg_for=s.xg_for * 3) if s.game_id.startswith("2025") else s for s in league.store.team_stats],
    )
    assert fit_league_constants(mutated, 2025).constants_id == c25.constants_id
    # But changing 2024 data does change them.
    changed = _copy(league.store, team_stats=[replace(s, xg_for=s.xg_for * 1.5) if s.game_id.startswith("2024") else s
                                              for s in league.store.team_stats])
    assert fit_league_constants(changed, 2025).constants_id != c25.constants_id


def test_no_history_falls_back_to_labelled_defaults(league):
    c24 = fit_league_constants(league.store, 2024)
    assert c24.train_seasons == ()
    assert all(src.startswith("DEFAULT:") for src in c24.sources.values())


def test_rolling_window(league):
    c = fit_league_constants(league.store, 2026, window=1)
    assert c.train_seasons == (2025,)
    assert fit_league_constants(league.store, 2026, window=3).train_seasons == (2024, 2025)


def test_constants_persist_write_once_and_verify(league, tmp_path):
    store = LeagueConstantsStore(tmp_path)
    c = fit_league_constants(league.store, 2025)
    path = store.save(c)
    assert store.save(c) == path  # idempotent for identical content
    assert store.load(path) == c
    path.chmod(0o644)
    path.write_text(path.read_text().replace('"window": 3', '"window": 2'))
    with pytest.raises(ConstantsIntegrityError):
        store.load(path)
    with pytest.raises(ConstantsIntegrityError):
        store.save(c)


def test_pipeline_rejects_constants_from_another_season(league):
    from datetime import timedelta

    from nhl.backtest.walk_forward import slate_days
    from nhl.contracts import PredictionMode
    from nhl.data.pit import LeakageError
    from nhl.pipeline import run_slate

    games = list(slate_days([g for g in league.store.games if g.season == 2025]).values())[40]
    as_of = min(g.start_time for g in games) - timedelta(minutes=60)
    with pytest.raises(LeakageError):
        run_slate(league.store, league.rosters, 2025, games, as_of, PredictionMode.BACKTEST, n_sims=500,
                  constants=fit_league_constants(league.store, 2026))
    run = run_slate(league.store, league.rosters, 2025, games, as_of, PredictionMode.BACKTEST, n_sims=500)
    assert run.constants.train_seasons == (2024,)
