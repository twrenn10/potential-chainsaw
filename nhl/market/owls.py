"""Owls Insight transport and raw-response capture.

Parsing deliberately lives below the raw capture boundary: every HTTP body,
including an error body, is persisted before callers may inspect or normalize it.
Credentials are supplied only in the Authorization header and are never included
in URLs, snapshot metadata, or exception text.
"""

from __future__ import annotations

import os
import csv
import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Mapping, Protocol

from nhl.data.snapshots import RawSnapshotStore, SnapshotEntry
from nhl.timeutil import utcnow
from nhl.timeutil import fmt_ts, parse_ts
from nhl.data.odds import MARKET_COLUMNS
from nhl.market.settlement import SettlementRegistry


BASE_URL = "https://api.owlsinsight.com"
RATE_HEADERS = {
    "retry-after",
    "x-ratelimit-limit-minute",
    "x-ratelimit-remaining-minute",
    "x-ratelimit-limit-month",
    "x-ratelimit-remaining-month",
    "content-type",
    "date",
}


class OwlsConfigurationError(RuntimeError):
    pass


class OwlsHTTPError(RuntimeError):
    def __init__(self, status: int, retryable: bool) -> None:
        super().__init__(f"Owls Insight request failed with HTTP {status}")
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]


class Transport(Protocol):
    def __call__(self, url: str, headers: Mapping[str, str], timeout: float) -> HTTPResponse: ...


def urllib_transport(url: str, headers: Mapping[str, str], timeout: float) -> HTTPResponse:
    request = urllib.request.Request(url, headers=dict(headers))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed base URL
            return HTTPResponse(response.status, response.read(), dict(response.headers.items()))
    except urllib.error.HTTPError as exc:
        return HTTPResponse(exc.code, exc.read(), dict(exc.headers.items()))


def _safe_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in headers.items() if str(k).lower() in RATE_HEADERS}


def _key(endpoint: str, params: Mapping[str, object] | None) -> str:
    clean = endpoint.strip("/").replace("/", "_") or "root"
    query = urllib.parse.urlencode(sorted((str(k), str(v)) for k, v in (params or {}).items() if v is not None))
    return f"owls/{clean}" + (f"/{query}" if query else "")


