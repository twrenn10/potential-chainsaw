"""Shadow-forward runner: scheduled, reproducible state transitions (no daemon).

Daily lifecycle (each step is one idempotent invocation; see
docs/forward_testing_protocol.md):

  capture (schedule / finals / rosters / odds / goalies)  -> raw immutable snapshots
  price   (any number of times before puck drop)          -> append_if_changed artifacts
  close   (after puck drop)                               -> close-v1 selections, append-only
  settle  (after results)                                 -> grading + CLV rows (derived)
  verify                                                  -> hash chains
  export                                                  -> deterministic desk reports

The runner uses exactly the same ``nhl.pipeline.run_slate`` as walk-forward
research and replay, so there is one pricing path. State comes from a
``StateSource`` (production: replay of the raw snapshot store). All writes are
append-only; the cycle log is an append-only JSONL file.

A SIMULATED clock (tests, fixture drills) is allowed only explicitly and stamps
every artifact DEV_SYNTHETIC with HARD_BLOCK:SIMULATED_CLOCK.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

from nhl.contracts import PredictionMode, RosterSlot
from nhl.data.pit import HistoricalStore
from nhl.desk.reports import export_reports, freshness_rows
from nhl.features.league_constants import LeagueConstants, LeagueConstantsStore, fit_league_constants
from nhl.governance.overrides import OverrideLog
from nhl.ledger.closes import CloseStore
from nhl.ledger.predictions import PredictionStore
from nhl.ledger.settlement import closes_for_store, settle_predictions
from nhl.pipeline import run_slate
from nhl.timeutil import fmt_ts, utcnow

ET = ZoneInfo("America/New_York")


class StateSource(Protocol):
    def __call__(self, now: datetime) -> tuple[HistoricalStore, dict[int, list[RosterSlot]]]: ...


@dataclass
class StaticStateSource:
    """A fixed store (synthetic/fixture drills). Visibility is still governed by as_of."""

    store: HistoricalStore
    rosters: dict[int, list[RosterSlot]] = field(default_factory=dict)

    def __call__(self, now: datetime) -> tuple[HistoricalStore, dict[int, list[RosterSlot]]]:
        return self.store, self.rosters


@dataclass
class ReplayStateSource:
    """Production: rebuild state from the raw snapshot store on every invocation."""

    raw_root: Path
    data_origin: str = "LIVE"
    xg_model: object | None = None

    def __call__(self, now: datetime) -> tuple[HistoricalStore, dict[int, list[RosterSlot]]]:
        from nhl.data.ingest import replay, replay_rosters
        from nhl.data.snapshots import RawSnapshotStore

        raw = RawSnapshotStore(self.raw_root)
        store, _ = replay(raw, data_origin=self.data_origin, xg_model=self.xg_model)
        return store, replay_rosters(raw)


class ForwardRunner:
    def __init__(self, root: str | Path, state: StateSource, clock: Callable[[], datetime] = utcnow,
                 simulated_clock: bool = False, n_sims: int | None = None, seed: str = "forward") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.state = state
        self.clock = clock
        self.simulated_clock = simulated_clock
        self.n_sims = n_sims
        self.seed = seed
        self.predictions = PredictionStore(self.root / "predictions.sqlite")
        self.closes = CloseStore(self.root / "closes.sqlite")
        self.overrides = OverrideLog(self.root / "overrides.sqlite")
        self.constants_store = LeagueConstantsStore(self.root)
        self.log_path = self.root / "cycles.jsonl"

    def _log(self, event: dict) -> None:
        with self.log_path.open("a", encoding="utf-8") as h:
            h.write(json.dumps(event, sort_keys=True) + "\n")

    def constants(self, store: HistoricalStore, season: int) -> LeagueConstants:
        c = fit_league_constants(store, season)
        self.constants_store.save(c)  # write-once; identical refits are no-ops
        return c

    def price(self, day: date, season: int) -> dict:
        """Price every not-yet-started game on ``day`` at as_of = now. Appends only
        artifacts whose state changed since the last stored artifact (reprice)."""

        now = self.clock()
        store, rosters = self.state(now)
        view = store.view(now)
        games = [g for g in view.games() if g.season == season and g.start_time.astimezone(ET).date() == day and g.start_time > now]
        summary = {"event": "PRICE", "now": fmt_ts(now), "day": day.isoformat(), "games": len(games), "inserted": 0,
                   "unchanged_state": 0, "skipped_identical": 0, "simulated_clock": self.simulated_clock}
        if games:
            c = self.constants(store, season)
            run = run_slate(store, rosters, season, games, now, PredictionMode.FORWARD, n_sims=self.n_sims, seed=self.seed,
                            created_at=now, constants=c, simulated_clock=self.simulated_clock)
            res = self.predictions.append_if_changed(run.artifacts, now=fmt_ts(now))
            summary.update(res, constants_id=c.constants_id, skipped=[f"{g}:{w}" for g, w in (run.skipped or [])],
                           errors=[f"{g}:{e}" for g, e in run.errors])
        self._log(summary)
        return summary

    def capture_close(self, day: date) -> dict:
        """Persist close-v1 selections for games on ``day`` whose cutoff has passed."""

        now = self.clock()
        store, _ = self.state(now)
        latest = store.latest_games()
        results = {r.game_id: r for r in store.results}
        ids = set()
        for gid, g in latest.items():
            actual = results[gid].actual_start if gid in results else None
            cutoff = actual or g.start_time
            if g.start_time.astimezone(ET).date() == day and cutoff <= now:
                ids.add(gid)
        closes = list(closes_for_store(store, ids).values())
        # Only observations visible now may be used: the store view enforces as_of.
        res = self.closes.append(closes, fmt_ts(now))
        summary = {"event": "CLOSE", "now": fmt_ts(now), "day": day.isoformat(), "games": len(ids),
                   "available": sum(1 for c in closes if c.available), "unavailable": sum(1 for c in closes if not c.available), **res}
        self._log(summary)
        return summary

    def settle(self) -> list[dict]:
        store, _ = self.state(self.clock())
        return settle_predictions(store, list(self.predictions.rows()))

    def verify(self) -> dict:
        p_ok, p_head = self.predictions.verify_chain()
        c_ok, c_head = self.closes.verify_chain()
        return {"predictions_chain": p_ok, "predictions_head": p_head, "closes_chain": c_ok, "closes_head": c_head,
                "predictions": self.predictions.count(), "closes": len(self.closes.rows())}

    def export(self, out_dir: str | Path, day: date | None = None, max_odds_age_min: float = 30.0) -> dict[str, str]:
        now = self.clock()
        store, rosters = self.state(now)
        rows = list(self.predictions.rows())
        latest = store.latest_games()
        ids = sorted({r["game_id"] for r in rows if day is None or latest[r["game_id"]].start_time.astimezone(ET).date() == day})
        season_rosters = [s for v in rosters.values() for s in v]
        fresh = freshness_rows(store.view(now), ids, season_rosters, max_odds_age_min)
        matchups = {gid: f"{g.away}@{g.home}" for gid, g in latest.items()}
        quality = []
        if isinstance(self.state, ReplayStateSource):
            from nhl.data.live_quality import source_quality_rows
            from nhl.data.snapshots import RawSnapshotStore

            quality = source_quality_rows(RawSnapshotStore(self.state.raw_root), now)
        paths = export_reports(out_dir, rows, settle_predictions(store, rows), matchups, fresh,
                               self.overrides.rows(), set(ids), quality)
        from nhl.forward.evidence import build_forward_cohort, first_slate_audit

        out = Path(out_dir)
        cohort = build_forward_cohort(rows)
        (out / "FORWARD_EVIDENCE.json").write_text(json.dumps(cohort, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (out / "FIRST_SLATE_AUDIT.json").write_text(
            json.dumps(first_slate_audit(rows), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        paths["forward_evidence"] = str(out / "FORWARD_EVIDENCE.json")
        paths["first_slate_audit"] = str(out / "FIRST_SLATE_AUDIT.json")
        return paths
