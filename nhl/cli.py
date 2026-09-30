"""Command line entry points.

  python -m nhl.cli demo --out out/demo            # synthetic end-to-end run (offline)
  python -m nhl.cli verify --db out/demo/predictions.sqlite
  python -m nhl.cli fetch-schedule --date 2026-10-07 --store store/   # live NHL API
  python -m nhl.cli capture --date 2026-10-07 --store store/ --rosters TOR,MTL --season 2026
  python -m nhl.cli replay --store store/ --out out/ingest
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from nhl.backtest.evaluate import evaluate
from nhl.backtest.metrics import calibration_table
from nhl.backtest.walk_forward import BacktestConfig, run_walk_forward, slate_days
from nhl.contracts import PredictionMode
from nhl.desk.board import export_desk
from nhl.ledger.predictions import PredictionStore
from nhl.pipeline import run_slate


def cmd_demo(args: argparse.Namespace) -> int:
    from nhl.synthetic import generate

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    db = out / "predictions.sqlite"
    if db.exists():
        db.unlink()  # a backtest run writes a fresh immutable store
    t0 = time.time()
    print("generating synthetic league (SYNTHETIC origin; always BLOCKED)...", flush=True)
    league = generate(seed=args.seed, odds_sims=args.odds_sims)
    store = league.store
    preds = PredictionStore(db)
    cfg = BacktestConfig(season=2025, start=date.fromisoformat(args.start), end=date.fromisoformat(args.end), n_sims=args.n_sims)
    print(f"walk-forward {cfg.start}..{cfg.end}", flush=True)
    summary = run_walk_forward(store, league.rosters, cfg, preds, progress=True)
    print(f"  {summary['games_priced']} games, {summary['artifacts']} artifacts, {len(summary['errors'])} errors", flush=True)
    report = evaluate(store, preds, reps=args.bootstrap)
    ok, head = preds.verify_chain()
    report["artifact_chain"] = {"verified": ok, "head": head, "rows": preds.count()}
    report["walk_forward"] = summary
    (out / "backtest_report.json").write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n")

    # Calibration table for ML (canonical HOME side), from stored artifacts + results.
    import numpy as np

    results = {r.game_id: r for r in store.results}
    ys, ps = [], []
    for r in preds.rows("WHERE market = 'ML' AND selection = 'HOME'"):
        res = results.get(r["game_id"])
        if res:
            ys.append(1.0 if res.winner == "HOME" else 0.0)
            ps.append(r["model_probability"])
    with (out / "calibration_ML.csv").open("w", newline="") as h:
        w = csv.DictWriter(h, fieldnames=["bin_lo", "bin_hi", "n", "mean_p", "hit_rate"], lineterminator="\n")
        w.writeheader()
        w.writerows(calibration_table(np.array(ys), np.array(ps)))

    # Desk + pricing sample for one slate day.
    days = slate_days([g for g in store.games if g.season == 2025])
    desk_day = date.fromisoformat(args.desk_date)
    games = days[desk_day]
    as_of = min(g.start_time for g in games) - timedelta(minutes=60)
    run = run_slate(store, league.rosters, 2025, games, as_of, PredictionMode.BACKTEST, n_sims=args.n_sims * 4, seed="desk")
    paths = export_desk(out / f"desk_{desk_day}", str(desk_day), run.artifacts, run.priced, run.health)
    sample = out / "pricing_sample.csv"
    with sample.open("w", newline="") as h:
        cols = ["game_id", "matchup", "puck_drop", "goalies", "p_home_ml", "p_reg_home", "p_reg_draw", "p_reg_away",
                "p_ot_or_so", "e_total", "p_home_-1.5", "p_over_5.5", "p_over_6.5"]
        w = csv.DictWriter(h, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        from nhl.contracts import MarketType, Selection

        for p in run.priced:
            gp = p.pricing
            w.writerow({
                "game_id": p.game.game_id, "matchup": f"{p.game.away}@{p.game.home}", "puck_drop": p.game.start_time.isoformat(),
                "goalies": p.goalie_state, "p_home_ml": round(gp.p_home_ml, 4),
                "p_reg_home": round(gp.p_reg[0], 4), "p_reg_draw": round(gp.p_reg[1], 4), "p_reg_away": round(gp.p_reg[2], 4),
                "p_ot_or_so": round(gp.p_ot + gp.p_so, 4), "e_total": round(gp.mean_total, 3),
                "p_home_-1.5": round(gp.outcome_probs(MarketType.PUCK_LINE, Selection.HOME, -1.5)[0], 4),
                "p_over_5.5": round(gp.outcome_probs(MarketType.TOTAL, Selection.OVER, 5.5)[0], 4),
                "p_over_6.5": round(gp.outcome_probs(MarketType.TOTAL, Selection.OVER, 6.5)[0], 4),
            })
    print(f"desk -> {paths['text']}")
    print(f"report -> {out / 'backtest_report.json'}  ({time.time() - t0:.0f}s)")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    ok, info = PredictionStore(args.db).verify_chain()
    print(("OK head=" if ok else "BROKEN: ") + info)
    return 0 if ok else 1


def cmd_fetch_schedule(args: argparse.Namespace) -> int:
    from nhl.data.nhl_api import NHLApiClient, parse_schedule
    from nhl.data.snapshots import RawSnapshotStore

    store = RawSnapshotStore(args.store)
    entry = NHLApiClient(store).fetch_schedule(args.date)
    for g in parse_schedule(store.get_json(entry), entry):
        print(g.game_id, g.start_time.isoformat(), f"{g.away}@{g.home}")
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    from nhl.data.ingest import capture_day
    from nhl.data.nhl_api import NHLApiClient
    from nhl.data.snapshots import RawSnapshotStore

    client = NHLApiClient(RawSnapshotStore(args.store))
    teams = [t.strip() for t in args.rosters.split(",") if t.strip()] if args.rosters else []
    for e in capture_day(client, date.fromisoformat(args.date), roster_teams=teams, season=args.season):
        print(e.snapshot_id, e.key, e.fetched_at.isoformat())
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    from nhl.data.ingest import replay
    from nhl.data.snapshots import RawSnapshotStore

    store, rep = replay(RawSnapshotStore(args.store))
    paths = rep.write(args.out)
    print(json.dumps({"games": len(store.games), "results": len(store.results), "odds": len(store.odds), **paths}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nhl")
    sub = parser.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo", help="synthetic end-to-end walk-forward + desk")
    d.add_argument("--out", default="out/demo")
    d.add_argument("--start", default="2025-10-20")
    d.add_argument("--end", default="2026-04-15")
    d.add_argument("--desk-date", default="2026-01-20")
    d.add_argument("--n-sims", type=int, default=3000)
    d.add_argument("--odds-sims", type=int, default=1500)
    d.add_argument("--bootstrap", type=int, default=300)
    d.add_argument("--seed", type=int, default=7)
    d.set_defaults(fn=cmd_demo)
    v = sub.add_parser("verify", help="verify the prediction artifact hash chain")
    v.add_argument("--db", required=True)
    v.set_defaults(fn=cmd_verify)
    f = sub.add_parser("fetch-schedule", help="fetch + store + parse the NHL schedule (network)")
    f.add_argument("--date", required=True)
    f.add_argument("--store", default="store")
    f.set_defaults(fn=cmd_fetch_schedule)
    c = sub.add_parser("capture", help="live capture: schedule, finals, optional rosters (network)")
    c.add_argument("--date", required=True)
    c.add_argument("--store", default="store")
    c.add_argument("--rosters", default="", help="comma-separated team codes")
    c.add_argument("--season", type=int, default=None)
    c.set_defaults(fn=cmd_capture)
    r = sub.add_parser("replay", help="rebuild a provenanced store from raw snapshots + ingest report")
    r.add_argument("--store", default="store")
    r.add_argument("--out", default="out/ingest")
    r.set_defaults(fn=cmd_replay)
    args = parser.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
