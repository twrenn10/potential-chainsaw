from dataclasses import replace
from datetime import timedelta

import numpy as np
import pytest

from nhl.config import load
from nhl.contracts import Game, GoalieReport, GoalieState, MarketType, Selection
from nhl.data.pit import HistoricalStore
from nhl.features.priors import goalie_priors, team_priors
from nhl.features.ratings import fit_ratings, league_baselines
from nhl.features.rest_travel import team_schedule_features
from nhl.market.novig import american_to_decimal, decimal_to_american, devig, overround, prob_to_american
from nhl.models.game_state import GameRates, TeamRates, simulate
from nhl.models.goalie_start import starter_distribution
from nhl.pricing.engine import price_slate
from nhl.pricing.markets import GamePricing, fair_decimal
from nhl.timeutil import parse_ts

CFG = load("simulator")


def avg_rates(scale: float = 1.0) -> TeamRates:
    return TeamRates(ev=CFG["rate_5v5"] * scale, pp=CFG["rate_pp"] * scale, sh=CFG["rate_sh"], pen=CFG["penalty_rate"],
                     six_v_five=CFG["rate_6v5_attack"] * scale, empty_net=CFG["rate_empty_net"], ot=CFG["rate_3v3"] * scale)


def test_rest_travel_b2b():
    g1 = Game("2026020001", 2026, "REGULAR", "2026-10-07T23:00:00Z", "TOR", "MTL")
    g2 = Game("2026020002", 2026, "REGULAR", "2026-10-08T23:30:00Z", "BOS", "TOR")
    g3 = Game("2026020003", 2026, "REGULAR", "2026-10-11T23:00:00Z", "TOR", "BOS")
    f = team_schedule_features([g1, g2, g3])
    assert f[("TOR", "2026020002")].b2b and f[("TOR", "2026020002")].travel_km > 500
    assert not f[("TOR", "2026020003")].b2b and f[("TOR", "2026020003")].days_rest == 2


def test_simulator_league_average_is_plausible():
    o = simulate([GameRates(avg_rates(), avg_rates())], 20000, 3)[0]
    tie = (o.reg_h == o.reg_a).mean()
    assert 0.17 < tie < 0.28
    assert 5.6 < (o.fin_h + o.fin_a).mean() < 6.5
    assert 0.45 < o.home_win.mean() < 0.55


def test_pulled_goalie_creates_late_goals_both_ways():
    base = simulate([GameRates(avg_rates(), avg_rates())], 20000, 5)[0]
    no_pull = simulate([GameRates(avg_rates(), avg_rates())], 20000, 5,
                       {**CFG, "pull_trailing_by_1_seconds": 0, "pull_trailing_by_2_seconds": 0})[0]
    assert (base.reg_h + base.reg_a).mean() > (no_pull.reg_h + no_pull.reg_a).mean()
    # 6v5 comebacks raise regulation ties; empty-netters widen margins.
    assert (base.reg_h == base.reg_a).mean() > (no_pull.reg_h == no_pull.reg_a).mean()
    s_h, s_a = base.settlement()
    n_h, n_a = no_pull.settlement()
    assert (np.abs(s_h - s_a) >= 2).mean() > (np.abs(n_h - n_a) >= 2).mean()


def test_stronger_team_wins_more_and_is_deterministic():
    a = simulate([GameRates(avg_rates(1.2), avg_rates(0.85))], 5000, 11)[0]
    b = simulate([GameRates(avg_rates(1.2), avg_rates(0.85))], 5000, 11)[0]
    assert a.home_win.mean() > 0.6
    assert np.array_equal(a.fin_h, b.fin_h)


def test_pricing_consistency():
    o = simulate([GameRates(avg_rates(1.1), avg_rates())], 20000, 2)[0]
    gp = GamePricing.from_outcomes([o], [1.0])
    assert abs(sum(gp.p_reg) - 1) < 1e-9
    h = gp.outcome_probs(MarketType.PUCK_LINE, Selection.HOME, -1.5)
    a = gp.outcome_probs(MarketType.PUCK_LINE, Selection.AWAY, 1.5)
    assert abs(h[0] + a[0] - 1) < 1e-9
    over = gp.outcome_probs(MarketType.TOTAL, Selection.OVER, 6.0)
    assert over[1] > 0.05 and abs(sum(over) - 1) < 1e-9
    assert gp.p_home_ml > gp.p_reg[0]  # ML also collects OT/SO wins
    w, p, l = gp.outcome_probs(MarketType.ML, Selection.HOME)
    assert abs(1 / fair_decimal(w, p, l) - w) < 1e-9


