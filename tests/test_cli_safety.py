import pytest

from nhl.cli import main


def test_research_commands_require_explicit_synthetic_flag(tmp_path):
    with pytest.raises(SystemExit) as e:
        main(["backtest", "--out", str(tmp_path / "bt")])
    assert "--synthetic" in str(e.value)


def test_simulated_clock_requires_explicit_flags(tmp_path):
    base = ["price-slate", "--forward-root", str(tmp_path / "f"), "--store", str(tmp_path / "s"), "--date", "2026-10-07",
            "--season", "2026"]
    with pytest.raises(SystemExit) as e:
        main(base + ["--at", "2026-10-07T12:00:00Z"])
    assert "--simulated-clock" in str(e.value)
    with pytest.raises(SystemExit):
        main(base + ["--simulated-clock"])
    with pytest.raises(SystemExit) as e:
        main(base + ["--allow-non-causal"])
    assert "non-causal" in str(e.value)


def test_existing_outputs_are_not_overwritten_without_flag(tmp_path):
    out = tmp_path / "drill"
    out.mkdir()
    (out / "keep.txt").write_text("x")
    with pytest.raises(SystemExit) as e:
        main(["forward-drill", "--synthetic", "--out", str(out)])
    assert "--overwrite" in str(e.value) and (out / "keep.txt").exists()


def test_leakage_audit_cli_passes():
    assert main(["leakage-audit", "--synthetic", "--days", "2025-11-12", "--n-sims", "300"]) == 0
