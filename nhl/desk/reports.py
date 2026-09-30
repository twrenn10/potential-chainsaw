"""Deterministic desk exports.

Every report is a pure function of stored state (artifacts, closes, results, overrides,
point-in-time inputs). No export embeds the export time; rows are fully sorted; CSV
uses fixed column order and ``\\n`` line endings, JSON is key-sorted. Two runs from
identical state are byte-identical.

Reports:
* CURRENT_SLATE.csv      -- latest artifact per (game, outcome, book) for the slate
* PREDICTION_HISTORY.csv -- one row per immutable artifact, in append order
* CLV_REPORT.csv         -- artifact execution price vs the defined close (close-v1)
* GRADING_REPORT.csv     -- result and settlement status per artifact
* GOVERNANCE_REPORT.csv  -- lane, evidence, eligibility, every block reason, overrides
* DATA_FRESHNESS.csv     -- stale / missing inputs per game and category
* REPORTS_manifest.json  -- sha256 of every file + row counts
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable

from nhl.contracts import RosterSlot
from nhl.data.pit import PointInTimeView
from nhl.governance.overrides import effective_status
from nhl.ledger.predictions import COLUMNS
from nhl.timeutil import fmt_ts

SLATE_COLUMNS = [
    "game_id", "matchup", "puck_drop", "market", "period", "selection", "line", "team", "sportsbook", "execution_price",
    "execution_decimal", "raw_implied_probability", "no_vig_probability", "novig_method", "model_probability",
    "model_fair_price", "probability_edge", "ev_per_unit", "goalie_state", "created_at", "as_of", "status",
    "effective_status", "evidence_lane", "eligibility", "block_reasons", "quote_age_minutes", "market_observed_at",
    "prediction_id",
]
CLV_COLUMNS = ["prediction_id", "game_id", "market", "selection", "line", "team", "sportsbook", "evidence_lane", "as_of",
               "execution_price", "no_vig_at_prediction", "close_status", "close_reason", "close_basis", "close_cutoff",
               "close_price", "close_no_vig", "clv_ev", "clv_prob_move", "edge_at_close", "model_probability"]
GRADING_COLUMNS = ["prediction_id", "game_id", "market", "selection", "line", "team", "sportsbook", "evidence_lane",
                   "status", "eligibility", "execution_price", "settlement", "outcome", "profit_units", "probability_edge",
                   "ev_per_unit"]
GOV_COLUMNS = ["prediction_id", "game_id", "market_key", "sportsbook", "as_of", "evidence_lane", "status",
               "effective_status", "eligibility", "shadow_lane", "block_reasons", "reason_codes", "overrides"]
FRESH_COLUMNS = ["game_id", "category", "status", "latest_available_at", "age_minutes", "detail"]


def _fmt(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, float):
        return repr(round(v, 8))
    return v


def write_csv(path: Path, rows: Iterable[dict[str, Any]], columns: list[str]) -> int:
    n = 0
    with path.open("w", encoding="utf-8", newline="") as h:
        w = csv.DictWriter(h, fieldnames=columns, lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: _fmt(r.get(c)) for c in columns})
            n += 1
    return n


def current_slate(rows: list[dict[str, Any]], matchups: dict[str, str], overrides: dict[str, list[dict]]) -> list[dict]:
    latest: dict[tuple[str, str, str], dict] = {}
    for r in rows:  # rows are in append order -> last wins
        latest[(r["game_id"], r["market_key"], r["sportsbook"])] = r
    out = []
    for key in sorted(latest):
        r = dict(latest[key])
        r["matchup"] = matchups.get(r["game_id"], "")
        r["effective_status"] = effective_status(r["status"], overrides.get(r["prediction_id"], []))
        out.append(r)
    return sorted(out, key=lambda r: (r["puck_drop"], r["game_id"], r["market"], r["market_key"], r["sportsbook"]))


def governance_rows(rows: list[dict[str, Any]], overrides: dict[str, list[dict]]) -> list[dict]:
    out = []
    for r in rows:
        ov = overrides.get(r["prediction_id"], [])
        out.append({**r, "effective_status": effective_status(r["status"], ov),
                    "overrides": json.dumps([{k: o[k] for k in ("action", "operator", "reason", "effective", "refusal")} for o in ov],
                                            sort_keys=True)})
    return out


def freshness_rows(view: PointInTimeView, game_ids: list[str], rosters: list[RosterSlot], max_odds_age_min: float,
                   stale_roster_days: float = 14.0, stale_stats_days: float = 3.0) -> list[dict]:
    """Per game: odds, schedule, goalie reports, rosters, team inputs, player inputs, event-level inputs."""

    now = view.as_of
    games = {g.game_id: g for g in view.games()}
    stats = view.team_stats()
    results = view.results()
    players = view.player_seasons()
    out = []

    def add(gid: str, cat: str, latest: datetime | None, limit: timedelta | None, detail: str = "") -> None:
        if latest is None:
            out.append({"game_id": gid, "category": cat, "status": "MISSING", "latest_available_at": "", "age_minutes": "", "detail": detail})
            return
        age = (now - latest).total_seconds() / 60.0
        status = "STALE" if limit is not None and now - latest > limit else "OK"
        out.append({"game_id": gid, "category": cat, "status": status, "latest_available_at": fmt_ts(latest),
                    "age_minutes": round(age, 1), "detail": detail})

    for gid in sorted(game_ids):
        g = games.get(gid)
        if g is None:
            add(gid, "schedule", None, None, "not in schedule at as_of")
            continue
        add(gid, "schedule", g.available_at, None, g.status)
        odds = [o for o in view.odds_snapshots({gid})]
        add(gid, "odds", max((o.snapshot_ts for o in odds), default=None), timedelta(minutes=max_odds_age_min), f"{len(odds)} observations")
        reps = view.goalie_report_history(gid)
        for team in (g.away, g.home):
            tr = [r for r in reps if r.team == team]
            add(gid, f"goalie_reports:{team}", max((r.available_at for r in tr), default=None), timedelta(hours=30),
                ",".join(sorted({r.state.value for r in tr})) or "workload model only")
            rs = [s.available_at for s in rosters if s.team == team and s.available_at is not None and s.available_at <= now]
            add(gid, f"roster:{team}", max(rs, default=None), timedelta(days=stale_roster_days))
            ts = [s.available_at for s in stats if s.team == team]
            add(gid, f"team_inputs:{team}", max(ts, default=None), timedelta(days=stale_stats_days))
        add(gid, "player_inputs", max((p.available_at for p in players), default=None), None, f"{len(players)} player-seasons")
        add(gid, "event_inputs", max((r.available_at for r in results), default=None), timedelta(days=stale_stats_days),
            f"{len(results)} results")
    return out


def export_reports(
    out_dir: str | Path,
    prediction_rows: list[dict[str, Any]],
    settled_rows: list[dict[str, Any]],
    matchups: dict[str, str],
    freshness: list[dict[str, Any]],
    overrides: list[dict[str, Any]] | None = None,
    slate_game_ids: set[str] | None = None,
) -> dict[str, str]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    ov: dict[str, list[dict]] = {}
    for o in overrides or []:
        ov.setdefault(o["prediction_id"], []).append(o)
    slate_src = [r for r in prediction_rows if slate_game_ids is None or r["game_id"] in slate_game_ids]
    settled = sorted(settled_rows, key=lambda r: r["prediction_id"])
    files = {
        "CURRENT_SLATE.csv": (current_slate(slate_src, matchups, ov), SLATE_COLUMNS),
        "PREDICTION_HISTORY.csv": (prediction_rows, COLUMNS),
        "CLV_REPORT.csv": (settled, CLV_COLUMNS),
        "GRADING_REPORT.csv": (settled, GRADING_COLUMNS),
        "GOVERNANCE_REPORT.csv": (governance_rows(prediction_rows, ov), GOV_COLUMNS),
        "DATA_FRESHNESS.csv": (sorted(freshness, key=lambda r: (r["game_id"], r["category"])), FRESH_COLUMNS),
    }
    counts, paths = {}, {}
    for name, (rows, cols) in files.items():
        counts[name] = write_csv(out / name, rows, cols)
        paths[name] = str(out / name)
    manifest = {"files": {n: sha256((out / n).read_bytes()).hexdigest() for n in sorted(files)}, "row_counts": counts}
    (out / "REPORTS_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    paths["manifest"] = str(out / "REPORTS_manifest.json")
    return paths
