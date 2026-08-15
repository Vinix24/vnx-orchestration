#!/usr/bin/env python3
"""OI-1222: workflow validation gate tests.

Pins the ``workflow-validate`` gate in ``scripts/local-ci.sh`` and exercises
the ``check_workflows`` core: YAML parse layer, actionlint wiring, and the
hard failure (with install instruction) when actionlint is missing.
"""
from __future__ import annotations

import json
import shlex
import sys
import textwrap
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))
from check_workflows import (  # noqa: E402
    ACTIONLINT_INSTALL_HINT,
    check_workflows,
    find_actionlint,
    run_actionlint,
    validate_yaml,
    workflow_files,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _workflow(tmp_path: Path, body: str) -> Path:
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    f = wf_dir / "test.yml"
    f.write_text(textwrap.dedent(body), encoding="utf-8")
    return f


def _fake_actionlint(tmp_path: Path, stdout: str, exit_code: int = 1) -> Path:
    fake = tmp_path / "fake-actionlint"
    lines = ["#!/usr/bin/env bash"]
    if stdout:
        lines.append(f"printf '%s\\n' {shlex.quote(stdout)}")
    lines.append(f"exit {exit_code}")
    fake.write_text("\n".join(lines) + "\n", encoding="utf-8")
    fake.chmod(0o755)
    return fake


def _finding_json(filepath: str, message: str) -> str:
    return json.dumps(
        [
            {
                "filepath": filepath,
                "line": 1,
                "column": 1,
                "kind": "expression",
                "message": message,
                "snippet": "      - run: echo bad\n               ^~~",
            }
        ]
    )


# ---------------------------------------------------------------------------
# YAML layer
# ---------------------------------------------------------------------------


def test_validate_yaml_accepts_valid(tmp_path: Path) -> None:
    f = _workflow(
        tmp_path,
        """\
        name: CI
        on: [push]
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: echo hi
        """,
    )
    assert validate_yaml(f) == []


def test_validate_yaml_rejects_invalid(tmp_path: Path) -> None:
    f = _workflow(
        tmp_path,
        """\
        name: CI
        on: [push
          broken
        """,
    )
    errors = validate_yaml(f)
    assert len(errors) == 1
    assert "invalid YAML" in errors[0]


def test_validate_yaml_reports_unreadable_file(tmp_path: Path) -> None:
    f = tmp_path / ".github" / "workflows" / "missing.yml"
    errors = validate_yaml(f)
    assert len(errors) == 1
    assert "cannot read file" in errors[0]


# ---------------------------------------------------------------------------
# Workflow file discovery
# ---------------------------------------------------------------------------


def test_workflow_files_finds_yml_and_yaml(tmp_path: Path) -> None:
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    (wf_dir / "a.yml").write_text("name: a\n", encoding="utf-8")
    (wf_dir / "b.yaml").write_text("name: b\n", encoding="utf-8")
    (wf_dir / "c.txt").write_text("not a workflow\n", encoding="utf-8")
    names = [p.name for p in workflow_files(tmp_path)]
    assert names == ["a.yml", "b.yaml"]


def test_workflow_files_empty_when_dir_missing(tmp_path: Path) -> None:
    assert workflow_files(tmp_path) == []


def test_check_workflows_empty_when_no_workflows(tmp_path: Path) -> None:
    assert check_workflows(tmp_path) == []


# ---------------------------------------------------------------------------
# actionlint wiring
# ---------------------------------------------------------------------------


def test_check_workflows_reports_missing_actionlint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _workflow(tmp_path, "name: CI\non: [push]\njobs: {}\n")
    monkeypatch.setattr("check_workflows.find_actionlint", lambda: None)
    findings = check_workflows(tmp_path)
    assert any("[actionlint missing]" in f for f in findings)
    assert any("brew install actionlint" in f for f in findings)
    assert any(ACTIONLINT_INSTALL_HINT.splitlines()[0] in f for f in findings)


def test_run_actionlint_surfaces_findings(tmp_path: Path) -> None:
    f = _workflow(tmp_path, "name: CI\non: [push]\njobs: {}\n")
    fake = _fake_actionlint(
        tmp_path, _finding_json(".github/workflows/test.yml", "fake finding")
    )
    findings = run_actionlint([f.relative_to(tmp_path)], fake, cwd=tmp_path)
    assert len(findings) == 1
    assert ".github/workflows/test.yml:1:1" in findings[0]
    assert "fake finding" in findings[0]


def test_run_actionlint_empty_json_is_clean(tmp_path: Path) -> None:
    f = _workflow(tmp_path, "name: CI\non: [push]\njobs: {}\n")
    fake = _fake_actionlint(tmp_path, "[]", exit_code=0)
    assert run_actionlint([f.relative_to(tmp_path)], fake, cwd=tmp_path) == []


def test_check_workflows_surfaces_actionlint_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _workflow(tmp_path, "name: CI\non: [push]\njobs: {}\n")
    fake = _fake_actionlint(
        tmp_path, _finding_json(".github/workflows/test.yml", "fake finding")
    )
    monkeypatch.setattr("check_workflows.find_actionlint", lambda: fake)
    findings = check_workflows(tmp_path)
    assert any("fake finding" in f for f in findings)


# ---------------------------------------------------------------------------
# Gate registration pin (fails when the step is removed from local-ci.sh)
# ---------------------------------------------------------------------------


def test_local_ci_registers_workflow_validate_gate() -> None:
    script = (VNX_ROOT / "scripts" / "local-ci.sh").read_text(encoding="utf-8")
    assert 'run_gate "workflow-validate"' in script
    assert "check_workflows.py" in script


# ---------------------------------------------------------------------------
# Repo-level scan
# ---------------------------------------------------------------------------


def test_repo_workflows_pass_validation() -> None:
    if find_actionlint() is None:
        pytest.skip("actionlint not installed")
    findings = check_workflows(VNX_ROOT)
    if findings:
        pytest.fail(
            "workflow validation finding(s) in repo:\n" + "\n".join(findings)
        )
