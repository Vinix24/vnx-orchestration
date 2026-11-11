"""The nightly beacon keeps a refused (or degraded) cycle visible, with its reason.

Real run_phase and real doctor check on tmp stores; only the cycle report is fixed.
Nothing touches ~/.vnx-data.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

import vnx_doctor  # noqa: E402

BEACON = "learning_loop_nightly_beacon.json"

REFUSED = {
    "status": "refused",
    "reason": "ledger_source_check: double_end",
    "ledger_verdict": {"status": "refused", "reason_code": "double_end"},
    "statistics": {},
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_LEARNING_LOOP_PERSIST", raising=False)
    monkeypatch.delenv("VNX_LEARNING_LOOP_CUTOFF", raising=False)
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
    state_dir = tmp_path / "vnx-data" / "vnx-dev" / "state"
    state_dir.mkdir(parents=True)
    paths = {
        "VNX_STATE_DIR": str(state_dir),
        "VNX_HOME": str(tmp_path / "repo"),
        "VNX_DATA_DIR": str(state_dir.parent),
        "PROJECT_ROOT": str(tmp_path / "repo"),
    }
    return state_dir, paths


def _run(state_dir: Path, report=None, error=None) -> int:
    import learning_loop
    import learning_loop_nightly

    cycle = {"side_effect": error} if error else {"return_value": report}
    with patch.object(learning_loop.LearningLoop, "daily_learning_cycle", **cycle), patch.object(
        learning_loop_nightly, "resolve_project_id", return_value="vnx-dev"
    ), patch.object(learning_loop_nightly, "_add_capture_audit"):
        return learning_loop_nightly.run_phase(state_dir)


def _beacon(state_dir: Path) -> dict:
    return json.loads((state_dir / BEACON).read_text(encoding="utf-8"))


def test_refused_report_lands_as_refused_with_reason(env):
    state_dir, _ = env
    assert _run(state_dir, dict(REFUSED)) == 0
    beacon = _beacon(state_dir)
    assert beacon["status"] == "refused"
    assert beacon["reason"] == "ledger_source_check: double_end"
    assert set(beacon["counts"].values()) == {0}


def test_doctor_warns_on_a_refused_beacon(env):
    state_dir, paths = env
    (state_dir / "conversation_analyzer.log").write_text("[x] run\n")
    assert _run(state_dir, dict(REFUSED)) == 0
    with patch("vnx_paths.resolve_project_id", return_value="vnx-dev"):
        (result,) = vnx_doctor.check_learning_loop_beacon(paths)
    assert result.status == vnx_doctor.WARN
    assert "'refused'" in result.message


def test_degraded_report_keeps_its_reason(env):
    state_dir, _ = env
    report = {"status": "degraded", "reason": "probe_health=degraded", "statistics": {}}
    assert _run(state_dir, report) == 0
    beacon = _beacon(state_dir)
    assert beacon["status"] == "degraded"
    assert beacon["reason"] == "probe_health=degraded"


def test_ok_report_has_no_reason(env):
    state_dir, _ = env
    assert _run(state_dir, {"status": "ok", "reason": "ignored", "statistics": {"receipts_read": 4}}) == 0
    beacon = _beacon(state_dir)
    assert beacon["status"] == "ok"
    assert beacon["reason"] is None
    assert beacon["counts"]["receipts_read"] == 4


def test_report_without_status_is_still_ok(env):
    state_dir, _ = env
    assert _run(state_dir, {"statistics": {}}) == 0
    beacon = _beacon(state_dir)
    assert beacon["status"] == "ok"
    assert beacon["reason"] is None


def test_exception_in_the_cycle_is_failed_without_reason(env):
    state_dir, _ = env
    assert _run(state_dir, error=RuntimeError("kapot")) == 1
    beacon = _beacon(state_dir)
    assert beacon["status"] == "failed"
    assert beacon["reason"] is None
    assert "kapot" in beacon["error"]
