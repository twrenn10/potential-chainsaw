"""NHL Desk: point-in-time NHL pricing engine.

Pipeline (mirrors tournament_v2's super_engine flow, NHL-native internals):
raw snapshots -> point-in-time view -> features -> goalie mixture -> game-state
simulation -> fair prices -> no-vig market -> edge -> governance lane ->
immutable prediction artifact -> ledger/grading/CLV -> desk exports.
"""

__version__ = "0.1.0"
MODEL_VERSION = "nhl-gs-0.1.0"
FEATURE_VERSION = "nhl-feat-0.1.0"
