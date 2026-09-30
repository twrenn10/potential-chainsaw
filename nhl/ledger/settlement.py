"""Join immutable prediction artifacts to results and to the defined close.

This single join feeds evaluation, the grading report and the CLV report, so the
three can never disagree. Nothing here writes to the prediction store.

Per artifact:
* settlement: PENDING (no result yet), VOID (game postponed/cancelled, no result),
  else SETTLED with WIN / LOSS / PUSH under the market's settlement convention;
* close: the close-v1 selection for the same book and market group
  (``nhl.market.snapshots.select_closes``), using actual puck drop when a result
  carries it and the scheduled time otherwise (labelled). If the close is
  unavailable the CLV fields are empty and the reason is carried -- no substitute.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from nhl.contracts import MarketType, Selection
from nhl.data.pit import HistoricalStore
from nhl.ledger.closes import CloseStore
from nhl.ledger.grading import profit_units, settle
from nhl.market.snapshots import CloseSelection, select_closes
from nhl.timeutil import fmt_ts


def closes_for_store(store: HistoricalStore, game_ids: set[str]) -> dict[tuple[str, str, str], CloseSelection]:
    games = store.latest_games()
    results = {r.game_id: r for r in store.results}
    odds: dict[str, list] = defaultdict(list)
    for o in store.odds:
        if o.game_id in game_ids:
            odds[o.game_id].append(o)
    out = {}
    for gid in sorted(game_ids):
        g = games.get(gid)
        if g is None:
            continue
        actual = results[gid].actual_start if gid in results else None
        for c in select_closes(odds[gid], g.start_time, actual):
            out[(c.game_id, c.book, c.market_id)] = c
    return out


def settle_predictions(
    store: HistoricalStore,
    rows: list[dict[str, Any]],
    close_store: CloseStore | None = None,
    captured_at: str = "",
) -> list[dict[str, Any]]:
    games = store.latest_games()
    results = {r.game_id: r for r in store.results}
    closes = closes_for_store(store, {r["game_id"] for r in rows})
    if close_store is not None:
        close_store.append(list(closes.values()), captured_at)
    out = []
    for r in rows:
        g = games.get(r["game_id"])
        res = results.get(r["game_id"])
        market, sel = MarketType(r["market"]), Selection(r["selection"])
        if res is None:
            settlement = "VOID" if g is not None and g.status in {"PPD", "CNCL"} else "PENDING"
            outcome, profit = settlement, 0.0 if settlement == "VOID" else None
        else:
            team_is_home = None if r["team"] is None else r["team"] == (g.home if g else "")
            o = settle(res, market, sel, r["line"], team_is_home)
            settlement, outcome, profit = "SETTLED", o.value, profit_units(o, r["execution_decimal"])
        c = closes.get((r["game_id"], r["sportsbook"], r["market_id"]))
        row = {
            "prediction_id": r["prediction_id"], "game_id": r["game_id"], "market": r["market"], "period": r["period"],
            "selection": r["selection"], "line": r["line"], "team": r["team"], "sportsbook": r["sportsbook"],
            "evidence_lane": r["evidence_lane"], "status": r["status"], "eligibility": r["eligibility"],
            "as_of": r["as_of"], "execution_price": r["execution_price"], "execution_decimal": r["execution_decimal"],
            "no_vig_at_prediction": r["no_vig_probability"], "model_probability": r["model_probability"],
            "probability_edge": r["probability_edge"], "ev_per_unit": r["ev_per_unit"], "shadow_lane": r["shadow_lane"],
            "settlement": settlement, "outcome": outcome, "profit_units": profit,
            "close_status": "MISSING" if c is None else c.status, "close_reason": "" if c is None else c.reason,
            "close_basis": "" if c is None else c.cutoff_basis, "close_cutoff": "" if c is None else fmt_ts(c.cutoff),
            "close_price": None, "close_no_vig": None, "clv_ev": None, "clv_prob_move": None, "edge_at_close": None,
        }
        if c is not None and c.available and c.quote is not None:
            p_close = c.quote.no_vig[sel]
            row.update(close_price=c.quote.prices[sel], close_no_vig=round(p_close, 6),
                       clv_ev=round(r["execution_decimal"] * p_close - 1.0, 6),
                       clv_prob_move=round(p_close - r["no_vig_probability"], 6),
                       edge_at_close=round(r["model_probability"] - p_close, 6))
        out.append(row)
    return out
