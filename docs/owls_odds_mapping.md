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
Provider-close reconciliation export is still pending.

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
nhl record-goalie-report --store STORE --game-id ID --team TRI --goalie-id NHL_ID \
  --state CONFIRMED --source SOURCE --published-at UTC_TIMESTAMP
```

`OWLS_INSIGHT_API_KEY` is required and is used only in the Bearer header.

