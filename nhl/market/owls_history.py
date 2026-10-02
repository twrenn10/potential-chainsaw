"""Bounded Owls historical retrieval for audit-only, post-hoc analysis."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any, Callable, Mapping

from nhl.data.snapshots import SnapshotEntry
from nhl.market.owls import OwlsHTTPError, OwlsInsightClient
from nhl.timeutil import fmt_ts
from nhl.timeutil import parse_ts


def normalize_archived_snapshots(records: list[dict], event_map: Mapping[str, str]) -> tuple[list[dict], list[str]]:
    """Market-v2-shaped audit rows; never written to the replayable odds source."""

    market_map = {"h2h": "ML", "spreads": "PUCK_LINE", "totals": "TOTAL"}
    side_map = {"home": "HOME", "away": "AWAY", "draw": "DRAW", "over": "OVER", "under": "UNDER"}
    out, excluded = [], []
    for i, row in enumerate(records):
        provider_event = str(row.get("providerEventId", ""))
        game_id = event_map.get(provider_event)
        market, selection = market_map.get(row.get("market")), side_map.get(str(row.get("side", "")).lower())
        if not game_id:
            excluded.append(f"UNMAPPED_EVENT:{provider_event}"); continue
        if market is None or selection is None:
            excluded.append(f"UNSUPPORTED_MARKET_OR_SIDE:{i}"); continue
        if row.get("inPlay") is not False:
            excluded.append(f"PHASE_NOT_PREGAME:{i}"); continue
        timestamp, price = row.get("recordedAt"), row.get("price")
        try: observed = parse_ts(timestamp)
        except (TypeError, ValueError):
            excluded.append(f"NO_HISTORICAL_TIMESTAMP:{i}"); continue
        if not isinstance(price, (int, float)) or -100 < float(price) < 100:
            excluded.append(f"INVALID_PRICE:{i}"); continue
        line = row.get("pointValue")
        if market != "ML" and not isinstance(line, (int, float)):
            excluded.append(f"MISSING_LINE:{i}"); continue
        out.append({"provider": "owls", "source_ts": "", "observed_at": fmt_ts(observed),
                    "game_id": game_id, "book": str(row.get("book", "")).lower(), "market": market,
                    "period": "FULL_GAME", "selection": selection, "line": "" if line is None else line,
                    "team": "", "participant_id": "", "price": price, "odds_format": "AMERICAN",
                    "status": "OPEN", "max_stake": ""})
    out.sort(key=lambda r: tuple(str(r[k]) for k in ("game_id", "book", "market", "selection", "line", "observed_at")))
    return out, excluded


@dataclass
class PageResult:
    records: list[dict] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    pages_requested: int = 0
    pages_completed: int = 0
    duplicates: int = 0
    completeness: str = "COMPLETE"
    errors: list[str] = field(default_factory=list)


def _at(root: Any, path: tuple[str, ...]) -> Any:
    value = root
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def capture_offset_pages(
    client: OwlsInsightClient,
    endpoint: str,
    params: Mapping[str, object],
    records_path: tuple[str, ...],
    *,
    identity: Callable[[dict], str],
    limit: int = 500,
    max_pages: int = 100,
    max_records: int = 50_000,
) -> PageResult:
    """Capture offset pages raw-first with repeat/size guards and stable dedupe."""

    if not 1 <= limit <= 500 or max_pages < 1 or max_records < 1:
        raise ValueError("invalid pagination bounds")
    out = PageResult()
    seen_pages: set[str] = set()
    unique: dict[str, dict] = {}
    offset = 0
    for _ in range(max_pages):
        out.pages_requested += 1
        query = {**params, "limit": limit, "offset": offset}
        try:
            entry = client.capture(endpoint, query)
        except OwlsHTTPError as exc:
            out.completeness = "AUTH_FAILURE" if exc.status in {401, 403} else (
                "PARTIAL_RATE_LIMIT" if exc.status == 429 else "PARTIAL_PROVIDER_ERROR")
            out.errors.append(f"HTTP_{exc.status}")
            break
        out.artifacts.append(entry.snapshot_id)
        try:
            payload = client.store.get_json(entry)
            records = _at(payload, records_path)
            if not isinstance(records, list):
                raise ValueError("records path is not a list")
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            out.completeness = "INVALID_RESPONSE"
            out.errors.append(type(exc).__name__)
            break
        digest = sha256(json.dumps(records, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if digest in seen_pages and records:
            out.completeness = "PARTIAL_PAGINATION_GUARD"
            out.errors.append("REPEATED_PAGE")
            break
        seen_pages.add(digest)
        out.pages_completed += 1
        for record in records:
            if not isinstance(record, dict):
                out.completeness = "INVALID_RESPONSE"
                out.errors.append("NON_OBJECT_RECORD")
                break
            key = identity(record)
            if key in unique:
                out.duplicates += 1
            else:
                unique[key] = record
        if out.completeness == "INVALID_RESPONSE":
            break
        if len(unique) > max_records:
            out.completeness = "PARTIAL_PAGINATION_GUARD"
            out.errors.append("MAX_RECORDS")
            break
        pagination = _at(payload, ("data", "pagination")) or {}
        has_more = pagination.get("hasMore") if isinstance(pagination, dict) else None
        if has_more is False or (has_more is None and len(records) < limit):
            break
        offset += limit
    else:
        out.completeness = "PARTIAL_PAGINATION_GUARD"
        out.errors.append("MAX_PAGES")
    out.records = [unique[key] for key in sorted(unique)][:max_records]
    if not out.records and out.completeness == "COMPLETE":
        out.completeness = "EMPTY_VALID"
    return out


def capture_bulk_history(client: OwlsInsightClient, league: str, start_date: str, end_date: str,
                         *, max_pages: int = 100, max_records: int = 50_000,
                         event_map: Mapping[str, str] | None = None) -> tuple[dict, SnapshotEntry]:
    """Capture games and provider closes. Output remains explicitly post-hoc/audit-only."""

    common = {"sport": league.lower(), "startDate": start_date, "endDate": end_date}
    games = capture_offset_pages(client, "/api/v1/history/games", common, ("data", "games"),
                                 identity=lambda r: str(r.get("eventId", "")), max_pages=max_pages,
                                 max_records=max_records)
    remaining_pages = max_pages - games.pages_requested
    remaining_records = max_records - len(games.records)
    if remaining_pages > 0 and remaining_records > 0:
        closes = capture_offset_pages(client, "/api/v1/history/closing-odds", common, ("data", "odds"),
                                      identity=lambda r: "|".join(str(r.get(k, "")) for k in
                                          ("eventId", "book", "source", "lastUpdate")),
                                      max_pages=remaining_pages, max_records=remaining_records)
        remaining_pages -= closes.pages_requested
        remaining_records -= len(closes.records)
    else:
        closes = PageResult(completeness="PARTIAL_PAGINATION_GUARD", errors=["GLOBAL_GUARD"])
    results_artifacts, results_errors = [], []
    try:
        results_artifacts.append(client.capture("/api/v1/nhl/results").snapshot_id)
    except OwlsHTTPError as exc:
        results_errors.append(f"RESULTS_HTTP_{exc.status}")
    odds_results: list[PageResult] = []
    for game in games.records:
        event_id = str(game.get("eventId", ""))
        if not event_id or remaining_pages <= 0 or remaining_records <= 0:
            break
        result = capture_offset_pages(client, "/api/v1/history/odds", {"eventId": event_id},
                                      ("data", "snapshots"),
                                      identity=lambda r, eid=event_id: eid + "|" + "|".join(str(r.get(k, "")) for k in
                                          ("book", "market", "side", "pointValue", "recordedAt")),
                                      max_pages=remaining_pages, max_records=remaining_records)
        result.records = [{**record, "providerEventId": event_id} for record in result.records]
        odds_results.append(result)
        remaining_pages -= result.pages_requested
        remaining_records -= len(result.records)
        if result.completeness not in {"COMPLETE", "EMPTY_VALID"}:
            break
    states = [games.completeness, closes.completeness] + [r.completeness for r in odds_results]
    if all(s in {"COMPLETE", "EMPTY_VALID"} for s in states):
        completeness = "EMPTY_VALID" if all(s == "EMPTY_VALID" for s in states) else "COMPLETE"
    else:
        completeness = next(s for s in states if s not in {"COMPLETE", "EMPTY_VALID"})
    if results_errors and completeness in {"COMPLETE", "EMPTY_VALID"}:
        completeness = "PARTIAL_PROVIDER_ERROR"
    artifacts = games.artifacts + closes.artifacts + results_artifacts + [sid for result in odds_results for sid in result.artifacts]
    generated = max((e.fetched_at for sid in artifacts for e in client.store.entries()
                     if e.snapshot_id == sid), default=client.clock())
    run_seed = json.dumps([league.upper(), start_date, end_date, artifacts], separators=(",", ":"))
    historical_records = [x for r in odds_results for x in r.records]
    normalized, normalization_exclusions = normalize_archived_snapshots(historical_records, event_map or {})
    provider_times = sorted(r["observed_at"] for r in normalized)
    manifest = {
        "schema_version": "owls-history-manifest-v1", "provider": "owls", "league": league.upper(),
        "requested_start_date": start_date, "requested_end_date": end_date,
        "endpoints": ["/api/v1/history/games", "/api/v1/history/odds", "/api/v1/history/closing-odds",
                      "/api/v1/nhl/results"],
        "pages_requested": games.pages_requested + closes.pages_requested + sum(r.pages_requested for r in odds_results),
        "pages_completed": games.pages_completed + closes.pages_completed + sum(r.pages_completed for r in odds_results),
        "records_received": len(games.records) + len(closes.records) + sum(len(r.records) for r in odds_results),
        "records_deduplicated": games.duplicates + closes.duplicates + sum(r.duplicates for r in odds_results),
        "event_count": len(games.records), "normalized_market_count": len(normalized),
        "excluded_market_count": len(normalization_exclusions),
        "first_provider_timestamp": provider_times[0] if provider_times else "",
        "last_provider_timestamp": provider_times[-1] if provider_times else "",
        "completeness_status": completeness, "errors": games.errors + closes.errors + results_errors +
            [e for r in odds_results for e in r.errors],
        "run_id": "OH:" + sha256(run_seed.encode()).hexdigest()[:20],
        "generated_at_utc": fmt_ts(generated), "raw_artifact_lineage": artifacts,
        "causality": "POST_HOC_ANALYSIS_ONLY", "eligible_for_model_input": False,
        "games": games.records, "historical_odds": historical_records,
        "normalized_market_v2_audit_rows": normalized, "normalization_exclusions": normalization_exclusions,
        "provider_closes": closes.records,
    }
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    entry = client.store.put("owls_history_manifest", manifest["run_id"], payload, generated,
                             {"provider": "owls", "schema_version": manifest["schema_version"],
                              "completeness_status": completeness})
    return manifest, entry
