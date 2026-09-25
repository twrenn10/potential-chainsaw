"""UTC timestamp helpers. Every timestamp in the system is an aware UTC datetime.

Naive datetimes are rejected: an ambiguous clock is how leakage sneaks in.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

UTC = timezone.utc


def parse_ts(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        token = value.strip()
        if token.endswith("Z"):
            token = token[:-1] + "+00:00"
        dt = datetime.fromisoformat(token)
    if dt.tzinfo is None:
        raise ValueError(f"naive timestamp not allowed: {value!r}")
    return dt.astimezone(UTC)


def fmt_ts(dt: datetime) -> str:
    return parse_ts(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def plus(dt: datetime, **kwargs: float) -> datetime:
    return parse_ts(dt) + timedelta(**kwargs)
