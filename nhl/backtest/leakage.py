"""Leakage audit: predictions must be byte-identical whether the store holds the
future or has been physically truncated to what was knowable at ``as_of``."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

from nhl.contracts import PredictionMode, RosterSlot
from nhl.data.pit import HistoricalStore
from nhl.pipeline import run_slate

from .walk_forward import slate_days


def truncate(store: HistoricalStore, as_of) -> HistoricalStore:
    keep = lambda rows: [r for r in rows if r.available_at is None or r.available_at <= as_of]  # noqa: E731
    return HistoricalStore(
        games=keep(store.games), results=keep(store.results), team_stats=keep(store.team_stats),
        goalie_stats=keep(store.goalie_stats), goalie_reports=keep(store.goalie_reports),
        odds=[o for o in store.odds if o.snapshot_ts <= as_of], player_seasons=keep(store.player_seasons),
        data_origin=store.data_origin,
    )


def _strip(artifacts) -> list[dict]:
    return [replace(a, created_at="X").content() for a in artifacts]


def audit(store: HistoricalStore, rosters: dict[int, list[RosterSlot]], season: int, days: list[date],
          n_sims: int = 800, lead_minutes: int = 60) -> list[dict]:
    all_days = slate_days([g for g in store.games if g.season == season])
    out = []
    for d in days:
        games = all_days.get(d)
        if not games:
            out.append({"day": d.isoformat(), "status": "NO_GAMES"})
            continue
        as_of = min(g.start_time for g in games) - timedelta(minutes=lead_minutes)
        cut_rosters = {k: [s for s in v if s.available_at is None or s.available_at <= as_of] for k, v in rosters.items()}
        full = run_slate(store, rosters, season, games, as_of, PredictionMode.BACKTEST, n_sims=n_sims, created_at=as_of)
        cut = run_slate(truncate(store, as_of), cut_rosters, season, games, as_of, PredictionMode.BACKTEST, n_sims=n_sims,
                        created_at=as_of)
        same = _strip(full.artifacts) == _strip(cut.artifacts)
        out.append({"day": d.isoformat(), "as_of": as_of.isoformat(), "artifacts": len(full.artifacts),
                    "status": "EQUIVALENT" if same else "LEAK_DETECTED"})
    return out
