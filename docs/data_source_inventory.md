# Data-source inventory

Phase 3 update (2026-10-01 UTC): NHL API and MoneyPuck live capture succeeded. Odds and pregame goalie reports remain externally blocked. Raw live snapshots are kept in the ignored capture store; see `live_source_validation.md`.

| Source | What | Access | Grain / timestamp | Status in repo |
|---|---|---|---|---|
| NHL API `api-web.nhle.com/v1/schedule/{date}` | Schedule, venues, start times | Public, undocumented | Game; `fetched_at` of snapshot | Client + parser + fixture tests |
| NHL API `.../gamecenter/{id}/play-by-play` | Events, `situationCode` strength, goals by period type (REG/OT/SO) | Public, undocumented | Event; result `available_at` = start + 4h | Client + parser (events, results incl. shootout) |
| NHL API `.../gamecenter/{id}/boxscore` | Goalie `starter` flag, TOI, shots/saves | Public, undocumented | Game | Client + parser; wins over MoneyPuck for `started` |
| NHL stats API `api.nhle.com/stats/rest/en/shiftcharts` | Shifts, for line/TOI context | Public, undocumented | Shift | Client + parser (not yet used in features) |
| NHL API `.../roster/{TEAM}/{SEASON}` | Roster snapshot for priors | Public, undocumented | Capture time (live only) | Client + checked parser (Phase 2) |
| In-house xG (`nhl/features/xg.py`) | Walk-forward xG from play-by-play | Derived | puck drop + 4h; model trained on prior seasons | Implemented; causal replacement for backfilled MoneyPuck xG |
| MoneyPuck all-teams game-by-game CSV | 5on5 / 5on4 / all: xG, goals, SOG, penalties, TOI | Listed public download; non-commercial terms + attribution | Team-game-situation; next day 12:00 UTC plus vintage rule | Live-captured and parsed; two impossible zero-TOI rows rejected. `seasonSummary/.../teams.csv` is explicitly not this dataset |
| MoneyPuck goalie game-by-game CSV | xGA, GA, shots on goal, TOI | Free download | Goalie-game; next day | Parser (starter inferred by TOI, flagged) |
| MoneyPuck skater season CSV | 5v5 on-ice xGF/xGA for player priors | Free download | Player-season; July 1 after season | Parser |
| The Odds API / market-v2 feed | ML, 3-way, puck line, totals, team totals and research props by book | Paid credential; none configured | provider `source_ts` ≤ `observed_at` ≤ fetch | Raw+normalized adapter implemented; live capture blocked on API key |
| Goalie confirmations (DailyFaceoff, beat reporters, or vendor) | Starter state (CONFIRMED/EXPECTED/PROBABLE/PROJECTED/SCRATCHED) with publication time and optional confidence | Scrape or paid; **check ToS**; none configured | Publication timestamp | Checked parser, provenance, lineage and conflict handling, provider interface. Fixture-only |
| Line combinations / lineups | Lineup state | TBD | Timestamp | Not implemented (`lineup_state=UNKNOWN`) |

## Validation status (Phase 2)
All parsers have `*_checked` variants that return records, per-record rejections and warnings. Strict wrappers raise if anything was rejected. Validation covers ids and season consistency, explicit-UTC start times, schedule/game state, teams, period/clock rules, `situationCode` sanity, event owners, penalty durations, score reconstruction versus reported score, SOG cross-checks (as warnings), starter uniqueness, saves/shots/GA consistency, strict integers (no `2.7 -> 2` coercion), finite non-negative floats, duplicate and tied rows (treated as ambiguous and dropped), and the timestamp sanity of odds and goalie reports.

**These checks have only been exercised against source-format fixtures. No live payload has been validated. The field names flagged VERIFY must be confirmed against live data.**

## Network note
The earlier 403 network blocker is resolved for `api-web.nhle.com` and listed `moneypuck.com` downloads. Live odds and goalie capture require provider access not present in this environment.
