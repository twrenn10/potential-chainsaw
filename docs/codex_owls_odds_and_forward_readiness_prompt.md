# Codex prompt: Owls Insight odds provider + remaining forward-readiness blockers

## Context
Repository `twrenn10/potential-chainsaw`, branch `claude/nhl-pricing-engine-e9tmet`. Build on the Phase 3 commit `b84f1725e20f17089c78308e184438e7af4eac09`, which is local and not yet pushed.

Phase 3 state:
- 137 tests pass.
- A live Oct 2 schedule, 246 roster slots and 2,766 MoneyPuck goalie-game rows (VINTAGE_UNVERIFIED) have been captured.
- Two real-clock pricing attempts failed closed, because there was no causal goalie state and no real odds.

Read `AGENTS.md` first. Every rule in it is binding. Then read:
- `docs/odds_and_close_methodology.md`
- `docs/goalie_information_lineage.md`
- `docs/forward_testing_protocol.md`
- `docs/governance_gates.md`
- `docs/temporal_provenance.md`

The odds provider is **Owls Insight** (`https://api.owlsinsight.com`). Known from its official SDKs (`owls-insight` on PyPI, `github.com/owlsinsight/owls-insight-sdk-go`):
- API key from env `OWLS_INSIGHT_API_KEY`.
- `GET /api/v1/{sport}/odds` (also moneyline/spreads/totals variants), `GET /api/v1/{sport}/realtime`.
- `GET /api/odds/history` (paginated, `offset`/`limit` ≤ 500), a closing-odds call, props history.
- `/api/v1/{sport}/schedule` and results calls, WebSocket stream, webhooks.
- Odds data: `data` maps book → events (`eventId`). Markets have `book`, `market` (h2h/spreads/totals/…), `outcomes` with `price`, optional `point`, and a capture `timestamp`. Every field is optional.
- Rate limits: `429` with `Retry-After`, `X-RateLimit-Remaining-Month`. MVP plan: 3 concurrent history requests. WebSocket connections are IP-throttled.

**These details come from SDK docs, not from a payload.** Verify every field name, unit, price format and timestamp semantic against a real captured response before writing the parser. Do not guess.

## Hard rules (do not violate)
- Never commit, log or print the API key. Read it only from `OWLS_INSIGHT_API_KEY`. Fail closed with a clear error if it is missing.
- Raw-first: every HTTP response body is written to `RawSnapshotStore` (source `odds`, key `owls/<endpoint>/<params>`, meta = url without key, status, rate-limit headers) **before** parsing.
- Parse into the existing **market-v2 contract** (`nhl/data/odds.py::MARKET_COLUMNS` / `OddsSnapshot`). Do not change pricing, close selection, evaluation, governance or gate code to fit the provider. The adapter adapts to the contract.
- Use `*_checked` style parsing: reject with a reason, never coerce. Unknown market, missing price, impossible line, ambiguous event mapping, or a timestamp without UTC all mean reject.
- Temporal provenance:
  - `observed_at` = the provider's per-price capture `timestamp`, if the payload has one. Otherwise the fetch time, valid for live capture only.
  - `source_ts` only if the provider states a last-change time.
  - Historical rows need a provider observation timestamp. Without one they are `NO_HISTORICAL_TIMESTAMP` (non-causal).
- Do not tune the model. Do not edit `validation_gates.json` or `governance.json`. Do not weaken or delete tests.
- Do not fabricate data, fixtures that pretend to be real responses, closes or CLV. Sanitised real responses are fine as fixtures. Remove the key, and record the capture date in the fixture filename.
- Prices must stay a function of information (see AGENTS.md); reprices go through `append_if_changed`.
- Commit in small, tested steps (`python3 -m pytest -q` before each commit). Do not push or open a PR unless the user asks.

## Tasks

### 1. Owls adapter (`nhl/market/owls.py`)
- Implement the `OddsProvider` protocol (`nhl/market/providers.py`), with an injectable HTTP transport for tests. Handle 429 by honouring `Retry-After` with bounded retries, and record the rate-limit headers in the snapshot meta. Fail closed on 401/403.
- **First capture one real NHL response per endpoint you will use** (odds, the market-specific variants, history, closing, schedule/results, props if the plan includes them). Store them raw, inspect them, and write the parser against what you observe. Document the observed schema in `docs/owls_odds_mapping.md`.
- Market mapping. Map only what you can verify; reject the rest with a reason.
  - `h2h` → `ML`. Confirm it is full game including OT/SO.
  - `spreads` → `PUCK_LINE` only if confirmed full-game ±1.5-style hockey lines. HOME/AWAY lines must be opposite.
  - `totals` → `TOTAL`, confirming full-game-with-OT settlement.
  - Regulation 3-way → `REG_3WAY` if offered.
  - Team totals → `TEAM_TOTAL`.
  - Goalie saves / player shots props → `GOALIE_SAVES` / `PLAYER_SOG`, with a mapped NHL `participant_id`. Reject a prop if the player cannot be mapped unambiguously.
  - Period markets → the correct `Period` value. Never collapse periods into full game.
  - Record per-book settlement conventions in `docs/owls_odds_mapping.md`. If a book's OT/shootout rule for totals or puck line cannot be established, keep that market for that book `UNVERIFIED_SETTLEMENT`, excluded from artifacts (add a reason code; do not silently price).
