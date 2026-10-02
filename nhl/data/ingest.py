"""Capture and replay.

``capture_day``: live job. Fetches the schedule for a date plus final play-by-play and
boxscores for games that are over, and optionally rosters; every byte lands in the
raw snapshot store first.

``replay``: deterministic rebuild of a ``HistoricalStore`` from raw snapshots. Every
record passes the checked parsers and carries provenance; every rejection and warning
lands in an ``IngestReport`` (summary JSON + anomalies CSV, the tournament_v2
builder pattern). Replaying the same raw store always yields the same records.
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from nhl.contracts import Game

from . import moneypuck
from . import provenance as prov
from .nhl_api import (
    NHLApiClient,
    parse_boxscore_goalies_checked,
    parse_result_checked,
    parse_roster_checked,
    parse_schedule_checked,
)
from .odds import is_market_v2, parse_goalie_reports_checked, parse_market_observations_checked, parse_odds_checked
from .pit import HistoricalStore
from .snapshots import RawSnapshotStore, SnapshotEntry
from .validation import Rejection


@dataclass
class IngestReport:
    counts: Counter = field(default_factory=Counter)
    rejections: list[Rejection] = field(default_factory=list)
    warnings: list[Rejection] = field(default_factory=list)
    non_causal: Counter = field(default_factory=Counter)
    rules: Counter = field(default_factory=Counter)

    def absorb(self, what: str, result: Any) -> None:
        self.rejections.extend(result.rejections)
        self.warnings.extend(result.warnings)
        self.counts[f"{what}.rejected"] += len(result.rejections)

    def note(self, what: str, records: list) -> None:
        self.counts[what] += len(records)
        for r in records:
            p = getattr(r, "provenance", None)
            if p is None:
                self.non_causal[f"{what}:UNATTESTED"] += 1
                continue
            self.rules[f"{what}:{p.rule}"] += 1
            if not p.causal:
                self.non_causal[f"{what}:{p.rule}"] += 1

    def write(self, out_dir: str | Path) -> dict[str, str]:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        summary = {
            "counts": dict(sorted(self.counts.items())),
            "provenance_rules": dict(sorted(self.rules.items())),
            "non_causal": dict(sorted(self.non_causal.items())),
            "n_rejections": len(self.rejections),
            "n_warnings": len(self.warnings),
        }
        sp = out / "INGEST_summary.json"
        sp.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        ap = out / "INGEST_anomalies.csv"
        with ap.open("w", newline="", encoding="utf-8") as h:
            w = csv.writer(h, lineterminator="\n")
            w.writerow(["severity", "source", "record_key", "reason"])
            for sev, rows in (("REJECT", self.rejections), ("WARN", self.warnings)):
                for r in sorted(rows, key=lambda x: (x.source, x.record_key, x.reason)):
                    w.writerow([sev, r.source, r.record_key, r.reason])
        return {"summary": str(sp), "anomalies": str(ap)}


def capture_day(client: NHLApiClient, day: date, final_games: bool = True, roster_teams: list[str] | None = None,
                season: int | None = None, fetched_at: datetime | None = None) -> list[SnapshotEntry]:
    """Live capture. Idempotent at the byte level (content-addressed store).
    ``fetched_at`` defaults to the wall clock; pass it only for replayed/test captures."""

    entries = [client.fetch_schedule(day.isoformat(), fetched_at)]
    if final_games:
        payload = client.store.get_json(entries[0])
        for d in payload.get("gameWeek", []) or []:
            if d.get("date") != day.isoformat():
                continue
            for g in d.get("games", []) or []:
                if g.get("gameState") in {"FINAL", "OFF"}:
                    entries.append(client.fetch_pbp(str(g["id"]), fetched_at))
                    entries.append(client.fetch_boxscore(str(g["id"]), fetched_at))
    for team in roster_teams or []:
        if season is None:
            raise ValueError("season required for roster capture")
        entries.append(client.fetch_roster(team, season, fetched_at))
    return entries


def _dedupe_versions(games: list[Game]) -> list[Game]:
    """Keep a schedule version only when it differs from the previous known version."""

    out: list[Game] = []
    last: dict[str, tuple] = {}
    for g in sorted(games, key=lambda x: (x.game_id, x.available_at, x.provenance.snapshot_id if x.provenance else "")):
        sig = (g.start_time, g.status, g.home, g.away)
        if last.get(g.game_id) == sig:
            continue
        last[g.game_id] = sig
        out.append(g)
    return out


def replay(
    raw: RawSnapshotStore,
    data_origin: str = "HISTORICAL",
    schedule_overrides: dict | None = None,
    vintage_attestations: list[dict] | None = None,
    goalie_report_mode: prov.CaptureMode = prov.CaptureMode.LIVE,
    xg_model: Any | None = None,
) -> tuple[HistoricalStore, IngestReport]:
    rep = IngestReport()
    store = HistoricalStore(data_origin=data_origin)
    entries = sorted(raw.entries(), key=lambda e: (e.fetched_at, e.source, e.key, e.snapshot_id))

    games: list[Game] = []
    for e in (e for e in entries if e.source == "nhl_api" and e.key.startswith("schedule/")):
        res = parse_schedule_checked(raw.get_json(e), e, prov.CaptureMode.LIVE, schedule_overrides)
        rep.absorb("schedule", res)
        games.extend(res.records)
    store.games = sorted(_dedupe_versions(games), key=lambda g: (g.start_time, g.game_id, g.available_at))
    rep.note("schedule_versions", store.games)
    latest = store.latest_games()

    # Latest snapshot per game wins for final event data (immutable facts; re-fetches identical).
    def latest_by_key(prefix: str) -> dict[str, SnapshotEntry]:
        best: dict[str, SnapshotEntry] = {}
        for e in entries:
            if e.source == "nhl_api" and e.key.startswith(prefix):
                best[e.key.split("/", 1)[1]] = e
        return best

    events_by_game: dict[str, list[dict]] = {}
    pbp_entries = latest_by_key("pbp/")
    for gid, e in sorted(pbp_entries.items()):
        g = latest.get(gid)
        if g is None:
            rep.rejections.append(Rejection("pbp", gid, "game not in any schedule snapshot"))
            continue
        payload = raw.get_json(e)
        result, ev = parse_result_checked(payload, g.start_time, e)
        rep.absorb("pbp", ev)
        if result is not None:
            store.results.append(result)
        if not ev.rejections:
            events_by_game[gid] = ev.records
    rep.note("results", store.results)

    for gid, e in sorted(latest_by_key("boxscore/").items()):
        g = latest.get(gid)
        if g is None:
            continue
        res = parse_boxscore_goalies_checked(raw.get_json(e), g.start_time, e)
        rep.absorb("boxscore", res)
        store.goalie_stats.extend(res.records)
    rep.note("goalie_games", store.goalie_stats)

    for key, e in sorted(latest_by_key("roster/").items()):
        team, season = key.split("/")
        res = parse_roster_checked(raw.get_json(e), team, int(season), e)
        rep.absorb("roster", res)
        rep.note("roster_slots", res.records)
        # Rosters are returned by the caller via ``replay_rosters``; stats only here.

    if xg_model is not None:
        from nhl.features.xg import team_game_stats_from_pbp

        for gid, evs in sorted(events_by_game.items()):
            g = latest[gid]
            if int(gid[:4]) <= xg_model.trained_through:
                rep.counts["xg.skipped_in_sample"] += 1
                continue
            e = pbp_entries[gid]
            store.team_stats.extend(team_game_stats_from_pbp(xg_model, evs, g.start_time, g.home, g.away, e.snapshot_id, e.fetched_at))
    schedule_seasons = {g.season for g in store.games}
    needed_moneypuck_seasons = frozenset(schedule_seasons | {s - 1 for s in schedule_seasons}) or None
    for e in (e for e in entries if e.source == "moneypuck" and e.key.startswith("teams")):
        try:
            res = moneypuck.parse_team_games_checked(raw.get_bytes(e), e, vintage_attestations,
                                                      seasons=needed_moneypuck_seasons)
        except (moneypuck.SchemaError, UnicodeDecodeError, csv.Error) as exc:
            rep.rejections.append(Rejection("moneypuck_teams", e.key, f"dataset schema mismatch: {exc}"))
            rep.counts["moneypuck_teams.rejected"] += 1
            continue
        rep.absorb("moneypuck_teams", res)
        store.team_stats.extend(res.records)
    rep.note("team_stats", store.team_stats)

    mp_goalies = []
    for e in (e for e in entries if e.source == "moneypuck" and e.key.startswith("goalies/")):
        try:
            res = moneypuck.parse_goalie_games_checked(raw.get_bytes(e), e, vintage_attestations)
        except (moneypuck.SchemaError, UnicodeDecodeError, csv.Error) as exc:
            rep.rejections.append(Rejection("moneypuck_goalies", e.key, f"dataset schema mismatch: {exc}"))
            rep.counts["moneypuck_goalies.rejected"] += 1
            continue
        rep.absorb("moneypuck_goalies", res)
        mp_goalies.extend(res.records)
    if mp_goalies:
        box_keys = {(g.game_id, g.goalie_id) for g in mp_goalies}
        merged = moneypuck.merge_goalie_sources(mp_goalies, store.goalie_stats)
        merged.extend(g for g in store.goalie_stats if (g.game_id, g.goalie_id) not in box_keys)
        store.goalie_stats = sorted(merged, key=lambda g: (g.game_id, g.team, g.goalie_id))
    rep.note("moneypuck_goalie_games", mp_goalies)

    for e in (e for e in entries if e.source == "odds"):
        # Owls HTTP bodies are intentionally stored under source=odds. They are
        # immutable capture evidence, not the derived market-v2 contract.
        if e.meta.get("provider") == "owls" and e.meta.get("contract") != "market-v2":
            rep.counts["odds.raw_owls"] += 1
            continue
        payload = raw.get_bytes(e)
        res = parse_market_observations_checked(payload, e) if is_market_v2(payload) else parse_odds_checked(payload, e)
        rep.absorb("odds", res)
        store.odds.extend(res.records)
    rep.note("odds", store.odds)

    starts = {gid: g.start_time for gid, g in latest.items()}
    for e in (e for e in entries if e.source == "goalie_reports"):
        res = parse_goalie_reports_checked(raw.get_bytes(e), e, starts, goalie_report_mode)
        rep.absorb("goalie_reports", res)
        store.goalie_reports.extend(res.records)
    rep.note("goalie_reports", store.goalie_reports)
    return store, rep


def replay_rosters(raw: RawSnapshotStore) -> dict[int, list]:
    """All roster snapshots (every capture, not just the latest) grouped by season."""

    out: dict[int, list] = defaultdict(list)
    for e in sorted(raw.entries(source="nhl_api"), key=lambda e: (e.fetched_at, e.key)):
        if not e.key.startswith("roster/"):
            continue
        _, team, season = e.key.split("/")
        out[int(season)].extend(parse_roster_checked(raw.get_json(e), team, int(season), e).records)
    return dict(out)
