"""Raw immutable snapshot layer.

Every byte fetched from an external source is stored once, content-addressed by
SHA-256, before any parsing. Parsed records carry the ``snapshot_id`` they came
from, so any feature or prediction can be traced back to the exact payload.

Guarantees:
* write-once: blobs are created with O_EXCL and made read-only;
* verify-on-read: the hash is rechecked on every ``get``;
* the manifest is append-only JSONL (one line per fetch, even for identical bytes,
  because *when* we saw a payload matters as much as what it contained).
"""

from __future__ import annotations

import gzip
import json
import os
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterator

from nhl.timeutil import fmt_ts, parse_ts


class SnapshotIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True)
class SnapshotEntry:
    snapshot_id: str
    source: str
    key: str
    fetched_at: datetime
    sha256: str
    n_bytes: int
    meta: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps(
            {
                "snapshot_id": self.snapshot_id,
                "source": self.source,
                "key": self.key,
                "fetched_at": fmt_ts(self.fetched_at),
                "sha256": self.sha256,
                "n_bytes": self.n_bytes,
                "meta": self.meta,
            },
            sort_keys=True,
        )


class RawSnapshotStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.blob_dir = self.root / "raw" / "blobs"
        self.manifest_path = self.root / "raw" / "manifest.jsonl"
        self.blob_dir.mkdir(parents=True, exist_ok=True)

    def _blob_path(self, digest: str) -> Path:
        return self.blob_dir / digest[:2] / f"{digest}.gz"

    def put(
        self,
        source: str,
        key: str,
        payload: bytes,
        fetched_at: str | datetime,
        meta: dict[str, Any] | None = None,
    ) -> SnapshotEntry:
        if not source or not key:
            raise ValueError("source and key required")
        digest = sha256(payload).hexdigest()
        path = self._blob_path(digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            # mtime=0 keeps the gzip bytes deterministic.
            data = gzip.compress(payload, mtime=0)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
        entry = SnapshotEntry(
            snapshot_id=f"{source}:{digest[:20]}",
            source=source,
            key=key,
            fetched_at=parse_ts(fetched_at),
            sha256=digest,
            n_bytes=len(payload),
            meta=dict(meta or {}),
        )
        with self.manifest_path.open("a", encoding="utf-8") as handle:
            handle.write(entry.to_json() + "\n")
        return entry

    def get_bytes(self, entry: SnapshotEntry) -> bytes:
        payload = gzip.decompress(self._blob_path(entry.sha256).read_bytes())
        if sha256(payload).hexdigest() != entry.sha256:
            raise SnapshotIntegrityError(f"hash mismatch for {entry.snapshot_id}")
        return payload

    def get_json(self, entry: SnapshotEntry) -> Any:
        return json.loads(self.get_bytes(entry).decode("utf-8"))

    def entries(
        self,
        source: str | None = None,
        key: str | None = None,
        as_of: str | datetime | None = None,
    ) -> Iterator[SnapshotEntry]:
        """Manifest entries, optionally only those fetched at or before ``as_of``."""

        if not self.manifest_path.exists():
            return
        cutoff = parse_ts(as_of) if as_of is not None else None
        with self.manifest_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                raw = json.loads(line)
                entry = SnapshotEntry(
                    snapshot_id=raw["snapshot_id"],
                    source=raw["source"],
                    key=raw["key"],
                    fetched_at=parse_ts(raw["fetched_at"]),
                    sha256=raw["sha256"],
                    n_bytes=raw["n_bytes"],
                    meta=raw.get("meta", {}),
                )
                if source is not None and entry.source != source:
                    continue
                if key is not None and entry.key != key:
                    continue
                if cutoff is not None and entry.fetched_at > cutoff:
                    continue
                yield entry

    def latest(self, source: str, key: str, as_of: str | datetime | None = None) -> SnapshotEntry | None:
        best: SnapshotEntry | None = None
        for entry in self.entries(source=source, key=key, as_of=as_of):
            if best is None or entry.fetched_at >= best.fetched_at:
                best = entry
        return best
