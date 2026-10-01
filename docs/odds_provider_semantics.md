# Odds provider semantics

The provider-neutral `market-v2` observation contract remains authoritative. A The Odds API v4 adapter is implemented behind it. It requires `THE_ODDS_API_KEY` or `ODDS_API_KEY`; no credential was present on this host.

The adapter stores untouched vendor JSON under `odds_provider_raw` before producing a separate normalized `odds` snapshot. Secrets and credential-bearing URLs are never stored or logged. Provider event linkage is an explicit provider-event-id to NHL-game-id map; unmatched events and unsupported markets are rejected. Book identity, provider/source timestamps, observation/fetch timestamps, market, period, selection, line, original decimal price, status, and provider id are retained.

Supported mappings are h2h→moneyline, h2h 3-way→regulation 3-way, spreads→puck line, totals→full-game total, team totals, player shots, and goalie saves. Props remain research-only. The provider's snapshot endpoint returns currently offered markets; absence in a later capture is not silently converted to a close or cancellation. Suspension, reopen, removal, correction, reschedule, duplicate, and same-line price-move semantics require real credentials and repeated live captures.

No API key or repository integration was found. Verdict: **REAL ODDS CAPTURE BLOCKED**. No prices, closing lines, or CLV were fabricated.
