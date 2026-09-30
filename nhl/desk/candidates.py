"""Model-vs-market comparison: priced games x book quotes -> prediction artifacts.

Mirrors tournament_v2's reference-market -> edge -> context -> decision stages,
but the edge is in probability space (model vs no-vig market), not points.
"""

from __future__ import annotations

import json
from datetime import datetime

from nhl import FEATURE_VERSION, MODEL_VERSION
from nhl.config import config_hash
from nhl.contracts import MarketType, OddsSnapshot, PredictionMode, Selection, build_market_key, stable_digest
from nhl.data.pit import PointInTimeView
from nhl.data.quality import HealthReport
from nhl.governance.lanes import decide
from nhl.ledger.predictions import PredictionArtifact, make_prediction_id
from nhl.market.novig import american_to_decimal, prob_to_american
from nhl.market.snapshots import GroupQuote, quotes_at
from nhl.pricing.engine import PricedGame
from nhl.pricing.markets import ev_per_unit, no_push_prob
from nhl.timeutil import fmt_ts


def all_config_hash() -> str:
    return stable_digest([config_hash(n) for n in ("simulator", "goalie_start", "governance", "validation_gates",
                                                     "backfill_overrides", "moneypuck_vintages")])


def data_snapshot_id(view: PointInTimeView, odds_used: list[OddsSnapshot]) -> str:
    """Fingerprint of everything visible to this prediction run."""

    parts = [
        fmt_ts(view.as_of), view.data_origin, f"strict={view.strict}",
        str(len(view.games())), str(len(view.results())), str(len(view.team_stats())),
        str(len(view.goalie_stats())), str(len(view.player_seasons())),
        *sorted({s.snapshot_id for s in odds_used}),
    ]
    return "DS:" + stable_digest(parts, n=16)


def selection_line(q: GroupQuote, sel: Selection) -> float | None:
    if q.key.market is MarketType.PUCK_LINE and q.key.line is not None:
        return q.key.line if sel is Selection.HOME else -q.key.line
    return q.key.line


def build_artifacts(
    view: PointInTimeView,
    priced: list[PricedGame],
    odds: list[OddsSnapshot],
    health: HealthReport,
    mode: PredictionMode,
    created_at: datetime,
    lineup_state: str = "UNKNOWN",
    extra_provenance_blocks: list[str] | None = None,
    constants_id: str = "",
) -> list[PredictionArtifact]:
    by_game = {p.game.game_id: p for p in priced}
    visible = [s for s in odds if s.game_id in by_game and s.snapshot_ts <= view.as_of]
    snap_id = data_snapshot_id(view, visible)
    cfg_hash = all_config_hash() if not constants_id else stable_digest([all_config_hash(), constants_id])
    out: list[PredictionArtifact] = []
    quotes = quotes_at(visible, view.as_of)
    # Computed after every view access above, so the flags cover all inputs used.
    prov_blocks = sorted(set(view.provenance_blocks() + (extra_provenance_blocks or []) + ([] if view.strict else ["NON_STRICT_VIEW"])))
    for q in quotes:
        pg = by_game[q.key.game_id]
        game = pg.game
        team_is_home = None if q.key.team is None else q.key.team == game.home
        age_min = (view.as_of - q.observed_at).total_seconds() / 60.0
        for sel, price in q.prices.items():
            line = selection_line(q, sel)
            win, push, loss = pg.pricing.outcome_probs(q.key.market, sel, line, team_is_home)
            model_p = no_push_prob(win, push, loss)
            nv = q.no_vig[sel]
            edge = model_p - nv
            decision = decide(
                market=q.key.market.value,
                edge=edge,
                data_origin=view.data_origin,
                health_blocks=health.game_blocks(game.game_id),
                odds_age_minutes=age_min,
                goalies_confirmed=pg.both_confirmed,
                provenance_blocks=prov_blocks,
            )
            mkey = build_market_key(game.game_id, q.key.market, sel, line, q.key.team)
            as_of_s = fmt_ts(view.as_of)
            out.append(
                PredictionArtifact(
                    prediction_id=make_prediction_id(game.game_id, mkey, q.book, as_of_s, MODEL_VERSION, mode.value),
                    created_at=fmt_ts(created_at),
                    as_of=as_of_s,
                    puck_drop=fmt_ts(game.start_time),
                    mode=mode.value,
                    data_origin=view.data_origin,
                    game_id=game.game_id,
                    market=q.key.market.value,
                    selection=sel.value,
                    line=line,
                    team=q.key.team,
                    market_key=mkey,
                    sportsbook=q.book,
                    market_price=int(price),
                    market_observed_at=fmt_ts(q.observed_at),
                    no_vig_probability=round(nv, 6),
                    model_probability=round(model_p, 6),
                    model_p_win=round(win, 6),
                    model_p_push=round(push, 6),
                    fair_price=prob_to_american(model_p) if 0.0 < model_p < 1.0 else None,
                    edge=round(edge, 6),
                    ev_per_unit=round(ev_per_unit(win, push, loss, american_to_decimal(price)), 6),
                    goalie_state=pg.goalie_state,
                    lineup_state=lineup_state,
                    model_version=MODEL_VERSION,
                    feature_version=FEATURE_VERSION,
                    config_hash=cfg_hash,
                    data_snapshot_id=snap_id,
                    health_score=round(health.score, 4),
                    status=decision.lane.value,
                    shadow_lane=decision.shadow_lane.value,
                    reason_codes=json.dumps(decision.reasons),
                )
            )
    return out