class OwlsInsightClient:
    """Capture-only Owls HTTP client with bounded 429 retry handling."""

    def __init__(
        self,
        store: RawSnapshotStore,
        api_key: str | None = None,
        *,
        transport: Transport = urllib_transport,
        timeout: float = 30.0,
        max_retries: int = 2,
        max_retry_after: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.store = store
        self._api_key = api_key or os.getenv("OWLS_INSIGHT_API_KEY")
        if not self._api_key:
            raise OwlsConfigurationError("OWLS_INSIGHT_API_KEY is not configured")
        self.transport = transport
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_retry_after = max_retry_after
        self.sleep = sleep
        self.clock = clock

    def capture(self, endpoint: str, params: Mapping[str, object] | None = None) -> SnapshotEntry:
        endpoint = "/" + endpoint.lstrip("/")
        query = urllib.parse.urlencode([(str(k), str(v)) for k, v in (params or {}).items() if v is not None])
        url = BASE_URL + endpoint + ("?" + query if query else "")
        headers = {"Authorization": f"Bearer {self._api_key}", "User-Agent": "nhl-desk/0.1"}
        key = _key(endpoint, params)
        attempt = 0
        while True:
            at = self.clock()
            try:
                response = self.transport(url, headers, self.timeout)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                self.store.record_failure("odds", key, at, "TRANSPORT_ERROR", type(exc).__name__, retryable=True)
                raise
            safe = _safe_headers(response.headers)
            entry = self.store.put(
                "odds",
                key,
                response.body,
                at,
                {"provider": "owls", "url": url, "http_status": response.status,
                 "response_headers": safe, "attempt": attempt + 1},
            )
            if 200 <= response.status < 300:
                return entry
            retryable = response.status == 429 or response.status >= 500
            self.store.record_failure("odds", key, at, "HTTP_ERROR", f"HTTP {response.status}",
                                      status=response.status, retryable=retryable)
            if response.status in {401, 403}:
                raise OwlsHTTPError(response.status, False)
            if response.status != 429 or attempt >= self.max_retries:
                raise OwlsHTTPError(response.status, retryable)
            raw_wait = safe.get("retry-after", "1")
            try:
                wait = float(raw_wait)
            except ValueError:
                wait = 1.0
            self.sleep(max(0.0, min(wait, self.max_retry_after)))
            attempt += 1


TEAM_NAMES = {
    "Anaheim Ducks": "ANA", "Boston Bruins": "BOS", "Buffalo Sabres": "BUF",
    "Carolina Hurricanes": "CAR", "Columbus Blue Jackets": "CBJ", "Calgary Flames": "CGY",
    "Chicago Blackhawks": "CHI", "Colorado Avalanche": "COL", "Dallas Stars": "DAL",
    "Detroit Red Wings": "DET", "Edmonton Oilers": "EDM", "Florida Panthers": "FLA",
    "Los Angeles Kings": "LAK", "Minnesota Wild": "MIN", "Montreal Canadiens": "MTL",
    "New Jersey Devils": "NJD", "Nashville Predators": "NSH", "New York Islanders": "NYI",
    "New York Rangers": "NYR", "Ottawa Senators": "OTT", "Philadelphia Flyers": "PHI",
    "Pittsburgh Penguins": "PIT", "Seattle Kraken": "SEA", "San Jose Sharks": "SJS",
    "St. Louis Blues": "STL", "Tampa Bay Lightning": "TBL", "Toronto Maple Leafs": "TOR",
    "Utah Mammoth": "UTA", "Vancouver Canucks": "VAN", "Vegas Golden Knights": "VGK",
    "Winnipeg Jets": "WPG", "Washington Capitals": "WSH",
}


def events_from_board(payload: bytes) -> list[dict]:
    """Return each provider event once from the book-keyed live board."""

    root = json.loads(payload)
    data = root.get("data") if isinstance(root, dict) else None
    if not isinstance(data, dict):
        raise ValueError("Owls odds payload data must be a book-keyed object")
    unique: dict[str, dict] = {}
    for events in data.values():
        if not isinstance(events, list):
            raise ValueError("Owls odds book value must be an event list")
        for event in events:
            event_id = event.get("eventId")
            if event_id:
                unique.setdefault(str(event_id), event)
    return list(unique.values())


def build_event_map(events: list[dict], games: list[object], tolerance_seconds: int = 15 * 60) -> tuple[dict[str, str], list[str]]:
    """Match teams and start time; zero or multiple candidates are never guessed."""

    mapped: dict[str, str] = {}
    rejected: list[str] = []
    for event in events:
        event_id = str(event.get("eventId") or "")
        try:
            home, away = TEAM_NAMES[event["home_team"]], TEAM_NAMES[event["away_team"]]
            start = parse_ts(event["commence_time"])
        except (KeyError, TypeError, ValueError) as exc:
            rejected.append(f"INVALID_EVENT:{event_id}:{type(exc).__name__}")
            continue
        candidates = [g for g in games if getattr(g, "home", None) == home and getattr(g, "away", None) == away
                      and abs((getattr(g, "start_time") - start).total_seconds()) <= tolerance_seconds]
        if len(candidates) == 1:
            mapped[event_id] = str(getattr(candidates[0], "game_id"))
        else:
            code = "UNMAPPED_EVENT" if not candidates else "AMBIGUOUS_EVENT"
            rejected.append(f"{code}:{event_id}:{len(candidates)}")
    return mapped, rejected


def normalize_live_board(
    payload: bytes,
    fetched_at: datetime,
    event_map: Mapping[str, str],
    *,
    verified_settlement: set[tuple[str, str]] | None = None,
    settlement_registry: SettlementRegistry | None = None,
) -> tuple[bytes, list[str]]:
    """Normalize verified main lines from a captured live board to market-v2.

    ``verified_settlement`` contains ``(book, provider_market)`` pairs whose NHL
    overtime/shootout convention has been independently established. An empty set
    intentionally emits no game-market observations.
    """

    verified_settlement = verified_settlement or set()
    root = json.loads(payload)
    if not isinstance(root, dict) or not isinstance(root.get("data"), dict):
        raise ValueError("Owls odds payload must contain a book-keyed data object")
    rows: list[dict] = []
    rejected: list[str] = []
    seen: set[tuple] = set()
    market_map = {"h2h": "ML", "spreads": "PUCK_LINE", "totals": "TOTAL"}
    for raw_book, events in root["data"].items():
        book = str(raw_book).strip().lower()
        if not isinstance(events, list):
            rejected.append(f"INVALID_BOOK_EVENTS:{book}")
            continue
        for event in events:
            event_id = str(event.get("eventId") or "")
            game_id = event_map.get(event_id)
            if not game_id:
                rejected.append(f"UNMAPPED_EVENT:{event_id}")
                continue
            home, away = event.get("home_team"), event.get("away_team")
            for bookmaker in event.get("bookmakers") or []:
                actual_book = str(bookmaker.get("key") or book).strip().lower()
                for market in bookmaker.get("markets") or []:
                    raw_market = str(market.get("key") or "")
                    canonical = market_map.get(raw_market)
                    if canonical is None:
                        rejected.append(f"UNKNOWN_MARKET:{actual_book}:{raw_market}")
                        continue
                    legacy_verified = (actual_book, raw_market) in verified_settlement
                    decision = settlement_registry.decision("owls", actual_book, "NHL", canonical, fetched_at) \
                        if settlement_registry else None
                    if not legacy_verified and not (decision and decision.compatible):
                        reason = decision.reason if decision else "UNVERIFIED_SETTLEMENT"
                        rejected.append(f"{reason}:{actual_book}:{raw_market}")
                        continue
                    source_ts_raw = market.get("last_update") or bookmaker.get("last_update")
                    try:
                        source_ts = parse_ts(source_ts_raw) if source_ts_raw else None
                    except (TypeError, ValueError):
                        rejected.append(f"INVALID_TIMESTAMP:{event_id}:{actual_book}:{raw_market}")
                        continue
                    outcomes = market.get("outcomes")
                    if not isinstance(outcomes, list):
                        rejected.append(f"MISSING_OUTCOMES:{event_id}:{actual_book}:{raw_market}")
                        continue
                    points: list[float] = []
                    candidate_rows: list[dict] = []
                    for outcome in outcomes:
                        price = outcome.get("price")
                        if price is None:
                            rejected.append(f"MISSING_PRICE:{event_id}:{actual_book}:{raw_market}")
                            continue
                        if not isinstance(price, (int, float)) or -100 < float(price) < 100:
                            rejected.append(f"INVALID_AMERICAN_PRICE:{event_id}:{actual_book}:{raw_market}:{price}")
                            continue
                        name = outcome.get("name")
                        if canonical in {"ML", "PUCK_LINE"}:
                            selection = "HOME" if name == home else "AWAY" if name == away else None
                        else:
                            selection = str(name).upper() if str(name).lower() in {"over", "under"} else None
                        if selection is None:
                            rejected.append(f"AMBIGUOUS_SELECTION:{event_id}:{actual_book}:{raw_market}:{name}")
                            continue
                        point = outcome.get("point")
                        if canonical != "ML":
                            if not isinstance(point, (int, float)):
                                rejected.append(f"MISSING_LINE:{event_id}:{actual_book}:{raw_market}:{selection}")
                                continue
                            points.append(float(point))
                        limits = market.get("limits") or []
                        risk = next((x.get("amount") for x in limits if x.get("type") == "maxRiskStake"), "")
                        candidate_rows.append({
                            "provider": "owls", "source_ts": fmt_ts(source_ts) if source_ts else "",
                            "observed_at": fmt_ts(fetched_at), "game_id": game_id, "book": actual_book,
                            "market": canonical, "period": "FULL_GAME", "selection": selection,
                            "line": "" if point is None else point, "team": "", "participant_id": "",
                            "price": price, "odds_format": "AMERICAN", "status": "OPEN", "max_stake": risk,
                        })
                    if canonical == "PUCK_LINE" and (len(points) != 2 or abs(sum(points)) > 1e-9):
                        rejected.append(f"NON_OPPOSITE_SPREAD:{event_id}:{actual_book}")
                        continue
                    for row in candidate_rows:
                        sig = tuple(row[c] for c in MARKET_COLUMNS)
                        if sig not in seen:
                            seen.add(sig)
                            rows.append(row)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(MARKET_COLUMNS), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode(), rejected


def capture_and_normalize_live(
    client: OwlsInsightClient,
    games: list[object],
    verified_settlement: set[tuple[str, str]] | None = None,
    settlement_registry: SettlementRegistry | None = None,
) -> tuple[SnapshotEntry, SnapshotEntry, list[str]]:
    raw_entry = client.capture("/api/v1/nhl/odds")
    body = client.store.get_bytes(raw_entry)
    event_map, rejected = build_event_map(events_from_board(body), games)
    map_payload = json.dumps({"event_map": event_map, "rejected": rejected}, sort_keys=True).encode()
    map_entry = client.store.put("owls_event_map", raw_entry.key, map_payload, raw_entry.fetched_at,
                                 {"provider": "owls", "derived_from": raw_entry.snapshot_id})
    normalized, parse_rejections = normalize_live_board(body, raw_entry.fetched_at, event_map,
                                                         verified_settlement=verified_settlement,
                                                         settlement_registry=settlement_registry)
    derived = client.store.put("odds", "owls-normalized/" + raw_entry.fetched_at.date().isoformat(), normalized,
                               raw_entry.fetched_at, {"provider": "owls", "contract": "market-v2",
                               "derived_from": raw_entry.snapshot_id, "event_map": map_entry.snapshot_id,
                               "normalization_rejections": len(rejected) + len(parse_rejections)})
    return raw_entry, derived, rejected + parse_rejections


def normalize_line_history(payload: bytes, fetched_at: datetime, game_id: str) -> tuple[bytes, list[str]]:
    """Convert `/api/odds/history`; every row must carry its own UTC timestamp."""

    root = json.loads(payload)
    data = root.get("data") if isinstance(root, dict) else None
    if not isinstance(data, dict):
        raise ValueError("Owls history payload missing data object")
    canonical = {"h2h": "ML", "spreads": "PUCK_LINE", "totals": "TOTAL"}.get(data.get("market"))
    rows, rejected = [], []
    if canonical is None:
        rejected.append(f"UNKNOWN_MARKET:{data.get('market')}")
    side = str(data.get("side") or "").lower()
    selection = {"home": "HOME", "away": "AWAY", "over": "OVER", "under": "UNDER", "draw": "DRAW"}.get(side)
    for i, point in enumerate(data.get("history") or []):
        timestamp, price = point.get("timestamp"), point.get("price")
        if not timestamp:
            rejected.append(f"NO_HISTORICAL_TIMESTAMP:{i}")
            continue
        try:
            observed = parse_ts(timestamp)
        except (TypeError, ValueError):
            rejected.append(f"INVALID_HISTORICAL_TIMESTAMP:{i}")
            continue
        if canonical is None or selection is None or price is None:
            rejected.append(f"INVALID_HISTORY_ROW:{i}")
            continue
        line = point.get("point")
        if canonical != "ML" and not isinstance(line, (int, float, str)):
            rejected.append(f"MISSING_LINE:{i}")
            continue
        rows.append({"provider": "owls", "source_ts": "", "observed_at": fmt_ts(observed),
                     "game_id": game_id, "book": str(data.get("book") or "").lower(),
                     "market": canonical, "period": "FULL_GAME", "selection": selection,
                     "line": "" if line is None else line, "team": "", "participant_id": "",
                     "price": price, "odds_format": "AMERICAN", "status": "OPEN", "max_stake": ""})
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(MARKET_COLUMNS), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode(), rejected
