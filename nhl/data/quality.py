"""Data-quality and freshness gates for a slate at an ``as_of``.

Each check yields PASS / WARN / FAIL with detail. Any FAIL sets ``hard_fail``;
governance turns hard failures into BLOCKED rows (never promotable), the same way
tournament_v2 routes ``hard_block_reasons``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from nhl.contracts import MarketType

from .pit import LeakageError, PointInTimeView, assert_visible


@dataclass(frozen=True)
class Check:
    name: str
    status: str  # PASS | WARN | FAIL
    detail: str = ""
    game_id: str = ""


@dataclass
class HealthReport:
    as_of: str
    checks: list[Check] = field(default_factory=list)

    @property
    def hard_fail(self) -> bool:
        return any(c.status == "FAIL" for c in self.checks)

    @property
    def score(self) -> float:
        if not self.checks:
            return 0.0
        pts = {"PASS": 1.0, "WARN": 0.5, "FAIL": 0.0}
        return sum(pts[c.status] for c in self.checks) / len(self.checks)

    def game_blocks(self, game_id: str) -> list[str]:
        return sorted({c.name for c in self.checks if c.status == "FAIL" and c.game_id in ("", game_id)})

    def to_dict(self) -> dict[str, Any]:
        counts = {s: sum(1 for c in self.checks if c.status == s) for s in ("PASS", "WARN", "FAIL")}
        return {
            "as_of": self.as_of,
            "score": round(self.score, 4),
            "hard_fail": self.hard_fail,
            "counts": counts,
            "failures": [c.__dict__ for c in self.checks if c.status != "PASS"],
        }


def evaluate_slate(
    view: PointInTimeView,
    game_ids: list[str],
    max_odds_age_minutes: float = 30.0,
    min_team_games_or_prior: int = 1,
    teams_with_prior: set[str] | None = None,
) -> HealthReport:
    report = HealthReport(as_of=view.as_of.isoformat())
    games = {g.game_id: g for g in view.games()}
    played = view.team_games_played()
    starts_by_team: dict[str, int] = {}
    for gs in view.goalie_stats():
        if gs.started:
            starts_by_team[gs.team] = starts_by_team.get(gs.team, 0) + 1
    priors = teams_with_prior or set()

    try:
        assert_visible(view.results(), view.as_of, "result")
        assert_visible(view.team_stats(), view.as_of, "team_stats")
        assert_visible(view.goalie_stats(), view.as_of, "goalie_stats")
        report.checks.append(Check("NO_FUTURE_RECORDS", "PASS"))
    except LeakageError as exc:  # pragma: no cover - view guarantees this
        report.checks.append(Check("NO_FUTURE_RECORDS", "FAIL", str(exc)))

    for gid in game_ids:
        g = games.get(gid)
        if g is None:
            report.checks.append(Check("SCHEDULE_KNOWN", "FAIL", "game not visible in schedule", gid))
            continue
        report.checks.append(Check("SCHEDULE_KNOWN", "PASS", game_id=gid))
        if view.as_of >= g.start_time:
            report.checks.append(Check("PREGAME", "FAIL", "as_of is at/after puck drop", gid))
        else:
            report.checks.append(Check("PREGAME", "PASS", game_id=gid))

        ml = view.odds(gid, MarketType.ML)
        if not ml:
            report.checks.append(Check("ODDS_PRESENT", "WARN", "no ML prices", gid))
        else:
            report.checks.append(Check("ODDS_PRESENT", "PASS", game_id=gid))
            newest = max(s.snapshot_ts for s in ml)
            age = view.as_of - newest
            status = "PASS" if age <= timedelta(minutes=max_odds_age_minutes) else "WARN"
            report.checks.append(Check("ODDS_FRESH", status, f"age_min={age.total_seconds() / 60:.1f}", gid))
            books_by_sel: dict[str, set[str]] = {}
            for s in ml:
                books_by_sel.setdefault(s.book, set()).add(s.selection.value)
            one_sided = sorted(b for b, sels in books_by_sel.items() if len(sels) < 2)
            report.checks.append(
                Check("ODDS_TWO_SIDED", "WARN" if one_sided else "PASS", ",".join(one_sided), gid)
            )

        for team in (g.home, g.away):
            has_history = len(played.get(team, [])) >= min_team_games_or_prior or team in priors
            report.checks.append(
                Check("TEAM_RATING_INPUTS", "PASS" if has_history else "FAIL", team, gid)
            )
            reps = [r for r in view.goalie_reports(gid) if r.team == team]
            if reps or starts_by_team.get(team, 0) > 0:
                report.checks.append(Check("GOALIE_INPUTS", "PASS", team, gid))
            else:
                report.checks.append(Check("GOALIE_INPUTS", "FAIL", f"{team}: no report and no start history", gid))
    return report
