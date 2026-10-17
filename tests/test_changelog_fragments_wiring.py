"""CHANGELOG fragments: the workflow step, the worker instruction, and why fragments.

These tests import none of the new modules: they read the workflow and
``dispatch_create.sh`` as shipped and run the step's own ``run:`` text, so on
code without the fragment rule they fail on behaviour, not on a missing import.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "vnx-ci.yml"
DISPATCH_CREATE = REPO_ROOT / "scripts" / "lib" / "dispatch_create.sh"
JOB_NAME = "Lint Patterns (silent-except + atomic-write)"
STEP_NAME = "Changelog fragments"

BASE_CHANGELOG = (
    "# Changelog\n"
    "\n"
    "## [Unreleased]\n"
    "\n"
    "- legacy bullet\n"
    "\n"
    "## [1.0.0] - 2026-01-01\n"
    "\n"
    "- old entry\n"
)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def commit(repo: Path, files: dict, message: str = "c") -> None:
    for rel, content in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "t")
    git(path, "config", "commit.gpgsign", "false")
    commit(path, {"CHANGELOG.md": BASE_CHANGELOG, "VERSION": "1.0.0\n", "changelog.d/README.md": "# rule\n"}, "base")
    return path


def with_bullet(bullet: str) -> str:
    return BASE_CHANGELOG.replace("- legacy bullet\n", f"{bullet}\n\n- legacy bullet\n")


# ---------------------------------------------------------------------------
# Fragments do not conflict where CHANGELOG bullets do
# ---------------------------------------------------------------------------


def merge_tree_is_clean(repo: Path, ours: str, theirs: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "merge-tree", "--write-tree", ours, theirs],
        capture_output=True, text=True, check=False,
    )
    return result.returncode == 0


def test_fragments_merge_cleanly_where_changelog_bullets_conflict(repo):
    for branch, name in (("frag-one", "one"), ("frag-two", "two")):
        git(repo, "checkout", "-q", "-b", branch, "main")
        commit(repo, {f"changelog.d/{name}.md": f"- entry {name}\n"})
    for branch, name in (("bullet-one", "one"), ("bullet-two", "two")):
        git(repo, "checkout", "-q", "-b", branch, "main")
        commit(repo, {"CHANGELOG.md": with_bullet(f"- entry {name}")})
    assert merge_tree_is_clean(repo, "frag-one", "frag-two")
    assert merge_tree_is_clean(repo, "frag-two", "frag-one")
    assert not merge_tree_is_clean(repo, "bullet-one", "bullet-two")
    assert not merge_tree_is_clean(repo, "bullet-two", "bullet-one")


# ---------------------------------------------------------------------------
# The workflow step
# ---------------------------------------------------------------------------


def lint_patterns_steps() -> list:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = [job for job in workflow["jobs"].values() if job.get("name") == JOB_NAME]
    assert len(jobs) == 1
    return jobs[0]["steps"]


def fragment_step() -> dict:
    steps = lint_patterns_steps()
    names = [step.get("name") for step in steps]
    assert STEP_NAME in names, f"no '{STEP_NAME}' step in {JOB_NAME}"
    return steps[names.index(STEP_NAME)]


def step_runs_for(step: dict, event_name: str) -> bool:
    """Evaluate the step's ``if:``, which must be one equality on the event name."""
    match = re.fullmatch(r"github\.event_name == '([a-z_]+)'", step.get("if", ""))
    assert match, step.get("if")
    return event_name == match.group(1)


def test_step_is_its_own_step_after_the_function_size_ratchet():
    names = [step.get("name") for step in lint_patterns_steps()]
    assert names.count(STEP_NAME) == 1
    assert names.index(STEP_NAME) > names.index("Function size ratchet") > names.index("Run lint gate")
    step = fragment_step()
    assert step["if"] == "github.event_name == 'pull_request'"
    assert "check_changelog_fragments.py" in step["run"]


@pytest.mark.parametrize("event_name,runs", [("pull_request", True), ("push", False)], ids=["pr-event", "push-event"])
def test_step_runs_on_pull_requests_and_is_skipped_on_pushes(event_name, runs):
    assert step_runs_for(fragment_step(), event_name) is runs


def test_step_command_line_uses_the_merge_base_and_the_pr_number(repo):
    step = fragment_step()
    assert "${{" not in step["run"], "event values reach the script through env, not inline"
    assert step["env"]["BASE_REF"] == "${{ github.base_ref }}"
    assert step["env"]["PR_NUMBER"] == "${{ github.event.pull_request.number }}"
    git(repo, "checkout", "-q", "-b", "pr")
    commit(repo, {"changelog.d/x.md": "- x\n"}, "a fragment")
    git(repo, "checkout", "-q", "main")
    commit(repo, {"other.txt": "main moved on\n"}, "main moves")
    git(repo, "update-ref", "refs/remotes/origin/main", "main")
    git(repo, "checkout", "-q", "pr")
    merge_base = git(repo, "merge-base", "origin/main", "HEAD").strip()
    assert merge_base != git(repo, "rev-parse", "origin/main").strip()
    env = {**os.environ, "VNX_HOME": str(REPO_ROOT), "BASE_REF": "main", "PR_NUMBER": "4242"}
    result = subprocess.run(["bash", "-c", step["run"]], cwd=repo, env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"base {merge_base} " in result.stdout
    assert "pr #4242" in result.stdout


# ---------------------------------------------------------------------------
# The worker instruction
# ---------------------------------------------------------------------------


def test_worker_instruction_names_the_fragment_and_forbids_changelog_edits():
    script = f'source "{DISPATCH_CREATE}"; _emit_receipt_template DISP-ID-1 12 A gate_x'
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True,
        env={**os.environ, "VNX_DATA_DIR": "/nonexistent-data-dir"},
    )
    assert "add `changelog.d/DISP-ID-1.md` with your bullet(s)" in result.stdout
    assert "Never edit `CHANGELOG.md`" in result.stdout
    assert "update `CHANGELOG.md`" not in result.stdout
