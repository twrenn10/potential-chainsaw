"""Model-vs-market comparison: priced games x book quotes -> prediction artifacts.

Mirrors tournament_v2's reference-market -> edge -> context -> decision stages,
but the edge is in probability space (model vs no-vig market), not points.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict
from datetime import datetime

from nhl import FEATURE_VERSION, MODEL_VERSION
from nhl.config import config_hash
from nhl.contracts import MarketType, OddsSnapshot, PredictionMode, Selection, build_market_key, stable_digest
from nhl.data.pit import PointInTimeView
from nhl.data.quality import HealthReport
from nhl.governance.lanes import decide, eligibility, evidence_lane
from nhl.ledger.predictions import SCHEMA_VERSION, PredictionArtifact, make_prediction_id, state_fingerprint
from nhl.market.novig import prob_to_american
from nhl.market.snapshots import GroupQuote, quotes_at
from nhl.pricing.engine import PricedGame
from nhl.pricing.markets import ev_per_unit, fair_decimal, no_push_prob
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


def ratings_fingerprint(ratings) -> str:
    """Parameter snapshot: every fitted value (the fit timestamp is excluded)."""

    d = {k: v for k, v in asdict(ratings).items() if k not in ("as_of", "n_rows")}
    blob = json.dumps(d, sort_keys=True, default=lambda x: round(x, 10) if isinstance(x, float) else str(x))
    return "PR:" + stable_digest([blob], 16)


def build_artifacts(
    view: PointInTimeView,
    priced: list[PricedGame],
    odds: list[OddsSnapshot],
    health: HealthReport,
    mode: PredictionMode,
    created_at: datetime,
    lineup_state: str = "UNKNOWN",
    extra_provenance_blocks: list[str] | None = None,
    constants=None,
    roster_fingerprints: dict[str, str] | None = None,
    parameter_fingerprint: str = "",
    simulated_clock: bool = False,
) -> list[PredictionArtifact]:
    by_game = {p.game.game_id: p for p in priced}
    visible = [s for s in odds if s.game_id in by_game and s.snapshot_ts <= view.as_of]
    snap_id = data_snapshot_id(view, visible)
    constants_id = constants.constants_id if constants is not None else ""
    defaults = sorted(k for k, v in constants.sources.items() if v.startswith("DEFAULT")) if constants is not None else []
    cfg_hash = all_config_hash() if not constants_id else stable_digest([all_config_hash(), constants_id])
    rfp = roster_fingerprints or {}
    out: list[PredictionArtifact] = []
    quotes = quotes_at(visible, view.as_of)
    # Computed after every view access above, so the flags cover all inputs used.
    prov_blocks = sorted(set(view.provenance_blocks() + (extra_provenance_blocks or []) + ([] if view.strict else ["NON_STRICT_VIEW"])))
    if simulated_clock:
        prov_blocks = sorted(set(prov_blocks) | {"SIMULATED_CLOCK"})
    evidence = evidence_lane(view.data_origin, mode.value, view.strict, [b for b in prov_blocks if b != "SIMULATED_CLOCK"],
                             simulated_clock)
    as_of_s = fmt_ts(view.as_of)
    for q in quotes:
        if not q.key.market.priced:
            continue  # props: keyed/de-vigged/closed at the data layer, not priced (research-only)
        pg = by_game[q.key.game_id]
        game = pg.game
        team_is_home = None if q.key.team is None else q.key.team == game.home
        age_min = (view.as_of - q.observed_at).total_seconds() / 60.0
        for sel, price in q.prices.items():
            line = selection_line(q, sel)
            win, push, loss = pg.pricing.outcome_probs(q.key.market, sel, line, team_is_home)
            model_p = no_push_prob(win, push, loss)
            nv = q.no_vig[sel]
            raw = q.raw_implied[sel]
            dec = q.decimals[sel]
            prob_edge = model_p - nv
            ev = ev_per_unit(win, push, loss, dec)
            fair_dec = fair_decimal(win, push, loss) if win > 0 else None
            fair_dec = fair_dec if fair_dec is not None and math.isfinite(fair_dec) and fair_dec > 1.0 else None
            decision = decide(
                market=q.key.market.value, edge=prob_edge, data_origin=view.data_origin,
                health_blocks=health.game_blocks(game.game_id), odds_age_minutes=age_min,
                goalies_confirmed=pg.both_confirmed, provenance_blocks=prov_blocks, ev_per_unit=ev, evidence=evidence,
            )
            reasons = decision.reasons + [f"SOFT:{f}" for f in pg.goalie_flags]
            mkey = build_market_key(game.game_id, q.key.market, sel, line, q.key.team, q.key.period, q.key.participant)
            content = dict(
                prediction_id=make_prediction_id(game.game_id, mkey, q.book, as_of_s, MODEL_VERSION, mode.value),
                schema_version=SCHEMA_VERSION, created_at=fmt_ts(created_at), as_of=as_of_s,
                puck_drop=fmt_ts(game.start_time), mode=mode.value, data_origin=view.data_origin, evidence_lane=evidence.value,
                game_id=game.game_id, market=q.key.market.value, period=q.key.period.value, selection=sel.value, line=line,
                team=q.key.team, participant=q.key.participant, market_key=mkey, market_id=q.key.market_id,
                sportsbook=q.book, provider=q.provider, market_snapshot_ref=q.ref, market_observed_at=fmt_ts(q.observed_at),
                quote_age_minutes=round(age_min, 3),
                execution_price=int(price), execution_decimal=round(dec, 8), raw_implied_probability=round(raw, 8),
                no_vig_probability=round(nv, 6), novig_method=q.novig.method, market_overround=round(q.novig.overround, 8),
                model_probability=round(model_p, 6), model_p_win=round(win, 6), model_p_push=round(push, 6),
                model_fair_price=prob_to_american(model_p) if 0.0 < model_p < 1.0 else None,
                model_fair_decimal=round(fair_dec, 8) if fair_dec is not None else None,
                probability_edge=round(prob_edge, 6),
                fair_price_edge=round(dec / fair_dec - 1.0, 6) if fair_dec is not None else None,
                ev_per_unit=round(ev, 6),
                goalie_state=pg.goalie_state, goalie_fingerprint=pg.goalie_fingerprint, lineup_state=lineup_state,
                roster_fingerprint="R:" + stable_digest([rfp.get(game.home, ""), rfp.get(game.away, "")], 16),
                model_version=MODEL_VERSION, feature_version=FEATURE_VERSION, parameter_fingerprint=parameter_fingerprint,
                constants_id=constants_id, default_constants=json.dumps(defaults), config_hash=cfg_hash,
                data_snapshot_id=snap_id, health_score=round(health.score, 4),
                status=decision.lane.value, shadow_lane=decision.shadow_lane.value,
                eligibility=eligibility(evidence, decision.lane).value,
                block_reasons=json.dumps(sorted(r for r in reasons if r.startswith("HARD_BLOCK:"))),
                reason_codes=json.dumps(reasons), state_fingerprint="",
            )
            content["state_fingerprint"] = state_fingerprint(content)
            out.append(PredictionArtifact(**content))
    return out
