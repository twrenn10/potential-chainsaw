"""Walk-forward league constants for the simulator and rating priors (gap 3B).

Every constant used to price season S is fit ONLY from seasons completed before S,
read through a strict ``PointInTimeView`` at S's cutoff (Sep 1 of S), over a
rolling window of at most ``window`` seasons. Constants without enough history,
or that cannot be estimated from the available data, fall back to the static
defaults in ``simulator.json`` and are labelled ``DEFAULT:<reason>``. They are never
silently fit from the season being evaluated.

Each fit is persisted write-once as JSON with its training window, sample sizes,
provenance of each value and a content hash (``constants_id``). The id is stamped
into every prediction artifact's config hash.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from nhl.config import load
from nhl.data.pit import HistoricalStore

# Constants we can estimate from team-game stats and results today.
FITTABLE = ("rate_5v5", "rate_pp", "penalty_rate", "rate_3v3", "shootout_home_prob")
# Used by the simulator but not yet estimable from ingested data (need event-level fits).
DEFAULT_ONLY = ("rate_sh", "rate_6v5_attack", "rate_empty_net", "rate_4v4_mult", "score_effect_beta",
                "score_effect_beta_p3", "pull_trailing_by_1_seconds", "pull_trailing_by_2_seconds")
MIN_TEAM_HOURS = 300.0
MIN_TIED_GAMES = 100


class ConstantsIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True)
class LeagueConstants:
    target_season: int
    fit_as_of: str
    window: int
    train_seasons: tuple[int, ...]
    values: dict[str, float]
    sources: dict[str, str]  # FIT | DEFAULT:<reason>
    n: dict[str, float]
    constants_id: str = ""

    def payload(self) -> dict:
        d = asdict(self)
        d.pop("constants_id")
        d["train_seasons"] = list(self.train_seasons)
        return d

    def apply(self, sim_cfg: dict) -> dict:
        return {**sim_cfg, **self.values}


def season_cutoff(season: int) -> datetime:
    return datetime(season, 9, 1, tzinfo=timezone.utc)


def _with_id(c: LeagueConstants) -> LeagueConstants:
    blob = json.dumps(c.payload(), sort_keys=True, separators=(",", ":"))
    return LeagueConstants(**{**c.__dict__, "constants_id": "LC:" + sha256(blob.encode()).hexdigest()[:16]})


def fit_league_constants(store: HistoricalStore, target_season: int, window: int = 3) -> LeagueConstants:
    defaults = load("simulator")
    view = store.view(season_cutoff(target_season))  # strict: non-causal rows are invisible
    stats = [s for s in view.team_stats() if int(s.game_id[:4]) < target_season]
    results = [r for r in view.results() if int(r.game_id[:4]) < target_season]
    seasons = sorted({int(s.game_id[:4]) for s in stats} | {int(r.game_id[:4]) for r in results})
    train = tuple(seasons[-window:])
    stats = [s for s in stats if int(s.game_id[:4]) in train]
    results = [r for r in results if int(r.game_id[:4]) in train]

    values: dict[str, float] = {}
    sources: dict[str, str] = {}
    n: dict[str, float] = {}

    def per60(situation: str, num) -> tuple[float | None, float]:
        rows = [s for s in stats if s.situation == situation and s.toi_sec > 0]
        hours = sum(s.toi_sec for s in rows) / 3600.0
        if hours < MIN_TEAM_HOURS:
            return None, hours
        return sum(num(s) for s in rows) / hours, hours

    for key, sit, num in (
        ("rate_5v5", "5on5", lambda s: s.xg_for),
        ("rate_pp", "5on4", lambda s: s.xg_for),
        ("penalty_rate", "all", lambda s: s.penalties_for),
    ):
        v, hours = per60(sit, num)
        n[key] = round(hours, 3)
        if v is None:
            values[key], sources[key] = float(defaults[key]), f"DEFAULT:exposure<{MIN_TEAM_HOURS}h"
        else:
            values[key], sources[key] = round(v, 6), "FIT"

    ot = sum(1 for r in results if r.end_type == "OT")
    so = sum(1 for r in results if r.end_type == "SO")
    n["rate_3v3"] = ot + so
    if ot + so >= MIN_TIED_GAMES and 0 < ot < ot + so:
        p = ot / (ot + so)
        values["rate_3v3"] = round(-math.log(1 - p) * 3600.0 / (2 * defaults["ot_seconds"]), 6)
        sources["rate_3v3"] = "FIT"
    else:
        values["rate_3v3"], sources["rate_3v3"] = float(defaults["rate_3v3"]), f"DEFAULT:tied_games<{MIN_TIED_GAMES}"
    so_home = sum(1 for r in results if r.end_type == "SO" and r.shootout_winner == "HOME")
    n["shootout_home_prob"] = so
    if so >= 50:
        values["shootout_home_prob"] = round((so_home + 25) / (so + 50), 6)  # shrunk to 0.5
        sources["shootout_home_prob"] = "FIT"
    else:
        values["shootout_home_prob"], sources["shootout_home_prob"] = float(defaults["shootout_home_prob"]), "DEFAULT:shootouts<50"
    for key in DEFAULT_ONLY:
        values[key], sources[key] = float(defaults[key]), "DEFAULT:not_estimable_from_ingested_data"

    return _with_id(LeagueConstants(
        target_season=target_season, fit_as_of=season_cutoff(target_season).strftime("%Y-%m-%dT%H:%M:%SZ"),
        window=window, train_seasons=train, values=dict(sorted(values.items())), sources=dict(sorted(sources.items())),
        n=dict(sorted(n.items())),
    ))


class LeagueConstantsStore:
    """Write-once JSON files: <root>/league_constants/season=<S>/<constants_id>.json."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root) / "league_constants"

    def save(self, c: LeagueConstants) -> Path:
        path = self.root / f"season={c.target_season}" / f"{c.constants_id.replace(':', '_')}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        blob = json.dumps({"constants_id": c.constants_id, **c.payload()}, sort_keys=True, indent=2) + "\n"
        if path.exists():
            if path.read_text(encoding="utf-8") != blob:
                raise ConstantsIntegrityError(f"{path} exists with different content")
            return path
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
        with os.fdopen(fd, "w", encoding="utf-8") as h:
            h.write(blob)
        return path

    def load(self, path: str | Path) -> LeagueConstants:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        cid = raw.pop("constants_id")
        raw["train_seasons"] = tuple(raw["train_seasons"])
        c = _with_id(LeagueConstants(**raw))
        if c.constants_id != cid:
            raise ConstantsIntegrityError(f"{path}: content hash {c.constants_id} != recorded {cid}")
        return c
