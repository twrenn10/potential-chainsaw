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

from . import provenance as prov
from .snapshots import RawSnapshotStore, SnapshotEntry
from .validation import FieldError, ParseResult, strict_int, strict_utc

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
    res = parse_odds_checked(payload, entry)
    return res.require_clean(), res.dupes


class OddsParseResult(ParseResult[OddsSnapshot]):
    dupes: int = 0


def parse_odds_checked(payload: bytes, entry: SnapshotEntry) -> OddsParseResult:
    """Odds with provenance. ``snapshot_ts`` is the source's observation time, so it is
    the publication time: historically knowable even when the file is fetched later.
    A row observed AFTER the file was fetched is impossible and rejected."""

    out = OddsParseResult()
    reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig")))
    _check_columns(reader, ODDS_COLUMNS)
    seen: set[tuple] = set()
    for i, r in enumerate(reader):
        key = f"row{i + 2}:{r.get('game_id')}:{r.get('market')}:{r.get('selection')}"
        try:
            ts = strict_utc(r["snapshot_ts"], "snapshot_ts")
            p = prov.published_report("odds:" + (r.get("source") or "unknown"), entry.snapshot_id, entry.fetched_at,
                                      ts, ts, prov.CaptureMode.BACKFILL)
            snap = OddsSnapshot(
                snapshot_ts=ts, book=r["book"].strip().lower(), game_id=r["game_id"],
                market=MarketType(r["market"].strip().upper()), selection=Selection(r["selection"].strip().upper()),
                price_american=strict_int(r["price_american"], "price_american", None), line=_opt_float(r["line"]),
                team=(r["team"] or "").strip() or None, max_stake=_opt_float(r["max_stake"]), source=r["source"],
                snapshot_id=entry.snapshot_id, provenance=p,
            )
        except (FieldError, prov.ProvenanceError, ValueError, KeyError) as exc:
            out.reject("odds", key, str(exc))
            continue
        k = (snap.snapshot_ts, snap.book, snap.game_id, snap.market, snap.selection, snap.line, snap.team)
        if k in seen:
            # Same outcome at the same instant twice: identical price is a harmless dupe,
            # a different price is ambiguous and both are dropped.
            prev = next(x for x in out.records if (x.snapshot_ts, x.book, x.game_id, x.market, x.selection, x.line, x.team) == k)
            if prev.price_american == snap.price_american:
                out.dupes += 1
            else:
                out.reject("odds", key, "conflicting prices for the same outcome and instant")
                out.records.remove(prev)
            continue
        seen.add(k)
        out.records.append(snap)
    out.records.sort(key=lambda s: (s.game_id, s.market.value, s.team or "", s.line or 0.0, s.selection.value, s.book, s.snapshot_ts))
    return out


def parse_goalie_reports_checked(
    payload: bytes, entry: SnapshotEntry, starts: dict[str, datetime], mode: prov.CaptureMode = prov.CaptureMode.LIVE
) -> ParseResult[GoalieReport]:
    """Starter reports. The ``available_at`` column is the SOURCE publication time and
    becomes ``published_at``; an empty value means only the capture time is known,
    which is causal only for a live capture before puck drop (``starts[game_id]``)."""

    out: ParseResult[GoalieReport] = ParseResult()
    reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig")))
    _check_columns(reader, GOALIE_COLUMNS)
    for i, r in enumerate(reader):
        key = f"row{i + 2}:{r.get('game_id')}:{r.get('team')}"
        try:
            start = starts.get(r["game_id"])
            if start is None:
                raise FieldError(f"unknown game {r['game_id']}")
            published = strict_utc(r["available_at"], "available_at") if r["available_at"].strip() else None
            if published is not None and published >= start:
                raise FieldError("report published at/after puck drop is not pregame information")
            p = prov.published_report("goalie:" + r["source"], entry.snapshot_id, entry.fetched_at, start, published, mode)
            out.records.append(GoalieReport(
                game_id=r["game_id"], team=r["team"], goalie_id=r["goalie_id"],
                state=GoalieState(r["state"].strip().upper()), source=r["source"],
                available_at=p.available_at, provenance=p,
            ))
        except (FieldError, prov.ProvenanceError, ValueError, KeyError) as exc:
            out.reject("goalie_reports", key, str(exc))
    out.records.sort(key=lambda g: (g.game_id, g.team, g.available_at))
    return out


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
