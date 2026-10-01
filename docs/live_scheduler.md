# Live scheduler

`nhl/config/live_scheduler.json` defines morning, pregame, near-close, and postgame cadence. `nhl scheduler-plan` renders the plan from captured schedule state. The scheduler contains no pricing code: deployments invoke the existing capture, replay, price, close, settle, verify, and export commands.

Morning refreshes schedule, rosters, MoneyPuck inputs, odds, goalie reports, then prices. Pregame refreshes increase from hourly to every 15 minutes inside two hours. Pricing still uses `append_if_changed`, so unchanged source fingerprints produce no new artifact. Postgame captures final state/results, selects a non-fabricated close, grades, computes CLV, verifies chains, and exports. Scheduler time never enters the model seed or feature state.

Production deployment (launchd/cron/container scheduling) is intentionally not installed by repository code. It should be enabled only after odds and goalie credentials are configured and a smoke capture succeeds.
