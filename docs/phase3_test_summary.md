# Phase 3 test and verification summary

- Baseline at `68937800`: 128 passed.
- Phase 3 final suite: 136 passed.
- Live NHL replay: 47 schedule versions, 2 results, 8 goalie-game rows, 0 NHL rejects.
- Live MoneyPuck replay: 11,190 accepted team-game-situation rows; 3 explicit rejects; 11,150 historical rows remain vintage-unverified.
- Captured-live replay: two independent report exports were byte-identical.
- Empty live prediction and close stores: both hash chains verify at the genesis hash. No genuine prediction was fabricated solely to make the chain non-empty.
- Existing suite continues to verify deterministic pricing/export, leakage equivalence, stale-price blocking, simulated-clock exclusion, grading/CLV fixtures, and unrelated-game isolation.
