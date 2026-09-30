"""Joint MAP team / special-teams / penalty / goalie ratings.

Instead of multiplying separately-estimated factors (which double counts: season
xGA already contains some home/rest/goalie signal), each rate is a single
log-linear Poisson model whose effects are estimated *together*, with Gaussian
priors (hierarchical shrinkage toward player-based priors):

  5v5 xG_for(i vs j)  = toi * exp(mu + home*[i home] + b2b_own*[i b2b] + b2b_opp*[j b2b] + att_i + dfn_j)
  PP xG_for(i vs j)   = toi_5on4 * exp(mu_pp + pp_i + pk_j)
  penalties_taken(i)  = toi_all  * exp(mu_pen + take_i + draw_j)
  goals_against(k)    = xGA_k * exp(-g_k)                          (goalie k)

xG is modelled for team strength (less noisy than goals); goaltending enters only
as goals|xG, so team defence and goalie skill are not double counted.
Fits use only rows visible in the ``PointInTimeView`` for the target season,
exponentially time-decayed; prior seasons enter through the priors.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from nhl.contracts import TEAMS
from nhl.data.pit import PointInTimeView, assert_visible

from .priors import GoaliePrior, TeamPrior
from .rest_travel import team_schedule_features

TEAM_LIST = sorted(TEAMS)
T_IDX = {t: i for i, t in enumerate(TEAM_LIST)}


@dataclass
class FitConfig:
    half_life_days: float = 90.0
    team_prior_sd: float = 0.07
    team_prior_sd_no_prior: float = 0.12
    pp_prior_sd: float = 0.10
    pen_prior_sd: float = 0.10
    goalie_prior_sd: float = 0.06
    home_prior: tuple[float, float] = (0.035, 0.05)
    goals_per_xg_pseudo_xg: float = 300.0  # shrink league xG->goals factor toward 1
    b2b_prior: tuple[float, float] = (0.0, 0.10)
    newton_iters: int = 25


@dataclass
class Ratings:
    as_of: str
    mu_5v5: float
    home: float
    b2b_own: float
    b2b_opp: float
    att: dict[str, float]
    dfn: dict[str, float]
    mu_pp: float
    pp: dict[str, float]
    pk: dict[str, float]
    mu_pen: float
    take: dict[str, float]
    draw: dict[str, float]
    goalie: dict[str, float]  # relative to league (league intercept removed)
    goals_per_xg_5v5: float = 1.0
    goals_per_xg_pp: float = 1.0
    n_rows: dict[str, int] = field(default_factory=dict)

    def ev_rate(self, team: str, opp: str, is_home: bool, b2b: bool, opp_b2b: bool) -> float:
        eta = self.mu_5v5 + self.att[team] + self.dfn[opp] + (self.home if is_home else 0.0)
        eta += (self.b2b_own if b2b else 0.0) + (self.b2b_opp if opp_b2b else 0.0)
        return math.exp(eta)

    def pp_rate(self, team: str, opp: str) -> float:
        return math.exp(self.mu_pp + self.pp[team] + self.pk[opp])

    def pen_rate(self, team: str, opp: str) -> float:
        return math.exp(self.mu_pen + self.take[team] + self.draw[opp])

    def goalie_mult(self, goalie_id: str | None) -> float:
        """Multiplier on the OPPONENT's goals per xG. Unknown goalie -> league average."""

        return math.exp(-self.goalie.get(goalie_id or "", 0.0))


def fit_log_linear(
    X: np.ndarray,
    y: np.ndarray,
    offset: np.ndarray,
    w: np.ndarray,
    prior_mean: np.ndarray,
    prior_sd: np.ndarray,
    iters: int = 25,
) -> np.ndarray:
    """MAP estimate of a weighted Poisson log-linear model with Gaussian priors (Newton)."""

    prec = 1.0 / prior_sd**2
    theta = prior_mean.copy()
    for _ in range(iters):
        eta = np.clip(offset + X @ theta, -30, 30)
        mu = np.exp(eta)
        grad = X.T @ (w * (mu - y)) + prec * (theta - prior_mean)
        hess = (X * (w * mu)[:, None]).T @ X + np.diag(prec)
        step = np.linalg.solve(hess, grad)
        theta = theta - step
        if np.max(np.abs(step)) < 1e-8:
            break
    return theta