- Book identity: normalise to stable lower-case ids. Keep the provider's raw book name in meta. Exchanges and prediction markets are separate `book`s; never mix them into sportsbook consensus.
- Event mapping: Owls `eventId` → NHL `game_id` via the captured NHL schedule (teams + start time within a tolerance). An ambiguous or no match is rejected, never guessed. Persist the mapping table as an append-only snapshot so replays are deterministic.
- `price` format: detect American vs decimal from the documented/observed format, and keep the exact original value (decimal in `price_decimal`). `nil` price is a rejected leg, not a zero. Map provider suspension/removal flags to `market_status` only if the payload actually has them.

### 2. Capture commands
- `nhl capture-odds --provider owls --date D --store S`: live pre-game snapshots.
- `nhl capture-odds-history --provider owls --date D --store S`: historical backfill via `/api/odds/history` with pagination and the 3-concurrency limit. Rows keep the provider observation timestamps.
- Optional `nhl stream-odds` (WebSocket) writing every update raw-first. One stream per key, with backoff on throttle.
- Replay must auto-detect the Owls raw snapshots and produce identical records on re-run (add a determinism test).

### 3. Closing line
Our close remains **close-v1**, computed from captured observations (`select_closes`). Owls' closing-odds endpoint may be captured and stored as **provider-reported close** for reconciliation only:
- report the disagreement between provider close and close-v1 in a new `CLOSE_RECONCILIATION.csv` export;
- never use it to fill an UNAVAILABLE close-v1.

### 4. Actual puck drop
Add `actual_start` (R7 in `docs/leakage_audit.md`) from a verified source: NHL play-by-play/landing fields, or Owls results, if one demonstrably carries actual drop time. If none does, keep `SCHEDULED_FALLBACK` and document why.

### 5. Goalie-report feed (other immediate blocker)
- Check whether Owls exposes starting goalies/lineups. If it does, implement it as a `GoalieReportProvider` producing the existing goalie CSV contract, using provider publication timestamps.
- If it does not, do **not** scrape sites without authorization. Instead add `nhl record-goalie-report`: an operator-entry command that appends a timestamped report (source, goalie id, state, `published_at` as stated by the source, entry time). It goes raw-first into `goalie_reports` with `CaptureMode.LIVE` provenance. List authorised feed options in the doc for the user to choose.

### 6. Operations
- Add `deploy/cron.example` (or a systemd timer example) implementing the schedule in `docs/forward_testing_protocol.md`: capture, price cycles every 30–60 min until last puck drop, close, ingest results, grade, CLV, verify, export.
- Document the required network allowlist: `api.owlsinsight.com`, the WebSocket host, `api-web.nhle.com`, `api.nhle.com`, `moneypuck.com`.

### 7. Tests (fixture-based; real sanitised responses once captured)
At minimum:
1. Market mapping per market, plus rejection of unknown and unverified-settlement markets.
2. Price format and nil price.
3. Event mapping: exact, ambiguous and missing.
4. Timestamp provenance: live vs history vs missing timestamp.
5. 429/Retry-After handling and 401 fail-closed.
6. Raw-first ordering: the snapshot exists even if parsing fails.
7. Deterministic replay.
8. The provider close is never used to fill close-v1.
9. A full real-clock `forward-cycle` on recorded Owls + NHL fixtures, through `ReplayStateSource`, ending in verified chains and a deterministic export.
10. The API key never appears in any stored snapshot, log or exception.

All existing tests must stay green.

### 8. Then run it for real
With the key set and the hosts allowed:
1. Capture today's schedule, rosters, goalie reports and Owls odds.
2. Run `nhl forward-cycle` repeatedly until puck drop, then `capture-close`.
3. The next day run `ingest-results`, `grade`, `clv`, `verify`, `evaluate-gates` and `export-desk`.

Only artifacts with `evidence_lane = SHADOW_FORWARD` count. If anything fails closed, report exactly why. Do not work around it.

## Deliverables
- Code + tests + `docs/owls_odds_mapping.md`.
- Updated `docs/data_source_inventory.md`, `docs/odds_and_close_methodology.md` and `docs/PHASE2_REPORT.md` blockers (or a new `docs/PHASE3_REPORT.md`).
- A final summary stating:
  - which Owls endpoints and fields were verified against real payloads;
  - which markets and books are mapped vs rejected;
  - whether a real SHADOW_FORWARD artifact, a real close-v1 and a real CLV value now exist;
  - what still blocks gate evaluation.
