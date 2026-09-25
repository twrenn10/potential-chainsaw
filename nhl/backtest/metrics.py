"""Probabilistic scoring: log loss, Brier, calibration, blend weight vs the market."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

EPS = 1e-6


def _clip(p: np.ndarray) -> np.ndarray:
    return np.clip(p, EPS, 1 - EPS)


def logit(p: np.ndarray) -> np.ndarray:
    p = _clip(p)
    return np.log(p / (1 - p))


def log_loss(y: np.ndarray, p: np.ndarray) -> float:
    p = _clip(p)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def brier(y: np.ndarray, p: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def multiclass_log_loss(y_idx: np.ndarray, P: np.ndarray) -> float:
    P = _clip(P / P.sum(axis=1, keepdims=True))
    return float(-np.mean(np.log(P[np.arange(len(y_idx)), y_idx])))


def ece(y: np.ndarray, p: np.ndarray, bins: int = 10) -> float:
    edges = np.linspace(0, 1, bins + 1)
    idx = np.clip(np.digitize(p, edges) - 1, 0, bins - 1)
    total = 0.0
    for b in range(bins):
        m = idx == b
        if m.any():
            total += m.mean() * abs(p[m].mean() - y[m].mean())
    return float(total)


def calibration_table(y: np.ndarray, p: np.ndarray, bins: int = 10) -> list[dict[str, float]]:
    edges = np.linspace(0, 1, bins + 1)
    idx = np.clip(np.digitize(p, edges) - 1, 0, bins - 1)
    rows = []
    for b in range(bins):
        m = idx == b
        if m.any():
            rows.append({"bin_lo": float(edges[b]), "bin_hi": float(edges[b + 1]), "n": int(m.sum()),
                         "mean_p": float(p[m].mean()), "hit_rate": float(y[m].mean())})
    return rows


def _logistic_fit(X: np.ndarray, y: np.ndarray, iters: int = 50, ridge: float = 1e-6) -> np.ndarray:
    beta = np.zeros(X.shape[1])
    for _ in range(iters):
        z = np.clip(X @ beta, -30, 30)
        mu = 1 / (1 + np.exp(-z))
        grad = X.T @ (mu - y) + ridge * beta
        H = (X * (mu * (1 - mu))[:, None]).T @ X + ridge * np.eye(X.shape[1])
        step = np.linalg.solve(H, grad)
        beta -= step
        if np.max(np.abs(step)) < 1e-10:
            break
    return beta


def calibration_slope(y: np.ndarray, p: np.ndarray) -> tuple[float, float]:
    """Fit y ~ a + b*logit(p). Perfect calibration: a=0, b=1. b<1 = overconfident."""

    X = np.column_stack([np.ones_like(p), logit(p)])
    a, b = _logistic_fit(X, y)
    return float(a), float(b)


def blend_weight(y: np.ndarray, p_market: np.ndarray, p_model: np.ndarray) -> float:
    """w in logit(p) = logit(p_mkt) + w*(logit(p_model) - logit(p_mkt)), fit by ML.

    w > 0 means the model adds information beyond the market.
    """

    base = logit(p_market)
    delta = logit(p_model) - base
    w = 0.0
    for _ in range(50):
        z = np.clip(base + w * delta, -30, 30)
        mu = 1 / (1 + np.exp(-z))
        g = float(np.sum((mu - y) * delta))
        h = float(np.sum(mu * (1 - mu) * delta**2)) + 1e-9
        step = g / h
        w -= step
        if abs(step) < 1e-10:
            break
    return float(w)


@dataclass
class BinaryReport:
    n: int
    log_loss_model: float
    log_loss_close: float
    brier_model: float
    brier_close: float
    ece_model: float
    calib_intercept: float
    calib_slope: float
    blend_w: float
    blend_w_ci: tuple[float, float]
    delta_log_loss_ci: tuple[float, float]

    @property
    def delta_log_loss(self) -> float:
        return self.log_loss_model - self.log_loss_close


def binary_report(y: np.ndarray, p_model: np.ndarray, p_close: np.ndarray, groups: np.ndarray, reps: int, seed: int) -> BinaryReport:
    a, b = calibration_slope(y, p_model)
    w = blend_weight(y, p_close, p_model)
    rng = np.random.default_rng(seed)
    uniq = np.unique(groups)
    pos = {g: np.flatnonzero(groups == g) for g in uniq}
    ws, dl = [], []
    for _ in range(reps):
        pick = rng.choice(uniq, size=len(uniq), replace=True)
        idx = np.concatenate([pos[g] for g in pick])
        ws.append(blend_weight(y[idx], p_close[idx], p_model[idx]))
        dl.append(log_loss(y[idx], p_model[idx]) - log_loss(y[idx], p_close[idx]))
    q = lambda v: (float(np.quantile(v, 0.025)), float(np.quantile(v, 0.975))) if v else (math.nan, math.nan)  # noqa: E731
    return BinaryReport(
        n=int(len(y)),
        log_loss_model=log_loss(y, p_model), log_loss_close=log_loss(y, p_close),
        brier_model=brier(y, p_model), brier_close=brier(y, p_close),
        ece_model=ece(y, p_model), calib_intercept=a, calib_slope=b,
        blend_w=w, blend_w_ci=q(ws), delta_log_loss_ci=q(dl),
    )
