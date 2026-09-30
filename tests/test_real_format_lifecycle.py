"""Definition-of-done drill on REAL-FORMAT fixtures through the production path:
raw capture -> replay -> point-in-time state -> price -> immutable artifacts ->
close -> results -> grade -> CLV -> gates -> desk export. Data origin FIXTURE and a
simulated clock keep every artifact DEV-lane and hard-blocked."""

import json
from datetime import date

from nhl.backtest.evaluate import evaluate
from nhl.data.nhl_api import NHLApiClient
from nhl.data.snapshots import RawSnapshotStore
from nhl.forward.runner import ForwardRunner, ReplayStateSource
from nhl.market.providers import FileProvider, capture_goalie_reports, capture_odds
from nhl.timeutil import parse_ts

DAY = date(2026, 10, 7)


def transport(fixtures, final: bool):
    sched = json.loads((fixtures / "nhl_schedule.json").read_text())
    if final:
        sched["gameWeek"][0]["games"][0]["gameState"] = "OFF"
    pages = {"schedule/2026-10-07": sched,
             "gamecenter/2026020001/play-by-play": json.loads((fixtures / "nhl_pbp_shootout.json").read_text()),
             "gamecenter/2026020001/boxscore": json.loads((fixtures / "nhl_boxscore.json").read_text()),
             "roster/TOR/20262027": json.loads((fixtures / "nhl_roster.json").read_text())}

    def get(url):
        return json.dumps(next(v for k, v in pages.items() if url.endswith(k))).encode()

    return get


def test_real_format_fixture_lifecycle(fixtures, tmp_path):
    raw_root = tmp_path / "raw"
    raw = RawSnapshotStore(raw_root)
    t = {"now": parse_ts("2026-10-07T15:05:00Z")}
    clock = lambda: t["now"]  # noqa: E731
    NHLApiClient(raw, transport(fixtures, False)).fetch_schedule("2026-10-07", t["now"])
    NHLApiClient(raw, transport(fixtures, False)).fetch_roster("TOR", 2026, t["now"])
    capture_odds(FileProvider("fixturefeed", fixtures / "provider" / "odds"), raw, DAY, parse_ts("2026-10-07T23:10:00Z"))
    capture_goalie_reports(FileProvider("fixturefeed", fixtures / "provider" / "goalies"), raw, DAY, parse_ts("2026-10-07T21:00:00Z"))

    runner = ForwardRunner(tmp_path / "fwd", ReplayStateSource(raw_root, data_origin="FIXTURE"), clock=clock,
                           simulated_clock=True, n_sims=1500)
    t["now"] = parse_ts("2026-10-07T21:00:00Z")
    p = runner.price(DAY, 2026)
    assert p["inserted"] > 0
    assert any("2026020002" in e for e in p["errors"])  # no goalie info at all for LAK/UTA: not priced, reported
    rows = list(runner.predictions.rows())
    markets = {r["market"] for r in rows}
    assert markets == {"ML", "TOTAL", "REG_3WAY"}  # props observed but not priced
    assert all(r["status"] == "BLOCKED" and r["eligibility"] == "INELIGIBLE_DEV" for r in rows)
    assert all("HARD_BLOCK:FIXTURE_SOURCE" in r["block_reasons"] for r in rows)
    ml = next(r for r in rows if r["market"] == "ML" and r["selection"] == "HOME")
    assert ml["execution_price"] == -142 and "CONFIRMED" in ml["goalie_state"]
    assert ml["raw_implied_probability"] > ml["no_vig_probability"]
    reg = next(r for r in rows if r["market"] == "REG_3WAY" and r["selection"] == "HOME")
    assert reg["execution_decimal"] == 2.15

    # Finals captured after the game; close captured.
    t["now"] = parse_ts("2026-10-08T03:30:00Z")
    fin = NHLApiClient(raw, transport(fixtures, True))
    from nhl.data.ingest import capture_day

    capture_day(fin, DAY, fetched_at=t["now"])
    c = runner.capture_close(DAY)
    closes = {r["market_id"]: r for r in runner.closes.rows()}
    assert closes["2026020001:ML"]["status"] == "AVAILABLE" and closes["2026020001:ML"]["cutoff_basis"] == "SCHEDULED_FALLBACK"
    assert closes["2026020001:TOTAL:+6.5"]["status"] == "UNAVAILABLE"  # suspended at close: not fabricated
    assert closes["2026020001:GOALIE_SAVES:P8479361:+27.5"]["reason"].startswith("STALE")
    assert c["available"] >= 1
    settled = runner.settle()
    ml_s = next(r for r in settled if r["market"] == "ML" and r["selection"] == "HOME")
    assert ml_s["settlement"] == "SETTLED" and ml_s["outcome"] == "WIN"  # TOR won the shootout
    assert ml_s["close_price"] == -150 and ml_s["clv_ev"] is not None and ml_s["clv_ev"] < 0
    tot = next(r for r in settled if r["market"] == "TOTAL")
    assert tot["close_status"] == "UNAVAILABLE" and tot["clv_ev"] is None
    store, _ = runner.state(t["now"])
    rep = evaluate(store, runner.predictions, mode="FORWARD", reps=20)
    assert all(not g["passed"] for g in rep["gates"].values())
    a = runner.export(tmp_path / "e1", DAY)
    b = runner.export(tmp_path / "e2", DAY)
    assert all(open(a[k], "rb").read() == open(b[k], "rb").read() for k in a)
    assert runner.verify()["predictions_chain"] and runner.verify()["closes_chain"]
