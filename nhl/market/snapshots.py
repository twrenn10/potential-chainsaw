"""Market views over timestamped observations: quotes at ``as_of``, the close, movement.

A *market group* is every outcome of one book's market for one game, period, line,
team/participant (e.g. BookA TOTAL 6.5 OVER+UNDER). A group quote exists only when
the group is COMPLETE, every leg's latest observation is OPEN, the legs were observed
within ``max_leg_skew_minutes`` of each other, and the no-vig transform accepts the
prices (see ``nhl.market.novig.no_vig``). Anything else is a rejected group with a
reason -- it is never normalised into a quote.

Closing line (``close_rule_version`` in ``nhl/config/market.json``): the final valid,
complete group state observed strictly before puck drop. Actual puck drop is used
when known; otherwise scheduled puck drop, labelled ``SCHEDULED_FALLBACK``. If the
final state is suspended, incomplete, non-contemporaneous, malformed or stale, the
close is UNAVAILABLE. No earlier quote is substituted.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from nhl.config import load
from nhl.contracts import MarketStatus, MarketType, OddsSnapshot, Period, Selection

from .novig import MalformedMarket, NoVigResult, no_vig

ET = ZoneInfo("America/New_York")
SELECTIONS = {
    MarketType.ML: (Selection.HOME, Selection.AWAY),
    MarketType.REG_3WAY: (Selection.HOME, Selection.DRAW, Selection.AWAY),
    MarketType.PUCK_LINE: (Selection.HOME, Selection.AWAY),
    MarketType.TOTAL: (Selection.OVER, Selection.UNDER),
    MarketType.TEAM_TOTAL: (Selection.OVER, Selection.UNDER),
    MarketType.GOALIE_SAVES: (Selection.OVER, Selection.UNDER),
    MarketType.PLAYER_SOG: (Selection.OVER, Selection.UNDER),
}


@dataclass(frozen=True)
class GroupKey:
    game_id: str
    market: MarketType
    line: float | None  # PUCK_LINE keyed by the HOME line
    team: str | None
    period: Period | None = None
    participant: str | None = None

    def __post_init__(self) -> None:
        # Normalise so keys built from artifacts and from observations compare equal.
        object.__setattr__(self, "period", Period(self.period) if self.period else self.market.default_period)

    @property
    def market_id(self) -> str:
        """Deterministic, book-agnostic id of the market group."""

        period = self.period or self.market.default_period
        head = self.market.value if period is self.market.default_period else f"{self.market.value}@{period.value}"
        parts = [self.game_id, head]
        if self.team:
            parts.append(self.team)
        if self.participant:
            parts.append(f"P{self.participant}")
        if self.line is not None:
            parts.append(f"{self.line:+.1f}")
        return ":".join(parts)


@dataclass(frozen=True)
class GroupQuote:
    key: GroupKey
    book: str
    observed_at: datetime  # newest leg
    oldest_leg_at: datetime
    prices: dict[Selection, int]  # American (display)
    decimals: dict[Selection, float]  # exact prices used for all maths
    novig: NoVigResult
    max_stake: float | None
    legs: tuple[tuple[str, str, str], ...]  # (selection, observed_at ISO, snapshot_id)
    provider: str

    @property
    def no_vig(self) -> dict[Selection, float]:
        return {Selection(s): p for s, p in zip(self.novig.selections, self.novig.no_vig)}

    @property
    def raw_implied(self) -> dict[Selection, float]:
        return {Selection(s): p for s, p in zip(self.novig.selections, self.novig.raw_implied)}

    @property
    def ref(self) -> str:
        """Market snapshot reference: the exact observations the quote was built from."""

        return "|".join(f"{s}@{t}#{sid}" for s, t, sid in self.legs)


@dataclass(frozen=True)
class GroupReject:
    key: GroupKey
    book: str
    reason: str


def group_key(s: OddsSnapshot) -> GroupKey:
    line = s.line
    if s.market is MarketType.PUCK_LINE and s.selection is Selection.AWAY and line is not None:
        line = -line
    return GroupKey(s.game_id, s.market, line, s.team, s.period, s.participant)


def _cfg() -> dict:
    return load("market")


def _evaluate_group(
    book: str, gk: GroupKey, legs: dict[Selection, list[OddsSnapshot]], cfg: dict, method: str | None
) -> GroupQuote | GroupReject:
    needed = SELECTIONS[gk.market]
    latest: dict[Selection, OddsSnapshot] = {}
    for sel in needed:
        obs = legs.get(sel)
        if not obs:
            return GroupReject(gk, book, f"INCOMPLETE:missing {sel.value}")
        newest = max(o.snapshot_ts for o in obs)
        at_newest = [o for o in obs if o.snapshot_ts == newest]
        if len({(o.decimal, o.market_status) for o in at_newest}) > 1:
            return GroupReject(gk, book, f"DUPLICATED_SELECTION:conflicting {sel.value} observations at one instant")
        latest[sel] = at_newest[0]
    for sel, o in latest.items():
        if o.market_status is not MarketStatus.OPEN:
            return GroupReject(gk, book, f"{o.market_status.value}:{sel.value}")
    times = [latest[s].snapshot_ts for s in needed]
    if max(times) - min(times) > timedelta(minutes=cfg["max_leg_skew_minutes"]):
        return GroupReject(gk, book, "LEGS_NOT_CONTEMPORANEOUS")
    max_over = cfg["max_overround"]["three_way" if len(needed) == 3 else "two_way"]
    try:
        nv = no_vig([s.value for s in needed], [latest[s].decimal for s in needed], method or cfg["novig_method"],
                    cfg["min_overround"], max_over)
    except MalformedMarket as exc:
        return GroupReject(gk, book, f"MALFORMED:{exc}")
    stakes = [latest[s].max_stake for s in needed if latest[s].max_stake is not None]
    return GroupQuote(
        key=gk, book=book, observed_at=max(times), oldest_leg_at=min(times),
        prices={s: latest[s].price_american for s in needed}, decimals={s: latest[s].decimal for s in needed},
        novig=nv, max_stake=min(stakes) if stakes else None,
        legs=tuple((s.value, latest[s].snapshot_ts.strftime("%Y-%m-%dT%H:%M:%SZ"), latest[s].snapshot_id) for s in needed),
        provider=latest[needed[0]].provider or latest[needed[0]].source,
    )


def _grouped(odds: list[OddsSnapshot], cutoff: datetime, inclusive: bool = True) -> dict[tuple[str, GroupKey], dict[Selection, list[OddsSnapshot]]]:
    groups: dict[tuple[str, GroupKey], dict[Selection, list[OddsSnapshot]]] = defaultdict(lambda: defaultdict(list))
    for s in odds:
        if s.snapshot_ts > cutoff or (not inclusive and s.snapshot_ts == cutoff):
            continue
        groups[(s.book, group_key(s))][s.selection].append(s)
    return groups


def _order(item) -> tuple:
    (book, gk) = item[0]
    return (gk.game_id, gk.market.value, (gk.period or gk.market.default_period).value, gk.team or "",
            gk.participant or "", str(gk.line), book)


def quote_groups(
    odds: list[OddsSnapshot], as_of: datetime, method: str | None = None
) -> tuple[list[GroupQuote], list[GroupReject]]:
    """Latest state of every (book, group) visible at ``as_of``: valid quotes and rejects."""

    cfg = _cfg()
    quotes, rejects = [], []
    for (book, gk), legs in sorted(_grouped(odds, as_of).items(), key=_order):
        r = _evaluate_group(book, gk, legs, cfg, method)
        (quotes if isinstance(r, GroupQuote) else rejects).append(r)
    return quotes, rejects


def quotes_at(odds: list[OddsSnapshot], as_of: datetime, max_pair_skew: timedelta | None = None,
              method: str | None = None) -> list[GroupQuote]:
    return quote_groups(odds, as_of, method)[0]


def consensus_no_vig(quotes: list[GroupQuote]) -> dict[GroupKey, dict[Selection, float]]:
    """Equal-weight mean of per-book no-vig probabilities for each group."""

    acc: dict[GroupKey, list[dict[Selection, float]]] = defaultdict(list)
    for q in quotes:
        acc[q.key].append(q.no_vig)
    return {gk: {sel: sum(r[sel] for r in rows) / len(rows) for sel in rows[0]} for gk, rows in acc.items()}


# --------------------------------------------------------------------------- close

@dataclass(frozen=True)
class CloseSelection:
    game_id: str
    book: str
    market_id: str
    key: GroupKey
    status: str  # AVAILABLE | UNAVAILABLE
    reason: str  # "" when available
    rule: str
    rule_version: str
    cutoff: datetime
    cutoff_basis: str  # ACTUAL | SCHEDULED_FALLBACK
    quote: GroupQuote | None
    staleness_minutes: float | None

    @property
    def available(self) -> bool:
        return self.status == "AVAILABLE"


def close_cutoff(scheduled: datetime, actual: datetime | None) -> tuple[datetime, str]:
    if actual is not None:
        return actual, "ACTUAL"
    return scheduled, "SCHEDULED_FALLBACK"


def select_closes(
    odds: list[OddsSnapshot], scheduled_start: datetime, actual_start: datetime | None = None, method: str | None = None
) -> list[CloseSelection]:
    """One CloseSelection (available or not) for every (book, group) observed before the cutoff."""

    cfg = _cfg()
    cutoff, basis = close_cutoff(scheduled_start, actual_start)
    max_stale = timedelta(minutes=cfg["max_close_staleness_minutes"]["value"])
    out = []
    for (book, gk), legs in sorted(_grouped(odds, cutoff, inclusive=False).items(), key=_order):
        r = _evaluate_group(book, gk, legs, cfg, method)
        common = dict(game_id=gk.game_id, book=book, market_id=gk.market_id, key=gk, rule=cfg["close_rule"],
                      rule_version=cfg["close_rule_version"], cutoff=cutoff, cutoff_basis=basis)
        if isinstance(r, GroupReject):
            out.append(CloseSelection(status="UNAVAILABLE", reason=r.reason, quote=None, staleness_minutes=None, **common))
            continue
        stale = (cutoff - r.oldest_leg_at).total_seconds() / 60.0
        if cutoff - r.oldest_leg_at > max_stale:
            out.append(CloseSelection(status="UNAVAILABLE", reason=f"STALE:{stale:.1f}min", quote=None,
                                      staleness_minutes=stale, **common))
            continue
        out.append(CloseSelection(status="AVAILABLE", reason="", quote=r, staleness_minutes=stale, **common))
    return out


def closing_quotes(odds: list[OddsSnapshot], puck_drop: datetime, actual_start: datetime | None = None) -> list[GroupQuote]:
    """Valid closes only (compat helper)."""

    return [c.quote for c in select_closes(odds, puck_drop, actual_start) if c.available and c.quote is not None]


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
    """First valid quote per (book, group)."""

    cfg = _cfg()
    by_group: dict[tuple[str, GroupKey], list[OddsSnapshot]] = defaultdict(list)
    for s in odds:
        by_group[(s.book, group_key(s))].append(s)
    out = []
    for (book, gk), rows in sorted(by_group.items(), key=lambda kv: _order((kv[0], None))):
        for ts in sorted({r.snapshot_ts for r in rows}):
            legs: dict[Selection, list[OddsSnapshot]] = defaultdict(list)
            for r in rows:
                if r.snapshot_ts <= ts:
                    legs[r.selection].append(r)
            q = _evaluate_group(book, gk, legs, cfg, None)
            if isinstance(q, GroupQuote):
                out.append(q)
                break
    return out
