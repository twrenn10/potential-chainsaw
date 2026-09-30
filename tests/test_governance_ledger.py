import sqlite3

import pytest

from nhl.contracts import BetResult, GameResult, Lane, MarketType, Selection
from nhl.governance.lanes import GovernanceError, assert_allowed, decide
from nhl.ledger.bets import BetLedger, LedgerError, Placement
from nhl.ledger.clv import compute_clv
from nhl.ledger.grading import settle
from nhl.ledger.predictions import ArtifactError, PredictionArtifact, PredictionStore


def artifact(**kw) -> PredictionArtifact:
    base = dict(
        prediction_id="P:1", created_at="2026-10-07T20:00:00Z", as_of="2026-10-07T20:00:00Z",
        puck_drop="2026-10-07T23:00:00Z", mode="FORWARD", data_origin="LIVE", game_id="2026020001",
        market="ML", selection="HOME", line=None, team=None, market_key="2026020001:ML:HOME",
        sportsbook="booka", market_price=-120, market_observed_at="2026-10-07T19:55:00Z",
        no_vig_probability=0.53, model_probability=0.56, model_p_win=0.56, model_p_push=0.0,
        fair_price=-127, edge=0.03, ev_per_unit=0.026, goalie_state="HOME:CONFIRMED(1@0.985)|AWAY:UNKNOWN(2@0.7)",
        lineup_state="UNKNOWN", model_version="m", feature_version="f", config_hash="c",
        data_snapshot_id="DS:x", health_score=1.0, status="UNVALIDATED", shadow_lane="WATCH", reason_codes="[]",
    )
    base.update(kw)
    return PredictionArtifact(**base)


def test_artifacts_append_only_and_chained(tmp_path):
    db = tmp_path / "p.sqlite"
    store = PredictionStore(db)
    assert store.append([artifact(), artifact(prediction_id="P:2")], now="2026-10-07T20:00:01Z")["inserted"] == 2
    assert store.append([artifact()], now="2026-10-07T20:00:02Z")["skipped_identical"] == 1
    with pytest.raises(ArtifactError):
        store.append([artifact(model_probability=0.9)], now="2026-10-07T20:00:02Z")  # silent rewrite refused
    with pytest.raises(sqlite3.DatabaseError):
        store.conn.execute("UPDATE predictions SET edge = 0.5")
    with pytest.raises(sqlite3.DatabaseError):
        store.conn.execute("DELETE FROM predictions")
    ok, _ = store.verify_chain()
    assert ok
    # Tamper out-of-band: drop the trigger and edit -> chain verification fails.
    store.conn.execute("DROP TRIGGER predictions_no_update")
    store.conn.execute("UPDATE predictions SET edge = 0.5 WHERE prediction_id = 'P:1'")
    store.conn.commit()
    ok, why = store.verify_chain()
    assert not ok and "hash mismatch" in why


def test_negative_zero_and_nan_round_trip_safety(tmp_path):
    store = PredictionStore(tmp_path / "z.sqlite")
    store.append([artifact(ev_per_unit=-0.0, edge=-0.0)], now="2026-10-07T20:00:01Z")
    assert store.verify_chain()[0]
    assert store.append([artifact(ev_per_unit=-0.0, edge=-0.0)], now="2026-10-07T20:00:02Z")["skipped_identical"] == 1
    with pytest.raises(ArtifactError):
        store.append([artifact(prediction_id="P:nan", edge=float("nan"))], now="2026-10-07T20:00:02Z")


def test_forward_artifacts_rejected_after_puck_drop(tmp_path):
    store = PredictionStore(tmp_path / "p.sqlite")
    with pytest.raises(ArtifactError):
        store.append([artifact()], now="2026-10-07T23:00:00Z")
    with pytest.raises(ArtifactError):
        store.append([artifact(as_of="2026-10-07T23:10:00Z")], now="2026-10-07T20:00:00Z")
    # Backtest rows may be written later (created now) but as_of must still be pre-game.
    assert store.append([artifact(mode="BACKTEST", prediction_id="P:b")], now="2027-01-01T00:00:00Z")["inserted"] == 1


