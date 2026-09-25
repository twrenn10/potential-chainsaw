"""Append-only bet ledger: placements and settlements are separate immutable events.

Phase 1 permits PAPER entries only (for tracking CLV plumbing). A bet may only
reference an existing prediction artifact, and BLOCKED predictions can never be bet.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from nhl.contracts import BetResult, Lane, stable_digest

from .predictions import PredictionStore


class LedgerError(RuntimeError):
    pass


@dataclass(frozen=True)
class Placement:
    prediction_id: str
    placed_at: str
    book: str
    price_american: int
    stake: float
    paper: bool
    max_stake_seen: float | None = None  # limit shown by the book at placement
    requested_stake: float | None = None  # what we wanted; > max_stake_seen = limited


class BetLedger:
    def __init__(self, path: str | Path, predictions: PredictionStore) -> None:
        self.predictions = predictions
        self.conn = sqlite3.connect(str(path))
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS placements (
                bet_id TEXT PRIMARY KEY, prediction_id TEXT NOT NULL, placed_at TEXT NOT NULL,
                book TEXT NOT NULL, price_american INTEGER NOT NULL, stake REAL NOT NULL, paper INTEGER NOT NULL,
                max_stake_seen REAL, requested_stake REAL
            );
            CREATE TABLE IF NOT EXISTS settlements (
                bet_id TEXT PRIMARY KEY REFERENCES placements(bet_id), settled_at TEXT NOT NULL,
                result TEXT NOT NULL, profit REAL NOT NULL, close_price INTEGER, close_no_vig REAL, clv_ev REAL
            );
            CREATE TRIGGER IF NOT EXISTS placements_ro_u BEFORE UPDATE ON placements BEGIN SELECT RAISE(ABORT, 'append-only'); END;
            CREATE TRIGGER IF NOT EXISTS placements_ro_d BEFORE DELETE ON placements BEGIN SELECT RAISE(ABORT, 'append-only'); END;
            CREATE TRIGGER IF NOT EXISTS settlements_ro_u BEFORE UPDATE ON settlements BEGIN SELECT RAISE(ABORT, 'append-only'); END;
            CREATE TRIGGER IF NOT EXISTS settlements_ro_d BEFORE DELETE ON settlements BEGIN SELECT RAISE(ABORT, 'append-only'); END;
            """
        )
        self.conn.commit()

    def place(self, p: Placement) -> str:
        rows = list(self.predictions.rows("WHERE prediction_id = ?", (p.prediction_id,)))
        if not rows:
            raise LedgerError(f"unknown prediction {p.prediction_id}")
        status = rows[0]["status"]
        if status == Lane.BLOCKED.value:
            raise LedgerError("BLOCKED predictions can never be bet")
        if status in (Lane.UNVALIDATED.value, Lane.PASS.value, Lane.WATCH.value) and not p.paper:
            raise LedgerError(f"{status} predictions allow paper tracking only")
        bet_id = "BET:" + stable_digest([p.prediction_id, p.placed_at, p.book, str(p.price_american), f"{p.stake:.4f}"], 16)
        with self.conn:
            self.conn.execute(
                "INSERT INTO placements VALUES (?,?,?,?,?,?,?,?,?)",
                (bet_id, p.prediction_id, p.placed_at, p.book, p.price_american, p.stake, int(p.paper), p.max_stake_seen, p.requested_stake),
            )
        return bet_id

    def settle(self, bet_id: str, settled_at: str, result: BetResult, profit: float,
               close_price: int | None, close_no_vig: float | None, clv_ev: float | None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO settlements VALUES (?,?,?,?,?,?,?)",
                (bet_id, settled_at, result.value, profit, close_price, close_no_vig, clv_ev),
            )

    def open_bets(self) -> list[str]:
        """Reconciliation: placements with no settlement event."""

        cur = self.conn.execute(
            "SELECT p.bet_id FROM placements p LEFT JOIN settlements s ON p.bet_id = s.bet_id WHERE s.bet_id IS NULL ORDER BY p.bet_id"
        )
        return [r[0] for r in cur]
