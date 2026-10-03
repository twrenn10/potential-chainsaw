"""Provider-neutral odds capture.

A provider adapter only has to return bytes in the v2 market-observation CSV
contract (``nhl.data.odds.MARKET_COLUMNS``). Everything downstream -- raw snapshot
storage, parsing, provenance, quotes, no-vig, close selection, CLV -- is shared, so
adding a real vendor does not touch pricing or evaluation.

No real provider is configured in this repository (no credentials/source exist);
``FileOddsProvider`` replays captured files and is what tests use.
"""

from __future__ import annotations

import csv
import io
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Protocol

from nhl.data.snapshots import RawSnapshotStore, SnapshotEntry
from nhl.timeutil import utcnow
from nhl.timeutil import fmt_ts
from nhl.contracts import canonical_team
from nhl.data.odds import MARKET_COLUMNS


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


class ProviderConfigurationError(RuntimeError):
    pass


class TheOddsApiProvider:
    """The Odds API v4 adapter.

    The vendor JSON is captured independently from the normalized market-v2 CSV.
    Event linkage is explicit: callers supply provider-event-id -> NHL-game-id. No
    fuzzy match is silently accepted, and unmatched events are reported.
    """

    name = "the_odds_api"
    endpoint = "https://api.the-odds-api.com/v4/sports/icehockey_nhl/odds"
    MARKET_MAP = {
        "h2h": "ML", "h2h_3_way": "REG_3WAY", "spreads": "PUCK_LINE",
        "totals": "TOTAL", "team_totals": "TEAM_TOTAL",
        "player_shots_on_goal": "PLAYER_SOG", "player_saves": "GOALIE_SAVES",
    }

    def __init__(self, api_key: str | None = None, regions: str = "us", timeout: float = 30.0) -> None:
        self.api_key = api_key or os.getenv("THE_ODDS_API_KEY") or os.getenv("ODDS_API_KEY")
        self.regions = regions
        self.timeout = timeout
        if not self.api_key:
            raise ProviderConfigurationError("THE_ODDS_API_KEY/ODDS_API_KEY is not configured")

    def fetch_raw(self) -> tuple[bytes, dict[str, str]]:
        query = urllib.parse.urlencode({
            "apiKey": self.api_key, "regions": self.regions,
            "markets": ",".join(self.MARKET_MAP), "oddsFormat": "decimal", "dateFormat": "iso",
        })
        req = urllib.request.Request(f"{self.endpoint}?{query}", headers={"User-Agent": "nhl-desk/0.1"})
        with urllib.request.urlopen(req, timeout=self.timeout) as response:  # noqa: S310 - fixed host
            headers = {k.lower(): v for k, v in response.headers.items() if k.lower() in {
                "x-requests-remaining", "x-requests-used", "x-requests-last", "date", "content-type"
            }}
            return response.read(), headers

    def normalize(self, payload: bytes, observed_at: datetime, event_map: dict) -> tuple[bytes, list[str]]:
        events = json.loads(payload)
        if not isinstance(events, list):
            raise ValueError("odds provider payload must be a list")
        rows, rejected = [], []
        for event in events:
            provider_id = str(event.get("id", ""))
            mapping = event_map.get(provider_id)
            if not mapping:
                rejected.append(f"unmapped_event:{provider_id}")
                continue
            if isinstance(mapping, str):
                game_id, team_map, player_map = mapping, {}, {}
            else:
                game_id = mapping.get("game_id")
                team_map = mapping.get("teams", {})
                player_map = mapping.get("players", {})
            if not game_id:
                rejected.append(f"unmapped_event:{provider_id}")
                continue
            home_name, away_name = event.get("home_team"), event.get("away_team")
            for book in event.get("bookmakers", []) or []:
                book_ts = book.get("last_update")
                for market in book.get("markets", []) or []:
                    canonical = self.MARKET_MAP.get(market.get("key"))
                    if canonical is None:
                        rejected.append(f"unsupported_market:{market.get('key')}")
                        continue
                    source_ts = market.get("last_update") or book_ts or ""
                    for outcome in market.get("outcomes", []) or []:
                        name = str(outcome.get("name", ""))
                        selection, team, participant = self._selection(
                            canonical, name, home_name, away_name, team_map, player_map)
                        if selection is None:
                            rejected.append(f"unmapped_selection:{provider_id}:{canonical}:{name}")
                            continue
                        point = outcome.get("point")
                        # Team totals identify the named team; player props identify the participant.
                        rows.append({
                            "provider": self.name, "source_ts": source_ts,
                            "observed_at": fmt_ts(observed_at), "game_id": game_id,
                            "book": str(book.get("key", "")).lower(), "market": canonical,
                            "period": "REGULATION" if canonical == "REG_3WAY" else "FULL_GAME",
                            "selection": selection, "line": "" if point is None else point,
                            "team": team or "", "participant_id": participant or "",
                            "price": outcome.get("price"), "odds_format": "DECIMAL", "status": "OPEN",
                            "max_stake": "",
                        })
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=list(MARKET_COLUMNS), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        return buf.getvalue().encode(), rejected

    @staticmethod
    def _selection(market: str, name: str, home: str | None, away: str | None,
                   team_map: dict | None = None, player_map: dict | None = None) -> tuple[str | None, str | None, str | None]:
        team_map, player_map = team_map or {}, player_map or {}
        low = name.casefold()
        if market in {"ML", "REG_3WAY", "PUCK_LINE"}:
            if low == "draw":
                return "DRAW", None, None
            if name == home:
                return "HOME", None, None
            if name == away:
                return "AWAY", None, None
            return None, None, None
        if low in {"over", "under"}:
            return low.upper(), None, None
        # Some feeds prefix team/player name and suffix Over/Under.
        side = "OVER" if low.endswith(" over") else "UNDER" if low.endswith(" under") else None
        if side is None:
            return None, None, None
        entity = name.rsplit(" ", 1)[0]
        if market == "TEAM_TOTAL":
            try:
                return side, canonical_team(team_map.get(entity, entity)), None
            except ValueError:
                return None, None, None
        participant = player_map.get(entity)
        return (side, None, participant) if participant else (None, None, None)