def _decay(age_days: np.ndarray, half_life: float) -> np.ndarray:
    return 0.5 ** (np.maximum(age_days, 0.0) / half_life)


def fit_ratings(
    view: PointInTimeView,
    season: int,
    team_priors: dict[str, TeamPrior],
    goalie_priors: dict[str, GoaliePrior],
    league: dict[str, float],
    cfg: FitConfig | None = None,
) -> Ratings:
    cfg = cfg or FitConfig()
    stats = [s for s in view.team_stats() if int(s.game_id[:4]) == season]
    assert_visible(stats, view.as_of, "team_stats")
    sched = team_schedule_features(view.games())
    game_start = {g.game_id: g.start_time.timestamp() for g in view.games()}
    # Decay reference = newest visible data, NOT the clock: ratings are a function of the
    # information available, so re-running later with no new data gives identical values.
    data_times = [game_start[s.game_id] for s in stats if s.game_id in game_start]
    as_of_ts = max(data_times) if data_times else view.as_of.timestamp()
    n_t = len(TEAM_LIST)

    def age(gid: str) -> float:
        return (as_of_ts - game_start.get(gid, as_of_ts)) / 86400.0

    # ---- 5v5 team model: [mu, home, b2b_own, b2b_opp, att(32), dfn(32)]
    rows = [s for s in stats if s.situation == "5on5" and s.toi_sec > 0]
    k = 4 + 2 * n_t
    X = np.zeros((len(rows), k))
    y = np.zeros(len(rows))
    off = np.zeros(len(rows))
    ages = np.zeros(len(rows))
    for r, s in enumerate(rows):
        X[r, 0] = 1.0
        X[r, 1] = 1.0 if s.is_home else 0.0
        own = sched.get((s.team, s.game_id))
        opp = sched.get((s.opponent, s.game_id))
        X[r, 2] = 1.0 if own and own.b2b else 0.0
        X[r, 3] = 1.0 if opp and opp.b2b else 0.0
        X[r, 4 + T_IDX[s.team]] = 1.0
        X[r, 4 + n_t + T_IDX[s.opponent]] = 1.0
        y[r] = s.xg_for
        off[r] = math.log(s.toi_sec / 3600.0)
        ages[r] = age(s.game_id)
    pm = np.zeros(k)
    ps = np.zeros(k)
    pm[0], ps[0] = math.log(league["rate_5v5"]), 0.5
    pm[1], ps[1] = cfg.home_prior
    pm[2], ps[2] = cfg.b2b_prior
    pm[3], ps[3] = cfg.b2b_prior
    for t, i in T_IDX.items():
        pr = team_priors.get(t)
        sd = cfg.team_prior_sd if pr else cfg.team_prior_sd_no_prior
        pm[4 + i], ps[4 + i] = (pr.att if pr else 0.0), sd
        pm[4 + n_t + i], ps[4 + n_t + i] = (pr.dfn if pr else 0.0), sd
    theta = fit_log_linear(X, y, off, _decay(ages, cfg.half_life_days), pm, ps, cfg.newton_iters) if len(rows) else pm

    # ---- PP model on 5on4 rows: [mu_pp, pp(32), pk(32)]
    mu_pp, pp, pk = _fit_pair_model(
        [(s.team, s.opponent, s.xg_for, s.toi_sec, age(s.game_id)) for s in stats if s.situation == "5on4" and s.toi_sec > 0],
        math.log(league["rate_pp_xg"]), cfg.pp_prior_sd, cfg,
    )
    # ---- penalties taken on 'all' rows: [mu_pen, take(32), draw(32)]
    mu_pen, take, draw = _fit_pair_model(
        [(s.team, s.opponent, float(s.penalties_for), s.toi_sec, age(s.game_id)) for s in stats if s.situation == "all" and s.toi_sec > 0],
        math.log(league["penalty_rate"]), cfg.pen_prior_sd, cfg,
    )

    # ---- league goals-per-xG at 5v5 and PP (xG models drift by season)
    def goals_per_xg(situation: str) -> float:
        rs = [s for s in stats if s.situation == situation]
        w = _decay(np.array([age(s.game_id) for s in rs]), cfg.half_life_days) if rs else np.zeros(0)
        g = float(np.sum(w * np.array([s.goals_for for s in rs]))) if rs else 0.0
        x = float(np.sum(w * np.array([s.xg_for for s in rs]))) if rs else 0.0
        k = cfg.goals_per_xg_pseudo_xg
        return (g + k) / (x + k)

    k5, kpp = goals_per_xg("5on5"), goals_per_xg("5on4")

    # ---- goalies: goals_against = xGA * exp(c - g_k); c = league intercept, g relative
    g_rows = [
        g for g in view.goalie_stats()
        if int(g.game_id[:4]) == season and g.xg_against > 0 and not math.isnan(g.xg_against)
    ]
    ids = sorted({g.goalie_id for g in g_rows} | set(goalie_priors))
    goalie: dict[str, float] = {gid: goalie_priors[gid].g if gid in goalie_priors else 0.0 for gid in ids}
    if g_rows:
        gi = {gid: i + 1 for i, gid in enumerate(ids)}
        Xg = np.zeros((len(g_rows), len(ids) + 1))
        yg = np.zeros(len(g_rows))
        og = np.zeros(len(g_rows))
        ag = np.zeros(len(g_rows))
        for r, g in enumerate(g_rows):
            Xg[r, 0] = 1.0
            Xg[r, gi[g.goalie_id]] = -1.0
            yg[r] = g.goals_against
            og[r] = math.log(g.xg_against)
            ag[r] = age(g.game_id)
        pmg = np.array([0.0] + [goalie[gid] for gid in ids])
        psg = np.array([0.3] + [cfg.goalie_prior_sd] * len(ids))
        th = fit_log_linear(Xg, yg, og, _decay(ag, cfg.half_life_days * 2), pmg, psg, cfg.newton_iters)
        goalie = {gid: float(th[gi[gid]]) for gid in ids}

    return Ratings(
        as_of=view.as_of.isoformat(),
        mu_5v5=float(theta[0]),
        home=float(theta[1]),
        b2b_own=float(theta[2]),
        b2b_opp=float(theta[3]),
        att={t: float(theta[4 + i]) for t, i in T_IDX.items()},
        dfn={t: float(theta[4 + n_t + i]) for t, i in T_IDX.items()},
        mu_pp=mu_pp, pp=pp, pk=pk, mu_pen=mu_pen, take=take, draw=draw,
        goalie=goalie,
        goals_per_xg_5v5=k5,
        goals_per_xg_pp=kpp,
        n_rows={"5on5": len(rows), "goalie": len(g_rows)},
    )


