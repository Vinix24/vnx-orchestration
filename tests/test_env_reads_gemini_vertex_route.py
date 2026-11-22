"""config-een-oppervlak PR-Q2: the Vertex route of the Gemini gate is gone.

``vertex_ai_runner.py``, the Vertex branch of ``gate_runner.py`` and the four names
``VNX_GEMINI_ROUTING`` / ``VNX_VERTEX_PROJECT`` / ``VNX_VERTEX_REGION`` / ``VNX_VERTEX_MODEL``
no longer exist. The Gemini gate stays on the Gemini CLI.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import gate_recorder as _rec
from gate_runner import GateRunner

DELETED_NAMES = (
    "VNX_GEMINI_ROUTING",
    "VNX_VERTEX_PROJECT",
    "VNX_VERTEX_REGION",
    "VNX_VERTEX_MODEL",
)


def test_no_tracked_source_names_a_deleted_variable():
    proc = subprocess.run(
        ["git", "grep", "-nwE", "|".join(DELETED_NAMES), "--",
         "scripts", "vnx_cli", "dashboard", "bin", "hooks", "tests", ":!tests/test_env_reads_gemini_vertex_route.py"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    # exit 1 = no match (the wanted outcome); 0 = a hit; anything else = grep itself failed.
    assert proc.returncode == 1, f"rc={proc.returncode}\n{proc.stdout}{proc.stderr}"


def test_the_vertex_runner_module_is_gone():
    assert not (REPO_ROOT / "scripts" / "lib" / "vertex_ai_runner.py").exists()


def test_gate_runner_has_no_vertex_path():
    assert not hasattr(GateRunner, "_run_vertex_path")
    assert not hasattr(GateRunner, "_run_vertex_ai")


def test_gemini_gate_resolves_to_the_cli_path():
    assert _rec.resolve_gate_provider("gemini_review") == (_rec.GATE_PROVIDER_PATH_BINARY, "gemini")


@pytest.mark.parametrize("name", DELETED_NAMES)
def test_a_set_variable_still_runs_the_cli_path(monkeypatch, tmp_path, name):
    monkeypatch.setenv(name, "vertex")
    state_dir, reports_dir = tmp_path / "state", tmp_path / "reports"
    state_dir.mkdir()
    reports_dir.mkdir()
    runner = GateRunner(state_dir=state_dir, reports_dir=reports_dir)
    called = {}

    def fake_subprocess_path(self, **kwargs):
        called["binary"] = kwargs["binary"]
        return {"status": "ran"}

    monkeypatch.setattr(GateRunner, "_run_subprocess_path", fake_subprocess_path)
    monkeypatch.setattr(GateRunner, "_build_gemini_prompt", lambda self, payload: "prompt")
    monkeypatch.setattr("gate_runner.shutil.which", lambda b: f"/usr/bin/{b}")
    monkeypatch.setattr(GateRunner, "_refuse_if_switched_off", lambda self, *a, **k: None)
    monkeypatch.setattr(GateRunner, "_mark_executing", lambda self, *a, **k: None)
    result = runner.run(gate="gemini_review", request_payload={"pr_number": 1, "branch": "b"}, pr_number=1, pr_id="PR-1")
    assert called.get("binary") == "gemini", result
