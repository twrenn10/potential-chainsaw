"""Odds conversion and vig removal."""

from __future__ import annotations

import math
from dataclasses import dataclass


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


class MalformedMarket(ValueError):
    """A quote group that must not be normalised (incomplete, duplicated, impossible)."""


@dataclass(frozen=True)
class NoVigResult:
    """Everything needed to reproduce a no-vig calculation.

    ``raw_implied`` = 1/decimal per outcome (includes the book's margin);
    ``no_vig`` = the same outcomes after removing the margin with ``method``.
    """

    selections: tuple[str, ...]
    decimals: tuple[float, ...]
    raw_implied: tuple[float, ...]
    overround: float
    method: str
    no_vig: tuple[float, ...]
    k: float | None = None  # power-method exponent

    def raw(self, selection: str) -> float:
        return self.raw_implied[self.selections.index(selection)]

    def fair(self, selection: str) -> float:
        return self.no_vig[self.selections.index(selection)]


def _power_k(raw: list[float], tol: float = 1e-12) -> float:
    lo, hi = 0.5, 3.0
    k = 1.0
    for _ in range(200):
        k = (lo + hi) / 2
        s = sum(q**k for q in raw)
        if abs(s - 1.0) < tol:
            break
        if s > 1.0:
            lo = k
        else:
            hi = k
    return k


def no_vig(
    selections: list[str],
    decimals: list[float],
    method: str = "multiplicative",
    min_overround: float = 0.0,
    max_overround: float | None = None,
) -> NoVigResult:
    """Explicit no-vig transform for a COMPLETE market (2-way or 3-way).

    Rejects (never normalises): fewer than two outcomes, duplicated selections,
    non-finite or <= 1.0 decimal prices, an overround below ``min_overround`` (a
    book offering more than 100% back is stale or erroneous) or above
    ``max_overround``.
    """

    if len(selections) < 2 or len(selections) != len(decimals):
        raise MalformedMarket("need every outcome of the market")
    if len(set(selections)) != len(selections):
        raise MalformedMarket(f"duplicated selections {selections}")
    if len(selections) > 3:
        raise MalformedMarket("only 2-way and 3-way markets are supported")
    for d in decimals:
        if not math.isfinite(d) or d <= 1.0:
            raise MalformedMarket(f"impossible decimal price {d}")
    raw = [1.0 / d for d in decimals]
    over = sum(raw) - 1.0
    if over < min_overround - 1e-12:
        raise MalformedMarket(f"overround {over:.4f} below {min_overround}")
    if max_overround is not None and over > max_overround:
        raise MalformedMarket(f"overround {over:.4f} above {max_overround}")
    k = None
    if method == "multiplicative":
        probs = [q / sum(raw) for q in raw]
    elif method == "power":
        k = _power_k(raw)
        powered = [q**k for q in raw]
        probs = [q / sum(powered) for q in powered]
    else:
        raise MalformedMarket(f"unknown no-vig method {method!r}")
    return NoVigResult(tuple(selections), tuple(decimals), tuple(raw), over, method, tuple(probs), k)


def decimal_to_implied(dec: float) -> float:
    if not math.isfinite(dec) or dec <= 1.0:
        raise ValueError(f"invalid decimal odds {dec}")
    return 1.0 / dec
