import json
import math

import numpy as np
import pytest

from nhl.data import nhl_api
from nhl.data.provenance import ProvenanceError
from nhl.features.xg import FEATURES, fit_xg, score_events, team_game_stats_from_pbp
from nhl.timeutil import parse_ts


def synth_events(season: int, n_games: int, seed: int, goal_boost: float = 0.0) -> list[dict]:
    """Shot events whose goal probability falls with distance (known generating process)."""

    rng = np.random.default_rng(seed)
    out = []
    for g in range(n_games):
        gid = f"{season}02{g + 1:04d}"
        t = 0
        for k in range(60):
            t += int(rng.integers(20, 90))
            x = float(rng.uniform(30, 88)) * (1 if rng.random() < 0.5 else -1)
            y = float(rng.uniform(-35, 35))
            dist = math.hypot(89 - abs(x), y)
            p = 1 / (1 + math.exp(-(0.5 - 0.12 * dist + goal_boost)))
            is_goal = rng.random() < p
            out.append({"game_id": gid, "event_id": k, "period": 1 + min(t // 1200, 2), "period_type": "REG",
                        "game_seconds": min(t, 3599), "event_type": "goal" if is_goal else "shot-on-goal",
                        "team": "TOR" if k % 2 else "MTL", "is_home": bool(k % 2), "strength": "5v5",
                        "situation_code": "1551", "x": x, "y": y, "shot_type": "wrist"})
    return out


def test_xg_fits_only_prior_seasons_and_recovers_distance_effect():
    hist = synth_events(2022, 150, 1) + synth_events(2023, 150, 2)
    target = synth_events(2024, 20, 3, goal_boost=5.0)  # absurd future data
    m1 = fit_xg(hist, 2024)
    m2 = fit_xg(hist + target, 2024)
    assert m1 == m2 and m1.train_seasons == (2022, 2023) and m1.trained_through == 2023
    assert m1.coef[FEATURES.index("distance")] < 0
    scored = score_events(m1, target, 2024)
    assert len(scored) == len(target) and all(0 < p < 1 for _, p in scored)
    with pytest.raises(ProvenanceError):
        score_events(m1, hist, 2023)
    with pytest.raises(ValueError):
        fit_xg(target, 2022)


def test_team_stats_from_fixture_pbp_are_causal(fixtures):
    payload = json.loads((fixtures / "nhl_pbp_shootout.json").read_text())
    events = nhl_api.parse_pbp_events(payload)
    model = fit_xg(synth_events(2024, 80, 5) + synth_events(2025, 80, 6), 2026)
    rows = team_game_stats_from_pbp(model, events, parse_ts("2026-10-07T23:00:00Z"), "TOR", "MTL", "nhl_api:x",
                                    parse_ts("2027-06-01T00:00:00Z"))
    assert rows and all(r.provenance.causal and r.provenance.rule == "WALK_FORWARD_MODEL" for r in rows)
    assert all(r.available_at == parse_ts("2026-10-08T03:00:00Z") for r in rows)
    allrow = next(r for r in rows if r.team == "TOR" and r.situation == "all")
    assert allrow.goals_for == 2 and allrow.penalties_against == 1  # MTL took the penalty
    assert allrow.source.startswith("nhl_xg:nhlxg-")
