"""Immutable prediction artifacts.

Every priced outcome is written once, before puck drop, to an append-only SQLite
table:

* UPDATE and DELETE are rejected by triggers;
* each row stores ``row_hash = sha256(canonical row + prev_hash)``, so any edit made
  outside SQLite (or after dropping the triggers) breaks ``verify_chain``;
* ``as_of`` must be strictly before ``puck_drop``; FORWARD rows additionally require
  the wall-clock ``created_at`` to be before puck drop (no post-hoc forward picks);
* re-running the same prediction is idempotent only if the content is identical --
  a changed row with the same ``prediction_id`` is refused, never overwritten.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterator

from nhl.contracts import PredictionMode, stable_digest
from nhl.timeutil import fmt_ts, parse_ts, utcnow

GENESIS = "0" * 64


class ArtifactError(RuntimeError):
    pass


@dataclass(frozen=True)
class PredictionArtifact:
    prediction_id: str
    created_at: str
    as_of: str
    puck_drop: str
    mode: str
    data_origin: str
    game_id: str
    market: str
    selection: str
    line: float | None
    team: str | None
    market_key: str
    sportsbook: str
    market_price: int
    market_observed_at: str
    no_vig_probability: float
    model_probability: float  # win prob conditional on no push
    model_p_win: float
    model_p_push: float
    fair_price: int | None
    edge: float  # model_probability - no_vig_probability
    ev_per_unit: float  # at market_price, push-aware
    goalie_state: str
    lineup_state: str
    model_version: str
    feature_version: str
    config_hash: str
    data_snapshot_id: str
    health_score: float
    status: str
    shadow_lane: str
    reason_codes: str  # JSON list

    def content(self) -> dict[str, Any]:
        return asdict(self)


COLUMNS = list(PredictionArtifact.__dataclass_fields__)
_REAL = {"line", "no_vig_probability", "model_probability", "model_p_win", "model_p_push", "edge", "ev_per_unit", "health_score"}
_INT = {"market_price", "fair_price"}


def make_prediction_id(game_id: str, market_key: str, sportsbook: str, as_of: str, model_version: str, mode: str) -> str:
    return "P:" + stable_digest([game_id, market_key, sportsbook, as_of, model_version, mode], n=20)


def _canonical(content: dict[str, Any]) -> str:
    return json.dumps(content, sort_keys=True, separators=(",", ":"), default=str)


class PredictionStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        cols = ",\n".join(
            f"{c} {'REAL' if c in _REAL else 'INTEGER' if c in _INT else 'TEXT'}" for c in COLUMNS
        )
        self.conn.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS predictions (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                {cols},
                prev_hash TEXT NOT NULL,
                row_hash TEXT NOT NULL,
                UNIQUE(prediction_id)
            );
            CREATE TRIGGER IF NOT EXISTS predictions_no_update BEFORE UPDATE ON predictions
            BEGIN SELECT RAISE(ABORT, 'predictions are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS predictions_no_delete BEFORE DELETE ON predictions
            BEGIN SELECT RAISE(ABORT, 'predictions are append-only'); END;
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def _head(self) -> str:
        row = self.conn.execute("SELECT row_hash FROM predictions ORDER BY seq DESC LIMIT 1").fetchone()
        return row[0] if row else GENESIS

    def append(self, artifacts: list[PredictionArtifact], now: str | None = None) -> dict[str, int]:
        """Append artifacts atomically. Returns counts of inserted / identical-skipped rows."""

        wall = parse_ts(now) if now else utcnow()
        inserted = skipped = 0
        with self.conn:
            prev = self._head()
            for a in artifacts:
                as_of, drop, created = parse_ts(a.as_of), parse_ts(a.puck_drop), parse_ts(a.created_at)
                if as_of >= drop:
                    raise ArtifactError(f"{a.prediction_id}: as_of {a.as_of} not before puck drop {a.puck_drop}")
                if a.mode == PredictionMode.FORWARD.value and (created >= drop or wall >= drop):
                    raise ArtifactError(f"{a.prediction_id}: forward prediction written at/after puck drop")
                content = a.content()
                existing = self.conn.execute(
                    f"SELECT {', '.join(COLUMNS)} FROM predictions WHERE prediction_id = ?", (a.prediction_id,)
                ).fetchone()
                if existing is not None:
                    if _canonical(dict(zip(COLUMNS, existing))) == _canonical(content):
                        skipped += 1
                        continue
                    raise ArtifactError(f"{a.prediction_id}: refusing to rewrite an existing prediction")
                row_hash = sha256((_canonical(content) + prev).encode("utf-8")).hexdigest()
                self.conn.execute(
                    f"INSERT INTO predictions ({', '.join(COLUMNS)}, prev_hash, row_hash) VALUES ({', '.join('?' * (len(COLUMNS) + 2))})",
                    [content[c] for c in COLUMNS] + [prev, row_hash],
                )
                prev = row_hash
                inserted += 1
        return {"inserted": inserted, "skipped_identical": skipped}

    def rows(self, where: str = "", params: tuple = ()) -> Iterator[dict[str, Any]]:
        cur = self.conn.execute(f"SELECT {', '.join(COLUMNS)} FROM predictions {where} ORDER BY seq", params)
        for r in cur:
            yield dict(zip(COLUMNS, r))

    def verify_chain(self) -> tuple[bool, str]:
        prev = GENESIS
        for r in self.conn.execute(f"SELECT seq, {', '.join(COLUMNS)}, prev_hash, row_hash FROM predictions ORDER BY seq"):
            seq, values, prev_hash, row_hash = r[0], r[1:-2], r[-2], r[-1]
            content = dict(zip(COLUMNS, values))
            if prev_hash != prev:
                return False, f"seq {seq}: prev_hash mismatch"
            if sha256((_canonical(content) + prev).encode("utf-8")).hexdigest() != row_hash:
                return False, f"seq {seq}: content hash mismatch"
            prev = row_hash
        return True, prev

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0])


def now_iso() -> str:
    return fmt_ts(utcnow())