def test_governance_phase1():
    d = decide("ML", 0.10, "LIVE", [], 5.0, True)
    assert d.lane is Lane.UNVALIDATED and d.shadow_lane is Lane.MODEL_PLUS
    assert decide("ML", 0.10, "SYNTHETIC", [], 5.0, True).lane is Lane.BLOCKED
    assert decide("ML", 0.10, "LIVE", ["GOALIE_INPUTS"], 5.0, True).lane is Lane.BLOCKED
    assert decide("ML", 0.10, "LIVE", [], 90.0, True).lane is Lane.BLOCKED
    assert "GOALIE_UNCONFIRMED_PRICED_AS_MIXTURE" in decide("TOTAL", 0.0, "LIVE", [], 1.0, False).reasons
    with pytest.raises(GovernanceError):
        assert_allowed(Lane.ACTIONABLE)
    # Even with the config switch flipped, phase 1 refuses ACTIONABLE.
    from nhl.config import load

    cfg = load("governance")
    cfg["actionable_enabled"] = True
    with pytest.raises(GovernanceError):
        assert_allowed(Lane.ACTIONABLE, cfg)
    assert decide("ML", 0.10, "LIVE", [], 1.0, True, frozenset({"ML"}), cfg).lane is Lane.UNVALIDATED


def test_grading_conventions():
    so = GameResult("2026020001", 2, 2, 2, 2, 1, 1, "SO", "AWAY", "2026-10-08T03:00:00Z")
    assert settle(so, MarketType.ML, Selection.AWAY) is BetResult.WIN
    assert settle(so, MarketType.REG_3WAY, Selection.DRAW) is BetResult.WIN
    assert settle(so, MarketType.PUCK_LINE, Selection.AWAY, -1.5) is BetResult.LOSS  # SO win = +1
    assert settle(so, MarketType.PUCK_LINE, Selection.HOME, 1.5) is BetResult.WIN
    assert settle(so, MarketType.TOTAL, Selection.OVER, 4.5) is BetResult.WIN  # 2-2 + SO goal = 5
    assert settle(so, MarketType.TOTAL, Selection.UNDER, 5.0) is BetResult.PUSH
    assert settle(so, MarketType.TEAM_TOTAL, Selection.OVER, 2.5, team_is_home=False) is BetResult.WIN
    reg = GameResult("2026020002", 4, 1, 4, 1, 2, 0, "REG", None, "2026-10-08T03:00:00Z")
    assert settle(reg, MarketType.PUCK_LINE, Selection.HOME, -1.5) is BetResult.WIN
    assert settle(reg, MarketType.TEAM_TOTAL, Selection.UNDER, 1.5, team_is_home=False) is BetResult.WIN


def test_clv():
    c = compute_clv(bet_price=110, p_nv_at_bet=0.46, p_nv_close=0.50, model_p=0.52)
    assert c.clv_ev == pytest.approx(0.05)
    assert c.edge_at_close == pytest.approx(0.02) and c.prob_move == pytest.approx(0.04)


def test_bet_ledger_rules(tmp_path):
    preds = PredictionStore(tmp_path / "p.sqlite")
    preds.append([artifact(), artifact(prediction_id="P:blk", status="BLOCKED")], now="2026-10-07T20:00:01Z")
    ledger = BetLedger(tmp_path / "b.sqlite", preds)
    with pytest.raises(LedgerError):
        ledger.place(Placement("P:blk", "2026-10-07T20:01:00Z", "booka", -120, 1.0, paper=True))
    with pytest.raises(LedgerError):
        ledger.place(Placement("P:1", "2026-10-07T20:01:00Z", "booka", -120, 1.0, paper=False))
    bet = ledger.place(Placement("P:1", "2026-10-07T20:01:00Z", "booka", -120, 1.0, paper=True, max_stake_seen=250.0, requested_stake=500.0))
    assert ledger.open_bets() == [bet]
    ledger.settle(bet, "2026-10-08T03:00:00Z", BetResult.WIN, 0.833, -130, 0.55, 0.008)
    assert ledger.open_bets() == []
    with pytest.raises(sqlite3.DatabaseError):
        ledger.conn.execute("DELETE FROM settlements")
