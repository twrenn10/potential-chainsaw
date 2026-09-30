# Odds, no-vig and closing-line methodology

Code: `nhl/contracts/schemas.py::OddsSnapshot`, `nhl/data/odds.py`, `nhl/market/novig.py`, `nhl/market/snapshots.py`, `nhl/ledger/closes.py`, `nhl/market/providers.py`. Config: `nhl/config/market.json`. Values there marked `PROVISIONAL` are operational choices, not established thresholds.

## 1. Market observation contract (provider-neutral, "market-v2")

One row = one book's price for one outcome at one instant (`MARKET_COLUMNS`):

| Field | Meaning / rule |
|---|---|
| `provider` | Provider identity (required) |
| `source_ts` | Provider's own last-change time (optional). Must be ≤ `observed_at` |
| `observed_at` | When the price was observed. This is `snapshot_ts` = `available_at`. Must be ≤ fetch time |
| fetch time | `provenance.fetched_at`, from the raw snapshot store |
| `game_id`, `book` | Canonical NHL id; book lower-cased |
| `market` | `ML`, `REG_3WAY`, `PUCK_LINE`, `TOTAL`, `TEAM_TOTAL`, `GOALIE_SAVES`, `PLAYER_SOG` |
| `period` | `FULL_GAME` / `REGULATION` / `P1`. The default is the market's settlement period |
| `selection` | Must be valid for the market (e.g. no `HOME` on a total) |
| `line` | Required exactly for line markets, on the market's step grid and in range (e.g. a puck line of 0 is rejected) |
| `team` / `participant_id` | Required for `TEAM_TOTAL` / props respectively, forbidden otherwise |
| `price`, `odds_format` | `AMERICAN` (integer, \|p\| ≥ 100) or `DECIMAL` (1.001–1001). **Decimal prices are kept exactly** (`price_decimal`); the American value is display only |
| `status` | `OPEN` / `SUSPENDED` / `CLOSED` |
| `max_stake` | Limit shown, if the feed provides one |

Malformed rows are **rejected with a reason, never repaired**. That covers missing fields, impossible prices or lines, no UTC offset, observation after fetch, `source_ts` after observation, and unknown format or status. Same-instant duplicates with an identical price are counted as harmless dupes. With a different price or status they are ambiguous, and all copies are dropped.

## 2. Structurally distinct markets and keys
- **Outcome key** (`build_market_key`): `game:MARKET[@PERIOD][:TEAM|:P<player>]:SELECTION[:±line]`, e.g. `2026020001:TOTAL:OVER:+6.5`, `2026020001:TOTAL@P1:OVER:+1.5`, `2026020001:GOALIE_SAVES:P8479361:OVER:+27.5`.
- **Market id** (`GroupKey.market_id`): the same without selection. The puck line is keyed by the HOME line.
- ML and REG_3WAY, full game and P1, and different players never share a key.
- Props are accepted, de-vigged and closed, but **not priced** by the engine (research-only).

## 3. Quotes and no-vig
A **group quote** (one book, one market id at `as_of`) exists only if:
1. every outcome of the market has an observation (2 legs, or HOME/DRAW/AWAY for 3-way);
2. each leg's **latest** observation is `OPEN`;
3. legs were observed within `max_leg_skew_minutes` (5) of each other;
4. there are no conflicting same-instant observations of a leg;
5. the no-vig transform accepts it.

Otherwise the group is **rejected** with a reason: `INCOMPLETE`, `SUSPENDED`, `LEGS_NOT_CONTEMPORANEOUS`, `DUPLICATED_SELECTION`, or `MALFORMED`.

`no_vig(selections, decimals, method)` returns a `NoVigResult` holding the decimals, **raw implied probabilities (1/decimal, including margin)**, the overround, the method (+ exponent `k` for `power`), and the no-vig probabilities. The default method is multiplicative (normalise raw implied to 1). The power method is available.

Rejected, not normalised:
- fewer than 2 or more than 3 outcomes;
- duplicated selections;
- decimal ≤ 1;
- **overround < 0** (a book paying back more than 100% is stale or erroneous);
- overround above 15% (2-way) or 25% (3-way), both PROVISIONAL.

Every artifact persists `execution_price`, `execution_decimal`, `raw_implied_probability`, `no_vig_probability`, `novig_method`, `market_overround` and `market_snapshot_ref`. The snapshot reference lists the exact leg observations (time + raw snapshot id), so any no-vig number can be recomputed.

## 4. Closing line: one rule (`close-v1`)
> **The close is the final valid, complete group state observed strictly before puck drop.**

- **Cutoff:** actual puck drop when a source provides it (`GameResult.actual_start`), basis `ACTUAL`. Otherwise scheduled puck drop, basis `SCHEDULED_FALLBACK`. The basis is persisted on every close and every CLV row. The two are never mixed silently.
- The candidate is the group's state at the cutoff: the latest observation per leg before the cutoff. It must pass every quote rule above. **If that final state is suspended, incomplete, non-contemporaneous or malformed, the close is `UNAVAILABLE`.** An earlier valid quote is never substituted.
- **Staleness:** if the oldest leg is more than `max_close_staleness_minutes` (60, PROVISIONAL) before the cutoff, the close is `UNAVAILABLE: STALE`.
- Every selection, available or not, is persisted append-only (`closes.sqlite`, hash-chained). A row records: close id, game, book, market id, rule + version, cutoff, basis, status, reason, provider, staleness, legs, prices, raw implied, no-vig, overround, method and capture time. Re-recording identical content is a no-op; different content for the same close id is refused.
- Evaluation and the CLV report use only these selections. An unavailable close means **no CLV value** for that artifact, and it is counted in `close_unavailable_reasons` and `close_availability`.

## 5. CLV
`clv_ev = execution_decimal × p_close_no_vig − 1`: the EV of the price taken, valued at the no-vig close. Also reported:
- `clv_prob_move` = p_close − p_at_prediction;
- `edge_at_close` = model − p_close.

For integer lines, `p_close` is the two-way no-vig probability. Pushes are not modelled in CLV; this is documented as a simplification.

## 6. Provider adapters and current status
`nhl/market/providers.py` defines `OddsProvider.fetch(day) -> bytes` in the market-v2 contract. `capture_odds` stores the bytes in the raw snapshot store, and `replay` auto-detects v2 vs the legacy v1 CSV. Adding a vendor needs only an adapter that emits market-v2. Nothing in pricing, close selection or evaluation changes.

**Status: no real odds provider or credentials exist.** Everything is exercised with fixture files (`tests/fixtures/provider/`) and synthetic odds. **Genuine CLV validation is externally blocked** until a timestamped live/historical odds source with closing coverage is connected.