def test_mixture_is_linear():
    o1 = simulate([GameRates(avg_rates(1.3), avg_rates())], 4000, 1)[0]
    o2 = simulate([GameRates(avg_rates(0.8), avg_rates())], 4000, 1)[0]
    mix = GamePricing.from_outcomes([o1, o2], [0.25, 0.75])
    p1 = GamePricing.from_outcomes([o1], [1]).p_home_ml
    p2 = GamePricing.from_outcomes([o2], [1]).p_home_ml
    assert abs(mix.p_home_ml - (0.25 * p1 + 0.75 * p2)) < 1e-9


def test_novig():
    assert american_to_decimal(-150) == pytest.approx(1.6667, abs=1e-4)
    assert decimal_to_american(2.5) == 150 and prob_to_american(0.6) == -150
    for method in ("multiplicative", "power"):
        q = devig([-150, 130], method)
        assert abs(sum(q) - 1) < 1e-9 and q[0] > 0.5
    assert overround([-110, -110]) == pytest.approx(0.0476, abs=1e-3)
    with pytest.raises(ValueError):
        devig([-110])


def test_goalie_start_states(league):
    store = league.store
    g = next(x for x in store.games if x.season == 2025 and x.start_time.month == 12)
    view = store.view(g.start_time - timedelta(hours=12))
    d = starter_distribution(view, g, g.home)
    assert abs(sum(p for _, p in d.probs) - 1) < 1e-6
    assert d.state in (GoalieState.UNKNOWN, GoalieState.PROJECTED)
    confirmed = GoalieReport(g.game_id, g.home, d.probs[-1][0], GoalieState.CONFIRMED, "t", g.start_time - timedelta(hours=1))
    s2 = HistoricalStore(games=store.games, goalie_stats=store.goalie_stats, goalie_reports=[confirmed])
    d2 = starter_distribution(s2.view(g.start_time - timedelta(minutes=30)), g, g.home)
    assert d2.state is GoalieState.CONFIRMED and d2.top == (confirmed.goalie_id, 0.985)
    # Before the report exists it is invisible.
    d3 = starter_distribution(s2.view(g.start_time - timedelta(hours=2)), g, g.home)
    assert d3.state is GoalieState.UNKNOWN


def test_goalie_start_b2b_favors_backup():
    from nhl.contracts import GoalieGameStats

    games, stats = [], []
    start = parse_ts("2026-10-01T23:00:00Z")
    for i in range(10):
        gid = f"20260200{i + 1:02d}"
        games.append(Game(gid, 2026, "REGULAR", start + timedelta(days=2 * i), "TOR", "MTL"))
        stats.append(GoalieGameStats(gid, "TOR", "8000001" if i % 5 else "8000002", True, 3600, 30, 3, 3.0, start + timedelta(days=2 * i, hours=4)))
    last = games[-1]
    nxt = Game("2026020099", 2026, "REGULAR", last.start_time + timedelta(hours=24), "BOS", "TOR")
    store = HistoricalStore(games=games + [nxt], goalie_stats=stats)
    normal = Game("2026020098", 2026, "REGULAR", last.start_time + timedelta(days=3), "BOS", "TOR")
    d_norm = starter_distribution(HistoricalStore(games=games + [normal], goalie_stats=stats).view(normal.start_time - timedelta(hours=3)), normal, "TOR")
    d_b2b = starter_distribution(store.view(nxt.start_time - timedelta(hours=3)), nxt, "TOR")
    assert d_norm.top[0] == "8000001"
    assert d_b2b.top[0] == "8000002"


def test_ratings_recover_truth_without_same_season_summaries(league):
    store = league.store
    view = store.view("2026-01-15T12:00:00Z")
    assert all(ps.season < 2025 for ps in view.player_seasons())
    tp = team_priors(view, league.rosters[2025], 2025, 2.6)
    r = fit_ratings(view, 2025, tp, goalie_priors(view, 2025), league_baselines(CFG))
    truth = league.truth["2025"]
    teams = sorted(truth["att"])
    assert np.corrcoef([r.att[t] for t in teams], [truth["att"][t] for t in teams])[0, 1] > 0.5
    assert np.corrcoef([r.dfn[t] for t in teams], [truth["dfn"][t] for t in teams])[0, 1] > 0.5


def test_price_slate_runs(league):
    store = league.store
    day_games = [g for g in store.games if g.season == 2025 and g.start_time.date().isoformat() == "2026-01-20"]
    assert day_games
    as_of = min(g.start_time for g in day_games) - timedelta(minutes=60)
    view = store.view(as_of)
    tp = team_priors(view, league.rosters[2025], 2025, 2.6)
    r = fit_ratings(view, 2025, tp, goalie_priors(view, 2025), league_baselines(CFG))
    priced, errors = price_slate(view, day_games, r, n_sims=2000)
    assert not errors and len(priced) == len(day_games)
    again, _ = price_slate(view, day_games, r, n_sims=2000)
    assert [p.pricing.p_home_ml for p in priced] == [p.pricing.p_home_ml for p in again]
    assert all(p.n_scenarios >= 1 for p in priced)
