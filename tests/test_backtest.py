from dataclasses import replace
from datetime import date, timedelta

from nhl.backtest.evaluate import evaluate
from nhl.backtest.walk_forward import ET, BacktestConfig, run_walk_forward, slate_days
from nhl.contracts import PredictionMode
from nhl.data.pit import HistoricalStore
from nhl.desk.board import export_desk
from nhl.ledger.predictions import PredictionStore
from nhl.pipeline import run_slate


def _strip(artifacts):
    return [replace(a, created_at="X").content() for a in artifacts]


def _truncate(store: HistoricalStore, as_of) -> HistoricalStore:
    """Physically remove everything not knowable at as_of."""

    keep = lambda rows: [r for r in rows if r.available_at is None or r.available_at <= as_of]  # noqa: E731
    return HistoricalStore(
        games=store.games,  # schedule is known in advance
        results=keep(store.results),
        team_stats=keep(store.team_stats),
        goalie_stats=keep(store.goalie_stats),
        goalie_reports=keep(store.goalie_reports),
        odds=[o for o in store.odds if o.snapshot_ts <= as_of],
        player_seasons=keep(store.player_seasons),
        data_origin=store.data_origin,
    )


def test_future_data_cannot_change_predictions(league):
    store = league.store
    days = slate_days([g for g in store.games if g.season == 2025])
    for day in (date(2025, 11, 12), date(2026, 2, 3)):
        games = days[day]
        as_of = min(g.start_time for g in games) - timedelta(minutes=60)
        full = run_slate(store, league.rosters, 2025, games, as_of, PredictionMode.BACKTEST, n_sims=1500)
        cut = run_slate(_truncate(store, as_of), league.rosters, 2025, games, as_of, PredictionMode.BACKTEST, n_sims=1500)
        assert full.artifacts and _strip(full.artifacts) == _strip(cut.artifacts)


def test_walk_forward_evaluate_and_gates(league, tmp_path):
    preds = PredictionStore(tmp_path / "bt.sqlite")
    s = run_walk_forward(league.store, league.rosters, BacktestConfig(2025, date(2025, 11, 1), date(2025, 11, 10), n_sims=1000), preds)
    assert s["games_priced"] > 30 and s["artifacts"] > 0 and not s["errors"]
    rows = list(preds.rows())
    assert all(r["as_of"] < r["puck_drop"] for r in rows)
    assert {r["status"] for r in rows} == {"BLOCKED"}  # synthetic is always blocked
    assert all("HARD_BLOCK:SYNTHETIC_SOURCE" in r["reason_codes"] for r in rows)
    rep = evaluate(league.store, preds, reps=20)
    assert rep["missing_close"] == 0
    assert set(rep["markets"]) >= {"ML", "PUCK_LINE", "TOTAL", "TEAM_TOTAL", "REG_3WAY"}
    assert all(not g["passed"] and "NOT_ELIGIBLE:SYNTHETIC_DATA" in g["reasons"] for g in rep["gates"].values())
    assert preds.verify_chain()[0]


def test_desk_export_is_deterministic(league, tmp_path):
    store = league.store
    games = slate_days([g for g in store.games if g.season == 2025])[date(2026, 1, 20)]
    as_of = min(g.start_time for g in games) - timedelta(minutes=60)
    outs = []
    for i in range(2):
        run = run_slate(store, league.rosters, 2025, games, as_of, PredictionMode.BACKTEST, n_sims=1000, created_at=as_of)
        paths = export_desk(tmp_path / f"d{i}", "2026-01-20", run.artifacts, run.priced, run.health)
        outs.append({k: open(v, "rb").read() for k, v in paths.items()})
    assert outs[0] == outs[1]
    text = outs[0]["text"].decode()
    assert "NHL DESK - 2026-01-20" in text and "ACTIONABLE" not in text


def test_postponed_game_skipped_then_priced_on_new_date(league, tmp_path):
    store = league.store
    days = slate_days([g for g in store.games if g.season == 2025])
    day = date(2025, 11, 12)
    target = sorted(days[day], key=lambda g: g.game_id)[0]
    new_start = target.start_time + timedelta(days=7)
    ppd = replace(target, status="PPD", available_at=target.start_time - timedelta(hours=6))
    moved = replace(target, status="SCHEDULED", start_time=new_start, available_at=target.start_time + timedelta(days=1))
    s2 = HistoricalStore(games=store.games + [ppd, moved], results=[r for r in store.results if r.game_id != target.game_id],
                         team_stats=store.team_stats, goalie_stats=store.goalie_stats, goalie_reports=store.goalie_reports,
                         odds=[o for o in store.odds if o.game_id != target.game_id], player_seasons=store.player_seasons,
                         data_origin=store.data_origin)
    end = new_start.astimezone(ET).date()
    preds = PredictionStore(tmp_path / "ppd.sqlite")
    s = run_walk_forward(s2, league.rosters, BacktestConfig(2025, day, end, n_sims=300), preds)
    assert f"{day}:{target.game_id}:STATUS_PPD" in s["skipped_status"]
    assert s["constants_train_seasons"] == [2024]


def test_unattested_real_origin_is_blocked(league):
    store = league.store
    real_shaped = HistoricalStore(games=store.games, results=store.results, team_stats=store.team_stats,
                                  goalie_stats=store.goalie_stats, goalie_reports=store.goalie_reports, odds=store.odds,
                                  player_seasons=store.player_seasons, data_origin="HISTORICAL")
    games = slate_days([g for g in store.games if g.season == 2025])[date(2026, 1, 20)]
    as_of = min(g.start_time for g in games) - timedelta(minutes=60)
    run = run_slate(real_shaped, league.rosters, 2025, games, as_of, PredictionMode.BACKTEST, n_sims=300)
    assert run.artifacts and all(a.status == "BLOCKED" and "UNATTESTED_PROVENANCE" in a.reason_codes for a in run.artifacts)
    assert all("SYNTHETIC_SOURCE" not in a.reason_codes for a in run.artifacts)
