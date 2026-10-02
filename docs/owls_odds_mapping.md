# Owls Insight NHL mapping

Verified against raw responses captured 2026-10-02 UTC from `api.owlsinsight.com`.
The ignored local capture store holds immutable response bodies and hashes; no API
credential is stored in metadata or payloads.

## Observed endpoints and wire fields

| Endpoint | Observed result |
|---|---|
| `/api/v1/nhl/odds`, `/moneyline`, `/spreads`, `/totals` | `data` is book → event list. Event identity is `eventId`; teams and `commence_time` are explicit. A bookmaker has `key`, `last_update`, and markets. Markets have `key`, `last_update`, outcomes with American `price` and optional numeric `point`, plus optional `maxRiskStake` limits. |
| `/api/v1/nhl/realtime` | Event list with Pinnacle markets, millisecond epoch `lastUpdate`/`seenAt`, phase/freshness, main and period/special markets. It is not used by the adapter yet. |
| `/api/odds/history` | One requested event/book/market/side. `history[]` has UTC `timestamp`, American `price`, and `point` when applicable. The observed response declares main-line-only quality. |
| `/api/v1/history/closing-odds` | Paginated `data.odds`; rows have event, book, source, teams, game date, markets, and epoch-seconds `lastUpdate`. These are reconciliation evidence only. |
| `/api/v1/nhl/schedule`, `/results` | Event identity, scheduled `startTime`, state, teams and scores. No actual puck-drop timestamp was observed; scheduled fallback remains in force. |
| `/api/v1/nhl/props` | Game → book → props. `shots_on_goal` and `saves` rows have player name, line, American over/under prices and UTC `lastUpdate`. No NHL player id is present, so props remain rejected until roster-name linkage is unique. |

The live capture contained Pinnacle, FanDuel, DraftKings, Novig, BetMGM,
Hard Rock, Betr, Bovada, Circa, Stations, South Point and Wynn. Novig is marked
as an exchange by Owls metadata and must remain a distinct book.

## Enabled mapping and fail-closed rules

`h2h → ML`, `spreads → PUCK_LINE`, and `totals → TOTAL` are structurally
implemented. They are emitted only when `(book, provider market)` appears in an
operator-supplied settlement attestation JSON. The observed API and SDK do not
state whether each book includes overtime/shootout or how a shootout goal settles
spread/total markets. Therefore no book/market pair is enabled by default and the
reason is `UNVERIFIED_SETTLEMENT`.

Unknown markets, missing/nonnumeric prices, invalid American prices, missing
lines, non-opposite puck-line pairs, unknown selections, invalid timestamps,
unmapped events and ambiguous events are rejected. Event matching requires exact
canonical home/away teams and start time within 15 minutes of the captured NHL
schedule. The resulting mapping is stored append-only as `owls_event_map`.

For live boards, `observed_at` is the HTTP capture time because no per-outcome
observation time is present. Market `last_update` becomes `source_ts`. History
uses each row's `timestamp` as `observed_at`; a row without one is
`NO_HISTORICAL_TIMESTAMP`.

Owls closing rows never enter `select_closes`; close-v1 remains authoritative.

## Settlement attestation registry

`nhl/config/settlement_attestations.json` is intentionally empty. Registry keys
are exact `(provider, sportsbook, league, market_type)` tuples. Each record carries
the settlement scope, explicit overtime/shootout booleans, evidence type and
reference, attestor, attestation time, status, notes and optional expiration.
Supported statuses are `VERIFIED`, `UNVERIFIED`, `CONFLICTING`, and `EXPIRED`.
Absence, conflict, expiry, future-dated evidence, and a scope other than
`FULL_GAME_OT_SO` all fail closed. A verified ML attestation does not cover totals,
another sportsbook, or another league. Attestations affect settlement
compatibility only; they cannot satisfy goalie causality or any other pricing gate.

## Historical capture and pagination

`capture-history` retrieves archived games, per-event odds snapshots,
provider-reported closes, and the available NHL results response. Offset advances
explicitly by the requested page size (maximum 500). Identical repeated pages,
the global page cap, and the global record cap stop retrieval. Records are
deduplicated by endpoint-specific stable identities and sorted by those identities.
Every response, including errors and retries, is stored before inspection.

The manifest records pages requested/completed, counts, deduplication, raw artifact
lineage, errors, and one of `COMPLETE`, `EMPTY_VALID`, `PARTIAL_RATE_LIMIT`,
`PARTIAL_PROVIDER_ERROR`, `PARTIAL_PAGINATION_GUARD`, `AUTH_FAILURE`, or
`INVALID_RESPONSE`. Empty success is distinct from failure. A bounded real capture
on 2026-09-30 correctly reported `PARTIAL_PAGINATION_GUARD` rather than pretending
the capped retrieval was complete.

Historical records are labelled `POST_HOC_ANALYSIS_ONLY` and
`eligible_for_model_input=false`. Their provider timestamps describe when Owls
recorded a price; the current fetch does not prove that this repository knew it at
decision time. Historical manifests are stored separately from replayable `odds`,
preventing hindsight data from entering a point-in-time model view.

## Provider-close reconciliation

`provider_close_reconciliation_v1` compares available close-v1 legs with Owls
provider closes by mapped event, sportsbook, market and selection. It preserves
both source artifacts and timestamps and reports exact, price, line, combined,
missing, unmatched, non-comparable, and unverified-settlement states. Output order
and run ids are deterministic for identical inputs. Summary metrics cover events,
comparable legs, exact matches, mismatches, missing sides, and unmatched keys.

The export is audit-only. **Provider closing odds and internally valid close-v1
are different concepts.** A provider close never fills an unavailable close-v1,
never writes the close ledger, and never creates CLV. Real validation against the
2026-09-25–2026-10-02 capture produced 600 `EVENT_UNMATCHED` legs and zero
comparable markets, accurately reflecting that those provider events lacked a
persisted NHL event mapping in the current captured slate.

## Allowed and prohibited influence

Owls raw/history/closing data may support provider diagnostics, coverage audits,
settlement-attested live market observations, and reconciliation. It may not
automatically influence model training, pricing, close-v1, CLV, grading, promotion,
or betting eligibility. Those paths still require ordinary causal provenance,
complete market groups, verified settlement, causal goalie reports, and every
existing governance gate.

## Goalie and actual-start findings

The inspected surfaces contain goalie saves props but no starting-goalie or
lineup publication. Use `nhl record-goalie-report` for an authorized human-read
source, preserving its stated publication time and the entry time. Potential
licensed alternatives requiring user selection/agreement include Sportradar,
Stats Perform, SportsDataIO and an authorized team/league feed. No unauthorized
site scraping is implemented.

The schedule/results response carries scheduled start only, not verified actual
puck drop. `SCHEDULED_FALLBACK` therefore remains correctly labelled.

## Commands

```text
nhl capture-odds --provider owls --date YYYY-MM-DD --store STORE [--settlement-file attestations.json]
nhl capture-odds-history --provider owls --date YYYY-MM-DD --store STORE
nhl capture-history --provider owls --league NHL --start-date YYYY-MM-DD \
  --end-date YYYY-MM-DD --store STORE --max-pages 100 --max-records 50000
nhl reconcile-provider-close --provider owls --store STORE \
  --forward-root FORWARD --out REPORTS [--settlement-file attestations.json]
nhl record-goalie-report --store STORE --game-id ID --team TRI --goalie-id NHL_ID \
  --state CONFIRMED --source SOURCE --published-at UTC_TIMESTAMP
```

`OWLS_INSIGHT_API_KEY` is required and is used only in the Bearer header.
