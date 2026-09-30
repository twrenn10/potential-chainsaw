"""Record-level validation primitives shared by every parser.

Parsers never coerce impossible or ambiguous values. A bad record is rejected with a
reason and the rest of the payload is still parsed; callers decide whether any
rejection is fatal (``ParseResult.require_clean``).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Generic, TypeVar

from nhl.timeutil import parse_ts

T = TypeVar("T")
_INT_RE = re.compile(r"^-?\d+(\.0+)?$")


class FieldError(ValueError):
    pass


class ParseError(ValueError):
    pass


@dataclass(frozen=True)
class Rejection:
    source: str
    record_key: str
    reason: str


@dataclass
class ParseResult(Generic[T]):
    records: list[T] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)
    warnings: list[Rejection] = field(default_factory=list)

    def reject(self, source: str, key: str, reason: str) -> None:
        self.rejections.append(Rejection(source, key, reason))

    def warn(self, source: str, key: str, reason: str) -> None:
        self.warnings.append(Rejection(source, key, reason))

    def require_clean(self) -> list[T]:
        if self.rejections:
            head = "; ".join(f"{r.record_key}: {r.reason}" for r in self.rejections[:5])
            raise ParseError(f"{len(self.rejections)} rejected record(s): {head}")
        return self.records


def req(d: Any, *path: str) -> Any:
    cur = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur or cur[p] is None:
            raise FieldError(f"missing field {'.'.join(path)}")
        cur = cur[p]
    return cur


def strict_int(value: Any, name: str, lo: int | None = 0, hi: int | None = None) -> int:
    """Integers only: '3', 3, '3.0' ok; '2.7', '', 'x', True are rejected."""

    if isinstance(value, bool):
        raise FieldError(f"{name}: boolean is not an integer")
    if isinstance(value, int):
        out = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise FieldError(f"{name}: non-integer {value}")
        out = int(value)
    elif isinstance(value, str) and _INT_RE.match(value.strip()):
        out = int(float(value.strip()))
    else:
        raise FieldError(f"{name}: not an integer: {value!r}")
    if lo is not None and out < lo:
        raise FieldError(f"{name}: {out} < {lo}")
    if hi is not None and out > hi:
        raise FieldError(f"{name}: {out} > {hi}")
    return out


def strict_float(value: Any, name: str, lo: float | None = 0.0, hi: float | None = None) -> float:
    if isinstance(value, bool) or value is None or (isinstance(value, str) and not value.strip()):
        raise FieldError(f"{name}: missing/invalid number {value!r}")
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise FieldError(f"{name}: not a number: {value!r}") from exc
    if not math.isfinite(out):
        raise FieldError(f"{name}: non-finite {value!r}")
    if lo is not None and out < lo:
        raise FieldError(f"{name}: {out} < {lo}")
    if hi is not None and out > hi:
        raise FieldError(f"{name}: {out} > {hi}")
    return out


def strict_utc(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or not (value.endswith("Z") or "+" in value[10:]):
        raise FieldError(f"{name}: timestamp without explicit UTC offset: {value!r}")
    try:
        return parse_ts(value)
    except ValueError as exc:
        raise FieldError(f"{name}: {exc}") from exc


def mmss(value: Any, name: str, max_seconds: int) -> int:
    if not isinstance(value, str) or not re.match(r"^\d{1,2}:\d{2}$", value):
        raise FieldError(f"{name}: bad clock {value!r}")
    m, s = value.split(":")
    if int(s) >= 60:
        raise FieldError(f"{name}: bad seconds {value!r}")
    out = int(m) * 60 + int(s)
    if out > max_seconds:
        raise FieldError(f"{name}: {value} exceeds {max_seconds}s")
    return out
