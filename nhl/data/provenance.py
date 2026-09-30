"""Temporal provenance: could this value actually have been known at ``as_of``?

Every ingested record gets a ``Provenance`` built by ONE of the named rules below.
The rule decides ``available_at`` and whether the record is *causal* (usable in a
strict walk-forward). Non-causal records are kept for diagnostics but are
invisible to strict ``PointInTimeView``s.

Information classes and their rules (see docs/temporal_provenance.md):

* IMMUTABLE_EVENT_FACT  -- final scores, play-by-play events, boxscore counts.
  The value is fixed when the game ends, so a later fetch does not change what was
  knowable: ``available_at = min(fetched_at, event_bound)``. Causal.
* VERSIONED_DERIVED     -- values produced by a third-party model that can be
  re-run later (MoneyPuck xG). Causal only if captured contemporaneously
  (``fetched_at <= event_bound + capture_grace``) or covered by an explicit
  vintage attestation. Otherwise NON-CAUSAL (``VINTAGE_UNVERIFIED``).
* PUBLISHED_REPORT      -- prices, starter reports, rosters. Needs the source's own
  publication/observation time. A live capture proves existence at ``fetched_at``.
  A backfill without a source timestamp is NON-CAUSAL (``NO_HISTORICAL_TIMESTAMP``).
* SCHEDULE              -- live: ``fetched_at``. Backfill: only through a documented
  override in ``nhl/config/backfill_overrides.json``; otherwise NON-CAUSAL.

A fetch timestamp is never used as evidence that old information was unavailable
(an immutable fact fetched today was still knowable then), and existence in the
database today is never used as evidence that information was available then.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from nhl.config import load
from nhl.timeutil import parse_ts


class CaptureMode(str, Enum):
    LIVE = "LIVE"  # fetched while the information was current
    BACKFILL = "BACKFILL"  # fetched after the fact


class Rule(str, Enum):
    EVENT_FACT = "EVENT_FACT"
    LIVE_CAPTURE = "LIVE_CAPTURE"
    SOURCE_PUBLISHED = "SOURCE_PUBLISHED"
    VINTAGE_CONTEMPORANEOUS = "VINTAGE_CONTEMPORANEOUS"
    VINTAGE_ATTESTED = "VINTAGE_ATTESTED"
    VINTAGE_UNVERIFIED = "VINTAGE_UNVERIFIED"
    WALK_FORWARD_MODEL = "WALK_FORWARD_MODEL"
    BACKFILL_OVERRIDE = "BACKFILL_OVERRIDE"
    NO_HISTORICAL_TIMESTAMP = "NO_HISTORICAL_TIMESTAMP"


@dataclass(frozen=True)
class Provenance:
    source: str
    snapshot_id: str
    fetched_at: datetime
    available_at: datetime
    rule: str
    causal: bool
    source_version: str = ""
    published_at: datetime | None = None  # source's own publication / observation time
    source_event_time: datetime | None = None  # when the underlying event happened
    effective_at: datetime | None = None  # when it takes effect (e.g. roster move)
    non_causal_reason: str = ""
    override_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "fetched_at", parse_ts(self.fetched_at))
        object.__setattr__(self, "available_at", parse_ts(self.available_at))
        for name in ("published_at", "source_event_time", "effective_at"):
            v = getattr(self, name)
            if v is not None:
                object.__setattr__(self, name, parse_ts(v))
        if not self.causal and not self.non_causal_reason:
            raise ValueError("non-causal provenance needs a reason")
        if self.published_at is not None and self.available_at < self.published_at:
            raise ValueError("available_at cannot precede published_at")


class ProvenanceError(ValueError):
    pass


def _declared_mode(mode: CaptureMode, fetched_at: datetime, reference: datetime) -> CaptureMode:
    """A capture declared LIVE but fetched after the reference instant is a backfill."""

    if mode is CaptureMode.LIVE and fetched_at > reference:
        return CaptureMode.BACKFILL
    return mode


def event_fact(source: str, snapshot_id: str, fetched_at: datetime, event_bound: datetime,
               source_version: str = "", source_event_time: datetime | None = None) -> Provenance:
    fetched_at, event_bound = parse_ts(fetched_at), parse_ts(event_bound)
    return Provenance(
        source=source, snapshot_id=snapshot_id, fetched_at=fetched_at,
        available_at=min(fetched_at, event_bound), rule=Rule.EVENT_FACT.value, causal=True,
        source_version=source_version, source_event_time=source_event_time,
    )


def published_report(source: str, snapshot_id: str, fetched_at: datetime, reference: datetime,
                     published_at: datetime | None, mode: CaptureMode, source_version: str = "",
                     effective_at: datetime | None = None) -> Provenance:
    """Prices / starter reports / rosters. ``reference`` = the instant after which the
    report is no longer pregame-relevant (usually puck drop)."""

    fetched_at, reference = parse_ts(fetched_at), parse_ts(reference)
    if published_at is not None:
        published_at = parse_ts(published_at)
        if published_at > fetched_at:
            raise ProvenanceError("published_at after fetched_at: clock or source error")
        return Provenance(source, snapshot_id, fetched_at, published_at, Rule.SOURCE_PUBLISHED.value, True,
                          source_version, published_at=published_at, effective_at=effective_at)
    if _declared_mode(mode, fetched_at, reference) is CaptureMode.LIVE:
        return Provenance(source, snapshot_id, fetched_at, fetched_at, Rule.LIVE_CAPTURE.value, True,
                          source_version, effective_at=effective_at)
    return Provenance(source, snapshot_id, fetched_at, fetched_at, Rule.NO_HISTORICAL_TIMESTAMP.value, False,
                      source_version, effective_at=effective_at,
                      non_causal_reason="backfilled report without a source publication timestamp")


def schedule(source: str, snapshot_id: str, fetched_at: datetime, season: int, puck_drop: datetime,
             mode: CaptureMode, overrides: dict | None = None) -> Provenance:
    fetched_at = parse_ts(fetched_at)
    if _declared_mode(mode, fetched_at, parse_ts(puck_drop)) is CaptureMode.LIVE:
        return Provenance(source, snapshot_id, fetched_at, fetched_at, Rule.LIVE_CAPTURE.value, True)
    overrides = overrides if overrides is not None else load("backfill_overrides")
    ov = overrides.get("schedule", {}).get(str(season))
    if ov:
        return Provenance(source, snapshot_id, fetched_at, parse_ts(ov["available_at"]), Rule.BACKFILL_OVERRIDE.value,
                          True, override_id=ov["override_id"])
    return Provenance(source, snapshot_id, fetched_at, fetched_at, Rule.NO_HISTORICAL_TIMESTAMP.value, False,
                      non_causal_reason=f"no documented schedule backfill override for season {season}")


def versioned_derived(source: str, snapshot_id: str, fetched_at: datetime, event_bound: datetime, season: int,
                      attestations: list[dict], capture_grace: timedelta = timedelta(hours=36)) -> Provenance:
    """Third-party model output (MoneyPuck xG)."""

    fetched_at, event_bound = parse_ts(fetched_at), parse_ts(event_bound)
    if fetched_at <= event_bound + capture_grace:
        return Provenance(source, snapshot_id, fetched_at, fetched_at,
                          Rule.VINTAGE_CONTEMPORANEOUS.value, True, source_version=f"captured:{fetched_at.date()}")
    for att in attestations:
        # The model must have been trained only on seasons before this row's season AND
        # been published before the row became visible.
        if int(att["trained_through_season"]) < season and parse_ts(att["published_at"]) <= event_bound:
            return Provenance(source, snapshot_id, fetched_at, event_bound, Rule.VINTAGE_ATTESTED.value, True,
                              source_version=att["model_id"], override_id=att["attestation_id"])
    return Provenance(source, snapshot_id, fetched_at, event_bound, Rule.VINTAGE_UNVERIFIED.value, False,
                      non_causal_reason="third-party model output backfilled; vintage not established")


def walk_forward_model(source: str, snapshot_id: str, fetched_at: datetime, event_bound: datetime,
                       model_id: str, model_trained_through: int, season: int) -> Provenance:
    """Our own model output. Causal iff the model saw only seasons before ``season``."""

    if model_trained_through >= season:
        raise ProvenanceError(f"model {model_id} trained through {model_trained_through} cannot score season {season}")
    # Inputs are immutable event facts and the model predates the season, so the
    # value was reproducible at event_bound even though we computed it later.
    return Provenance(source, snapshot_id, parse_ts(fetched_at), parse_ts(event_bound),
                      Rule.WALK_FORWARD_MODEL.value, True, source_version=model_id)
