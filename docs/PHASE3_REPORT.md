# Phase 3 report

## Verdict

**EXTERNALLY BLOCKED.** Live NHL, MoneyPuck and Owls Insight capture now work. The Owls REST schema was verified from real raw captures and a raw-first adapter was added. Its payload and official SDK do not establish per-book NHL overtime/shootout settlement, so markets remain fail-closed pending attestations. No authorized timestamped goalie source is available. A genuine pregame shadow slate, real close, genuine CLV, and end-to-end live grading therefore do not yet exist.

## Live results

- NHL: success; 47 schedule versions, two final games, eight goalie-game rows, zero NHL parser rejects.
- MoneyPuck: success; 126.5 MB game-level snapshot, 11,190 accepted rows, two zero-TOI rejects; 40 contemporaneous rows and 11,150 vintage-unverified historical rows.
- Odds: adapter implemented; no key; `REAL ODDS CAPTURE BLOCKED`.
- Goalie reports: no authorized timestamped pregame source; blocked.
- Genuine SHADOW_FORWARD artifacts/close/CLV/graded slate: zero/not available.
- Phase 2 assumptions: NHL fields held; MoneyPuck `seasonSummary/teams.csv` was incorrectly assumed game-level. Correct endpoint is now explicit and schema mismatch is reported rather than crashing.

## Safeguards and observability

Raw payload precedes parsing. HTTP metadata and hashes are retained. Fetch failures are append-only. Historical MoneyPuck remains non-causal without vintage lineage. ACTIONABLE remains disabled. Desk exports now include source quality, a forward cohort summary, and reprice audit. Scheduler configuration orchestrates existing commands only.

## Baseline and blockers

Local tag `nhl-phase2-baseline` points to `68937800bee10942b41e7ca77f5ff5578fe0f381`. The odds credential/provider-access blocker is resolved. Remaining external blockers are: verified sportsbook settlement conventions, an authorized pregame goalie feed, and the resulting passage of real time through at least one complete slate. The single biggest blocker to future ACTIONABLE promotion remains sustained genuine point-in-time odds/closing-line evidence; predetermined sample gates still apply.

## Verification

Phase 2 baseline: 128 tests passed. Phase 3 adds live-operations tests covering raw-before-parse, failed refresh retention/visibility, stale source reporting, MoneyPuck schema mismatch, provider normalization, forward-cohort exclusion, reprice audit, and scheduler cadence. See the final repository test run and Git history for exact totals and HEAD.

## 2026-10-02 genuine pregame attempt

At 19:10 UTC, before the five-game slate, the production path captured the live schedule and 246 roster slots for NYR–DET, WSH–CAR, BOS–WPG, STL–DAL, and ANA–VGK. MoneyPuck's listed 2025 goalie game archive was then stored untouched, normalized, and replayed: 2,766 rows, zero rejects. Replay had no odds observations or pregame goalie reports. All 2,766 backfilled goalie rows correctly remained `VINTAGE_UNVERIFIED` and invisible to the strict view.

Two real-clock pricing attempts produced zero artifacts and explicit errors for unavailable causal goalie start history. No missing input was repaired retrospectively. This is a genuine blocked live attempt, not a shadow-forward slate. It also exposed and fixed a capture/replay plumbing defect: the existing goalie-game parser was not reachable from the raw replay path.

On 2026-10-02 the Owls adapter captured live odds, moneyline, spreads, totals,
realtime, schedule, results, props, line history and provider-reported closes.
The real CLI capture stored an append-only event map and emitted an empty
market-v2 snapshot because every book/market lacked a settlement attestation.
A subsequent real-clock pricing run again emitted zero artifacts: all five games
still lacked causal goalie reports. This is the intended fail-closed result.
