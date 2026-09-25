"""Fair prices for ML, regulation 3-way, puck line, totals and team totals.

Probabilities are linear in the simulation distribution, so a goalie mixture is
priced by mixing the per-scenario PMFs with the starter weights.

Settlement conventions (verify per book before Phase 1E; recorded in each artifact):
* ML: includes OT and shootout.
* REG_3WAY: 60 minutes only.
* PUCK_LINE / TOTAL / TEAM_TOTAL: full game incl. OT; shootout winner +1 goal.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from nhl.contracts import MarketType, Selection
from nhl.models.game_state import SimOutcome

MAX_GOALS = 20


@dataclass
class GamePricing:
    """Mixed distributions for one game (all sum to 1)."""

    p_home_ml: float
    p_reg: tuple[float, float, float]  # HOME, DRAW, AWAY
    margin_pmf: dict[int, float]  # settlement home - away
    total_pmf: np.ndarray  # settlement total goals
    home_pmf: np.ndarray  # settlement home goals
    away_pmf: np.ndarray
    p_ot: float
    p_so: float
    mean_total: float

    @staticmethod
    def from_outcomes(outcomes: list[SimOutcome], weights: list[float]) -> "GamePricing":
        w = np.asarray(weights, dtype=float)
        w = w / w.sum()
        p_home = p_ot = p_so = 0.0
        p_reg = np.zeros(3)
        margin: dict[int, float] = {}
        total = np.zeros(2 * MAX_GOALS + 2)
        hp = np.zeros(MAX_GOALS + 1)
        ap = np.zeros(MAX_GOALS + 1)
        mean_total = 0.0
        for o, wi in zip(outcomes, w):
            n = len(o.reg_h)
            p_home += wi * float(o.home_win.mean())
            p_reg += wi * np.array([(o.reg_h > o.reg_a).mean(), (o.reg_h == o.reg_a).mean(), (o.reg_h < o.reg_a).mean()])
            p_ot += wi * float((o.end_type == 1).mean())
            p_so += wi * float((o.end_type == 2).mean())
            sh, sa = o.settlement()
            sh = np.minimum(sh, MAX_GOALS)
            sa = np.minimum(sa, MAX_GOALS)
            vals, counts = np.unique(sh - sa, return_counts=True)
            for v, c in zip(vals.tolist(), counts.tolist()):
                margin[v] = margin.get(v, 0.0) + wi * c / n
            total += wi * np.bincount(sh + sa, minlength=total.size)[: total.size] / n
            hp += wi * np.bincount(sh, minlength=hp.size)[: hp.size] / n
            ap += wi * np.bincount(sa, minlength=ap.size)[: ap.size] / n
            mean_total += wi * float((sh + sa).mean())
        return GamePricing(
            p_home_ml=p_home,
            p_reg=tuple(float(x) for x in p_reg),
            margin_pmf=dict(sorted(margin.items())),
            total_pmf=total,
            home_pmf=hp,
            away_pmf=ap,
            p_ot=p_ot,
            p_so=p_so,
            mean_total=mean_total,
        )

    # ---- outcome probabilities: (p_win, p_push, p_loss) for the selection
    def outcome_probs(
        self, market: MarketType, selection: Selection, line: float | None = None, team_is_home: bool | None = None
    ) -> tuple[float, float, float]:
        if market is MarketType.ML:
            p = self.p_home_ml if selection is Selection.HOME else 1.0 - self.p_home_ml
            return p, 0.0, 1.0 - p
        if market is MarketType.REG_3WAY:
            idx = {Selection.HOME: 0, Selection.DRAW: 1, Selection.AWAY: 2}[selection]
            return self.p_reg[idx], 0.0, 1.0 - self.p_reg[idx]
        if line is None:
            raise ValueError(f"{market.value} needs line")
        if market is MarketType.PUCK_LINE:
            # selection's line applies to that side: HOME -1.5 wins if margin - 1.5 > 0.
            sign = 1 if selection is Selection.HOME else -1
            win = push = 0.0
            for m, p in self.margin_pmf.items():
                adj = sign * m + line
                if adj > 0:
                    win += p
                elif adj == 0:
                    push += p
            return win, push, 1.0 - win - push
        if market is MarketType.TOTAL:
            return _over_under(self.total_pmf, line, selection)
        if market is MarketType.TEAM_TOTAL:
            if team_is_home is None:
                raise ValueError("TEAM_TOTAL needs team_is_home")
            return _over_under(self.home_pmf if team_is_home else self.away_pmf, line, selection)
        raise ValueError(market)


def _over_under(pmf: np.ndarray, line: float, selection: Selection) -> tuple[float, float, float]:
    goals = np.arange(pmf.size)
    over = float(pmf[goals > line].sum())
    push = float(pmf[goals == line].sum())
    under = 1.0 - over - push
    return (over, push, under) if selection is Selection.OVER else (under, push, over)


def no_push_prob(win: float, push: float, loss: float) -> float:
    denom = win + loss
    return win / denom if denom > 0 else 0.0


def fair_decimal(win: float, push: float, loss: float) -> float:
    """Decimal odds with zero EV given push-aware probabilities."""

    if win <= 0:
        return float("inf")
    return 1.0 + loss / win


def ev_per_unit(win: float, push: float, loss: float, decimal_odds: float) -> float:
    return win * (decimal_odds - 1.0) - loss
