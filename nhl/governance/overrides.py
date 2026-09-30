"""Explicit, logged, reasoned operator overrides -- never silent, never mutating.

An override is a separate append-only record that references a prediction; the
prediction artifact itself is never changed. The effective status shown on the
desk is derived (``effective_status``) and labelled as an override, so it is always
distinguishable from normal promotion.

* ``DEMOTE`` (block a row for operational reasons) is always allowed.
* ``PROMOTE_REQUEST`` is recorded for audit but has NO effect unless the market's
  validation gates passed and governance allows ACTIONABLE -- which the code refuses
  in the current phase. There is no path to manual promotion.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from nhl.contracts import Lane, stable_digest
from nhl.governance.lanes import GovernanceError, assert_allowed


class OverrideError(RuntimeError):
    pass


@dataclass(frozen=True)
class Override:
    prediction_id: str
    action: str  # DEMOTE | PROMOTE_REQUEST
    operator: str
    reason: str
    created_at: str


class OverrideLog:
    def __init__(self, path: str | Path) -> None:
        self.conn = sqlite3.connect(str(path))
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS overrides (override_id TEXT PRIMARY KEY, prediction_id TEXT NOT NULL,
                action TEXT NOT NULL, operator TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL,
                effective INTEGER NOT NULL, refusal TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS overrides_ro_u BEFORE UPDATE ON overrides BEGIN SELECT RAISE(ABORT, 'append-only'); END;
            CREATE TRIGGER IF NOT EXISTS overrides_ro_d BEFORE DELETE ON overrides BEGIN SELECT RAISE(ABORT, 'append-only'); END;
        """)
        self.conn.commit()

    def record(self, o: Override, gates_passed_for_market: bool = False) -> dict:
        if o.action not in {"DEMOTE", "PROMOTE_REQUEST"}:
            raise OverrideError(f"unknown override action {o.action!r}")
        if not o.operator.strip() or not o.reason.strip():
            raise OverrideError("override needs an operator and a reason")
        effective, refusal = True, ""
        if o.action == "PROMOTE_REQUEST":
            try:
                if not gates_passed_for_market:
                    raise GovernanceError("validation gates have not passed for this market")
                assert_allowed(Lane.ACTIONABLE)
            except GovernanceError as exc:
                effective, refusal = False, str(exc)
        oid = "O:" + stable_digest([o.prediction_id, o.action, o.operator, o.reason, o.created_at], 16)
        with self.conn:
            self.conn.execute("INSERT INTO overrides VALUES (?,?,?,?,?,?,?,?)",
                              (oid, o.prediction_id, o.action, o.operator, o.reason, o.created_at, int(effective), refusal))
        return {"override_id": oid, "effective": effective, "refusal": refusal}

    def rows(self) -> list[dict]:
        cur = self.conn.execute("SELECT override_id, prediction_id, action, operator, reason, created_at, effective, refusal "
                                "FROM overrides ORDER BY created_at, override_id")
        keys = ("override_id", "prediction_id", "action", "operator", "reason", "created_at", "effective", "refusal")
        return [dict(zip(keys, r)) for r in cur]


def effective_status(status: str, overrides: list[dict]) -> str:
    """Desk-facing status. Effective demotions show as BLOCKED(OVERRIDE); refused
    promotion requests change nothing."""

    if any(o["action"] == "DEMOTE" and o["effective"] for o in overrides):
        return "BLOCKED(OVERRIDE)"
    return status
