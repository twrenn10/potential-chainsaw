"""Market-by-market evaluation of stored artifacts against results and the no-vig CLOSE.

The primary benchmark is the no-vig closing market (close-v1), not ROI. For each
evidence lane and each market:

* probability quality: counts, Brier, log loss (model and close), calibration
  intercept/slope, ECE, mean predicted vs hit rate, mean no-vig, mean model-market
  difference, model-vs-close blend weight with game-level bootstrap CIs;
* execution quality (paper, 1 unit on every positive-EV row): CLV mean/median/CI,
  positive-CLV rate, mean execution decimal, mean EV, realised ROI (secondary);
* close availability (market completeness) and interpretation flags.

Binary markets are scored on one canonical side per quote (HOME / OVER) so the
complementary row is not double counted; pushes are excluded. REG_3WAY is scored
as a 3-class problem. Props are listed as NOT_PRICED.

Gates (``validation_gates.json``, all thresholds PROVISIONAL) only consider rows in
eligible evidence lanes; synthetic/dev data and historical research can never pass.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np

from nhl.config import load
from nhl.contracts import MarketType, Selection
from nhl.data.pit import HistoricalStore
from nhl.ledger.closes import CloseStore
from nhl.ledger.predictions import PredictionStore
from nhl.ledger.settlement import settle_predictions

from .metrics import binary_report, multiclass_log_loss

CANONICAL = {
    MarketType.ML: Selection.HOME,
    MarketType.PUCK_LINE: Selection.HOME,
    MarketType.TOTAL: Selection.OVER,
    MarketType.TEAM_TOTAL: Selection.OVER,
}
PROVENANCE_BLOCKS = ("HARD_BLOCK:NON_CAUSAL_INPUTS", "HARD_BLOCK:UNATTESTED_PROVENANCE", "HARD_BLOCK:NON_STRICT_VIEW",
                     "HARD_BLOCK:UNATTESTED_ROSTER")
DEV_LANE = "DEV_SYNTHETIC"


def _gv(g: dict, key: str):
    v = g[key]
    return v["value"] if isinstance(v, dict) and "value" in v else v


def _mean_ci(x: np.ndarray, reps: int, seed: int) -> tuple[float, float]:
    if len(x) == 0:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    means = [float(rng.choice(x, size=len(x), replace=True).mean()) for _ in range(reps)]
    return float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))


def _probability_block(rows: list[dict], market: MarketType, reps: int, seed: int) -> dict[str, Any] | None:
    if market is MarketType.REG_3WAY:
        tri: dict[tuple, dict] = {}
        for r in rows:
            if r["settlement"] != "SETTLED" or r["close_no_vig"] is None:
                continue
            d = tri.setdefault((r["game_id"], r["sportsbook"], r["as_of"]), {"m": {}, "c": {}, "y": None})
            d["m"][r["selection"]] = r["model_probability"]
            d["c"][r["selection"]] = r["close_no_vig"]
            if r["outcome"] == "WIN":
                d["y"] = r["selection"]
        order = ("HOME", "DRAW", "AWAY")
        ok = [d for d in tri.values() if d["y"] is not None and len(d["m"]) == 3 and len(d["c"]) == 3]
        if not ok:
            return None
        y = np.array([order.index(d["y"]) for d in ok])
        Pm = np.array([[d["m"][s] for s in order] for d in ok])
        Pc = np.array([[d["c"][s] for s in order] for d in ok])
        return {"n": len(ok), "log_loss_model": multiclass_log_loss(y, Pm), "log_loss_close": multiclass_log_loss(y, Pc),
                "delta_log_loss": multiclass_log_loss(y, Pm) - multiclass_log_loss(y, Pc)}
    canon = CANONICAL[market].value
    use = [r for r in rows if r["selection"] == canon and r["settlement"] == "SETTLED" and r["outcome"] in ("WIN", "LOSS")
           and r["close_no_vig"] is not None]
    if len(use) < 2:
        return None
    y = np.array([1.0 if r["outcome"] == "WIN" else 0.0 for r in use])
    pm = np.array([r["model_probability"] for r in use])
    pc = np.array([r["close_no_vig"] for r in use])
    pn = np.array([r["no_vig_at_prediction"] for r in use])
    br = binary_report(y, pm, pc, np.array([r["game_id"] for r in use]), reps, seed)
    return {**br.__dict__, "delta_log_loss": br.delta_log_loss, "avg_predicted": float(pm.mean()),
            "hit_rate": float(y.mean()), "avg_no_vig_at_prediction": float(pn.mean()), "avg_close_no_vig": float(pc.mean()),
            "avg_model_minus_market": float((pm - pn).mean())}


def _execution_block(rows: list[dict], reps: int, seed: int) -> dict[str, Any]:
    cand = [r for r in rows if r["ev_per_unit"] > 0]
    with_close = [r for r in cand if r["clv_ev"] is not None]
    clv = np.array([r["clv_ev"] for r in with_close])
    settled = [r for r in cand if r["settlement"] == "SETTLED"]
    roi = float(np.mean([r["profit_units"] for r in settled])) if settled else float("nan")
    return {
        "positive_ev_rows": len(cand),
        "clv_n": len(with_close),
        "clv_mean": float(clv.mean()) if len(clv) else float("nan"),
        "clv_median": float(np.median(clv)) if len(clv) else float("nan"),
        "clv_ci": _mean_ci(clv, max(reps // 5, 50), seed),
        "positive_clv_rate": float((clv > 0).mean()) if len(clv) else float("nan"),
        "avg_execution_decimal": float(np.mean([r["execution_decimal"] for r in cand])) if cand else float("nan"),
        "avg_ev_per_unit": float(np.mean([r["ev_per_unit"] for r in cand])) if cand else float("nan"),
        "realized_roi_secondary": roi,
        "settled_n": len(settled),
    }


def interpret(prob: dict | None, exe: dict) -> list[str]:
    """The distinctions the gates must never blur."""

    notes = []
    roi, clv, lo = exe["realized_roi_secondary"], exe["clv_mean"], exe["clv_ci"][0]
    if np.isfinite(roi) and np.isfinite(clv) and roi > 0 and clv < 0:
        notes.append("WARNING:POSITIVE_ROI_WITH_NEGATIVE_CLV (likely variance, not skill)")
    if np.isfinite(roi) and np.isfinite(lo) and roi < 0 and lo > 0:
        notes.append("NOTE:NEGATIVE_ROI_WITH_STABLE_POSITIVE_CLV (do not reject on ROI)")
    if prob and "calib_slope" in prob:
        calibrated = 0.85 <= prob["calib_slope"] <= 1.15 and prob["ece_model"] <= 0.025
        beats = prob["delta_log_loss"] < 0 and prob["blend_w_ci"][0] > 0
        if calibrated and not beats:
            notes.append("NOTE:WELL_CALIBRATED_WITHOUT_MARKET_ADVANTAGE (not a betting edge)")
    return notes


def evaluate(
    store: HistoricalStore,
    predictions: PredictionStore,
    mode: str = "BACKTEST",
    reps: int | None = None,
    seed: int = 1,
    close_store: CloseStore | None = None,
    captured_at: str = "",
    integrity: dict[str, bool] | None = None,
) -> dict[str, Any]:
    gates = load("validation_gates")
    reps = reps if reps is not None else int(_gv(gates["defaults"], "bootstrap_reps"))
    rows = list(predictions.rows("WHERE mode = ?", (mode,)))
    settled = settle_predictions(store, rows, close_store, captured_at)
    by_id = {r["prediction_id"]: r for r in rows}
    origins = sorted({r["data_origin"] for r in rows})
    prov_blocked = sum(1 for r in rows if any(t in r["reason_codes"] for t in PROVENANCE_BLOCKS))

    report: dict[str, Any] = {
        "mode": mode, "data_origins": origins, "n_artifacts": len(rows), "gate_version": gates["gate_version"],
        "provenance_blocked_artifacts": prov_blocked,
        "evidence_lanes": dict(sorted(_count(r["evidence_lane"] for r in rows).items())),
        "missing_close": sum(1 for r in settled if r["settlement"] == "SETTLED" and r["close_status"] != "AVAILABLE"),
        "close_unavailable_reasons": dict(sorted(_count(r["close_reason"].split(":")[0] for r in settled
                                                        if r["close_status"] == "UNAVAILABLE").items())),
        "markets": {}, "by_lane": {}, "execution": {}, "interpretation": {},
        "health_mean": float(np.mean([r["health_score"] for r in rows])) if rows else float("nan"),
    }
    lanes: dict[str, list[dict]] = defaultdict(list)
    for s in settled:
        lanes[by_id[s["prediction_id"]]["evidence_lane"]].append(s)
    for m in MarketType:
        mrows = [s for s in settled if s["market"] == m.value]
        if not m.priced:
            report["markets"][m.value] = {"status": "NOT_PRICED", "n": 0}
            continue
        prob = _probability_block(mrows, m, reps, seed)
        exe = _execution_block(mrows, reps, seed)
        graded = sum(1 for s in mrows if s["settlement"] == "SETTLED")
        avail = sum(1 for s in mrows if s["settlement"] == "SETTLED" and s["close_status"] == "AVAILABLE")
        if prob is not None:
            report["markets"][m.value] = {**prob, "predictions": len(mrows), "graded": graded,
                                          "close_availability": avail / graded if graded else float("nan")}
        report["execution"][m.value] = exe
        report["interpretation"][m.value] = interpret(prob, exe)
        for lane, lrows in lanes.items():
            lm = [s for s in lrows if s["market"] == m.value]
            if lm:
                report["by_lane"].setdefault(lane, {})[m.value] = {
                    "probability": _probability_block(lm, m, max(reps // 5, 50), seed),
                    "execution": _execution_block(lm, reps, seed), "predictions": len(lm)}
    report["clv"] = _legacy_clv(settled)
    report["gates"] = apply_gates(report, gates, integrity or {})
    return report


def _count(it) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for x in it:
        out[x] += 1
    return out


def _legacy_clv(rows: list[dict]) -> dict[str, Any]:
    """CLV by shadow lane (informational)."""

    out = {}
    for lane in ("PASS", "WATCH", "MODEL_PLUS"):
        sub = [r for r in rows if r["shadow_lane"] == lane and r["clv_ev"] is not None]
        if sub:
            clv = np.array([r["clv_ev"] for r in sub])
            out[lane] = {"n": len(sub), "mean_clv_ev": float(clv.mean()), "share_beat_close": float((clv > 0).mean())}
    return out


def apply_gates(report: dict[str, Any], gates: dict[str, Any], integrity: dict[str, bool]) -> dict[str, Any]:
    out = {}
    eligible_lanes = set(gates["eligible_evidence_lanes"])
    origins = set(report["data_origins"])
    for market, overrides in gates["markets"].items():
        g = {**gates["defaults"], **overrides}
        reasons: list[str] = []
        if origins & {"SYNTHETIC", "FIXTURE"}:
            reasons.append("NOT_ELIGIBLE:SYNTHETIC_DATA")
        if overrides.get("not_priced"):
            out[market] = {"passed": False, "reasons": reasons + ["NOT_PRICED:RESEARCH_ONLY_MARKET"],
                           "gate_version": gates["gate_version"]}
            continue
        lanes = {lane for lane, mk in report["by_lane"].items() if market in mk}
        if not (lanes & eligible_lanes):
            reasons.append("NOT_ELIGIBLE:NO_STRICT_OR_SHADOW_FORWARD_EVIDENCE")
        if lanes - eligible_lanes:
            reasons.append("NOT_ELIGIBLE:MIXED_WITH_" + "+".join(sorted(lanes - eligible_lanes)))
        if report.get("provenance_blocked_artifacts", 0) > 0:
            reasons.append("NOT_ELIGIBLE:NON_CAUSAL_OR_UNATTESTED_INPUTS")
        for check in gates["required_checks"]:
            if check == "NO_PROVENANCE_BLOCKS":
                continue
            if not integrity.get(check, False):
                reasons.append(f"CHECK_NOT_DEMONSTRATED:{check}")
        m = report["markets"].get(market)
        if m is None:
            reasons.append("NO_SCORED_ROWS")
        else:
            if m["n"] < _gv(g, "min_scored_games"):
                reasons.append(f"N<{_gv(g, 'min_scored_games')}")
            if m["delta_log_loss"] > _gv(g, "max_delta_logloss_vs_close"):
                reasons.append("LOGLOSS_WORSE_THAN_CLOSE")
            if "calib_slope" in m:
                lo, hi = _gv(g, "calibration_slope_range")
                if not lo <= m["calib_slope"] <= hi:
                    reasons.append("CALIBRATION_SLOPE_OUT_OF_RANGE")
                if abs(m["calib_intercept"]) > _gv(g, "calibration_intercept_abs_max"):
                    reasons.append("CALIBRATION_INTERCEPT_OUT_OF_RANGE")
                if m["ece_model"] > _gv(g, "max_ece"):
                    reasons.append("ECE_TOO_HIGH")
                if not m["blend_w_ci"][0] > _gv(g, "blend_weight_ci_lower_gt"):
                    reasons.append("NO_INFORMATION_BEYOND_MARKET")
            if m.get("close_availability", 1.0) < _gv(g, "min_close_availability"):
                reasons.append("CLOSE_AVAILABILITY_TOO_LOW")
        exe = report["execution"].get(market)
        if exe is None or exe["clv_n"] < _gv(g, "min_clv_samples"):
            reasons.append("CLV_SAMPLE_TOO_SMALL")
        elif not exe["clv_ci"][0] > _gv(g, "clv_ci_lower_gt"):
            reasons.append("NO_POSITIVE_CLV")
        if not report["health_mean"] >= _gv(g, "min_mean_health"):
            reasons.append("DATA_HEALTH_TOO_LOW")
        out[market] = {"passed": not reasons, "reasons": reasons, "gate_version": gates["gate_version"],
                       "thresholds_status": "PROVISIONAL", "roi_used": False}
    return out
