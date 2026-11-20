"""PR-2b: the deliberation-panel budgets, decision-router switches and seat-ledger flag are gone."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "analysis"))

NAMES = [
    "VNX_PANEL_DISTILLATE_BUDGET",
    "VNX_PANEL_REPORT_BACKSTOP",
    "VNX_PANEL_SEAT_DISTILLATE_BUDGET",
    "VNX_DECISION_BACKEND",
    "VNX_DECISION_JUDGE_BACKEND",
    "VNX_DECISION_TIMEOUT",
    "VNX_SEAT_LEDGER",
]


def test_no_tracked_source_names_a_deleted_variable():
    proc = subprocess.run(
        ["git", "grep", "-nwE", "|".join(NAMES), "--", "scripts", "vnx_cli", "dashboard", "bin", "hooks"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 1, f"unexpected git grep result ({proc.returncode}): {proc.stdout}{proc.stderr}"
    assert proc.stdout == ""


def test_fixed_values(monkeypatch, tmp_path):
    for n in NAMES:
        monkeypatch.setenv(n, "1")
    import deliberation_panel as dp
    import llm_decision_router as lr

    assert (dp._DISTILLATE_BUDGET, dp._SEAT_DISTILLATE_BUDGET, dp._REPORT_BACKSTOP) == (12000, 12000, 40000)
    router = lr.DecisionRouter(data_dir=tmp_path)
    assert router.backend == lr.Backend.DRY_RUN
    assert router.timeout == 30
    assert router._shadow_judge() is lr._rule_based_decision


def test_seat_ledger_ignores_env(monkeypatch):
    monkeypatch.setenv("VNX_SEAT_LEDGER", "/nonexistent/ledger.ndjson")
    import plan_gate_panel_effectiveness as pg

    path = pg.resolve_seat_ledger_path(None)
    assert path is None or str(path) != "/nonexistent/ledger.ndjson"
