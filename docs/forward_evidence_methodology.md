# Forward evidence methodology

The primary cohort contains only machine-qualified `SHADOW_FORWARD` artifacts. Cohorts are append-only logical groupings keyed by provider, model version, config hash, and parameter fingerprint; configuration/model changes create a different cohort rather than rewriting history.

Reporting is by market: artifact N, graded N, calibration, Brier/log loss, model probability versus no-vig market probability, CLV, positive-CLV rate, closing-price change, realized ROI, exclusions, and block reasons. Complementary outcomes are not double-counted. Missing closes remain missing. Small samples are descriptive only and cannot prove edge.

Current genuine counts are zero for ML, regulation 3-way, puck line, total, and team total. Props remain research-only. Synthetic fixture drills and historical MoneyPuck rows are excluded.
