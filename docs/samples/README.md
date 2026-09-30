# Samples (SYNTHETIC / DEV lane only)

Generated from code at commit `244132f` (no code changed after it except docs), configuration fingerprint
`all_config_hash() = 4e7753a32ab2` (includes backfill_overrides and moneypuck_vintages registries).

* `backtest/`: `nhl backtest --synthetic --out out/demo --overwrite`. Walk-forward 2025-26 synthetic season,
  15444 artifacts, chain verified=True, closes chain verified=True,
  leakage audit ['EQUIVALENT', 'EQUIVALENT'], gate_version 2026.09.30-v2.
* `forward_drill/`: `nhl forward-drill --synthetic --out out/drill --overwrite`. Simulated-clock shadow-forward lifecycle
  for 2026-01-20 (morning baseline, clock-only rerun, market move, goalie confirmations, late scratch, close, settle,
  export x2 byte-identical). Artifact config hashes: ['bcb139690e46']; constants: ['LC:67c7cb02a1bcdc94'];
  constants still at DEFAULT: ["pull_trailing_by_1_seconds", "pull_trailing_by_2_seconds", "rate_4v4_mult", "rate_6v5_attack", "rate_empty_net", "rate_pp", "rate_sh", "score_effect_beta", "score_effect_beta_p3"].
  Files: CURRENT_SLATE.csv (current slate board), PREDICTION_HISTORY.csv, CLV_REPORT.csv, GRADING_REPORT.csv,
  GOVERNANCE_REPORT.csv (includes a logged DEMOTE override and a refused PROMOTE_REQUEST), DATA_FRESHNESS.csv,
  REPORTS_manifest.json, cycles.jsonl.

Every row here is DEV_SYNTHETIC and BLOCKED. Nothing here is real-data or forward evidence.
