"""Operational quality summaries derived only from raw snapshot/failure manifests."""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from nhl.data.snapshots import RawSnapshotStore
from nhl.timeutil import fmt_ts, parse_ts


STALE_MINUTES = {"nhl_api": 180, "moneypuck": 2160, "odds": 30, "goalie_reports": 180}


def source_quality_rows(raw: RawSnapshotStore, now: datetime, rejected: Counter | None = None) -> list[dict]:
    entries = list(raw.entries())
    failures = list(raw.failures())
    sources = sorted(set(STALE_MINUTES) | {e.source for e in entries} | {f["source"] for f in failures})
    rows = []
    for source in sources:
        good = [e for e in entries if e.source == source]
        bad = [f for f in failures if f["source"] == source]
        latest = max((e.fetched_at for e in good), default=None)
        last_bad = max((parse_ts(f["attempted_at"]) for f in bad), default=None)
        age = (now - latest).total_seconds() / 60 if latest else None
        threshold = STALE_MINUTES.get(source, 1440)
        error = bool(last_bad and (latest is None or last_bad > latest))
        rows.append({
            "source": source, "last_successful_fetch": fmt_ts(latest) if latest else "",
            "age_minutes": "" if age is None else round(age, 1),
            "error_status": "ERROR" if error else "OK" if latest else "NEVER",
            "completeness": "AVAILABLE" if latest else "MISSING",
            "provenance_eligibility": "REPLAY_REQUIRED" if latest else "INELIGIBLE",
            "stale_threshold_minutes": threshold,
            "stale": latest is None or age > threshold,
            "rejected_records": int((rejected or {}).get(source, 0)),
            "anomaly_count": len(bad),
        })
    return rows
