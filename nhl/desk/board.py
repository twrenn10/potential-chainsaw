"""NHL Desk exports: board CSV + summary JSON + manifest JSON + anomalies CSV + text view.

Same artifact family as tournament_v2 builders (deterministic writers, summary,
manifest, anomalies). No letter grades: rows show probabilities, prices, edge and
the lane with its reason codes.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from hashlib import sha256
from pathlib import Path
from typing import Any

from nhl import FEATURE_VERSION, MODEL_VERSION
from nhl.data.quality import HealthReport
from nhl.ledger.predictions import COLUMNS, PredictionArtifact
from nhl.pricing.engine import PricedGame

LANE_ORDER = {"ACTIONABLE": 0, "MODEL_PLUS": 1, "WATCH": 2, "UNVALIDATED": 3, "PASS": 4, "BLOCKED": 5}


def _sorted(rows: list[PredictionArtifact]) -> list[PredictionArtifact]:
    return sorted(rows, key=lambda a: (a.puck_drop, a.game_id, a.market, a.team or "", a.line or 0.0, a.selection, a.sportsbook))


def _write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for r in rows:
            writer.writerow({c: r.get(c, "") if r.get(c) is not None else "" for c in columns})


def export_desk(
    out_dir: str | Path,
    slate_label: str,
    artifacts: list[PredictionArtifact],
    priced: list[PricedGame],
    health: HealthReport,
) -> dict[str, str]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rows = _sorted(artifacts)
    board = out / "NHL_DESK.csv"
    _write_csv(board, [a.content() for a in rows], COLUMNS)

    anomalies = [a for a in rows if a.status == "BLOCKED"]
    anomalies_path = out / "NHL_DESK_anomalies.csv"
    _write_csv(anomalies_path, [{"prediction_id": a.prediction_id, "game_id": a.game_id, "market_key": a.market_key,
                                 "reason_codes": a.reason_codes} for a in anomalies],
               ["prediction_id", "game_id", "market_key", "reason_codes"])

    goalie_counts = Counter()
    for p in priced:
        goalie_counts[p.home_starters.state.value] += 1
        goalie_counts[p.away_starters.state.value] += 1
    summary = {
        "slate": slate_label,
        "pipeline": "READY" if not health.hard_fail else "DEGRADED",
        "games": len(priced),
        "markets": len(rows),
        "lanes": dict(sorted(Counter(a.status for a in rows).items())),
        "evidence_lanes": dict(sorted(Counter(a.evidence_lane for a in rows).items())),
        "eligibility": dict(sorted(Counter(a.eligibility for a in rows).items())),
        "shadow_lanes": dict(sorted(Counter(a.shadow_lane for a in rows).items())),
        "goalies": dict(sorted(goalie_counts.items())),
        "data_health": health.to_dict(),
        "model_version": MODEL_VERSION,
        "feature_version": FEATURE_VERSION,
        "note": "No ACTIONABLE lane until gates pass. Shadow lanes are informational only.",
    }
    summary_path = out / "NHL_DESK_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    text_path = out / "NHL_DESK.txt"
    text_path.write_text(render_text(summary, rows, priced), encoding="utf-8")

    files = [board, anomalies_path, summary_path, text_path]
    manifest = {
        "slate": slate_label,
        "files": {f.name: sha256(f.read_bytes()).hexdigest() for f in files},
        "row_counts": {"board": len(rows), "anomalies": len(anomalies)},
        "data_snapshot_ids": sorted({a.data_snapshot_id for a in rows}),
        "config_hashes": sorted({a.config_hash for a in rows}),
    }
    manifest_path = out / "NHL_DESK_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"board": str(board), "summary": str(summary_path), "anomalies": str(anomalies_path),
            "text": str(text_path), "manifest": str(manifest_path)}


def render_text(summary: dict[str, Any], rows: list[PredictionArtifact], priced: list[PricedGame]) -> str:
    lines = [
        f"NHL DESK - {summary['slate']}",
        "",
        f"PIPELINE: {summary['pipeline']}",
        f"GAMES: {summary['games']}",
        f"MARKETS: {summary['markets']}",
        "",
        "LANES " + "  ".join(f"{k} {v}" for k, v in sorted(summary["lanes"].items(), key=lambda kv: LANE_ORDER.get(kv[0], 9))),
        "SHADOW (not actionable) " + "  ".join(f"{k} {v}" for k, v in sorted(summary["shadow_lanes"].items(), key=lambda kv: LANE_ORDER.get(kv[0], 9))),
        "",
        "GOALIES " + "  ".join(f"{k.lower()} {v}" for k, v in summary["goalies"].items()),
        f"DATA HEALTH {summary['data_health']['score'] * 100:.1f}%",
        "",
    ]
    by_game: dict[str, list[PredictionArtifact]] = {}
    for a in rows:
        by_game.setdefault(a.game_id, []).append(a)
    for p in priced:
        g = p.game
        lines.append(f"{g.away} @ {g.home}  ({g.start_time:%Y-%m-%d %H:%MZ})  P(OT)={p.pricing.p_ot + p.pricing.p_so:.3f}  E[total]={p.pricing.mean_total:.2f}")
        lines.append(f"  goalies: {p.goalie_state}")
        best = sorted(by_game.get(g.game_id, []), key=lambda a: (-a.probability_edge, a.market_key, a.sportsbook))[:4]
        for a in best:
            label = f"{a.market} {a.team + ' ' if a.team else ''}{a.selection}{'' if a.line is None else f' {a.line:+.1f}'}"
            fair = "n/a" if a.model_fair_price is None else f"{a.model_fair_price:+d}"
            lines.append(
                f"  {label:<26} px {a.execution_price:+5d}  raw {a.raw_implied_probability:.3f}  nv {a.no_vig_probability:.3f}"
                f"  model {a.model_probability:.3f}  fair {fair:>5}  p-edge {a.probability_edge * 100:+.1f}%"
                f"  EV {a.ev_per_unit * 100:+.1f}%  [{a.status} | {a.eligibility} | shadow {a.shadow_lane}]"
            )
        lines.append("")
    return "\n".join(lines)
