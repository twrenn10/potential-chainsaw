"""Market views over timestamped odds: reference price at as_of, open/close, movement marks.

A "market group" is every outcome of one book's market at one line (e.g. BookA
TOTAL 6.5 OVER+UNDER). No-vig probabilities require the complete group observed
from the same book, each price no older than ``max_pair_skew`` from the newest.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from nhl.contracts import MarketType, OddsSnapshot, Selection

from .novig import devig

ET = ZoneInfo("America/New_York")
SELECTIONS = {
    MarketType.ML: (Selection.HOME, Selection.AWAY),
    MarketType.REG_3WAY: (Selection.HOME, Selection.DRAW, Selection.AWAY),
    MarketType.PUCK_LINE: (Selection.HOME, Selection.AWAY),
    MarketType.TOTAL: (Selection.OVER, Selection.UNDER),
    MarketType.TEAM_TOTAL: (Selection.OVER, Selection.UNDER),
}


@dataclass(frozen=True)
class GroupKey:
    game_id: str
    market: MarketType
    line: float | None  # PUCK_LINE keyed by the HOME line
    team: str | None


@dataclass(frozen=True)
class GroupQuote:
    key: GroupKey
    book: str
    observed_at: datetime  # newest leg
    prices: dict[Selection, int]
    no_vig: dict[Selection, float]
    max_stake: float | None


def group_key(s: OddsSnapshot) -> GroupKey:
    line = s.line
    if s.market is MarketType.PUCK_LINE and s.selection is Selection.AWAY and line is not None:
        line = -line
    return GroupKey(s.game_id, s.market, line, s.team)


def quotes_at(
    odds: list[OddsSnapshot],
    as_of: datetime,
    max_pair_skew: timedelta = timedelta(minutes=5),
    method: str = "multiplicative",
) -> list[GroupQuote]:
    """Latest complete, de-vigged group per (book, group) visible at ``as_of``."""

    latest: dict[tuple[str, GroupKey, Selection], OddsSnapshot] = {}
    for s in odds:
        if s.snapshot_ts > as_of:
            continue
        k = (s.book, group_key(s), s.selection)
        if k not in latest or s.snapshot_ts >= latest[k].snapshot_ts:
            latest[k] = s
    groups: dict[tuple[str, GroupKey], dict[Selection, OddsSnapshot]] = defaultdict(dict)
    for (book, gk, sel), s in latest.items():
        groups[(book, gk)][sel] = s
    out = []
    for (book, gk), legs in sorted(groups.items(), key=lambda kv: (kv[0][1].game_id, kv[0][1].market.value, str(kv[0][1].line), kv[0][1].team or "", kv[0][0])):
        needed = SELECTIONS[gk.market]
        if any(sel not in legs for sel in needed):
            continue
        times = [legs[sel].snapshot_ts for sel in needed]
        if max(times) - min(times) > max_pair_skew:
            continue
        prices = {sel: legs[sel].price_american for sel in needed}
        probs = devig([prices[sel] for sel in needed], method)
        stakes = [legs[sel].max_stake for sel in needed if legs[sel].max_stake is not None]
        out.append(
            GroupQuote(
                key=gk,
                book=book,
                observed_at=max(times),
                prices=prices,
                no_vig=dict(zip(needed, probs)),
                max_stake=min(stakes) if stakes else None,
            )
        )
    return out


def consensus_no_vig(quotes: list[GroupQuote]) -> dict[GroupKey, dict[Selection, float]]:
    """Equal-weight mean of per-book no-vig probabilities for each group."""

    acc: dict[GroupKey, list[dict[Selection, float]]] = defaultdict(list)
    for q in quotes:
        acc[q.key].append(q.no_vig)
    out = {}
    for gk, rows in acc.items():
        out[gk] = {sel: sum(r[sel] for r in rows) / len(rows) for sel in rows[0]}
    return out


def closing_quotes(odds: list[OddsSnapshot], puck_drop: datetime) -> list[GroupQuote]:
    """Close = last complete quote strictly before puck drop."""

    return quotes_at(odds, puck_drop - timedelta(seconds=1))


def movement_marks(puck_drop: datetime) -> dict[str, datetime]:
    """OPEN is handled separately (first observation). Marks in US/Eastern clock time."""

    local = puck_drop.astimezone(ET)
    day = local.date()
    marks = {
        "9AM": datetime.combine(day, time(9, 0), ET),
        "NOON": datetime.combine(day, time(12, 0), ET),
        "3PM": datetime.combine(day, time(15, 0), ET),
        "T-60": local - timedelta(minutes=60),
    }
    return {k: v.astimezone(puck_drop.tzinfo) for k, v in marks.items() if v < local}


def opening_quotes(odds: list[OddsSnapshot]) -> list[GroupQuote]:
    """First complete quote per (book, group)."""

    by_group: dict[tuple[str, GroupKey], list[OddsSnapshot]] = defaultdict(list)
    for s in odds:
        by_group[(s.book, group_key(s))].append(s)
    out = []
    for (_book, _gk), rows in sorted(by_group.items(), key=lambda kv: (kv[0][1].game_id, kv[0][1].market.value, str(kv[0][1].line), kv[0][1].team or "", kv[0][0])):
        for ts in sorted({r.snapshot_ts for r in rows}):
            qs = quotes_at(rows, ts)
            if qs:
                out.append(qs[0])
                break
    return out
