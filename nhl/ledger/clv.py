"""Closing-line value.

* ``clv_ev``: EV per unit of the price taken, evaluated at the no-vig CLOSING
  probability (bet_decimal * p_close - 1). Positive = beat the close.
* ``edge_at_close``: model probability minus no-vig closing probability. A bet can
  win while the market moved against us; that bet should not reinforce the model.
"""

from __future__ import annotations

from dataclasses import dataclass

from nhl.market.novig import american_to_decimal


@dataclass(frozen=True)
class CLV:
    clv_ev: float
    prob_move: float  # p_close - p_at_bet (no-vig); + = market moved toward us
    edge_at_bet: float
    edge_at_close: float


def compute_clv(bet_price: int, p_nv_at_bet: float, p_nv_close: float, model_p: float) -> CLV:
    return CLV(
        clv_ev=american_to_decimal(bet_price) * p_nv_close - 1.0,
        prob_move=p_nv_close - p_nv_at_bet,
        edge_at_bet=model_p - p_nv_at_bet,
        edge_at_close=model_p - p_nv_close,
    )
