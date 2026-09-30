# Temporal provenance rules

Every value that can affect a prediction must answer one question: **could this value actually have been known at the prediction `as_of`?**

Each ingested record carries a `Provenance` (`nhl/data/provenance.py`) built by exactly one named rule:

| Field | Meaning |
|---|---|
| `fetched_at` | When *we* retrieved the bytes (raw snapshot manifest) |
| `published_at` | The source's own publication or observation time (odds `snapshot_ts`, report time) |
| `source_event_time` | When the underlying event happened (puck drop, for game facts) |
| `effective_at` | When a change takes effect (roster moves), where known |
| `available_at` | The earliest instant the value was knowable. This is the only field `PointInTimeView` filters on |
| `causal` / `non_causal_reason` | Whether the value may be used in strict walk-forward prediction |
| `source_version`, `override_id` | Model vintage, API version, or the documented override that was applied |

## Rules by information class

| Class | Examples | Rule | `available_at` | Causal? |
|---|---|---|---|---|
| Immutable event fact | final score, play-by-play events, boxscore saves/shots, goalie `starter` flag | `EVENT_FACT` | `min(fetched_at, puck drop + 4h)` | Yes. A later fetch cannot change what was knowable |
| Our walk-forward model output | in-house xG (`nhl/features/xg.py`) | `WALK_FORWARD_MODEL` | puck drop + 4h | Yes, only if the model was trained on seasons strictly before the scored season (enforced; otherwise it raises) |
| Third-party model output | MoneyPuck xG, season summaries | `VINTAGE_CONTEMPORANEOUS` | `fetched_at` | Yes, if captured within 36 h of availability (summaries: before the next season starts) |
| | | `VINTAGE_ATTESTED` | availability bound | Yes, via an entry in `nhl/config/moneypuck_vintages.json` (model trained only through an earlier season, published before use) |
| | | `VINTAGE_UNVERIFIED` | availability bound | **No.** Invisible to strict views |
| Published report | odds, goalie confirmations | `SOURCE_PUBLISHED` | `published_at` | Yes. Rejected if `published_at > fetched_at` (clock/source error) or, for starters, if published at or after puck drop |
| | | `LIVE_CAPTURE` | `fetched_at` | Yes. A live capture before the reference time proves existence |
| | | `NO_HISTORICAL_TIMESTAMP` | `fetched_at` | **No.** Backfilled without a source time |
| Rosters | `/v1/roster/{T}/{S}` | `LIVE_CAPTURE` / `NO_HISTORICAL_TIMESTAMP` | `fetched_at` | Only live captures. A capture after the season ended is forced to backfill (the endpoint shows an end-of-season view) |
| Schedule | `/v1/schedule/{date}` | `LIVE_CAPTURE` | `fetched_at` | Yes |
| | | `BACKFILL_OVERRIDE` | override date | Yes, only via a documented entry in `nhl/config/backfill_overrides.json` |
| | | `NO_HISTORICAL_TIMESTAMP` | `fetched_at` | **No** |

A capture declared `LIVE` but made after the relevant reference instant (usually puck drop) is automatically treated as a backfill.

## The two failure modes this prevents
1. **Today's fetch time used as evidence that old information was unavailable.** An event fact backfilled today keeps its historical `available_at` (`min(fetched_at, event bound)`), so historical backtests are not starved.
2. **Information treated as historically available merely because it exists in the database today.** Anything whose historical value depends on when it was fetched (third-party model output, un-timestamped reports, rosters, backfilled schedules) is non-causal unless a contemporaneous capture, a source timestamp, or a documented override proves otherwise.

## Enforcement
- `PointInTimeView` is **strict by default**: non-causal records are invisible, and `non_causal_hidden` counts them.
- A permissive view (`strict=False`, for diagnostics only) sets `non_causal_used`. Every resulting artifact gets `HARD_BLOCK:NON_CAUSAL_INPUTS` plus `HARD_BLOCK:NON_STRICT_VIEW`.
- Records with no provenance at all (legacy paths) give `HARD_BLOCK:UNATTESTED_PROVENANCE` for any non-synthetic data origin. Undated rosters give `HARD_BLOCK:UNATTESTED_ROSTER`.
- `evaluate` marks every gate `NOT_ELIGIBLE:NON_CAUSAL_OR_UNATTESTED_INPUTS` if any artifact carries one of these blocks.

## Documented overrides
Both registries are **empty** in this commit. Adding an entry requires an `evidence` field. Any change to them should be reviewed like a change to validation gates.
- `nhl/config/backfill_overrides.json`: per-season schedule availability for backfilled schedules. Postponed or rescheduled games within an overridden season remain a residual. Live captures record them as separate versions.
- `nhl/config/moneypuck_vintages.json`: MoneyPuck xG model attestations.
