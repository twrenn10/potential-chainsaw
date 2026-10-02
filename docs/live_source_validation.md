# Live source validation

Validation date: 2026-10-01 UTC. Raw snapshots remain in the ignored local `phase3_live/store` capture store.

## NHL API

`api-web.nhle.com` succeeded through `NHLApiClient`: schedule `2026-09-30`, plus PBP and boxscores for games `2026020006` and `2026020008`. The schedule snapshot was stored with URL, HTTP status, selected response headers, fetch time, byte count, and SHA-256 before parsing. Replay produced 47 schedule versions, two final results, and eight goalie-game rows with zero NHL parser rejects.

Observed semantics matched the parser for game id, eight-digit season, UTC start, game/game-schedule state, team ids/abbreviations, period descriptors, clocks, score, shootout/event representation, shots, situation code, goalie id, boxscore starter flag, and event ids. The API did not expose a trustworthy scheduled-record publication time for three already-started games in the week response; those remain `NO_HISTORICAL_TIMESTAMP` and non-causal. Actual puck drop is not present in the validated schedule/PBP payloads, so scheduled cutoff remains the explicitly labelled fallback.

Classification: no NHL parser defect; one unavailable field (actual puck drop); public API remains undocumented and therefore operationally unstable.

## MoneyPuck

Live access succeeded. The first listed `seasonSummary/2025/regular/teams.csv` capture proved that the file is a team-season/situation aggregate, not the game-level dataset assumed by the Phase 2 parser. This is a source semantic mismatch and source-path defect. The production client now distinguishes summary datasets and uses MoneyPuck's listed `careers/gameByGame/all_teams.csv` for team-game ingestion.

The untouched game-level file was captured (126,510,330 bytes; SHA-256 `2d24552722de2185b6dc456d5ace03ef9eb9475ea6d700add47bb747147bf5e9`). Checked replay retained 11,190 team-game-situation records. Two 5-on-5 rows with zero ice time were rejected, not repaired. Forty current rows were contemporaneous; 11,150 historical rows remain `VINTAGE_UNVERIFIED` and excluded from strict walk-forward. The files expose no reliable model-version/publication lineage, so the vintage rule is unchanged.

MoneyPuck's public download page permits listed downloads for non-commercial/journalistic use with attribution; it warns against unapproved scraping. This implementation downloads only explicitly listed files and does not crawl the site.

On 2026-10-02, the explicitly listed 2025 goalie game-by-game ZIP was captured before parsing. The untouched archive is stored under `moneypuck_raw`; a deterministic combined CSV is separately stored with a `derived_from` link and archive hash. Replay accepted 2,766 goalie-game rows with zero rejects. Because the capture occurred after the historical publication window and MoneyPuck exposes no model-version lineage, all rows remain `VINTAGE_UNVERIFIED` and blocked from strict live pricing.