def capture_live_odds(provider: TheOddsApiProvider, raw: RawSnapshotStore, day: date,
                      event_map: dict[str, str], fetched_at: datetime | None = None) -> tuple[SnapshotEntry, SnapshotEntry, list[str]]:
    """Capture untouched vendor JSON, then a derived market-v2 snapshot."""

    at = fetched_at or utcnow()
    key = f"{provider.name}/{day.isoformat()}"
    try:
        payload, headers = provider.fetch_raw()
    except urllib.error.HTTPError as exc:
        raw.record_failure("odds", key, at, "HTTP_ERROR", str(exc.reason), status=exc.code,
                           retryable=exc.code >= 500 or exc.code == 429)
        raise
    except (urllib.error.URLError, TimeoutError) as exc:
        raw.record_failure("odds", key, at, "TRANSPORT_ERROR", str(exc), retryable=True)
        raise
    original = raw.put("odds_provider_raw", key, payload, at, {
        "provider": provider.name, "http_status": 200, "response_headers": headers,
    })
    normalized, rejected = provider.normalize(payload, at, event_map)
    parsed = raw.put("odds", key, normalized, at, {
        "provider": provider.name, "contract": "market-v2", "derived_from": original.snapshot_id,
        "normalization_rejections": len(rejected),
    })
    return original, parsed, rejected


def capture_odds(provider: OddsProvider, raw: RawSnapshotStore, day: date, fetched_at: datetime | None = None) -> SnapshotEntry:
    return raw.put("odds", f"{provider.name}/{day.isoformat()}", provider.fetch(day), fetched_at or utcnow(),
                   {"provider": provider.name, "contract": "market-v2"})


def capture_goalie_reports(provider: GoalieReportProvider, raw: RawSnapshotStore, day: date,
                           fetched_at: datetime | None = None) -> SnapshotEntry:
    return raw.put("goalie_reports", f"{provider.name}/{day.isoformat()}", provider.fetch(day), fetched_at or utcnow(),
                   {"provider": provider.name})
