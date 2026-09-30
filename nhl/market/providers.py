"""Provider-neutral odds capture.

A provider adapter only has to return bytes in the v2 market-observation CSV
contract (``nhl.data.odds.MARKET_COLUMNS``). Everything downstream -- raw snapshot
storage, parsing, provenance, quotes, no-vig, close selection, CLV -- is shared, so
adding a real vendor does not touch pricing or evaluation.

No real provider is configured in this repository (no credentials/source exist);
``FileOddsProvider`` replays captured files and is what tests use.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Protocol

from nhl.data.snapshots import RawSnapshotStore, SnapshotEntry
from nhl.timeutil import utcnow


class OddsProvider(Protocol):
    name: str

    def fetch(self, day: date) -> bytes: ...


class GoalieReportProvider(Protocol):
    name: str

    def fetch(self, day: date) -> bytes: ...


class FileProvider:
    """Reads ``<root>/<YYYY-MM-DD>.csv``. Used for fixtures and offline replays."""

    def __init__(self, name: str, root: str | Path) -> None:
        self.name = name
        self.root = Path(root)

    def fetch(self, day: date) -> bytes:
        return (self.root / f"{day.isoformat()}.csv").read_bytes()


FileOddsProvider = FileProvider


def capture_odds(provider: OddsProvider, raw: RawSnapshotStore, day: date, fetched_at: datetime | None = None) -> SnapshotEntry:
    return raw.put("odds", f"{provider.name}/{day.isoformat()}", provider.fetch(day), fetched_at or utcnow(),
                   {"provider": provider.name, "contract": "market-v2"})


def capture_goalie_reports(provider: GoalieReportProvider, raw: RawSnapshotStore, day: date,
                           fetched_at: datetime | None = None) -> SnapshotEntry:
    return raw.put("goalie_reports", f"{provider.name}/{day.isoformat()}", provider.fetch(day), fetched_at or utcnow(),
                   {"provider": provider.name})
