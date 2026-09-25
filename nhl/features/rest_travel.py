"""Schedule-derived situational features: rest, back-to-backs, 3-in-4, travel, time zones.

Only the schedule (known in advance) is used, so these are leakage-free by
construction. v0.1 of the rating fit uses ``b2b`` only; the rest are computed and
exported so later model versions can test them walk-forward.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from nhl.contracts import Game

# (lat, lon, UTC offset in standard time). Approximate arena locations.
ARENAS: dict[str, tuple[float, float, int]] = {
    "ANA": (33.81, -117.88, -8), "BOS": (42.37, -71.06, -5), "BUF": (42.88, -78.88, -5),
    "CAR": (35.80, -78.72, -5), "CBJ": (39.97, -83.01, -5), "CGY": (51.04, -114.05, -7),
    "CHI": (41.88, -87.67, -6), "COL": (39.75, -105.01, -7), "DAL": (32.79, -96.81, -6),
    "DET": (42.34, -83.06, -5), "EDM": (53.55, -113.50, -7), "FLA": (26.16, -80.33, -5),
    "LAK": (34.04, -118.27, -8), "MIN": (44.94, -93.10, -6), "MTL": (45.50, -73.57, -5),
    "NJD": (40.73, -74.17, -5), "NSH": (36.16, -86.78, -6), "NYI": (40.72, -73.73, -5),
    "NYR": (40.75, -73.99, -5), "OTT": (45.30, -75.93, -5), "PHI": (39.90, -75.17, -5),
    "PIT": (40.44, -79.99, -5), "SEA": (47.62, -122.35, -8), "SJS": (37.33, -121.90, -8),
    "STL": (38.63, -90.20, -6), "TBL": (27.94, -82.45, -5), "TOR": (43.64, -79.38, -5),
    "UTA": (40.77, -111.90, -7), "VAN": (49.28, -123.11, -8), "VGK": (36.10, -115.18, -8),
    "WPG": (49.89, -97.14, -6), "WSH": (38.90, -77.02, -5),
}


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


@dataclass(frozen=True)
class RestTravel:
    team: str
    game_id: str
    days_rest: int  # full days since previous game; 0 = back-to-back; 7 = season opener cap
    b2b: bool
    three_in_four: bool
    travel_km: float
    tz_shift: int  # hours moved east since last game (negative = west)


def team_schedule_features(games: list[Game]) -> dict[tuple[str, str], RestTravel]:
    """Features for every (team, game_id) in ``games`` (must include prior games)."""

    by_team: dict[str, list[Game]] = {}
    for g in sorted(games, key=lambda x: (x.start_time, x.game_id)):
        by_team.setdefault(g.home, []).append(g)
        by_team.setdefault(g.away, []).append(g)
    out: dict[tuple[str, str], RestTravel] = {}
    for team, seq in by_team.items():
        for i, g in enumerate(seq):
            loc = ARENAS[g.home]
            if i == 0 or (g.start_time - seq[i - 1].start_time).days > 30:
                out[(team, g.game_id)] = RestTravel(team, g.game_id, 7, False, False, 0.0, 0)
                continue
            prev = seq[i - 1]
            days = _local_day(g.start_time, g.home) - _local_day(prev.start_time, prev.home)
            prev_loc = ARENAS[prev.home]
            recent = [s for s in seq[max(0, i - 3):i] if _local_day(g.start_time, g.home) - _local_day(s.start_time, s.home) <= 3]
            out[(team, g.game_id)] = RestTravel(
                team=team,
                game_id=g.game_id,
                days_rest=max(0, days - 1),
                b2b=days == 1,
                three_in_four=len(recent) >= 2,
                travel_km=round(haversine_km(prev_loc[:2], loc[:2]), 1),
                tz_shift=loc[2] - prev_loc[2],
            )
    return out


def _local_day(ts: datetime, home: str) -> int:
    offset = ARENAS[home][2]
    return int((ts.timestamp() + offset * 3600) // 86400)
