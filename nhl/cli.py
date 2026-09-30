"""NHL Desk operational CLI. Safe by default; unsafe modes need explicit flags.

Capture (network; raw immutable snapshots):
  nhl capture-schedule --date D --store S
  nhl capture-data     --date D --store S            (final play-by-play + boxscores = results)
  nhl capture-rosters  --teams TOR,MTL --season 2026 --store S
  nhl capture-goalies  --provider-dir DIR --provider NAME --date D --store S
  nhl capture-odds     --provider-dir DIR --provider NAME --date D --store S
State / parameters:
  nhl replay           --store S --out OUT
  nhl fit-params       --store S --season 2026 --out ROOT
Forward (shadow) lifecycle, one invocation per step:
  nhl price-slate      --forward-root F --store S --date D --season 2026
  nhl forward-cycle    --forward-root F --store S --date D --season 2026 [--provider-dir ...]
  nhl capture-close    --forward-root F --store S --date D
  nhl ingest-results   --date D --store S           (alias of capture-data)
  nhl grade | clv      --forward-root F --store S --out OUT
  nhl verify           --forward-root F | --db PRED.sqlite
  nhl evaluate-gates   --forward-root F --store S --out OUT
  nhl export-desk      --forward-root F --store S --date D --out OUT
Research:
  nhl backtest --synthetic --out OUT                 (alias: demo)
  nhl leakage-audit --synthetic --days 2025-11-12,2026-02-03
  nhl forward-drill --synthetic --out OUT            (simulated-clock lifecycle; DEV lane)

Unsafe flags (never defaults): --allow-non-causal (permissive view; every artifact is
hard-blocked NON_STRICT_VIEW), --simulated-clock --at TS (DEV lane, hard-blocked),
--overwrite (replace an existing output store).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from nhl.timeutil import parse_ts, utcnow


# --------------------------------------------------------------------------- helpers

def _fresh_dir(path: Path, overwrite: bool, what: str) -> None:
    if path.exists() and any(path.iterdir()):
        if not overwrite:
            raise SystemExit(f"refusing to reuse non-empty {what} {path}; pass --overwrite to replace it")
        import shutil

        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def _clock(args: argparse.Namespace):
    if getattr(args, "simulated_clock", False):
        if not args.at:
            raise SystemExit("--simulated-clock requires --at <UTC timestamp>")
        t = parse_ts(args.at)
        return (lambda: t), True
    if getattr(args, "at", None):
        raise SystemExit("--at is only allowed together with --simulated-clock")
    return utcnow, False


def _runner(args: argparse.Namespace):
    from nhl.forward.runner import ForwardRunner, ReplayStateSource

    clock, simulated = _clock(args)
    return ForwardRunner(args.forward_root, ReplayStateSource(Path(args.store)), clock=clock, simulated_clock=simulated,
                         n_sims=getattr(args, "n_sims", None))


def _client(store: str):
    from nhl.data.nhl_api import NHLApiClient
    from nhl.data.snapshots import RawSnapshotStore

    return NHLApiClient(RawSnapshotStore(store))


def _print(obj) -> int:
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))
    return 0


# --------------------------------------------------------------------------- capture

def cmd_capture_schedule(a):
    e = _client(a.store).fetch_schedule(a.date)
    return _print({"snapshot_id": e.snapshot_id, "key": e.key, "fetched_at": e.fetched_at})


def cmd_capture_data(a):
    from nhl.data.ingest import capture_day

    return _print([e.key for e in capture_day(_client(a.store), date.fromisoformat(a.date))])


def cmd_capture_rosters(a):
    c = _client(a.store)
    return _print([c.fetch_roster(t.strip(), a.season).key for t in a.teams.split(",") if t.strip()])


def cmd_capture_provider(a, kind: str):
    from nhl.data.snapshots import RawSnapshotStore
    from nhl.market.providers import FileProvider, capture_goalie_reports, capture_odds

    prov = FileProvider(a.provider, a.provider_dir)
    fn = capture_odds if kind == "odds" else capture_goalie_reports
    e = fn(prov, RawSnapshotStore(a.store), date.fromisoformat(a.date))
    return _print({"snapshot_id": e.snapshot_id, "key": e.key, "fetched_at": e.fetched_at})


def cmd_replay(a):
    from nhl.data.ingest import replay
    from nhl.data.snapshots import RawSnapshotStore

    store, rep = replay(RawSnapshotStore(a.store))
    return _print({"games": len(store.games), "results": len(store.results), "odds": len(store.odds), **rep.write(a.out)})


def cmd_fit_params(a):
    from nhl.forward.runner import ReplayStateSource
    from nhl.features.league_constants import LeagueConstantsStore, fit_league_constants

    store, _ = ReplayStateSource(Path(a.store))(utcnow())
    c = fit_league_constants(store, a.season)
    return _print({"constants_id": c.constants_id, "path": str(LeagueConstantsStore(a.out).save(c)),
                   "train_seasons": c.train_seasons, "sources": c.sources})


# --------------------------------------------------------------------------- forward

def cmd_price_slate(a):
    if a.allow_non_causal:
        raise SystemExit("--allow-non-causal is only supported for research backtests, not forward pricing")
    return _print(_runner(a).price(date.fromisoformat(a.date), a.season))


def cmd_forward_cycle(a):
    from nhl.data.snapshots import RawSnapshotStore
    from nhl.market.providers import FileProvider, capture_goalie_reports, capture_odds

    runner = _runner(a)
    day = date.fromisoformat(a.date)
    captured = []
    if a.provider_dir:
        raw = RawSnapshotStore(a.store)
        captured.append(capture_odds(FileProvider(a.provider, Path(a.provider_dir) / "odds"), raw, day, runner.clock()).key)
        gdir = Path(a.provider_dir) / "goalies"
        if gdir.exists():
            captured.append(capture_goalie_reports(FileProvider(a.provider, gdir), raw, day, runner.clock()).key)
    return _print({"captured": captured, "price": runner.price(day, a.season), "verify": runner.verify()})


def cmd_capture_close(a):
    return _print(_runner(a).capture_close(date.fromisoformat(a.date)))


def cmd_grade_or_clv(a, which: str):
    from nhl.desk.reports import CLV_COLUMNS, GRADING_COLUMNS, write_csv

    rows = sorted(_runner(a).settle(), key=lambda r: r["prediction_id"])
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    name, cols = ("GRADING_REPORT.csv", GRADING_COLUMNS) if which == "grade" else ("CLV_REPORT.csv", CLV_COLUMNS)
    return _print({"rows": write_csv(out / name, rows, cols), "path": str(out / name)})


def cmd_verify(a):
    from nhl.ledger.predictions import PredictionStore

    if a.forward_root:
        from nhl.ledger.closes import CloseStore

        root = Path(a.forward_root)
        p_ok, p_head = PredictionStore(root / "predictions.sqlite").verify_chain()
        c_ok, c_head = CloseStore(root / "closes.sqlite").verify_chain()
        _print({"predictions_chain": p_ok, "predictions_head": p_head, "closes_chain": c_ok, "closes_head": c_head})
        return 0 if p_ok and c_ok else 1
    ok, info = PredictionStore(a.db).verify_chain()
    print(("OK head=" if ok else "BROKEN: ") + info)
    return 0 if ok else 1


def cmd_evaluate_gates(a):
    from nhl.backtest.evaluate import evaluate

    runner = _runner(a)
    store, _ = runner.state(runner.clock())
    v = runner.verify()
    rep = evaluate(store, runner.predictions, mode="FORWARD", reps=a.bootstrap,
                   integrity={"ARTIFACT_CHAIN_VERIFIED": v["predictions_chain"] and v["closes_chain"]})
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "GATES.json").write_text(json.dumps(rep, indent=2, sort_keys=True, default=str) + "\n")
    return _print({k: v["passed"] for k, v in rep["gates"].items()})


def cmd_export_desk(a):
    return _print(_runner(a).export(a.out, date.fromisoformat(a.date) if a.date else None))


# --------------------------------------------------------------------------- research

def _require_synthetic(a):
    if not a.synthetic:
        raise SystemExit("research commands currently need --synthetic (no real historical store is available); "
                         "the flag makes the SYNTHETIC/DEV nature explicit")


def cmd_backtest(a):
    import numpy as np

    from nhl.backtest.evaluate import evaluate
    from nhl.backtest.leakage import audit
    from nhl.backtest.metrics import calibration_table
    from nhl.backtest.walk_forward import BacktestConfig, run_walk_forward, slate_days
    from nhl.contracts import PredictionMode
    from nhl.desk.board import export_desk
    from nhl.features.league_constants import LeagueConstantsStore
    from nhl.ledger.closes import CloseStore
    from nhl.ledger.predictions import PredictionStore
    from nhl.pipeline import run_slate
    from nhl.synthetic import generate

    _require_synthetic(a)
    out = Path(a.out)
    _fresh_dir(out, a.overwrite, "backtest output")
    t0 = time.time()
    print("generating synthetic league (SYNTHETIC origin; always BLOCKED)...", flush=True)
    league = generate(seed=a.seed, odds_sims=a.odds_sims)
    store = league.store
    preds = PredictionStore(out / "predictions.sqlite")
    cfg = BacktestConfig(season=2025, start=date.fromisoformat(a.start), end=date.fromisoformat(a.end), n_sims=a.n_sims)
    print(f"walk-forward {cfg.start}..{cfg.end}", flush=True)
    summary = run_walk_forward(store, league.rosters, cfg, preds, progress=True, constants_store=LeagueConstantsStore(out))
    print(f"  {summary['games_priced']} games, {summary['artifacts']} artifacts, {len(summary['errors'])} errors", flush=True)
    leak = audit(store, league.rosters, 2025, [date(2025, 11, 12), date(2026, 2, 3)], n_sims=600)
    ok, head = preds.verify_chain()
    closes = CloseStore(out / "closes.sqlite")
    rep = evaluate(store, preds, reps=a.bootstrap, close_store=closes, captured_at="backtest",
                   integrity={"ARTIFACT_CHAIN_VERIFIED": ok, "LEAKAGE_EQUIVALENCE": all(x["status"] == "EQUIVALENT" for x in leak)})
    rep["artifact_chain"] = {"verified": ok, "head": head, "rows": preds.count()}
    rep["closes_chain"] = closes.verify_chain()[0]
    rep["leakage_audit"] = leak
    rep["walk_forward"] = summary
    (out / "backtest_report.json").write_text(json.dumps(rep, indent=2, sort_keys=True, default=str) + "\n")
    results = {r.game_id: r for r in store.results}
    ys, ps = [], []
    for r in preds.rows("WHERE market = 'ML' AND selection = 'HOME'"):
        if r["game_id"] in results:
            ys.append(1.0 if results[r["game_id"]].winner == "HOME" else 0.0)
            ps.append(r["model_probability"])
    with (out / "calibration_ML.csv").open("w", newline="") as h:
        w = csv.DictWriter(h, fieldnames=["bin_lo", "bin_hi", "n", "mean_p", "hit_rate"], lineterminator="\n")
        w.writeheader()
        w.writerows(calibration_table(np.array(ys), np.array(ps)))
    days = slate_days([g for g in store.games if g.season == 2025])
    games = days[date.fromisoformat(a.desk_date)]
    as_of = min(g.start_time for g in games) - timedelta(minutes=60)
    run = run_slate(store, league.rosters, 2025, games, as_of, PredictionMode.BACKTEST, n_sims=a.n_sims * 4, seed="desk",
                    created_at=as_of)
    export_desk(out / f"desk_{a.desk_date}", a.desk_date, run.artifacts, run.priced, run.health)
    print(f"report -> {out / 'backtest_report.json'}  ({time.time() - t0:.0f}s)")
    return 0


def cmd_leakage_audit(a):
    from nhl.backtest.leakage import audit
    from nhl.synthetic import generate

    _require_synthetic(a)
    lg = generate(odds_sims=150)
    res = audit(lg.store, lg.rosters, 2025, [date.fromisoformat(d) for d in a.days.split(",")], n_sims=a.n_sims)
    _print(res)
    return 0 if all(r["status"] in ("EQUIVALENT", "NO_GAMES") for r in res) else 1


def cmd_forward_drill(a):
    """Simulated-clock shadow-forward lifecycle on synthetic data -> all desk reports.
    Everything produced is DEV_SYNTHETIC and hard-blocked (SIMULATED_CLOCK + SYNTHETIC)."""

    from nhl.backtest.walk_forward import slate_days
    from nhl.contracts import GoalieReport, GoalieState
    from nhl.data.pit import HistoricalStore
    from nhl.forward.runner import ForwardRunner, StaticStateSource
    from nhl.governance.overrides import Override
    from nhl.synthetic import generate

    _require_synthetic(a)
    out = Path(a.out)
    _fresh_dir(out, a.overwrite, "drill output")
    lg = generate(odds_sims=a.odds_sims)
    day = date.fromisoformat(a.date)
    games = slate_days([g for g in lg.store.games if g.season == 2025])[day]
    ids = {g.game_id for g in games}
    s = lg.store
    store = HistoricalStore(games=s.games, results=s.results, team_stats=s.team_stats, goalie_stats=s.goalie_stats,
                            goalie_reports=[r for r in s.goalie_reports if r.game_id not in ids], odds=s.odds,
                            player_seasons=s.player_seasons, data_origin=s.data_origin)
    later = [r for r in s.goalie_reports if r.game_id in ids]  # released as the day progresses
    first = min(g.start_time for g in games)
    t = {"now": first - timedelta(hours=9)}
    runner = ForwardRunner(out / "forward", StaticStateSource(store, lg.rosters), clock=lambda: t["now"],
                           simulated_clock=True, n_sims=a.n_sims)
    log = []
    for offset in (timedelta(hours=9), timedelta(hours=8, minutes=30), timedelta(hours=2, minutes=30), timedelta(minutes=70)):
        t["now"] = first - offset
        store.goalie_reports[:] = [r for r in s.goalie_reports if r.game_id not in ids] + [r for r in later if r.available_at <= t["now"]]
        log.append(runner.price(day, 2025))
    # A scratch arrives late for one team: explicit state change -> reprice.
    named = sorted((r for r in later if r.state is GoalieState.CONFIRMED), key=lambda r: (r.game_id, r.team))
    if named:
        r0 = named[0]
        store.goalie_reports.append(GoalieReport(r0.game_id, r0.team, r0.goalie_id, GoalieState.SCRATCHED, "team",
                                                 first - timedelta(minutes=50)))
        t["now"] = first - timedelta(minutes=40)
        log.append(runner.price(day, 2025))
    first_row = next(runner.predictions.rows())
    runner.overrides.record(Override(first_row["prediction_id"], "DEMOTE", "drill-operator", "demonstration of logged demotion",
                                     (first - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")))
    runner.overrides.record(Override(first_row["prediction_id"], "PROMOTE_REQUEST", "drill-operator", "demonstration: refused",
                                     (first - timedelta(minutes=29)).strftime("%Y-%m-%dT%H:%M:%SZ")), gates_passed_for_market=False)
    t["now"] = max(g.start_time for g in games) + timedelta(hours=5)
    log.append(runner.capture_close(day))
    paths = runner.export(out / "reports", day)
    paths2 = runner.export(out / "reports_rerun", day)
    same = all(Path(paths[k]).read_bytes() == Path(paths2[k]).read_bytes() for k in paths)
    return _print({"cycles": log, "verify": runner.verify(), "deterministic_export": same, "reports": paths})


# --------------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="nhl", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_, *specs):
        p = sub.add_parser(name, help=help_)
        for flags, kw in specs:
            p.add_argument(*flags, **kw)
        p.set_defaults(fn=fn)
        return p

    store = (("--store",), {"default": "store"})
    date_ = (("--date",), {"required": True})
    fwd = (("--forward-root",), {"required": True})
    season = (("--season",), {"type": int, "required": True})
    out = (("--out",), {"required": True})
    sim = [(("--simulated-clock",), {"action": "store_true"}), (("--at",), {"default": None}),
           (("--n-sims",), {"type": int, "default": None})]
    provider = [(("--provider-dir",), {"required": True}), (("--provider",), {"required": True})]

    add("capture-schedule", cmd_capture_schedule, "fetch + store the schedule (network)", date_, store)
    add("capture-data", cmd_capture_data, "fetch schedule + final pbp/boxscores (network)", date_, store)
    add("ingest-results", cmd_capture_data, "alias of capture-data", date_, store)
    add("capture-rosters", cmd_capture_rosters, "fetch roster snapshots (network)", (("--teams",), {"required": True}), season, store)
    add("capture-goalies", lambda a: cmd_capture_provider(a, "goalies"), "store a goalie-report provider file", *provider, date_, store)
    add("capture-odds", lambda a: cmd_capture_provider(a, "odds"), "store an odds provider file (market-v2)", *provider, date_, store)
    add("replay", cmd_replay, "rebuild a provenanced store + ingest report", store, out)
    add("fit-params", cmd_fit_params, "fit + persist walk-forward league constants", store, season, out)
    add("price-slate", cmd_price_slate, "price not-started games at now (FORWARD)", fwd, store, date_, season, *sim,
        (("--allow-non-causal",), {"action": "store_true"}))
    add("forward-cycle", cmd_forward_cycle, "capture provider files then price", fwd, store, date_, season, *sim,
        (("--provider-dir",), {"default": None}), (("--provider",), {"default": "file"}))
    add("capture-close", cmd_capture_close, "persist close-v1 selections for started games", fwd, store, date_, *sim)
    add("grade", lambda a: cmd_grade_or_clv(a, "grade"), "grading report", fwd, store, out, *sim)
    add("clv", lambda a: cmd_grade_or_clv(a, "clv"), "CLV report", fwd, store, out, *sim)
    add("verify", cmd_verify, "verify artifact/close hash chains", (("--forward-root",), {"default": None}), (("--db",), {"default": None}))
    add("evaluate-gates", cmd_evaluate_gates, "market-by-market evaluation + gates (FORWARD)", fwd, store, out, *sim,
        (("--bootstrap",), {"type": int, "default": 300}))
    add("export-desk", cmd_export_desk, "deterministic desk reports", fwd, store, out, *sim, (("--date",), {"default": None}))
    bt = [(("--synthetic",), {"action": "store_true"}), (("--out",), {"default": "out/demo"}),
          (("--start",), {"default": "2025-10-20"}), (("--end",), {"default": "2026-04-15"}),
          (("--desk-date",), {"default": "2026-01-20"}), (("--n-sims",), {"type": int, "default": 3000}),
          (("--odds-sims",), {"type": int, "default": 1500}), (("--bootstrap",), {"type": int, "default": 300}),
          (("--seed",), {"type": int, "default": 7}), (("--overwrite",), {"action": "store_true"})]
    add("backtest", cmd_backtest, "synthetic walk-forward + evaluation + desk", *bt)
    add("demo", cmd_backtest, "alias of backtest", *bt)
    add("leakage-audit", cmd_leakage_audit, "full vs truncated store equivalence", (("--synthetic",), {"action": "store_true"}),
        (("--days",), {"default": "2025-11-12,2026-02-03"}), (("--n-sims",), {"type": int, "default": 600}))
    add("forward-drill", cmd_forward_drill, "simulated-clock lifecycle -> desk reports (DEV lane)",
        (("--synthetic",), {"action": "store_true"}), (("--out",), {"default": "out/drill"}),
        (("--date",), {"default": "2026-01-20"}), (("--n-sims",), {"type": int, "default": 3000}),
        (("--odds-sims",), {"type": int, "default": 1500}), (("--overwrite",), {"action": "store_true"}))
    args = ap.parse_args(argv)
    if args.cmd == "verify" and not (args.forward_root or args.db):
        ap.error("verify needs --forward-root or --db")
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
