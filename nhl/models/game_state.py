"""Game-state Monte Carlo simulator.

Replaces an independent full-game Poisson draw. Time advances in ``dt`` steps
through regulation; in each step each team's scoring and penalty hazard depends on
the *current* state:

* manpower: 5v5 / 4v4 / PP / SH, from minor-penalty timers (a PP goal ends the minor);
* score effects: trailing teams generate more at even strength, leading teams less,
  stronger in the 3rd period;
* pulled goalie: in the 3rd, a team trailing by 1 (2) pulls with <= 150s (210s) left,
  switching to 6v5 attack rates and giving the leader an empty-net hazard;
* after a regulation tie: 5 minutes of 3v3 OT (sudden death), then a shootout.

Scenarios (e.g. goalie-pair mixtures, several games) run as one vectorised batch.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from nhl.config import load
from nhl.features.ratings import Ratings

END_REG, END_OT, END_SO = 0, 1, 2


@dataclass(frozen=True)
class TeamRates:
    """Per-60 rates for ONE team, already adjusted for opponent and opposing goalie."""

    ev: float
    pp: float
    sh: float
    pen: float  # penalties TAKEN per 60
    six_v_five: float
    empty_net: float
    ot: float


@dataclass(frozen=True)
class GameRates:
    home: TeamRates
    away: TeamRates
    so_home: float = 0.5


@dataclass
class SimOutcome:
    reg_h: np.ndarray
    reg_a: np.ndarray
    fin_h: np.ndarray  # incl. OT goal, excl. shootout
    fin_a: np.ndarray
    end_type: np.ndarray
    so_home_win: np.ndarray
    p1_h: np.ndarray
    p1_a: np.ndarray

    @property
    def home_win(self) -> np.ndarray:
        return np.where(self.end_type == END_SO, self.so_home_win, self.fin_h > self.fin_a)

    def settlement(self) -> tuple[np.ndarray, np.ndarray]:
        """Sportsbook goals: shootout winner credited +1."""

        so = self.end_type == END_SO
        return self.fin_h + (so & self.so_home_win), self.fin_a + (so & ~self.so_home_win)


def team_rates(
    ratings: Ratings,
    team: str,
    opp: str,
    is_home: bool,
    b2b: bool,
    opp_b2b: bool,
    opp_goalie: str | None,
    cfg: dict,
) -> TeamRates:
    g_mult = ratings.goalie_mult(opp_goalie)
    ev = ratings.ev_rate(team, opp, is_home, b2b, opp_b2b) * g_mult
    strength = ev / (cfg["rate_5v5"] * g_mult)  # team-vs-opp strength relative to league, pre-goalie
    return TeamRates(
        ev=ev,
        pp=ratings.pp_rate(team, opp) * g_mult,
        sh=cfg["rate_sh"] * g_mult,
        pen=ratings.pen_rate(team, opp),
        six_v_five=cfg["rate_6v5_attack"] * strength * g_mult,
        empty_net=cfg["rate_empty_net"] * math.sqrt(strength),
        ot=cfg["rate_3v3"] * strength * g_mult,
    )


def _col(values: list[float]) -> np.ndarray:
    return np.asarray(values, dtype=float)[:, None]


def simulate(scenarios: list[GameRates], n_sims: int, seed: int, cfg: dict | None = None) -> list[SimOutcome]:
    cfg = cfg or load("simulator")
    m = len(scenarios)
    if m == 0:
        return []
    rng = np.random.default_rng(seed)
    dt = float(cfg["dt_seconds"])
    reg_len = int(cfg["regulation_seconds"])
    n_steps = int(round(reg_len / dt))
    p1_step = int(round(cfg["period_seconds"] / dt))
    per_step = dt / 3600.0

    H = {k: _col([getattr(s.home, k) for s in scenarios]) for k in TeamRates.__dataclass_fields__}
    A = {k: _col([getattr(s.away, k) for s in scenarios]) for k in TeamRates.__dataclass_fields__}
    shape = (m, n_sims)
    sh = np.zeros(shape, dtype=np.int32)
    sa = np.zeros(shape, dtype=np.int32)
    ph = np.zeros(shape)  # home penalty time remaining
    pa = np.zeros(shape)
    p1_h = p1_a = None
    mult_44 = cfg["rate_4v4_mult"]
    cap = cfg["score_effect_cap"]
    pull1, pull2 = cfg["pull_trailing_by_1_seconds"], cfg["pull_trailing_by_2_seconds"]

    for step in range(n_steps):
        t = step * dt
        period = int(t // cfg["period_seconds"])
        left = reg_len - t
        beta = cfg["score_effect_beta_p3"] if period >= 2 else cfg["score_effect_beta"]
        ph = np.maximum(ph - dt, 0.0)
        pa = np.maximum(pa - dt, 0.0)
        diff = sh - sa
        h_short = ph > 0
        a_short = pa > 0
        if period >= 2 and left <= pull2:
            pulled_h = ((diff == -1) & (left <= pull1)) | (diff == -2)
            pulled_a = ((diff == 1) & (left <= pull1)) | (diff == 2)
        else:
            pulled_h = pulled_a = np.zeros(shape, dtype=bool)

        even = h_short == a_short
        both_short = h_short & a_short
        ev_mult = np.where(both_short, mult_44, 1.0)
        se_h = np.exp(beta * np.clip(-diff, -cap, cap))
        se_a = np.exp(beta * np.clip(diff, -cap, cap))
        rate_h = np.where(even, H["ev"] * ev_mult * se_h, np.where(a_short, H["pp"], H["sh"]))
        rate_a = np.where(even, A["ev"] * ev_mult * se_a, np.where(h_short, A["pp"], A["sh"]))
        rate_h = np.where(pulled_h, np.maximum(rate_h, H["six_v_five"]), rate_h)
        rate_a = np.where(pulled_a, np.maximum(rate_a, A["six_v_five"]), rate_a)
        rate_h = np.where(pulled_a, H["empty_net"], rate_h)
        rate_a = np.where(pulled_h, A["empty_net"], rate_a)

        u_h = rng.random(shape)
        u_a = rng.random(shape)
        g_h = u_h < rate_h * per_step
        g_a = u_a < rate_a * per_step
        # A penalty can start only for a team not already serving one; split the same uniform.
        pen_h = (~g_h) & (~h_short) & (u_h < (rate_h + H["pen"]) * per_step)
        pen_a = (~g_a) & (~a_short) & (u_a < (rate_a + A["pen"]) * per_step)

        sh += g_h
        sa += g_a
        pa = np.where(g_h & a_short & ~h_short, 0.0, pa)  # PP goal ends the minor
        ph = np.where(g_a & h_short & ~a_short, 0.0, ph)
        ph = np.where(pen_h, float(cfg["minor_seconds"]), ph)
        pa = np.where(pen_a, float(cfg["minor_seconds"]), pa)
        if step == p1_step - 1:
            p1_h, p1_a = sh.copy(), sa.copy()

    tie = sh == sa
    lam = (H["ot"] + A["ot"]) / 3600.0
    p_ot_goal = 1.0 - np.exp(-lam * cfg["ot_seconds"])
    u1, u2, u3 = rng.random(shape), rng.random(shape), rng.random(shape)
    ot_goal = tie & (u1 < p_ot_goal)
    ot_home = ot_goal & (u2 < H["ot"] / (H["ot"] + A["ot"]))
    ot_away = ot_goal & ~ot_home
    so = tie & ~ot_goal
    so_p = _col([s.so_home for s in scenarios])
    so_home = so & (u3 < so_p)
    end_type = np.where(so, END_SO, np.where(ot_goal, END_OT, END_REG))

    out = []
    for i in range(m):
        out.append(
            SimOutcome(
                reg_h=sh[i], reg_a=sa[i],
                fin_h=sh[i] + ot_home[i], fin_a=sa[i] + ot_away[i],
                end_type=end_type[i], so_home_win=so_home[i],
                p1_h=p1_h[i], p1_a=p1_a[i],
            )
        )
    return out
