"""OI-2699 / OI-2806: bin/vnx routes horizon plan-gate and deliverable to their own parsers."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN_VNX = REPO_ROOT / "bin" / "vnx"


def _run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "mode.json").write_text(
        json.dumps({"mode": "starter", "schema_version": 1}) + "\n", encoding="utf-8"
    )
    (tmp_path / "home").mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("VNX_")}
    env.pop("PYTHONPATH", None)
    env["HOME"] = str(tmp_path / "home")
    env["VNX_DATA_DIR"] = str(data_dir)
    env["VNX_DATA_DIR_EXPLICIT"] = "1"
    return subprocess.run(
        [str(BIN_VNX), *args],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_horizon_plan_gate_reaches_plan_gate_parser(tmp_path):
    result = _run(tmp_path, "horizon", "plan-gate", "--help")
    combined = result.stdout + result.stderr
    assert "invalid choice" not in combined, combined
    assert result.returncode == 0, combined
    assert "usage: vnx plan-gate" in result.stdout, combined
    assert "run" in result.stdout


def test_horizon_deliverable_reaches_deliverable_parser(tmp_path):
    result = _run(tmp_path, "horizon", "deliverable", "--help")
    combined = result.stdout + result.stderr
    assert "invalid choice" not in combined, combined
    assert result.returncode == 0, combined
    assert "usage: vnx deliverable" in result.stdout, combined


def test_horizon_help_still_shows_objective_parser(tmp_path):
    result = _run(tmp_path, "horizon", "--help")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "usage: vnx objective" in result.stdout, combined


def test_bin_vnx_help_names_impact_backtest():
    text = BIN_VNX.read_text()
    assert "impact collect|backtest|report" in text
