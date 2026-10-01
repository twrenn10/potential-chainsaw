"""Machine-readable Phase 3 forward-evidence qualification and cohort summaries."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from hashlib import sha256
from typing import Any, Iterable

from nhl.timeutil import parse_ts


def validate_forward_artifact(row: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if row.get("mode") != "FORWARD":
        reasons.append("NOT_FORWARD_MODE")
    if row.get("data_origin") != "LIVE":
        reasons.append("NOT_LIVE_ORIGIN")
    if row.get("evidence_lane") != "SHADOW_FORWARD":
        reasons.append("NOT_SHADOW_FORWARD")
    if parse_ts(row["created_at"]) >= parse_ts(row["puck_drop"]):
        reasons.append("CREATED_AT_OR_AFTER_CUTOFF")
    if parse_ts(row["as_of"]) >= parse_ts(row["puck_drop"]):
        reasons.append("AS_OF_OR_AFTER_CUTOFF")
    if not row.get("market_snapshot_ref") or not row.get("market_observed_at"):
        reasons.append("NO_REAL_MARKET_OBSERVATION")
    try:
        blocks = json.loads(row.get("block_reasons") or "[]")
    except json.JSONDecodeError:
        blocks = ["MALFORMED_BLOCK_REASONS"]
    if blocks:
        reasons.append("HARD_BLOCKED")
    if row.get("eligibility") not in {"EVALUATION_ONLY", "ACTIONABLE_ELIGIBLE"}:
        reasons.append("NOT_EVALUATION_ELIGIBLE")
    return not reasons, reasons


def build_forward_cohort(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    accepted, excluded = [], Counter()
    for row in rows:
        ok, reasons = validate_forward_artifact(row)
        if ok:
            accepted.append(row)
        else:
            excluded.update(reasons)
    markets = Counter(r["market"] for r in accepted)
    providers = sorted({r["provider"] for r in accepted})
    models = sorted({r["model_version"] for r in accepted})
    configs = sorted({r["config_hash"] for r in accepted})
    params = sorted({r["parameter_fingerprint"] for r in accepted})
    times = sorted(parse_ts(r["created_at"]) for r in accepted)
    identity = {
        "providers": providers, "model_versions": models, "config_fingerprints": configs,
        "parameter_fingerprints": params,
    }
    return {
        "cohort_id": "SF:" + sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:20],
        "observation_start": times[0].isoformat() if times else None,
        "observation_end": times[-1].isoformat() if times else None,
        "eligible_markets": sorted(markets), "sample_counts": dict(sorted(markets.items())),
        **identity, "n_artifacts": len(accepted), "excluded": dict(sorted(excluded.items())),
    }


def first_slate_audit(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per game/market/book showing reprices and their causal fingerprints."""

    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row["game_id"], row["market_id"], row["sportsbook"])].append(row)
    out = []
    for (gid, market_id, book), history in sorted(groups.items()):
        history.sort(key=lambda r: r["created_at"])
        changes = []
        for previous, current in zip(history, history[1:]):
            changed = [field for field in (
                "goalie_fingerprint", "roster_fingerprint", "data_snapshot_id", "market_snapshot_ref",
                "parameter_fingerprint", "constants_id", "config_hash",
            ) if previous[field] != current[field]]
            changes.append({"at": current["created_at"], "changed": changed,
                            "model_probability_delta": current["model_probability"] - previous["model_probability"]})
        out.append({
            "game_id": gid, "market_id": market_id, "sportsbook": book,
            "first_priced": history[0]["created_at"], "last_priced": history[-1]["created_at"],
            "reprices": len(history) - 1, "changes": changes,
            "unexplained_reprice": any(not c["changed"] for c in changes),
        })
    return out
