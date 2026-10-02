# Phase 3 external blockers

1. Real odds capture needs an odds-provider account/key and explicit provider-event to NHL-game linkage. The adapter is ready; neither `THE_ODDS_API_KEY` nor `ODDS_API_KEY` was configured.
2. Pregame goalie reporting needs an authorized provider/feed with publication or capture timestamps. NHL boxscore starter truth is postgame and cannot satisfy this requirement.
3. A genuine slate requires real time to pass from pregame captures through puck drop and final settlement. It cannot be recreated retrospectively.

Consequences: no genuine SHADOW_FORWARD artifact, close, CLV row, or graded live slate exists yet. This is not substituted with fixture output.

The 2026-10-02 pregame run proved these blockers operationally: five real games and ten live roster snapshots entered replay, but zero artifacts were emitted. The run logged missing causal goalie state for each game, while odds remained absent. Historical MoneyPuck goalie rows were available physically but correctly invisible to the strict view.
