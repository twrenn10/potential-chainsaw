"""Odds conversion and vig removal."""

from __future__ import annotations

import math


def american_to_decimal(price: int | float) -> float:
    p = float(price)
    if -100 < p < 100:
        raise ValueError(f"invalid American price {price}")
    return 1.0 + (p / 100.0 if p > 0 else 100.0 / -p)


def decimal_to_american(dec: float) -> int:
    if not math.isfinite(dec) or dec <= 1.0:
        raise ValueError(f"invalid decimal odds {dec}")
    if dec >= 2.0:
        return int(round((dec - 1.0) * 100))
    return int(round(-100.0 / (dec - 1.0)))


def implied_prob(price: int | float) -> float:
    return 1.0 / american_to_decimal(price)


def prob_to_american(p: float) -> int:
    return decimal_to_american(1.0 / p)


def devig_multiplicative(prices: list[int | float]) -> list[float]:
    raw = [implied_prob(p) for p in prices]
    s = sum(raw)
    return [r / s for r in raw]


def devig_power(prices: list[int | float], tol: float = 1e-10) -> list[float]:
    """Find k with sum(q_i^k) = 1. Shades favourite-longshot bias more than multiplicative."""

    raw = [implied_prob(p) for p in prices]
    lo, hi = 0.5, 3.0
    for _ in range(200):
        k = (lo + hi) / 2
        s = sum(q**k for q in raw)
        if abs(s - 1.0) < tol:
            break
        if s > 1.0:
            lo = k
        else:
            hi = k
    out = [q**k for q in raw]
    s = sum(out)
    return [q / s for q in out]


def devig(prices: list[int | float], method: str = "multiplicative") -> list[float]:
    if len(prices) < 2:
        raise ValueError("need every outcome of the market to remove vig")
    if method == "multiplicative":
        return devig_multiplicative(prices)
    if method == "power":
        return devig_power(prices)
    raise ValueError(method)


def overround(prices: list[int | float]) -> float:
    return sum(implied_prob(p) for p in prices) - 1.0
