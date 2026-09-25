# Data-source inventory

| Source | What | Access | Grain / timestamp | Status in repo |
|---|---|---|---|---|
| NHL API `api-web.nhle.com/v1/schedule/{date}` | Schedule, venues, start times | Public, undocumented | Game; `fetched_at` of snapshot | Client + parser + fixture tests |
| NHL API `.../gamecenter/{id}/play-by-play` | Events, `situationCode` strength, goals by period type (REG/OT/SO) | Public, undocumented | Event; result `available_at` = start + 4h | Client + parser (events, results incl. shootout) |
| NHL API `.../gamecenter/{id}/boxscore` | Goalie `starter` flag, TOI, shots/saves | Public, undocumented | Game | Client + parser; wins over MoneyPuck for `started` |
| NHL stats API `api.nhle.com/stats/rest/en/shiftcharts` | Shifts, for line/TOI context | Public, undocumented | Shift | Client + parser (not yet used in features) |
| MoneyPuck team game-by-game CSV | 5on5 / 5on4 / all: xG, goals, SOG, penalties, TOI | Free download (check terms) | Team-game-situation; next day 12:00 UTC | Parser + schema check; **verify `penaltiesFor` semantics** |
| MoneyPuck goalie game-by-game CSV | xGA, GA, shots on goal, TOI | Free download | Goalie-game; next day | Parser (starter inferred by TOI, flagged) |
| MoneyPuck skater season CSV | 5v5 on-ice xGF/xGA for player priors | Free download | Player-season; July 1 after season | Parser |
| Odds feed (vendor TBD) | ML, 3-way, puck line, totals, team totals by book, with limits | **Paid (needed)** | Observation timestamp | Canonical CSV schema, parser, dedupe, PIT view, de-vig, close/open/marks |
| Goalie confirmations (DailyFaceoff, beat reporters, or vendor) | Starter state with report time | Scrape or paid; **check ToS** | Report timestamp | Canonical CSV schema and parser |
| Line combinations / lineups | Lineup state | TBD | Timestamp | Not implemented (`lineup_state=UNKNOWN`) |

## Network note
This build environment's network policy denies `api-web.nhle.com`, `api.nhle.com`, `moneypuck.com` and `peter-tanner.com`. Ingestion is therefore tested against recorded-format fixtures in `tests/fixtures/`, and the end-to-end run uses the synthetic league. To run live ingestion, allow these hosts in the environment's network settings, or run the code on a machine with open access.
