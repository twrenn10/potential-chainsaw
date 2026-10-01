# Live outage behavior

Every successful response is content-addressed and append-only. Every failed attempt is appended to `raw/fetch_failures.jsonl` with source, safe key, attempt time, error class, HTTP status, retryability, and sanitized detail. Failures never delete or replace valid prior snapshots and are not treated as no-change refreshes.

HTTP 4xx/5xx, timeout/transport errors, malformed JSON, schema mismatch, partial rows, stale sources, duplicate/conflicting observations, reschedules, cancellations, and missing odds/goalie data are surfaced in logs, ingest anomalies, `DATA_FRESHNESS.csv`, or `LIVE_SOURCE_QUALITY.csv`. Stale odds hard-block governance. Missing odds cannot produce a price or close. Missing goalie confirmation stays a mixture and cannot be promoted. Scheduled cutoff is labelled when actual start is unavailable.

Retryable conditions are 429, 5xx, and transport/timeouts. Authentication/permission 4xx requires operator action. Logs must never contain API keys.
