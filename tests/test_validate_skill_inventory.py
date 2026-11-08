"""validate_skill.py --inventory (skills-port SP-1): JSON on a fixture, --list unchanged."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "validate_skill.py"

IDENTICAL = ["fabric-reference", "horizon", "pm", "refactor-safe"]
ANNOUNCEMENT = [
    "api-developer", "backend-developer", "data-analyst", "excel-reporter", "frontend-developer",
    "monitoring-specialist", "performance-profiler", "planner", "python-optimizer", "quality-engineer",
    "reviewer", "test-engineer",
]
SEMANTIC = [
    "architect", "database-engineer", "debugger", "featureplan-kickoff", "intelligence-engineer",
    "security-engineer", "supabase-expert", "t0-orchestrator", "vnx-manager",
]
UNSAFE = ["architect", "database-engineer", "debugger", "intelligence-engineer", "security-engineer"]
TYPES = {"horizon": "planning", "planner": "planning", "t0-orchestrator": "orchestration",
         "fabric-reference": "reference"}

# Output of scripts/validate_skill.py --list on origin/main 8c96dc306 for _small_project().
LIST_GOLDEN = (
    "Valid VNX Skills\n\n" + "=" * 70 + "\n\nPLANNING\n" + "-" * 70 + "\n"
    "  @planner (plan) [OK]\n    -> Plans work\n\nREVIEW\n" + "-" * 70 + "\n"
    "  @reviewer [OK]\n\n" + "=" * 70 + "\nTotal: 3 skills\n"
)


def _write(tree: Path, name: str, body: str) -> None:
    (tree / name).mkdir(parents=True, exist_ok=True)
    (tree / name / "SKILL.md").write_text(body, encoding="utf-8")


def _registry(tree: Path, names: list[str]) -> None:
    lines = ["skills:"]
    for n in names:
        lines += [f"  {n}:", f'    name: "@{n}"', f"    type: {TYPES.get(n, 'implementation')}"]
    (tree / "skills.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _plan_fixture(root: Path) -> tuple[Path, Path]:
    """shipped tree (root/home/skills) and project tree (root/proj/.claude/skills) of the plan."""
    shipped, project = root / "home" / "skills", root / "proj" / ".claude" / "skills"
    for name in IDENTICAL:
        _write(shipped, name, f"body {name}\n")
        _write(project, name, f"body {name}\n")
    for name in ANNOUNCEMENT:
        _write(shipped, name, f"intro\n\U0001F527 Skill actief: {name}\nend\n")
        _write(project, name, f"intro\nSkill actief: {name}\nend\n")
    for name in SEMANTIC:
        dead = "Memory in ~/.claude/projects/p/MEMORY.md\n" if name in UNSAFE else "no home path\n"
        _write(shipped, name, f"intro\n{dead}")
        _write(project, name, "intro\nsomething else entirely\n")
    _write(project, "panel", "repo local\n")
    _registry(project, IDENTICAL + ANNOUNCEMENT + SEMANTIC)
    return shipped, project


def _small_project(root: Path) -> Path:
    project = root / "proj" / ".claude" / "skills"
    for name in ("planner", "reviewer", "extra"):
        _write(project, name, "x\n")
    (project / "skills.yaml").write_text(
        'skills:\n  planner:\n    name: "@planner"\n    type: planning\n    domain: plan\n'
        '    responsibilities: ["Plans work"]\n  reviewer:\n    name: "@reviewer"\n    type: review\n',
        encoding="utf-8",
    )
    return project


def _run(root: Path, project: Path, *args: str, script: Path = SCRIPT) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("VNX") and k != "PROJECT_ROOT"}
    env.update(PROJECT_ROOT=str(root / "proj"), VNX_HOME=str(root / "home"),
               VNX_SKILLS_DIR=str(project), VNX_DATA_DIR=str(root / "data"), VNX_DATA_DIR_GUARD="off")
    return subprocess.run([sys.executable, str(script), *args], env=env, capture_output=True, text=True)


def test_inventory_on_the_plan_fixture(tmp_path):
    _, project = _plan_fixture(tmp_path)
    result = _run(tmp_path, project, "--inventory")
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["provider_unsafe"] == UNSAFE
    parity = data["parity"]
    assert parity["semantic"] == 9
    assert (parity["identical"], parity["announcement_only"], parity["shared"]) == (4, 12, 25)
    assert parity["semantic_names"] == SEMANTIC
    assert parity["only_project"] == ["panel"]
    assert data["t0_layer"] == ["fabric-reference", "horizon", "planner", "t0-orchestrator"]
    assert set(data["t0_layer"]).isdisjoint(data["worker_layer"])


def test_clean_skill_is_not_flagged(tmp_path):
    _, project = _plan_fixture(tmp_path)
    assert "reviewer" not in json.loads(_run(tmp_path, project, "--inventory").stdout)["provider_unsafe"]


def test_inventory_fails_closed_without_a_shipped_tree(tmp_path):
    _, project = _plan_fixture(tmp_path)
    shutil.rmtree(tmp_path / "home" / "skills")
    result = _run(tmp_path, project, "--inventory")
    assert result.returncode == 1 and result.stdout == ""


def test_inventory_rejects_extra_arguments(tmp_path):
    _, project = _plan_fixture(tmp_path)
    result = _run(tmp_path, project, "--inventory", "extra")
    assert result.returncode == 2 and result.stdout == ""


def test_unknown_flags_still_fail_as_before(tmp_path):
    _, project = _plan_fixture(tmp_path)
    result = _run(tmp_path, project, "--no-such-flag")
    assert result.returncode == 1
    assert result.stdout.startswith("Invalid skill: --no-such-flag")


def test_list_output_is_byte_equal_to_origin_main(tmp_path):
    project = _small_project(tmp_path)
    result = _run(tmp_path, project, "--list")
    assert result.returncode == 0
    assert result.stdout == LIST_GOLDEN


@pytest.mark.skipif(not (REPO / "skills" / "skills.yaml").is_file(), reason="repo skills tree absent")
def test_inventory_on_the_real_repo_has_the_shape(tmp_path):
    env = {**os.environ, "VNX_HOME": str(REPO), "VNX_DATA_DIR_GUARD": "off",
           "VNX_SKILLS_DIR": str(REPO / "skills"), "PROJECT_ROOT": str(REPO)}
    result = subprocess.run([sys.executable, str(SCRIPT), "--inventory"], env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    for key in ("t0_layer", "worker_layer", "provider_unsafe", "parity", "trees"):
        assert key in data
    for key in ("t0_layer", "worker_layer", "provider_unsafe"):
        assert data[key] == sorted(data[key])
    assert all(isinstance(data["parity"][k], int) for k in ("shared", "identical", "announcement_only", "semantic"))
