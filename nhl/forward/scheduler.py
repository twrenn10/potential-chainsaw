"""Configuration-driven scheduler plan; orchestration only, never pricing logic."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class ScheduledStep:
    due_at: datetime
    action: str
    reason: str


def build_plan(puck_drops: list[datetime], cfg: dict) -> list[ScheduledStep]:
    if not puck_drops:
        return []
    first, last = min(puck_drops), max(puck_drops)
    steps = [ScheduledStep(first - timedelta(hours=cfg["morning_hours_before_first"]), "morning", "baseline")]
    t = first - timedelta(hours=cfg["pregame_start_hours_before"])
    while t < last:
        minutes = (first - t).total_seconds() / 60
        cadence = cfg["near_close_minutes"] if minutes <= cfg["near_close_window_minutes"] else cfg["pregame_minutes"]
        steps.append(ScheduledStep(t, "refresh_and_price", "source-fingerprint-gated"))
        t += timedelta(minutes=cadence)
    steps.append(ScheduledStep(last + timedelta(hours=cfg["postgame_hours_after_last"]), "postgame", "results-grade-clv-verify-export"))
    return sorted(set(steps), key=lambda s: (s.due_at, s.action))
