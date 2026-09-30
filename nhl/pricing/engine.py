"""Slate pricing: goalie mixtures -> batched game-state simulation -> GamePricing."""

from __future__ import annotations

from dataclasses import dataclass

from nhl.config import load
from nhl.contracts import Game, stable_digest
from nhl.data.pit import PointInTimeView
from nhl.features.ratings import Ratings
from nhl.features.rest_travel import team_schedule_features
from nhl.models.game_state import GameRates, simulate, team_rates
from nhl.models.goalie_start import StarterDistribution, starter_distribution

from .markets import GamePricing


@dataclass
class PricedGame:
    game: Game
    pricing: GamePricing
    home_starters: StarterDistribution
    away_starters: StarterDistribution
    n_scenarios: int

    @property
    def goalie_state(self) -> str:
        """e.g. 'HOME:CONFIRMED(8479361@0.985)|AWAY:UNKNOWN(8478470@0.71)'."""

        def fmt(side: str, d: StarterDistribution) -> str:
            g, p = d.top
            conflict = "*CONFLICT" if "GOALIE_SOURCE_CONFLICT" in d.flags else ""
            return f"{side}:{d.state.value}{conflict}({g}@{p:.3f})"

        return f"{fmt('HOME', self.home_starters)}|{fmt('AWAY', self.away_starters)}"

    @property
    def both_confirmed(self) -> bool:
        return self.home_starters.confirmed and self.away_starters.confirmed

    @property
    def goalie_fingerprint(self) -> str:
        return "G:" + stable_digest([self.home_starters.fingerprint(), self.away_starters.fingerprint()], 16)

    @property
    def goalie_flags(self) -> list[str]:
        return sorted({f"{side}:{f}" for side, d in (("HOME", self.home_starters), ("AWAY", self.away_starters)) for f in d.flags})


def game_seed(game_id: str, as_of_iso: str) -> int:
    return int(stable_digest([game_id, as_of_iso], n=8), 16)


def price_slate(
    view: PointInTimeView,
    games: list[Game],
    ratings: Ratings,
    n_sims: int | None = None,
    seed: int | None = None,
    min_goalie_p: float = 0.03,
    sim_cfg: dict | None = None,
) -> tuple[list[PricedGame], list[tuple[str, str]]]:
    """Returns priced games and (game_id, error) for games that could not be priced.

    ``sim_cfg``: simulator config with walk-forward league constants applied."""

    cfg = sim_cfg or load("simulator")
    n_sims = n_sims or int(cfg["n_sims"])
    sched = team_schedule_features(view.games())
    scenarios: list[GameRates] = []
    plan: list[tuple[Game, StarterDistribution, StarterDistribution, list[float], int]] = []
    errors: list[tuple[str, str]] = []
    for g in sorted(games, key=lambda x: (x.start_time, x.game_id)):
        try:
            hd = starter_distribution(view, g, g.home)
            ad = starter_distribution(view, g, g.away)
        except ValueError as exc:
            errors.append((g.game_id, str(exc)))
            continue
        h_rt = sched.get((g.home, g.game_id))
        a_rt = sched.get((g.away, g.game_id))
        h_b2b = bool(h_rt and h_rt.b2b)
        a_b2b = bool(a_rt and a_rt.b2b)
        weights = []
        start = len(scenarios)
        for hg, hp in hd.truncated(min_goalie_p):
            for ag, ap in ad.truncated(min_goalie_p):
                scenarios.append(
                    GameRates(
                        home=team_rates(ratings, g.home, g.away, True, h_b2b, a_b2b, ag, cfg),
                        away=team_rates(ratings, g.away, g.home, False, a_b2b, h_b2b, hg, cfg),
                        so_home=cfg["shootout_home_prob"],
                    )
                )
                weights.append(hp * ap)
        plan.append((g, hd, ad, weights, start))

    base_seed = seed if seed is not None else game_seed("slate", view.as_of.isoformat())
    outcomes = simulate(scenarios, n_sims, base_seed, cfg)
    priced = []
    for g, hd, ad, weights, start in plan:
        outs = outcomes[start:start + len(weights)]
        priced.append(PricedGame(g, GamePricing.from_outcomes(outs, weights), hd, ad, len(weights)))
    return priced, errors
