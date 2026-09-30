"""Append-only store of closing-line selections (available AND unavailable).

Persisted per (game, book, market group, close rule version): the rule, the cutoff
instant and whether it was actual or scheduled puck drop, the selected legs (exact
observations), raw implied and no-vig probabilities with the method, staleness,
provider and completeness status. Unavailable closes are persisted too, with the
reason, so evaluation can never quietly substitute another price.
"""

from __future__ import annotations

import json
import sqlite3
from hashlib import sha256
from pathlib import Path
from typing import Any

from nhl.contracts import stable_digest
from nhl.market.snapshots import CloseSelection
from nhl.timeutil import fmt_ts

GENESIS = "0" * 64
COLUMNS = ("close_id", "game_id", "book", "market_id", "rule", "rule_version", "cutoff", "cutoff_basis", "status",
           "reason", "provider", "staleness_minutes", "legs", "prices", "raw_implied", "no_vig", "overround",
           "novig_method", "captured_at")


class CloseStoreError(RuntimeError):
    pass


def close_row(c: CloseSelection, captured_at: str) -> dict[str, Any]:
    q = c.quote
    return {
        "close_id": "C:" + stable_digest([c.game_id, c.book, c.market_id, c.rule_version, fmt_ts(c.cutoff)], 20),
        "game_id": c.game_id, "book": c.book, "market_id": c.market_id, "rule": c.rule, "rule_version": c.rule_version,
        "cutoff": fmt_ts(c.cutoff), "cutoff_basis": c.cutoff_basis, "status": c.status, "reason": c.reason,
        "provider": q.provider if q else "",
        "staleness_minutes": None if c.staleness_minutes is None else round(c.staleness_minutes, 3),
        "legs": json.dumps(q.legs if q else []),
        "prices": json.dumps({s.value: p for s, p in q.prices.items()} if q else {}, sort_keys=True),
        "raw_implied": json.dumps(dict(zip(q.novig.selections, [round(x, 8) for x in q.novig.raw_implied])) if q else {}, sort_keys=True),
        "no_vig": json.dumps(dict(zip(q.novig.selections, [round(x, 8) for x in q.novig.no_vig])) if q else {}, sort_keys=True),
        "overround": round(q.novig.overround, 8) if q else None,
        "novig_method": q.novig.method if q else "",
        "captured_at": captured_at,
    }


def _content(row: dict[str, Any]) -> str:
    return json.dumps({k: row[k] for k in COLUMNS if k != "captured_at"}, sort_keys=True, separators=(",", ":"))


class CloseStore:
    def __init__(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        cols = ", ".join(f"{c} {'REAL' if c in ('staleness_minutes', 'overround') else 'TEXT'}" for c in COLUMNS)
        self.conn.executescript(f"""
            CREATE TABLE IF NOT EXISTS closes (seq INTEGER PRIMARY KEY AUTOINCREMENT, {cols},
                prev_hash TEXT NOT NULL, row_hash TEXT NOT NULL, UNIQUE(close_id));
            CREATE TRIGGER IF NOT EXISTS closes_ro_u BEFORE UPDATE ON closes BEGIN SELECT RAISE(ABORT, 'closes are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS closes_ro_d BEFORE DELETE ON closes BEGIN SELECT RAISE(ABORT, 'closes are append-only'); END;
        """)
        self.conn.commit()

    def append(self, closes: list[CloseSelection], captured_at: str) -> dict[str, int]:
        ins = same = 0
        with self.conn:
            head = self.conn.execute("SELECT row_hash FROM closes ORDER BY seq DESC LIMIT 1").fetchone()
            prev = head[0] if head else GENESIS
            for c in closes:
                row = close_row(c, captured_at)
                old = self.conn.execute(f"SELECT {', '.join(COLUMNS)} FROM closes WHERE close_id = ?", (row["close_id"],)).fetchone()
                if old is not None:
                    if _content(dict(zip(COLUMNS, old))) == _content(row):
                        same += 1
                        continue
                    raise CloseStoreError(f"{row['close_id']}: refusing to rewrite a recorded close")
                h = sha256((_content(row) + prev).encode()).hexdigest()
                self.conn.execute(f"INSERT INTO closes ({', '.join(COLUMNS)}, prev_hash, row_hash) VALUES ({', '.join('?' * (len(COLUMNS) + 2))})",
                                  [row[k] for k in COLUMNS] + [prev, h])
                prev = h
                ins += 1
        return {"inserted": ins, "skipped_identical": same}

    def rows(self) -> list[dict[str, Any]]:
        cur = self.conn.execute(f"SELECT {', '.join(COLUMNS)} FROM closes ORDER BY seq")
        return [dict(zip(COLUMNS, r)) for r in cur]

    def lookup(self) -> dict[tuple[str, str, str], dict[str, Any]]:
        return {(r["game_id"], r["book"], r["market_id"]): r for r in self.rows()}

    def verify_chain(self) -> tuple[bool, str]:
        prev = GENESIS
        for r in self.conn.execute(f"SELECT seq, {', '.join(COLUMNS)}, prev_hash, row_hash FROM closes ORDER BY seq"):
            row = dict(zip(COLUMNS, r[1:-2]))
            if r[-2] != prev or sha256((_content(row) + prev).encode()).hexdigest() != r[-1]:
                return False, f"seq {r[0]}"
            prev = r[-1]
        return True, prev
