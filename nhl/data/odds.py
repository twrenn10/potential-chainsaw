"""Timestamped odds and goalie-report snapshot ingestion.

Canonical odds CSV (one row per book/outcome/observation)::

    snapshot_ts,book,game_id,market,selection,line,team,price_american,max_stake,source

Rules:
* ``snapshot_ts`` is when the price was OBSERVED, not when the file was written.
* Rows are never merged or overwritten; the same outcome at two times is two rows.
* Exact duplicates (same ts/book/outcome/price) are dropped and counted, mirroring
  tournament_v2's dedupe stage.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime

from nhl.contracts import GoalieReport, GoalieState, MarketType, OddsSnapshot, Selection

from .snapshots import RawSnapshotStore, SnapshotEntry

ODDS_COLUMNS = ("snapshot_ts", "book", "game_id", "market", "selection", "line", "team", "price_american", "max_stake", "source")
GOALIE_COLUMNS = ("available_at", "game_id", "team", "goalie_id", "state", "source")


def _opt_float(value: str | None) -> float | None:
    token = (value or "").strip()
    return float(token) if token else None


def _check_columns(reader: csv.DictReader, required: tuple[str, ...]) -> None:
    missing = [c for c in required if c not in (reader.fieldnames or [])]
    if missing:
        raise ValueError(f"missing columns: {missing}")


def parse_odds_csv(payload: bytes, snapshot_id: str = "") -> tuple[list[OddsSnapshot], int]:
    reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig")))
    _check_columns(reader, ODDS_COLUMNS)
    seen: set[tuple] = set()
    out: list[OddsSnapshot] = []
    dupes = 0
    for r in reader:
        snap = OddsSnapshot(
            snapshot_ts=r["snapshot_ts"],
            book=r["book"].strip().lower(),
            game_id=r["game_id"],
            market=MarketType(r["market"].strip().upper()),
            selection=Selection(r["selection"].strip().upper()),
            price_american=int(float(r["price_american"])),
            line=_opt_float(r["line"]),
            team=(r["team"] or "").strip() or None,
            max_stake=_opt_float(r["max_stake"]),
            source=r["source"],
            snapshot_id=snapshot_id,
        )
        key = (snap.snapshot_ts, snap.book, snap.game_id, snap.market, snap.selection, snap.line, snap.team, snap.price_american)
        if key in seen:
            dupes += 1
            continue
        seen.add(key)
        out.append(snap)
    out.sort(key=lambda s: (s.game_id, s.market.value, s.team or "", s.line or 0.0, s.selection.value, s.book, s.snapshot_ts))
    return out, dupes


def ingest_odds_file(store: RawSnapshotStore, key: str, payload: bytes, fetched_at: datetime) -> tuple[list[OddsSnapshot], int]:
    entry: SnapshotEntry = store.put("odds", key, payload, fetched_at)
    return parse_odds_csv(payload, snapshot_id=entry.snapshot_id)


def parse_goalie_reports_csv(payload: bytes) -> list[GoalieReport]:
    reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig")))
    _check_columns(reader, GOALIE_COLUMNS)
    out = [
        GoalieReport(
            game_id=r["game_id"],
            team=r["team"],
            goalie_id=r["goalie_id"],
            state=GoalieState(r["state"].strip().upper()),
            source=r["source"],
            available_at=r["available_at"],
        )
        for r in reader
    ]
    return sorted(out, key=lambda g: (g.game_id, g.team, g.available_at))


def odds_to_csv(rows: list[OddsSnapshot]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(ODDS_COLUMNS), lineterminator="\n")
    writer.writeheader()
    for s in rows:
        row = s.to_row()
        writer.writerow({c: ("" if row.get(c) is None else row.get(c)) for c in ODDS_COLUMNS})
    return buf.getvalue()