def _fit_pair_model(
    rows: list[tuple[str, str, float, float, float]], mu0: float, sd: float, cfg: FitConfig
) -> tuple[float, dict[str, float], dict[str, float]]:
    n_t = len(TEAM_LIST)
    k = 1 + 2 * n_t
    pm = np.zeros(k)
    ps = np.full(k, sd)
    pm[0], ps[0] = mu0, 0.5
    if not rows:
        theta = pm
    else:
        X = np.zeros((len(rows), k))
        y = np.zeros(len(rows))
        off = np.zeros(len(rows))
        ages = np.zeros(len(rows))
        for r, (team, opp, val, toi, a) in enumerate(rows):
            X[r, 0] = 1.0
            X[r, 1 + T_IDX[team]] = 1.0
            X[r, 1 + n_t + T_IDX[opp]] = 1.0
            y[r] = val
            off[r] = math.log(toi / 3600.0)
            ages[r] = a
        theta = fit_log_linear(X, y, off, _decay(ages, cfg.half_life_days), pm, ps, cfg.newton_iters)
    return (
        float(theta[0]),
        {t: float(theta[1 + i]) for t, i in T_IDX.items()},
        {t: float(theta[1 + n_t + i]) for t, i in T_IDX.items()},
    )


def league_baselines(sim_cfg: dict) -> dict[str, float]:
    """League-average rates used as prior means. PP xG/60 ~ PP goals/60 at league level."""

    return {
        "rate_5v5": sim_cfg["rate_5v5"],
        "rate_pp_xg": sim_cfg["rate_pp"],
        "penalty_rate": sim_cfg["penalty_rate"],
    }


def summarize(r: Ratings, top: int = 5) -> dict[str, list[tuple[str, float]]]:
    net = {t: r.att[t] - r.dfn[t] for t in TEAM_LIST}
    order = sorted(net.items(), key=lambda kv: -kv[1])
    return {"best": order[:top], "worst": order[-top:]}

