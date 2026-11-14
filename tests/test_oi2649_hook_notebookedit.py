"""OI-2649: the worker-scope hook treats NotebookEdit as a write-like tool."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HOOK_DIR = Path(__file__).resolve().parents[1] / "scripts" / "hooks"
sys.path.insert(0, str(HOOK_DIR))
import pretooluse_worker_scope_enforce as hook  # noqa: E402


@pytest.fixture
def wt(tmp_path, monkeypatch):
    root = tmp_path / "wt"
    (root / "tests").mkdir(parents=True)
    monkeypatch.setenv("VNX_ENFORCE_WORKER_PERMISSIONS", "1")
    monkeypatch.delenv("VNX_WORKER_ENFORCEMENT_SKIP", raising=False)
    monkeypatch.delenv("VNX_DISPATCH_PATHS", raising=False)
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    return root.resolve()


def _decide(monkeypatch, wt, role, tool, tool_input):
    monkeypatch.setenv("VNX_WORKER_ROLE", role)
    return hook.evaluate({"tool_name": tool, "tool_input": tool_input, "cwd": str(wt)})


def test_read_only_role_blocked_on_notebook_outside_scope(monkeypatch, wt):
    decision, reason = _decide(monkeypatch, wt, "code-reviewer", "NotebookEdit",
                               {"notebook_path": str(wt / "analysis.ipynb")})
    assert decision == "block"
    assert "analysis.ipynb" in reason and "code-reviewer" in reason


def test_code_worker_allowed_on_notebook_inside_scope(monkeypatch, wt):
    decision, _ = _decide(monkeypatch, wt, "quality-engineer", "NotebookEdit",
                          {"notebook_path": str(wt / "tests" / "demo.ipynb")})
    assert decision == "allow"


def test_code_worker_blocked_on_notebook_outside_scope(monkeypatch, wt):
    decision, _ = _decide(monkeypatch, wt, "quality-engineer", "NotebookEdit",
                          {"notebook_path": str(wt / "docs" / "demo.ipynb")})
    assert decision == "block"


def test_refused_policy_blocks_notebook_edit(monkeypatch, wt):
    def refuse(role):
        raise hook.PermissionPolicyRefused("refused", "source-empty")

    monkeypatch.setattr(hook, "resolve_worker_profile", refuse)
    decision, reason = _decide(monkeypatch, wt, "code-reviewer", "NotebookEdit",
                               {"notebook_path": str(wt / "tests" / "demo.ipynb")})
    assert decision == "block"
    assert "permission policy refused (source-empty)" in reason


def test_missing_path_matches_write_without_file_path(monkeypatch, wt):
    for bad in (None, "", 7):
        nb_input = {} if bad is None else {"notebook_path": bad}
        wr_input = {} if bad is None else {"file_path": bad}
        nb = _decide(monkeypatch, wt, "code-reviewer", "NotebookEdit", nb_input)
        wr = _decide(monkeypatch, wt, "code-reviewer", "Write", wr_input)
        assert nb == wr


def test_notebook_edit_ignores_file_path_key(monkeypatch, wt):
    # The path key is per tool: a stray file_path must not stand in for notebook_path.
    stray = _decide(monkeypatch, wt, "code-reviewer", "NotebookEdit",
                    {"file_path": str(wt / "x.ipynb")})
    assert stray == _decide(monkeypatch, wt, "code-reviewer", "NotebookEdit", {})


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit"])
def test_other_write_tools_unchanged(monkeypatch, wt, tool):
    out = _decide(monkeypatch, wt, "code-reviewer", tool, {"file_path": str(wt / "a.py")})
    assert out[0] == "block"
    ok = _decide(monkeypatch, wt, "quality-engineer", tool,
                 {"file_path": str(wt / "tests" / "a.py")})
    assert ok == ("allow", None)


@pytest.mark.parametrize("tool,tool_input", [
    ("Task", {"prompt": "x"}),
    ("mcp__example__tool", {"arg": 1}),
])
def test_task_and_mcp_stay_allowed_for_read_only_role(monkeypatch, wt, tool, tool_input):
    assert _decide(monkeypatch, wt, "code-reviewer", tool, tool_input) == ("allow", None)
