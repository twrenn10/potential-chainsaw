"""Deterministic synthetic league for offline development and pipeline tests.

Everything produced here is tagged ``data_origin=SYNTHETIC`` and governance
hard-blocks it (the tournament_v2 DEV_BOOTSTRAP_SOURCE rule). Results are drawn
from the same simulator family the model uses, so synthetic backtests verify
*mechanics* (no leakage, calibration plumbing, grading, CLV) -- NOT model validity.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np

from nhl.config import load
from nhl.contracts import (
    TEAMS,
    Game,
    GameResult,
    GoalieGameStats,
    GoalieReport,
    GoalieState,
    MarketType,
    OddsSnapshot,
    PlayerSeason,
    Selection,
    TeamGameStats,
)
from nhl.data.pit import HistoricalStore
from nhl.features.priors import RosterSlot
from nhl.market.novig import prob_to_american
from nhl.models.game_state import GameRates, TeamRates, simulate
from nhl.pricing.markets import GamePricing

TEAM_LIST = sorted(TEAMS)
LG_XG60 = 2.6
WEST = {"ANA", "LAK", "SJS", "SEA", "VAN", "VGK", "CGY", "EDM", "COL", "UTA"}


@dataclass
class SyntheticLeague:
    store: HistoricalStore
    rosters: dict[int, list[RosterSlot]]
    truth: dict[str, dict] = field(default_factory=dict)


def _schedule(rng: np.random.Generator, season: int, gid_offset: int = 0) -> list[Game]:
    need = {t: 82 for t in TEAM_LIST}
    games: list[Game] = []
    day = datetime(season, 10, 8, tzinfo=timezone.utc)
    n = 0
    while sum(need.values()) > 1 and day < datetime(season + 1, 5, 1, tzinfo=timezone.utc):
        avail = [t for t in TEAM_LIST if need[t] > 0]
        w = np.array([need[t] for t in avail], dtype=float)
        k = min(len(avail) // 2, int(rng.integers(5, 10)))
        chosen = list(rng.choice(avail, size=2 * k, replace=False, p=w / w.sum()))
        for i in range(k):
            home, away = chosen[2 * i], chosen[2 * i + 1]
            n += 1
            hour = 2 if home in WEST else 23
            start = day + timedelta(days=1 if hour == 2 else 0, hours=hour)
            games.append(
                Game(
                    game_id=f"{season}02{n + gid_offset:04d}", season=season, game_type="REGULAR",
                    start_time=start, home=home, away=away, venue=f"{home} Arena",
                    available_at=datetime(season, 7, 1, tzinfo=timezone.utc),
                )
            )
            need[home] -= 1
            need[away] -= 1
        day += timedelta(days=1)
    return games


def generate(
    seasons: tuple[int, ...] = (2024, 2025),
    seed: int = 7,
    odds_seasons: tuple[int, ...] | None = None,
    odds_sims: int = 1500,
) -> SyntheticLeague:
    rng = np.random.default_rng(seed)
    cfg = load("simulator")
    odds_seasons = odds_seasons if odds_seasons is not None else seasons[-1:]
    store = HistoricalStore(data_origin="SYNTHETIC")
    rosters: dict[int, list[RosterSlot]] = {}
    truth: dict[str, dict] = {}

    # Players with persistent talent; ~30% move team each offseason.
    n_per = 18
    players = []
    pid = 8470000
    for t in TEAM_LIST:
        for j in range(n_per):
            pid += 1
            players.append({"id": str(pid), "team": t, "pos": "D" if j >= 12 else "F",
                            "f": rng.normal(0, 0.25), "a": rng.normal(0, 0.25)})
    goalies = []
    for t in TEAM_LIST:
        for j in range(2):
            pid += 1
            goalies.append({"id": str(pid), "team": t, "g": rng.normal(0, 0.06), "starter": j == 0})

    for s_idx, season in enumerate(seasons):
        if s_idx > 0:
            for p in players:
                if rng.random() < 0.3:
                    p["team"] = TEAM_LIST[int(rng.integers(len(TEAM_LIST)))]
        roster = []
        att, dfn = {}, {}
        for t in TEAM_LIST:
            mine = [p for p in players if p["team"] == t]
            att[t] = math.log(1 + np.mean([p["f"] for p in mine]) / LG_XG60 * 1.5) + rng.normal(0, 0.03)
            dfn[t] = math.log(1 + np.mean([p["a"] for p in mine]) / LG_XG60 * 1.5) + rng.normal(0, 0.03)
            for p in mine:
                roster.append(RosterSlot(t, p["id"], p["pos"], 14.0 if p["pos"] == "F" else 19.0))
        rosters[season] = roster
        pp = {t: rng.normal(0, 0.10) for t in TEAM_LIST}
        pk = {t: rng.normal(0, 0.10) for t in TEAM_LIST}
        take = {t: rng.normal(0, 0.10) for t in TEAM_LIST}
        truth[str(season)] = {"att": att, "dfn": dfn, "pp": pp, "pk": pk, "take": take,
                              "goalie": {g["id"]: g["g"] for g in goalies}}

        games = _schedule(rng, season)
        store.games.extend(games)
        last_start: dict[str, tuple[datetime, str]] = {}
        starters: dict[str, tuple[str, str]] = {}
        scen: list[GameRates] = []
        for g in games:
            pair = []
            for team in (g.home, g.away):
                tg = [x for x in goalies if x["team"] == team]
                starter, backup = tg[0]["id"], tg[1]["id"]
                prev = last_start.get(team)
                if prev and (g.start_time - prev[0]) < timedelta(hours=30) and prev[1] == starter:
                    pick = backup
                else:
                    pick = starter if rng.random() < 0.72 else backup
                last_start[team] = (g.start_time, pick)
                pair.append(pick)
            starters[g.game_id] = (pair[0], pair[1])
            gm = {x["id"]: x["g"] for x in goalies}
            home_adv = 0.035

            def tr(team: str, opp: str, is_home: bool, opp_goalie: str) -> TeamRates:
                gmul = math.exp(-gm[opp_goalie])
                ev = LG_XG60 * math.exp(att[team] + dfn[opp] + (home_adv if is_home else 0.0)) * gmul
                strength = ev / (LG_XG60 * gmul)
                return TeamRates(
                    ev=ev, pp=cfg["rate_pp"] * math.exp(pp[team] + pk[opp]) * gmul, sh=cfg["rate_sh"] * gmul,
                    pen=cfg["penalty_rate"] * math.exp(take[team]), six_v_five=cfg["rate_6v5_attack"] * strength * gmul,
                    empty_net=cfg["rate_empty_net"] * math.sqrt(strength), ot=cfg["rate_3v3"] * strength * gmul,
                )

            scen.append(GameRates(tr(g.home, g.away, True, pair[1]), tr(g.away, g.home, False, pair[0])))

        outs = simulate(scen, 1, seed + season, cfg)
        for g, o, sc in zip(games, outs, scen):
            end = int(o.end_type[0])
            res = GameResult(
                game_id=g.game_id, home_goals=int(o.fin_h[0]), away_goals=int(o.fin_a[0]),
                reg_home_goals=int(o.reg_h[0]), reg_away_goals=int(o.reg_a[0]),
                p1_home_goals=int(o.p1_h[0]), p1_away_goals=int(o.p1_a[0]),
                end_type=["REG", "OT", "SO"][end],
                shootout_winner=("HOME" if bool(o.so_home_win[0]) else "AWAY") if end == 2 else None,
                available_at=g.start_time + timedelta(hours=4),
            )
            store.results.append(res)
            avail = datetime.strptime(g.start_time.strftime("%Y%m%d"), "%Y%m%d").replace(tzinfo=timezone.utc) + timedelta(days=1, hours=12)
            if avail <= g.start_time + timedelta(hours=4):
                avail += timedelta(days=1)
            hg, ag = starters[g.game_id]
            for team, opp, is_home, rates, opp_rates, gf, ga in (
                (g.home, g.away, True, sc.home, sc.away, res.home_goals, res.away_goals),
                (g.away, g.home, False, sc.away, sc.home, res.away_goals, res.home_goals),
            ):
                opp_goalie = ag if is_home else hg
                own_goalie = hg if is_home else ag
                raw_ev = rates.ev / math.exp(-truth[str(season)]["goalie"][opp_goalie])
                toi5 = float(rng.normal(2880, 120))
                xg5 = float(rng.gamma(20, raw_ev * toi5 / 3600 / 20))
                pens = int(rng.poisson(opp_rates.pen))
                toi_pp = max(pens * 95.0, 0.0)
                xg_pp = float(rng.gamma(10, max(rates.pp, 0.1) / math.exp(-truth[str(season)]["goalie"][opp_goalie]) * toi_pp / 3600 / 10)) if toi_pp > 0 else 0.0
                base = dict(game_id=g.game_id, team=team, opponent=opp, is_home=is_home, game_date=g.start_time.strftime("%Y%m%d"),
                            goals_for=gf, goals_against=ga, sog_for=int(rng.poisson(29)), sog_against=int(rng.poisson(29)),
                            available_at=avail, source="synthetic")
                store.team_stats.append(TeamGameStats(situation="5on5", toi_sec=toi5, xg_for=xg5, xg_against=0.0,
                                                      penalties_for=0, penalties_against=0, **base))
                if toi_pp > 0:
                    store.team_stats.append(TeamGameStats(situation="5on4", toi_sec=toi_pp, xg_for=xg_pp, xg_against=0.0,
                                                          penalties_for=0, penalties_against=0, **base))
                store.team_stats.append(TeamGameStats(situation="all", toi_sec=3600.0, xg_for=xg5 + xg_pp + 0.25, xg_against=0.0,
                                                      penalties_for=int(rng.poisson(rates.pen)), penalties_against=pens, **base))
                own_xga = float(rng.gamma(20, (opp_rates.ev / math.exp(-truth[str(season)]["goalie"][own_goalie]) * 0.8 + 0.6) / 20))
                store.goalie_stats.append(GoalieGameStats(game_id=g.game_id, team=team, goalie_id=own_goalie, started=True,
                                                          toi_sec=3600.0, shots_against=int(rng.poisson(29)), goals_against=ga,
                                                          xg_against=own_xga, available_at=avail))
                # Starter reports: confirmed ~T-90m (60%), projected ~T-6h (30%), none (10%).
                u = rng.random()
                if u < 0.6:
                    store.goalie_reports.append(GoalieReport(g.game_id, team, own_goalie, GoalieState.CONFIRMED, "synthetic", g.start_time - timedelta(minutes=90)))
                elif u < 0.9:
                    tg = [x["id"] for x in goalies if x["team"] == team]
                    named = own_goalie if rng.random() < 0.9 else [x for x in tg if x != own_goalie][0]
                    store.goalie_reports.append(GoalieReport(g.game_id, team, named, GoalieState.PROJECTED, "synthetic", g.start_time - timedelta(hours=6)))

        # Player-season summaries become visible after the season.
        for p in players:
            t = p["team"]
            store.player_seasons.append(
                PlayerSeason(
                    player_id=p["id"], season=season, team=t, position=p["pos"], toi_5v5_min=float(rng.normal(1000, 150)),
                    on_ice_xgf60_rel=LG_XG60 * (math.exp(att[t]) - 1) + 0.4 * p["f"] + float(rng.normal(0, 0.1)),
                    on_ice_xga60_rel=LG_XG60 * (math.exp(dfn[t]) - 1) + 0.4 * p["a"] + float(rng.normal(0, 0.1)),
                    available_at=datetime(season + 1, 7, 1, tzinfo=timezone.utc),
                )
            )

        if season in odds_seasons:
            _synthetic_odds(store, games, scen, rng, seed + 1000 + season, odds_sims)

    store.games.sort(key=lambda g: (g.start_time, g.game_id))
    return SyntheticLeague(store=store, rosters=rosters, truth=truth)


def _synthetic_odds(
    store: HistoricalStore, games: list[Game], scen: list[GameRates], rng: np.random.Generator, seed: int, n_sims: int
) -> None:
    """Market = truth + noise (open noisier than close) + ~4.5% vig."""

    outs = simulate(scen, n_sims, seed)
    vig = 0.0225

    def price_pair(p: float, noise_sd: float) -> tuple[int, int]:
        lp = math.log(p / (1 - p)) + rng.normal(0, noise_sd)
        q = 1 / (1 + math.exp(-lp))
        return prob_to_american(min(q + vig, 0.97)), prob_to_american(min(1 - q + vig, 0.97))

    for g, o in zip(games, outs):
        gp = GamePricing.from_outcomes([o], [1.0])
        marks = [(g.start_time - timedelta(hours=20), 0.20), (g.start_time - timedelta(hours=8), 0.14),
                 (g.start_time - timedelta(minutes=60), 0.09), (g.start_time - timedelta(minutes=5), 0.06)]
        fav_home = gp.p_home_ml >= 0.5
        pl_line = -1.5 if fav_home else 1.5
        tot_line = 6.5 if gp.mean_total >= 5.9 else 5.5
        for ts, sd in marks:
            rows = []
            h, a = price_pair(gp.p_home_ml, sd)
            rows += [(MarketType.ML, Selection.HOME, None, None, h), (MarketType.ML, Selection.AWAY, None, None, a)]
            pw = gp.outcome_probs(MarketType.PUCK_LINE, Selection.HOME, pl_line)[0]
            h, a = price_pair(pw, sd)
            rows += [(MarketType.PUCK_LINE, Selection.HOME, pl_line, None, h), (MarketType.PUCK_LINE, Selection.AWAY, -pl_line, None, a)]
            po = gp.outcome_probs(MarketType.TOTAL, Selection.OVER, tot_line)[0]
            ov, un = price_pair(po, sd)
            rows += [(MarketType.TOTAL, Selection.OVER, tot_line, None, ov), (MarketType.TOTAL, Selection.UNDER, tot_line, None, un)]
            for team, is_home in ((g.home, True), (g.away, False)):
                pt = gp.outcome_probs(MarketType.TEAM_TOTAL, Selection.OVER, 2.5, team_is_home=is_home)[0]
                ov, un = price_pair(pt, sd)
                rows += [(MarketType.TEAM_TOTAL, Selection.OVER, 2.5, team, ov), (MarketType.TEAM_TOTAL, Selection.UNDER, 2.5, team, un)]
            # 3-way: perturb then add vig.
            lp = np.log(np.array(gp.p_reg)) + rng.normal(0, sd, 3)
            q = np.exp(lp) / np.exp(lp).sum()
            for sel, qi in zip((Selection.HOME, Selection.DRAW, Selection.AWAY), q):
                rows.append((MarketType.REG_3WAY, sel, None, None, prob_to_american(min(qi * 1.05, 0.97))))
            for market, sel, line, team, price in rows:
                store.odds.append(OddsSnapshot(snapshot_ts=ts, book="synthbook", game_id=g.game_id, market=market,
                                               selection=sel, price_american=price, line=line, team=team,
                                               max_stake=500.0, source="synthetic", snapshot_id="synthetic"))
