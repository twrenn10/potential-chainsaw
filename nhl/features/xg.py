"""Walk-forward expected-goals model on NHL API play-by-play (the causal xG source).

Why: backfilled MoneyPuck xG is NON-CAUSAL unless its vintage is attested
(``nhl.data.provenance.versioned_derived``). This model is fit only on seasons
strictly before the season it scores, from immutable event facts, so its output
is causal by construction (``provenance.walk_forward_model``).

Model: ridge-regularised logistic regression on unblocked attempts (shot-on-goal,
missed-shot, goal; shootout excluded) with distance, angle, shot type, strength
state and a rebound flag. Coordinates: the attacked net is taken as the one on the
same side as the shot (x = +/-89). VERIFY against rink-side conventions of the live
feed before relying on it.

``team_game_stats_from_pbp`` turns scored events into ``TeamGameStats`` rows
(5on5 / 5on4 / all) so ratings can be fit without MoneyPuck.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

import numpy as np

from nhl.contracts import TeamGameStats, stable_digest
from nhl.data import provenance as prov
from nhl.data.nhl_api import RESULT_LAG, parse_situation_code

UNBLOCKED = {"shot-on-goal", "missed-shot", "goal"}
SHOT_TYPES = ("wrist", "snap", "slap", "backhand", "tip-in", "deflected", "wrap-around")
FEATURES = ("intercept", "distance", "distance_sq", "angle", *(f"type_{t}" for t in SHOT_TYPES),
            "pp", "sh", "empty_net", "rebound")


@dataclass(frozen=True)
class XGModel:
    model_id: str
    trained_through: int  # last season in the training set
    train_seasons: tuple[int, ...]
    coef: tuple[float, ...]
    n_train: int

    def predict(self, X: np.ndarray) -> np.ndarray:
        z = np.clip(X @ np.asarray(self.coef), -30, 30)
        return 1.0 / (1.0 + np.exp(-z))


def _features(ev: dict, prev: dict | None) -> list[float] | None:
    x, y = ev.get("x"), ev.get("y")
    if x is None or y is None:
        return None
    dx = 89.0 - abs(float(x))
    dist = math.hypot(dx, float(y))
    angle = math.degrees(math.atan2(abs(float(y)), max(dx, 0.1)))
    stype = (ev.get("shot_type") or "").lower()
    strength = ev.get("strength", "")
    rebound = bool(prev and prev["team"] == ev["team"] and 0 <= ev["game_seconds"] - prev["game_seconds"] <= 3)
    return [1.0, dist / 10.0, (dist / 10.0) ** 2, angle / 45.0, *(1.0 if stype == t else 0.0 for t in SHOT_TYPES),
            1.0 if strength == "PP" else 0.0, 1.0 if strength == "SH" else 0.0,
            1.0 if strength == "EN_FOR" else 0.0, 1.0 if rebound else 0.0]


def shot_rows(events: list[dict]) -> tuple[list[dict], np.ndarray, np.ndarray, int]:
    """Unblocked non-shootout attempts with coordinates -> (meta, X, y, n_missing_coords)."""

    meta, X, y = [], [], []
    missing = 0
    by_game: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        by_game[e["game_id"]].append(e)
    for gid in sorted(by_game):
        prev = None
        for e in sorted(by_game[gid], key=lambda r: (r["game_seconds"], str(r["event_id"]))):
            if e["event_type"] not in UNBLOCKED or e["period_type"] == "SO":
                continue
            f = _features(e, prev)
            prev = e
            if f is None:
                missing += 1
                continue
            meta.append(e)
            X.append(f)
            y.append(1.0 if e["event_type"] == "goal" else 0.0)
    return meta, np.asarray(X, dtype=float).reshape(-1, len(FEATURES)), np.asarray(y, dtype=float), missing


def fit_xg(events: list[dict], target_season: int, ridge: float = 1.0, iters: int = 50) -> XGModel:
    """Fit on seasons strictly before ``target_season`` only."""

    train = [e for e in events if int(e["game_id"][:4]) < target_season]
    seasons = tuple(sorted({int(e["game_id"][:4]) for e in train}))
    if not seasons:
        raise ValueError(f"no completed seasons before {target_season} to train xG")
    _, X, y, _ = shot_rows(train)
    beta = np.zeros(X.shape[1])
    pen = np.full(X.shape[1], ridge)
    pen[0] = 1e-6
    for _ in range(iters):
        z = np.clip(X @ beta, -30, 30)
        mu = 1.0 / (1.0 + np.exp(-z))
        grad = X.T @ (mu - y) + pen * beta
        H = (X * (mu * (1 - mu))[:, None]).T @ X + np.diag(pen)
        step = np.linalg.solve(H, grad)
        beta -= step
        if np.max(np.abs(step)) < 1e-9:
            break
    coef = tuple(round(float(b), 10) for b in beta)
    model_id = "nhlxg-" + stable_digest([",".join(map(str, seasons)), ",".join(f"{c:.10f}" for c in coef)], n=12)
    return XGModel(model_id=model_id, trained_through=seasons[-1], train_seasons=seasons, coef=coef, n_train=len(y))


def score_events(model: XGModel, events: list[dict], season: int) -> list[tuple[dict, float]]:
    if model.trained_through >= season:
        raise prov.ProvenanceError(f"{model.model_id} trained through {model.trained_through}; cannot score {season}")
    meta, X, _, _ = shot_rows([e for e in events if int(e["game_id"][:4]) == season])
    if not meta:
        return []
    return list(zip(meta, model.predict(X).tolist()))


def _situation_seconds(events: list[dict]) -> dict[tuple[bool, str], float]:
    """Seconds per (is_home, '5on5'|'5on4') from situationCode changes between events."""

    evs = sorted((e for e in events if e["period_type"] != "SO"), key=lambda r: (r["game_seconds"], str(r["event_id"])))
    out: dict[tuple[bool, str], float] = defaultdict(float)
    for a, b in zip(evs, evs[1:]):
        sit = parse_situation_code(a.get("situation_code"))
        dt = b["game_seconds"] - a["game_seconds"]
        if sit is None or dt <= 0 or not (sit["home_goalie_in"] and sit["away_goalie_in"]):
            continue
        h, w = sit["home_skaters"], sit["away_skaters"]
        if h == 5 and w == 5:
            out[(True, "5on5")] += dt
            out[(False, "5on5")] += dt
        elif h == 5 and w == 4:
            out[(True, "5on4")] += dt
        elif h == 4 and w == 5:
            out[(False, "5on4")] += dt
    return out


def team_game_stats_from_pbp(
    model: XGModel,
    events: list[dict],
    start_time: datetime,
    home: str,
    away: str,
    snapshot_id: str,
    computed_at: datetime,
) -> list[TeamGameStats]:
    """One game's 5on5 / 5on4 / all rows from our xG. Penalties: owner of a penalty event =
    penalized team (VERIFY on live feed)."""

    if not events:
        return []
    gid = events[0]["game_id"]
    season = int(gid[:4])
    scored = score_events(model, events, season)
    xg: dict[tuple[bool, str], float] = defaultdict(float)
    goals: dict[tuple[bool, str], int] = defaultdict(int)
    sog: dict[bool, int] = defaultdict(int)
    for e, p in scored:
        bucket = "5on5" if e["strength"] == "5v5" else "5on4" if (e["strength"] == "PP" and e["situation_code"] in ("1451", "1541")) else "other"
        xg[(e["is_home"], bucket)] += p
        xg[(e["is_home"], "all")] += p
    for e in events:
        if e["period_type"] == "SO" or e["is_home"] is None:
            continue
        if e["event_type"] == "goal":
            bucket = "5on5" if e["strength"] == "5v5" else "5on4" if (e["strength"] == "PP" and e["situation_code"] in ("1451", "1541")) else "other"
            goals[(e["is_home"], bucket)] += 1
            goals[(e["is_home"], "all")] += 1
        if e["event_type"] in ("shot-on-goal", "goal"):
            sog[e["is_home"]] += 1
    pens = {True: 0, False: 0}
    for e in events:
        if e["event_type"] == "penalty" and e["is_home"] is not None:
            pens[e["is_home"]] += 1
    secs = _situation_seconds(events)
    game_len = max((e["game_seconds"] for e in events if e["period_type"] != "SO"), default=3600)
    p = prov.walk_forward_model(f"nhl_xg:{model.model_id}", snapshot_id, computed_at, start_time + RESULT_LAG,
                                model.model_id, model.trained_through, season)
    out = []
    for is_home, team, opp in ((True, home, away), (False, away, home)):
        for sit in ("5on5", "5on4", "all"):
            toi = float(max(game_len, 3600)) if sit == "all" else secs.get((is_home, sit), 0.0)
            if toi <= 0:
                continue
            out.append(TeamGameStats(
                game_id=gid, team=team, opponent=opp, is_home=is_home, game_date=start_time.strftime("%Y%m%d"),
                situation=sit, toi_sec=toi, xg_for=xg[(is_home, sit)], xg_against=xg[(not is_home, sit)] if sit != "5on4" else 0.0,
                goals_for=goals[(is_home, sit)], goals_against=goals[(not is_home, sit)] if sit != "5on4" else 0,
                sog_for=sog[is_home], sog_against=sog[not is_home],
                penalties_for=pens[is_home] if sit == "all" else 0, penalties_against=pens[not is_home] if sit == "all" else 0,
                available_at=p.available_at, source=f"nhl_xg:{model.model_id}", provenance=p,
            ))
    return out
