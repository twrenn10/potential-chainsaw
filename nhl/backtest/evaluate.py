"""Post-hoc evaluation of stored artifacts vs results and the no-vig CLOSING market.

The closing line is the benchmark, not ROI: with hockey's outcome noise, thousands
of bets are needed before W/L says anything, while log loss vs the close and CLV
converge much faster.

For each market:
* binary markets are scored on one canonical side per quote (HOME / OVER) so the
  complementary row is not double counted; pushes are excluded;
* REG_3WAY is scored as a 3-class problem;
* gates from ``validation_gates.json`` are applied; SYNTHETIC data can never pass.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np

from nhl.config import load
from nhl.contracts import BetResult, MarketType, Selection
from nhl.data.pit import HistoricalStore
from nhl.ledger.clv import compute_clv
from nhl.ledger.grading import settle
from nhl.ledger.predictions import PredictionStore
from nhl.market.novig import american_to_decimal
from nhl.market.snapshots import GroupKey, closing_quotes
from nhl.timeutil import parse_ts

from .metrics import binary_report, multiclass_log_loss

CANONICAL = {
    MarketType.ML: Selection.HOME,
    MarketType.PUCK_LINE: Selection.HOME,
    MarketType.TOTAL: Selection.OVER,
    MarketType.TEAM_TOTAL: Selection.OVER,
}


def _group_key(row: dict[str, Any]) -> GroupKey:
    market = MarketType(row["market"])
    line = row["line"]
    if market is MarketType.PUCK_LINE and row["selection"] == "AWAY" and line is not None:
        line = -line
    return GroupKey(row["game_id"], market, line, row["team"])


def evaluate(store: HistoricalStore, predictions: PredictionStore, mode: str = "BACKTEST", reps: int | None = None, seed: int = 1) -> dict[str, Any]:
    gates = load("validation_gates")
    reps = reps if reps is not None else int(gates["defaults"]["bootstrap_reps"])
    results = {r.game_id: r for r in store.results}
    games = {g.game_id: g for g in store.games}
    odds_by_game: dict[str, list] = defaultdict(list)
    for s in store.odds:
        odds_by_game[s.game_id].append(s)
    close_cache: dict[str, dict[tuple[str, GroupKey], Any]] = {}

    def close_for(game_id: str) -> dict[tuple[str, GroupKey], Any]:
        if game_id not in close_cache:
            qs = closing_quotes(odds_by_game[game_id], games[game_id].start_time)
            close_cache[game_id] = {(q.book, q.key): q for q in qs}
        return close_cache[game_id]

    rows = list(predictions.rows("WHERE mode = ?", (mode,)))
    origins = sorted({r["data_origin"] for r in rows})
    binary: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    three: dict[tuple[str, str, str], dict[str, Any]] = {}
    clv_rows: list[dict[str, Any]] = []
    missing_close = 0
    for r in rows:
        res = results.get(r["game_id"])
        if res is None or res.available_at <= parse_ts(r["as_of"]):
            continue  # unresolved, or (impossible by construction) result known at as_of
        market = MarketType(r["market"])
        sel = Selection(r["selection"])
        gk = _group_key(r)
        cq = close_for(r["game_id"]).get((r["sportsbook"], gk))
        if cq is None:
            missing_close += 1
            continue
        p_close = cq.no_vig[sel]
        outcome = settle(res, market, sel, r["line"], None if r["team"] is None else r["team"] == games[r["game_id"]].home)
        c = compute_clv(r["market_price"], r["no_vig_probability"], p_close, r["model_probability"])
        clv_rows.append({"market": r["market"], "shadow_lane": r["shadow_lane"], "clv_ev": c.clv_ev,
                         "edge_at_bet": c.edge_at_bet, "edge_at_close": c.edge_at_close, "result": outcome.value,
                         "dec": american_to_decimal(r["market_price"])})
        if market is MarketType.REG_3WAY:
            k = (r["game_id"], r["sportsbook"], r["as_of"])
            d = three.setdefault(k, {"model": {}, "close": {}, "y": None})
            d["model"][sel.value] = r["model_probability"]
            d["close"][sel.value] = p_close
            if outcome is BetResult.WIN:
                d["y"] = sel.value
            continue
        if sel is not CANONICAL[market] or outcome is BetResult.PUSH:
            continue
        b = binary[market.value]
        b["y"].append(1.0 if outcome is BetResult.WIN else 0.0)
        b["p_model"].append(r["model_probability"])
        b["p_close"].append(p_close)
        b["group"].append(r["game_id"])

    report: dict[str, Any] = {"mode": mode, "data_origins": origins, "n_artifacts": len(rows),
                              "missing_close": missing_close, "markets": {}, "gate_version": gates["gate_version"]}
    for market, b in sorted(binary.items()):
        br = binary_report(np.array(b["y"]), np.array(b["p_model"]), np.array(b["p_close"]), np.array(b["group"]), reps, seed)
        report["markets"][market] = {**br.__dict__, "delta_log_loss": br.delta_log_loss}
    order = ("HOME", "DRAW", "AWAY")
    tri = [d for d in three.values() if d["y"] is not None and len(d["model"]) == 3]
    if tri:
        y = np.array([order.index(d["y"]) for d in tri])
        Pm = np.array([[d["model"][s] for s in order] for d in tri])
        Pc = np.array([[d["close"][s] for s in order] for d in tri])
        report["markets"]["REG_3WAY"] = {"n": len(tri), "log_loss_model": multiclass_log_loss(y, Pm),
                                         "log_loss_close": multiclass_log_loss(y, Pc),
                                         "delta_log_loss": multiclass_log_loss(y, Pm) - multiclass_log_loss(y, Pc)}
    report["clv"] = _clv_summary(clv_rows)
    report["gates"] = apply_gates(report, gates)
    return report


def _clv_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for lane in ("PASS", "WATCH", "MODEL_PLUS"):
        sub = [r for r in rows if r["shadow_lane"] == lane]
        if not sub:
            continue
        clv = np.array([r["clv_ev"] for r in sub])
        settled = [r for r in sub if r["result"] in ("WIN", "LOSS")]
        roi = float(np.mean([(r["dec"] - 1) if r["result"] == "WIN" else -1.0 for r in settled])) if settled else float("nan")
        out[lane] = {
            "n": len(sub),
            "mean_clv_ev": float(clv.mean()),
            "share_beat_close": float((clv > 0).mean()),
            "mean_edge_at_bet": float(np.mean([r["edge_at_bet"] for r in sub])),
            "mean_edge_at_close": float(np.mean([r["edge_at_close"] for r in sub])),
            "paper_roi_secondary": roi,
        }
    return out


def apply_gates(report: dict[str, Any], gates: dict[str, Any]) -> dict[str, Any]:
    out = {}
    synthetic = "SYNTHETIC" in report["data_origins"]
    for market, overrides in gates["markets"].items():
        g = {**gates["defaults"], **overrides}
        m = report["markets"].get(market)
        reasons = []
        if synthetic:
            reasons.append("NOT_ELIGIBLE:SYNTHETIC_DATA")
        if m is None:
            reasons.append("NO_SCORED_ROWS")
        else:
            if m["n"] < g["min_scored_games"]:
                reasons.append(f"N<{g['min_scored_games']}")
            if m["delta_log_loss"] > g["max_delta_logloss_vs_close"]:
                reasons.append("LOGLOSS_WORSE_THAN_CLOSE")
            if "calib_slope" in m:
                lo, hi = g["calibration_slope_range"]
                if not lo <= m["calib_slope"] <= hi:
                    reasons.append("CALIBRATION_SLOPE_OUT_OF_RANGE")
                if m["ece_model"] > g["max_ece"]:
                    reasons.append("ECE_TOO_HIGH")
                if not m["blend_w_ci"][0] > g["blend_weight_ci_lower_gt"]:
                    reasons.append("NO_INFORMATION_BEYOND_MARKET")
        out[market] = {"passed": not reasons, "reasons": reasons}
    return out
