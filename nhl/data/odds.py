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
from dataclasses import dataclass
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


@dataclass
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
            if not (r["source"] or "").strip():
                raise FieldError("source required")
            conf_raw = (r.get("confidence") or "").strip()
            from .validation import strict_float

            confidence = strict_float(conf_raw, "confidence", 0.0, 1.0) if conf_raw else None
            p = prov.published_report("goalie:" + r["source"], entry.snapshot_id, entry.fetched_at, start, published, mode)
            out.records.append(GoalieReport(
                game_id=r["game_id"], team=r["team"], goalie_id=r["goalie_id"],
                state=GoalieState(r["state"].strip().upper()), source=r["source"].strip(),
                available_at=p.available_at, provenance=p, confidence=confidence,
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


# --------------------------------------------------------------------------- v2 contract

MARKET_COLUMNS = ("provider", "source_ts", "observed_at", "game_id", "book", "market", "period", "selection", "line",
                  "team", "participant_id", "price", "odds_format", "status", "max_stake")


def is_market_v2(payload: bytes) -> bool:
    head = payload.decode("utf-8-sig").split("\n", 1)[0].strip().split(",")
    return set(MARKET_COLUMNS) <= set(head)


def parse_market_observations_checked(payload: bytes, entry: SnapshotEntry) -> OddsParseResult:
    """Provider-neutral market observation contract (see docs/odds_and_close_methodology.md).

    Per row: provider identity, provider ``source_ts`` (optional, <= observed_at),
    ``observed_at`` (observation time, <= fetch time), game, book, market, period,
    selection, line, team/participant, price in its ORIGINAL format, market status.
    Rows are rejected -- never repaired -- when any field is missing, impossible or
    ambiguous; same-instant duplicates with different prices/status are dropped.
    """

    from nhl.contracts import MarketStatus, OddsFormat, Period
    from nhl.market.novig import decimal_to_american

    out = OddsParseResult()
    reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig")))
    _check_columns(reader, MARKET_COLUMNS)
    seen: dict[tuple, OddsSnapshot] = {}
    for i, r in enumerate(reader):
        key = f"row{i + 2}:{r.get('game_id')}:{r.get('market')}:{r.get('selection')}"
        try:
            provider = (r["provider"] or "").strip()
            if not provider:
                raise FieldError("provider required")
            observed = strict_utc(r["observed_at"], "observed_at")
            source_ts = strict_utc(r["source_ts"], "source_ts") if (r["source_ts"] or "").strip() else None
            fmt = OddsFormat((r["odds_format"] or "").strip().upper())
            if fmt is OddsFormat.AMERICAN:
                american, dec = strict_int(r["price"], "price", None), None
            else:
                from .validation import strict_float

                dec = strict_float(r["price"], "price", 1.0)
                american = decimal_to_american(dec)
            status = MarketStatus((r["status"] or "").strip().upper())
            p = prov.published_report(f"odds:{provider}", entry.snapshot_id, entry.fetched_at, observed, observed,
                                      prov.CaptureMode.BACKFILL, source_version=provider)
            period_raw = (r["period"] or "").strip().upper()
            snap = OddsSnapshot(
                snapshot_ts=observed, book=r["book"].strip().lower(), game_id=r["game_id"],
                market=MarketType(r["market"].strip().upper()), selection=Selection(r["selection"].strip().upper()),
                price_american=american, line=_opt_float(r["line"]), team=(r["team"] or "").strip() or None,
                max_stake=_opt_float(r["max_stake"]), source=provider, snapshot_id=entry.snapshot_id, provenance=p,
                period=Period(period_raw) if period_raw else None,
                participant=(r["participant_id"] or "").strip() or None, odds_format=fmt, price_decimal=dec,
                source_ts=source_ts, market_status=status, provider=provider,
            )
        except (FieldError, prov.ProvenanceError, ValueError, KeyError) as exc:
            out.reject("market", key, str(exc))
            continue
        k = (snap.snapshot_ts, snap.book, snap.game_id, snap.market, snap.period, snap.selection, snap.line, snap.team,
             snap.participant, snap.provider)
        if k in seen:
            prev = seen[k]
            if (prev.decimal, prev.market_status) == (snap.decimal, snap.market_status):
                out.dupes += 1
            else:
                out.reject("market", key, "conflicting observations for the same outcome and instant")
                if prev in out.records:
                    out.records.remove(prev)
            continue
        seen[k] = snap
        out.records.append(snap)
    out.records.sort(key=lambda s: (s.game_id, s.market.value, s.period.value, s.team or "", s.participant or "",
                                    s.line or 0.0, s.selection.value, s.book, s.snapshot_ts))
    return out


def market_observations_to_csv(rows: list[OddsSnapshot]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(MARKET_COLUMNS), lineterminator="\n")
    w.writeheader()
    for s in rows:
        w.writerow({
            "provider": s.provider or s.source, "source_ts": "" if s.source_ts is None else s.source_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "observed_at": s.snapshot_ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "game_id": s.game_id, "book": s.book,
            "market": s.market.value, "period": s.period.value, "selection": s.selection.value,
            "line": "" if s.line is None else s.line, "team": s.team or "", "participant_id": s.participant or "",
            "price": s.price_american if s.price_decimal is None else s.price_decimal, "odds_format": s.odds_format.value,
            "status": s.market_status.value, "max_stake": "" if s.max_stake is None else s.max_stake,
        })
    return buf.getvalue()
