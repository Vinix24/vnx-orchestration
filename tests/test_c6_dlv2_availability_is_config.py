"""C6 step 2 — availability measures configuration, not file existence.

dispatch 20260911-c6, track ``gate-runner-provider-agnostisch``. The defect:
``_kimi_gate_available``/``_glm_gate_available``/``_deepseek_gate_available``
(gate_request_handler) and ``gate_runner.run`` line 193 all decided
availability by ``<runner_path>.exists()``. That makes availability a
statement about a FILE, which is wrong in two directions:

- a config-based gate (``harness_lane``) is available with NO file at all;
  measuring one made ``deepseek_gate`` a dead link (OI-1714) — the ONE
  provider that was actually working, registered as a script runner at
  ``scripts/deepseek_gate.py`` which has never existed;
- an unknown gate key books ``unavailable`` silently instead of failing loud.

The fix centralises availability in ``gate_recorder.gate_is_available``:
availability is decided by REGISTRATION plus lane executability — a
``harness_lane`` gate is available without a file, a ``script_runner``/
``path_binary`` gate keeps its file/binary check, and an unknown key raises
``UnknownGateProvider``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import gate_recorder as _rec
import gate_request_handler
from gate_request_handler import GateRequestHandlerMixin


# ---------------------------------------------------------------------------
# 1. A config-based gate is available without a file (RED on current main)
# ---------------------------------------------------------------------------


def test_config_based_gate_is_available_without_a_file():
    """Behavioral red/green pin (klaar-wanneer #1).

    On main, ``_deepseek_gate_available`` measured
    ``scripts/deepseek_gate.py``.exists(), which is False, so deepseek_gate
    was booked unavailable despite being a config-based harness lane. After
    the fix the same call returns True while the file still does not exist.
    """
    assert not (VNX_ROOT / "scripts" / "deepseek_gate.py").exists(), (
        "deepseek_gate must not grow a runner file — its availability is "
        "configuration, not a script"
    )

    assert GateRequestHandlerMixin()._deepseek_gate_available() is True


@pytest.fixture
def manager_env(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    data_dir = project_root / ".vnx-data"
    state_dir = data_dir / "state"
    reports_dir = data_dir / "unified_reports"
    for d in (
        state_dir / "review_gates" / "requests",
        state_dir / "review_gates" / "results",
        reports_dir,
    ):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("VNX_HOME", str(VNX_ROOT))
    monkeypatch.setenv("PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_REPORTS_DIR", str(reports_dir))
    monkeypatch.setenv("VNX_DISPATCH_DIR", str(data_dir / "dispatches"))
    monkeypatch.setenv("VNX_LOGS_DIR", str(data_dir / "logs"))
    monkeypatch.setenv("VNX_PIDS_DIR", str(data_dir / "pids"))
    monkeypatch.setenv("VNX_LOCKS_DIR", str(data_dir / "locks"))
    monkeypatch.setenv("VNX_DB_DIR", str(data_dir / "database"))
    return {
        "project_root": project_root,
        "state_dir": state_dir,
        "requests_dir": state_dir / "review_gates" / "requests",
        "results_dir": state_dir / "review_gates" / "results",
    }


def _make_manager():
    import review_gate_manager as rgm
    return rgm.ReviewGateManager()


def test_deepseek_request_books_requested_not_runner_missing(manager_env, monkeypatch):
    """OI-1714's request-time consequence: a deepseek_gate request is
    REQUESTED, never ``not_executable``/``gate_runner_missing``, because the
    gate is available by registration alone."""
    monkeypatch.setattr(gate_request_handler, "get_pr_head_sha", lambda pr_number: "e" * 40)
    manager = _make_manager()

    result = manager._dispatch_one_review(
        "deepseek_gate", pr_number=13, branch="feature/c6-availability",
        risk_class="low", changed_files=[], mode="per_pr",
        dispatch_id="c6-deepseek-available",
    )

    assert result["status"] == "requested"
    assert result.get("reason") is None


# ---------------------------------------------------------------------------
# 2. script_runner / path_binary without a file stay unavailable
# ---------------------------------------------------------------------------


def test_script_runner_without_file_is_still_unavailable(tmp_path, monkeypatch):
    """klaar-wanneer #2: a gate that DOES need a file keeps that check."""
    monkeypatch.setitem(
        _rec.GATE_PROVIDERS, "test_script_gate",
        (_rec.GATE_PROVIDER_SCRIPT_RUNNER, "scripts/test_script_gate.py"),
    )

    assert _rec.gate_is_available("test_script_gate", repo_root=tmp_path) is False

    (tmp_path / "scripts").mkdir(parents=True)
    (tmp_path / "scripts" / "test_script_gate.py").write_text("# runner", encoding="utf-8")
    assert _rec.gate_is_available("test_script_gate", repo_root=tmp_path) is True


def test_path_binary_without_binary_is_still_unavailable(monkeypatch):
    """klaar-wanneer #2: a path-binary gate needs its binary on PATH."""
    monkeypatch.setattr(_rec.shutil, "which", lambda _name: None)
    assert _rec.gate_is_available("codex_gate") is False

    monkeypatch.setattr(_rec.shutil, "which", lambda _name: "/usr/bin/codex")
    assert _rec.gate_is_available("codex_gate") is True


# ---------------------------------------------------------------------------
# 3. An unknown key fails LOUD
# ---------------------------------------------------------------------------


def test_unknown_gate_key_fails_loud():
    """klaar-wanneer #3: a typo in an operator-configured review stack must
    name the wrong key, never silently book an unavailable seat."""
    with pytest.raises(_rec.UnknownGateProvider, match="verzonnen_gate"):
        _rec.gate_is_available("verzonnen_gate")


# ---------------------------------------------------------------------------
# 4. The union (C6 merge of PR #1837 + #1838): all three chain links
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("gate,provider", [
    ("kimi_gate", "kimi"),
    ("glm_gate", "glm-harness"),
    ("deepseek_gate", "deepseek-harness"),
])
def test_all_three_chain_links_are_available_by_registration_alone(
    tmp_path, gate, provider,
):
    """The merge's klaar-wanneer #2: after the union, every link in the
    codex-outage takeover chain is harness-lane and available WITHOUT any
    file on disk.

    RED on either parent branch alone: on #1837 (dlv1) deepseek_gate was
    still a script runner at a path that has never existed (and
    ``gate_is_available`` did not exist yet at all); on #1838 (dlv2)
    kimi_gate/glm_gate were still script runners, so their registry kind was
    not ``harness_lane``. Only the union turns all three green at once.

    ``repo_root=tmp_path`` is the proof that no file is consulted: the empty
    tmp dir contains no runner script for ANY gate, yet a harness-lane gate
    is available by registration alone.
    """
    kind, name = _rec.GATE_PROVIDERS[gate]
    assert kind == _rec.GATE_PROVIDER_HARNESS_LANE
    assert name == provider
    assert _rec.gate_is_available(gate, repo_root=tmp_path) is True
