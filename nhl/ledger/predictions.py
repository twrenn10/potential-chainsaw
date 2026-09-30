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
import math
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


SCHEMA_VERSION = "artifact-v2"


@dataclass(frozen=True)
class PredictionArtifact:
    """One immutable priced outcome. The pricing chain is kept in distinct fields:

    model probability -> model fair odds -> raw market probability -> no-vig market
    probability -> probability edge -> execution price -> EV -> governance eligibility.
    """

    # identity / timing
    prediction_id: str
    schema_version: str
    created_at: str  # wall-clock write time
    as_of: str  # information cutoff
    puck_drop: str  # scheduled puck drop known at as_of
    mode: str  # FORWARD | BACKTEST
    data_origin: str
    evidence_lane: str  # DEV_SYNTHETIC | HISTORICAL_RESEARCH | STRICT_WALK_FORWARD | SHADOW_FORWARD | FORWARD_DEGRADED
    # market identity
    game_id: str
    market: str
    period: str
    selection: str
    line: float | None
    team: str | None
    participant: str | None
    market_key: str  # outcome key (book-agnostic)
    market_id: str  # group key (book-agnostic)
    sportsbook: str
    provider: str
    market_snapshot_ref: str  # exact observations the quote came from
    market_observed_at: str
    quote_age_minutes: float
    # market side of the chain
    execution_price: int  # American, as offered
    execution_decimal: float  # exact price used for EV
    raw_implied_probability: float  # 1/decimal, includes margin
    no_vig_probability: float  # benchmark
    novig_method: str
    market_overround: float
    # model side of the chain
    model_probability: float  # win prob conditional on no push
    model_p_win: float
    model_p_push: float
    model_fair_price: int | None  # American
    model_fair_decimal: float | None
    # comparisons (never a single ambiguous "edge")
    probability_edge: float  # model_probability - no_vig_probability
    fair_price_edge: float | None  # execution_decimal / model_fair_decimal - 1
    ev_per_unit: float  # push-aware EV at execution_price
    # state / lineage fingerprints
    goalie_state: str
    goalie_fingerprint: str
    lineup_state: str
    roster_fingerprint: str
    model_version: str
    feature_version: str
    parameter_fingerprint: str
    constants_id: str
    default_constants: str  # JSON list of simulator constants still at static defaults
    config_hash: str
    data_snapshot_id: str
    health_score: float
    # governance
    status: str  # action lane: BLOCKED | UNVALIDATED | ... | ACTIONABLE
    shadow_lane: str  # informational only
    eligibility: str
    block_reasons: str  # JSON list of HARD_BLOCK reasons
    reason_codes: str  # JSON list of all reasons (hard + soft)
    state_fingerprint: str  # everything price-relevant; equal => no new artifact needed

    def content(self) -> dict[str, Any]:
        return asdict(self)


COLUMNS = list(PredictionArtifact.__dataclass_fields__)
_REAL = {"line", "quote_age_minutes", "execution_decimal", "raw_implied_probability", "no_vig_probability",
         "market_overround", "model_probability", "model_p_win", "model_p_push", "model_fair_decimal",
         "probability_edge", "fair_price_edge", "ev_per_unit", "health_score"}
_INT = {"execution_price", "model_fair_price"}
STATE_EXCLUDE = {"prediction_id", "created_at", "as_of", "quote_age_minutes", "market_observed_at",
                 "market_snapshot_ref", "data_snapshot_id", "health_score", "state_fingerprint"}


def state_fingerprint(content: dict[str, Any]) -> str:
    """Hash of every price-relevant field. Re-observing an unchanged price, or the clock
    moving, does not change it; any change to inputs, prices, outputs or governance does."""

    return "S:" + sha256(_canonical({k: v for k, v in content.items() if k not in STATE_EXCLUDE}).encode()).hexdigest()[:24]


def make_prediction_id(game_id: str, market_key: str, sportsbook: str, as_of: str, model_version: str, mode: str) -> str:
    return "P:" + stable_digest([game_id, market_key, sportsbook, as_of, model_version, mode], n=20)


def _norm(value: Any) -> Any:
    # SQLite REAL cannot store -0.0 (reads back 0.0); hash what survives the round trip.
    if isinstance(value, float) and value == 0.0:
        return 0.0
    return value


def _canonical(content: dict[str, Any]) -> str:
    return json.dumps({k: _norm(v) for k, v in content.items()}, sort_keys=True, separators=(",", ":"), default=str)


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
        have = [r[1] for r in self.conn.execute("PRAGMA table_info(predictions)")]
        if have[1:-2] != COLUMNS:
            raise ArtifactError(f"{self.path}: artifact schema differs from {SCHEMA_VERSION}; open a new store "
                                "(existing stores are never altered)")

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
                if a.schema_version != SCHEMA_VERSION:
                    raise ArtifactError(f"{a.prediction_id}: schema {a.schema_version} != {SCHEMA_VERSION}")
                if a.state_fingerprint != state_fingerprint(content):
                    raise ArtifactError(f"{a.prediction_id}: state_fingerprint does not match content")
                bad = [k for k, v in content.items() if isinstance(v, float) and not math.isfinite(v)]
                if bad:
                    raise ArtifactError(f"{a.prediction_id}: non-finite values in {bad} (would not round-trip SQLite)")
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

    def latest_state(self, mode: str) -> dict[tuple[str, str, str], str]:
        """Latest state fingerprint per (game_id, market_key, sportsbook) for a mode."""

        out: dict[tuple[str, str, str], str] = {}
        cur = self.conn.execute(
            "SELECT game_id, market_key, sportsbook, state_fingerprint FROM predictions WHERE mode = ? ORDER BY seq", (mode,))
        for g, k, b, f in cur:
            out[(g, k, b)] = f
        return out

    def append_if_changed(self, artifacts: list[PredictionArtifact], now: str | None = None) -> dict[str, int]:
        """Reprice semantics: append only artifacts whose state differs from the latest
        stored artifact for the same outcome/book/mode. Never rewrites anything."""

        fresh, unchanged = [], 0
        latest = {m: self.latest_state(m) for m in {a.mode for a in artifacts}}
        for a in artifacts:
            if latest[a.mode].get((a.game_id, a.market_key, a.sportsbook)) == a.state_fingerprint:
                unchanged += 1
            else:
                fresh.append(a)
        res = self.append(fresh, now=now)
        res["unchanged_state"] = unchanged
        return res

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0])


def now_iso() -> str:
    return fmt_ts(utcnow())
