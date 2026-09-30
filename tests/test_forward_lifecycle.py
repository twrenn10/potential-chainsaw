from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from nhl.backtest.walk_forward import slate_days
from nhl.contracts import GoalieReport, GoalieState
from nhl.data.pit import HistoricalStore
from nhl.forward.runner import ForwardRunner, StaticStateSource

DAY = date(2026, 1, 20)


class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def _store_without_reports(league, day_games):
    s = league.store
    ids = {g.game_id for g in day_games}
    return HistoricalStore(games=s.games, results=s.results, team_stats=s.team_stats, goalie_stats=s.goalie_stats,
                           goalie_reports=[r for r in s.goalie_reports if r.game_id not in ids], odds=s.odds,
                           player_seasons=s.player_seasons, data_origin=s.data_origin)


def test_forward_lifecycle(league, tmp_path):
    games = slate_days([g for g in league.store.games if g.season == 2025])[DAY]
    first = min(g.start_time for g in games)
    store = _store_without_reports(league, games)
    clock = Clock(first - timedelta(hours=9))
    runner = ForwardRunner(tmp_path / "fwd", StaticStateSource(store, league.rosters), clock=clock, simulated_clock=True,
                           n_sims=1500)

    # Morning baseline.
    s1 = runner.price(DAY, 2025)
    assert s1["inserted"] > 0 and s1["unchanged_state"] == 0
    morning = {r["prediction_id"]: r for r in runner.predictions.rows()}
    assert all(r["evidence_lane"] == "DEV_SYNTHETIC" and "HARD_BLOCK:SIMULATED_CLOCK" in r["block_reasons"] for r in morning.values())

    # 18/20. Identical rerun at the same instant: nothing appended.
    s2 = runner.price(DAY, 2025)
    assert s2["inserted"] == 0 and s2["skipped_identical"] + s2["unchanged_state"] == s1["inserted"]

    # A later cycle with no new information (only the clock moved, no new odds mark
    # crossed): no reprice spam -- prices are a function of information, not the clock.
    clock.t = first - timedelta(hours=8, minutes=30)
    s3 = runner.price(DAY, 2025)
    assert s3["inserted"] == 0 and s3["unchanged_state"] == s1["inserted"]

    # New market observations (synthetic -8h marks) -> meaningful reprice.
    clock.t = first - timedelta(hours=2, minutes=30)
    s_mkt = runner.price(DAY, 2025)
    assert s_mkt["inserted"] > 0
    morning = {r["prediction_id"]: r for r in runner.predictions.rows()}

    # 15/19. A goalie confirmation for one team arrives -> that game reprices (new
    # artifacts), earlier artifacts untouched.
    target = sorted(games, key=lambda g: g.game_id)[0]
    backup = sorted({gs.goalie_id for gs in store.goalie_stats if gs.team == target.home})[-1]
    store.goalie_reports.append(GoalieReport(target.game_id, target.home, backup, GoalieState.CONFIRMED, "beat",
                                             first - timedelta(hours=2, minutes=15)))
    clock.t = first - timedelta(hours=2)
    s4 = runner.price(DAY, 2025)
    assert s4["inserted"] > 0
    after = list(runner.predictions.rows())
    new = [r for r in after if r["prediction_id"] not in morning]
    assert new and {r["game_id"] for r in new} == {target.game_id}
    for r in after:
        if r["prediction_id"] in morning:
            assert r == morning[r["prediction_id"]]  # old artifacts byte-for-byte unchanged
    assert any("CONFIRMED" in r["goalie_state"] for r in new)
    old_fp = {r["goalie_fingerprint"] for r in morning.values() if r["game_id"] == target.game_id}
    assert old_fp.isdisjoint({r["goalie_fingerprint"] for r in new})

    # 21. hash chain across reprices.
    v = runner.verify()
    assert v["predictions_chain"] and v["closes_chain"]

    # Close, settle (22-23), export twice (27-28).
    clock.t = max(g.start_time for g in games) + timedelta(hours=5)
    c = runner.capture_close(DAY)
    assert c["available"] > 0 and runner.capture_close(DAY)["inserted"] == 0
    settled = runner.settle()
    assert all(r["settlement"] == "SETTLED" for r in settled)
    with_clv = [r for r in settled if r["clv_ev"] is not None]
    assert with_clv and all(abs(r["clv_ev"] - (r["execution_decimal"] * r["close_no_vig"] - 1)) < 1e-5 for r in with_clv)
    a = runner.export(tmp_path / "exp1", DAY)
    b = runner.export(tmp_path / "exp2", DAY)
    for k in a:
        assert open(a[k], "rb").read() == open(b[k], "rb").read(), k
    slate = open(a["CURRENT_SLATE.csv"]).read()
    assert "effective_status" in slate.splitlines()[0] and "ACTIONABLE" not in slate
    assert runner.verify()["closes"] > 0
    lines = (tmp_path / "fwd" / "cycles.jsonl").read_text().splitlines()
    assert len(lines) == 7


def test_forward_rejects_post_puck_drop_pricing(league, tmp_path):
    games = slate_days([g for g in league.store.games if g.season == 2025])[DAY]
    clock = Clock(max(g.start_time for g in games) + timedelta(minutes=1))
    runner = ForwardRunner(tmp_path / "late", StaticStateSource(league.store, league.rosters), clock=clock,
                           simulated_clock=True, n_sims=500)
    assert runner.price(DAY, 2025)["games"] == 0  # nothing left to price; no post-drop artifacts
    assert runner.predictions.count() == 0


def test_old_artifact_unaffected_by_later_roster_and_market_changes(league, tmp_path):
    games = slate_days([g for g in league.store.games if g.season == 2025])[DAY]
    first = min(g.start_time for g in games)
    clock = Clock(first - timedelta(hours=6))
    base = league.store
    store = HistoricalStore(games=base.games, results=base.results, team_stats=base.team_stats, goalie_stats=base.goalie_stats,
                            goalie_reports=base.goalie_reports, odds=list(base.odds), player_seasons=base.player_seasons,
                            data_origin=base.data_origin)
    rosters = {k: list(v) for k, v in league.rosters.items()}
    runner = ForwardRunner(tmp_path / "r", StaticStateSource(store, rosters), clock=clock, simulated_clock=True, n_sims=800)
    runner.price(DAY, 2025)
    before = list(runner.predictions.rows())
    # New roster snapshot + a moved market arrive later.
    later = first - timedelta(hours=3)
    team = games[0].home
    rosters[2025] += [replace(s, available_at=later) for s in rosters[2025] if s.team == team][:-2]
    moved = [o for o in store.odds if o.game_id == games[0].game_id and o.snapshot_ts <= later]
    store.odds += [replace(o, snapshot_ts=later, price_american=o.price_american + (10 if o.price_american > 0 else -10))
                   for o in moved if o.snapshot_ts == max(m.snapshot_ts for m in moved)]
    clock.t = first - timedelta(hours=2)
    runner.price(DAY, 2025)
    after = {r["prediction_id"]: r for r in runner.predictions.rows()}
    for r in before:
        assert after[r["prediction_id"]] == r
    new = [r for pid, r in after.items() if pid not in {b["prediction_id"] for b in before}]
    assert any(r["game_id"] == games[0].game_id for r in new)
    assert runner.verify()["predictions_chain"]
