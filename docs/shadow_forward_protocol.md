# Shadow-forward protocol

The normal live lane is `SHADOW_FORWARD`; `ACTIONABLE` remains disabled. Every cycle must capture raw sources, replay strict point-in-time state, and price before scheduled puck drop using the real wall clock. Missing inputs are recorded and never backfilled into the evidence cohort.

`validate_forward_artifact` requires FORWARD mode, LIVE origin, SHADOW_FORWARD evidence, created/as-of before cutoff, a real market observation reference, no hard blocks, and evaluation eligibility. Synthetic, fixture, simulated-clock, historical-research, and degraded-forward rows are excluded. `FORWARD_EVIDENCE.json` preserves cohort identity by provider, model, config, and parameter fingerprints. `FIRST_SLATE_AUDIT.json` records reprice counts, causal fingerprint changes, and unexplained reprices.

The first genuine slate additionally requires real odds and a usable pregame goalie state or an explicitly recorded absence. Because no real odds feed is connected, no genuine Phase 3 artifact was created in this run.
