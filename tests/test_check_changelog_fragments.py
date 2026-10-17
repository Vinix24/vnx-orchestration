"""CHANGELOG fragments: the CI check, its workflow step and the worker instruction.

Every check test builds a throwaway git repository with a ``main`` and a PR
branch and runs ``scripts/ci/check_changelog_fragments.py`` on it as a
subprocess, with ``--base`` the merge base, exactly as the workflow step does.
Release PRs are built by the real assembler.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "ci"))

import check_changelog_fragments as check

CHECK = REPO_ROOT / "scripts" / "ci" / "check_changelog_fragments.py"
ASSEMBLER = REPO_ROOT / "scripts" / "release" / "assemble_changelog.py"
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


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def commit(repo: Path, files: dict, message: str = "c", remove: tuple = ()) -> None:
    for rel, content in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    for rel in remove:
        git(repo, "rm", "-q", "-r", rel)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", message)


def init_repo(path: Path, files: dict) -> Path:
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "t")
    git(path, "config", "commit.gpgsign", "false")
    commit(path, files, "base")
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return init_repo(tmp_path / "repo", {
        "CHANGELOG.md": BASE_CHANGELOG,
        "VERSION": "1.0.0\n",
        "changelog.d/README.md": "# rule\n",
    })


def pr_branch(repo: Path) -> None:
    git(repo, "checkout", "-q", "-b", "pr")


def run_check(repo: Path, pr: int | None = 4242) -> subprocess.CompletedProcess:
    base = git(repo, "merge-base", "main", "HEAD").strip()
    args = [sys.executable, str(CHECK), "--base", base, "--head", "HEAD"]
    if pr is not None:
        args += ["--pr", str(pr)]
    return subprocess.run(args, cwd=repo, capture_output=True, text=True, check=False)


def release_branch(repo: Path, version: str = "1.1.0", fragments: dict | None = None) -> None:
    """Land *fragments* on main one commit each, then cut the release with the assembler."""
    for name, text in (fragments or {"a.md": "- entry a\n", "b.md": "- entry b\n"}).items():
        commit(repo, {f"changelog.d/{name}": text}, f"add {name}")
    git(repo, "checkout", "-q", "-b", "release")
    commit(repo, {"VERSION": f"{version}\n"}, "bump VERSION")
    result = subprocess.run(
        [sys.executable, str(ASSEMBLER), "--version", version, "--date", "2026-10-17", "--rev", "main"],
        cwd=repo, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def edit_changelog(repo: Path, old: str, new: str) -> None:
    path = repo / "CHANGELOG.md"
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


# ---------------------------------------------------------------------------
# Rule 1: no new Unreleased bullet
# ---------------------------------------------------------------------------


def test_new_unreleased_bullet_fails_and_names_the_fragment_path(repo):
    pr_branch(repo)
    commit(repo, {"CHANGELOG.md": BASE_CHANGELOG.replace("- legacy bullet\n", "- my new bullet\n\n- legacy bullet\n")})
    result = run_check(repo)
    assert result.returncode == 1
    assert "- my new bullet" in result.stdout
    assert "changelog.d/<dispatch-id>.md" in result.stdout


def test_fragment_only_pr_passes(repo):
    pr_branch(repo)
    commit(repo, {"changelog.d/20261017-x.md": "- a fragment\n  continued\n"})
    result = run_check(repo)
    assert result.returncode == 0, result.stdout
    assert "changelog fragments: OK" in result.stdout


def test_removing_the_unreleased_heading_fails(repo):
    pr_branch(repo)
    commit(repo, {"CHANGELOG.md": BASE_CHANGELOG.replace("## [Unreleased]\n\n- legacy bullet\n\n", "")})
    result = run_check(repo)
    assert result.returncode == 1
    assert "lost its '## [Unreleased]' heading" in result.stdout


def test_consumer_without_fragment_directory_passes_and_does_nothing(tmp_path):
    repo = init_repo(tmp_path / "consumer", {"CHANGELOG.md": BASE_CHANGELOG})
    pr_branch(repo)
    commit(repo, {"CHANGELOG.md": BASE_CHANGELOG.replace("- legacy bullet\n", "- direct edit\n\n- legacy bullet\n")})
    result = run_check(repo)
    assert result.returncode == 0, result.stdout
    assert "nothing to check" in result.stdout


# ---------------------------------------------------------------------------
# Rule 2: valid fragments only
# ---------------------------------------------------------------------------


def test_invalid_fragment_fails_with_the_line(repo):
    pr_branch(repo)
    commit(repo, {"changelog.d/bad.md": "- entry\n### a heading\n"})
    result = run_check(repo)
    assert result.returncode == 1
    assert "bad.md:2: a heading line" in result.stdout


def test_symlinked_fragment_fails(repo):
    pr_branch(repo)
    (repo / "changelog.d" / "target.txt").write_text("- x\n", encoding="utf-8")
    (repo / "changelog.d" / "linked.md").symlink_to("target.txt")
    commit(repo, {})
    result = run_check(repo)
    assert result.returncode == 1
    assert "changelog.d/linked.md: a fragment must be a regular file" in result.stdout


def test_non_fragment_name_in_the_directory_fails(repo):
    pr_branch(repo)
    commit(repo, {"changelog.d/my fix.md": "- entry\n"})
    result = run_check(repo)
    assert result.returncode == 1
    assert "changelog.d/my fix.md: not a fragment name" in result.stdout


def test_removing_the_readme_fails(repo):
    pr_branch(repo)
    commit(repo, {}, remove=("changelog.d/README.md",))
    result = run_check(repo)
    assert result.returncode == 1
    assert "changelog.d/README.md was removed" in result.stdout


def test_editing_the_readme_passes(repo):
    pr_branch(repo)
    commit(repo, {"changelog.d/README.md": "# rule, reworded\n"})
    assert run_check(repo).returncode == 0


# ---------------------------------------------------------------------------
# Rule 3: a release PR matches the assembler
# ---------------------------------------------------------------------------


def test_release_pr_built_by_the_assembler_passes(repo):
    release_branch(repo)
    commit(repo, {}, "release 1.1.0")
    result = run_check(repo)
    assert result.returncode == 0, result.stdout
    assert "release PR" in result.stdout


def test_release_pr_with_an_intro_paragraph_passes(repo):
    release_branch(repo)
    edit_changelog(repo, "## [1.1.0] - 2026-10-17\n\n", "## [1.1.0] - 2026-10-17\n\nThis release folds\nfragments in.\n\n")
    commit(repo, {}, "release 1.1.0 with intro")
    result = run_check(repo)
    assert result.returncode == 0, result.stdout


def test_release_pr_with_a_subheading_in_the_intro_fails(repo):
    release_branch(repo)
    edit_changelog(repo, "## [1.1.0] - 2026-10-17\n\n", "## [1.1.0] - 2026-10-17\n\n### What's new\n\n")
    commit(repo, {}, "release with a subheading")
    result = run_check(repo)
    assert result.returncode == 1
    assert "only prose may precede the first bullet" in result.stdout


def test_release_pr_with_an_extra_hand_written_bullet_fails(repo):
    release_branch(repo)
    edit_changelog(repo, "- entry b\n", "- entry b\n\n- hand-written extra\n")
    commit(repo, {}, "release with an extra bullet")
    result = run_check(repo)
    assert result.returncode == 1
    assert "does not match what assemble_changelog.py builds" in result.stdout
    assert "+- hand-written extra" in result.stdout


def test_release_pr_leaving_a_consumed_fragment_fails(repo):
    release_branch(repo)
    git(repo, "checkout", "main", "--", "changelog.d/a.md")
    commit(repo, {}, "release that keeps a.md")
    result = run_check(repo)
    assert result.returncode == 1
    assert "changelog.d/a.md: a consumed fragment is left in place" in result.stdout


def test_release_pr_adding_a_new_fragment_fails(repo):
    release_branch(repo)
    commit(repo, {"changelog.d/sneaked.md": "- sneaked in\n"}, "release plus a fragment")
    result = run_check(repo)
    assert result.returncode == 1
    assert "changelog.d/sneaked.md: a release PR adds no new fragment" in result.stdout


def test_release_pr_removing_the_directory_fails(repo):
    release_branch(repo)
    commit(repo, {}, "release that drops changelog.d", remove=("changelog.d",))
    result = run_check(repo)
    assert result.returncode == 1
    assert "changelog.d/README.md was removed" in result.stdout


def test_release_pr_with_a_heading_that_is_not_version_fails(repo):
    release_branch(repo)
    commit(repo, {"VERSION": "1.1.1\n"}, "VERSION drifts from the heading")
    result = run_check(repo)
    assert result.returncode == 1
    assert "'## [1.1.0]' does not match VERSION at the head (1.1.1)" in result.stdout


def test_deleting_a_fragment_without_a_heading_is_a_release_and_fails(repo):
    commit(repo, {"changelog.d/a.md": "- entry a\n"}, "add a")
    pr_branch(repo)
    commit(repo, {}, "drop a", remove=("changelog.d/a.md",))
    result = run_check(repo)
    assert result.returncode == 1
    assert "adds exactly one version heading" in result.stdout


# ---------------------------------------------------------------------------
# Rule 4: grandfathered PRs
# ---------------------------------------------------------------------------


def legacy_repo(tmp_path: Path, listed: str | None) -> Path:
    files = {"CHANGELOG.md": BASE_CHANGELOG, "VERSION": "1.0.0\n", "changelog.d/README.md": "# rule\n"}
    if listed is not None:
        files[check.LEGACY_LIST_PATH] = listed
    repo = init_repo(tmp_path / "legacy", files)
    pr_branch(repo)
    return repo


def with_bullets(*bullets: str) -> str:
    added = "".join(f"{b}\n\n" for b in bullets)
    return BASE_CHANGELOG.replace("- legacy bullet\n", f"{added}- legacy bullet\n")


def test_listed_legacy_pr_with_unchanged_lines_passes(tmp_path):
    entry = check.legacy_entry(77, BASE_CHANGELOG, with_bullets("- open PR bullet"))
    repo = legacy_repo(tmp_path, f"# measured\n{entry}\n")
    commit(repo, {"CHANGELOG.md": with_bullets("- open PR bullet")})
    result = run_check(repo, pr=77)
    assert result.returncode == 0, result.stdout
    assert "PR #77 is grandfathered" in result.stdout


def test_listed_legacy_pr_with_one_more_bullet_fails(tmp_path):
    entry = check.legacy_entry(77, BASE_CHANGELOG, with_bullets("- open PR bullet"))
    repo = legacy_repo(tmp_path, f"{entry}\n")
    commit(repo, {"CHANGELOG.md": with_bullets("- open PR bullet", "- one more")})
    result = run_check(repo, pr=77)
    assert result.returncode == 1
    assert "PR #77 is grandfathered for the CHANGELOG.md lines" in result.stdout


def test_unlisted_pr_with_a_bullet_fails(tmp_path):
    entry = check.legacy_entry(77, BASE_CHANGELOG, with_bullets("- open PR bullet"))
    repo = legacy_repo(tmp_path, f"{entry}\n")
    commit(repo, {"CHANGELOG.md": with_bullets("- open PR bullet")})
    result = run_check(repo, pr=78)
    assert result.returncode == 1
    assert "adds 1 line(s) under '## [Unreleased]'" in result.stdout


def test_missing_legacy_file_is_an_empty_list(tmp_path):
    repo = legacy_repo(tmp_path, None)
    commit(repo, {"CHANGELOG.md": with_bullets("- open PR bullet")})
    result = run_check(repo, pr=77)
    assert result.returncode == 1
    assert "adds 1 line(s)" in result.stdout
    assert check.parse_legacy_list("") == ({}, [])


def test_legacy_list_is_read_from_the_merge_base_not_the_head(tmp_path):
    repo = legacy_repo(tmp_path, None)
    entry = check.legacy_entry(77, BASE_CHANGELOG, with_bullets("- self-listed"))
    commit(repo, {"CHANGELOG.md": with_bullets("- self-listed"), check.LEGACY_LIST_PATH: f"{entry}\n"})
    assert run_check(repo, pr=77).returncode == 1


def test_malformed_legacy_lines_are_errors():
    entries, errors = check.parse_legacy_list("12 " + "a" * 64 + "\nnot a line\n12 " + "b" * 64 + "\n")
    assert entries == {12: "a" * 64}
    assert len(errors) == 2
    assert ":2: expected '<pr-number> <sha256>'" in errors[0]
    assert "PR #12 is listed twice" in errors[1]


def test_added_lines_ignore_blank_lines_and_count_duplicates():
    head = with_bullets("- dup", "- dup")
    assert check.added_unreleased_lines(BASE_CHANGELOG, head) == ["- dup", "- dup"]
    assert check.added_unreleased_lines(BASE_CHANGELOG, BASE_CHANGELOG.replace("\n\n", "\n\n\n")) == []


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
        commit(repo, {"CHANGELOG.md": with_bullets(f"- entry {name}")})
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


def step_runs_for(step: dict, event_name: str) -> bool:
    """Evaluate the step's ``if:``, which must be one equality on the event name."""
    match = re.fullmatch(r"github\.event_name == '([a-z_]+)'", step["if"])
    assert match, step["if"]
    return event_name == match.group(1)


def test_step_is_its_own_step_after_the_function_size_ratchet():
    steps = lint_patterns_steps()
    names = [step.get("name") for step in steps]
    assert names.count(STEP_NAME) == 1
    assert names.index(STEP_NAME) > names.index("Function size ratchet") > names.index("Run lint gate")
    step = steps[names.index(STEP_NAME)]
    assert step["if"] == "github.event_name == 'pull_request'"
    assert "check_changelog_fragments.py" in step["run"]


@pytest.mark.parametrize("event_name,runs", [("pull_request", True), ("push", False)], ids=["pr-event", "push-event"])
def test_step_runs_on_pull_requests_and_is_skipped_on_pushes(event_name, runs):
    steps = lint_patterns_steps()
    step = steps[[s.get("name") for s in steps].index(STEP_NAME)]
    assert step_runs_for(step, event_name) is runs


def test_step_command_line_uses_the_merge_base_and_the_pr_number(repo):
    steps = lint_patterns_steps()
    step = steps[[s.get("name") for s in steps].index(STEP_NAME)]
    assert "${{" not in step["run"], "event values reach the script through env, not inline"
    assert step["env"]["BASE_REF"] == "${{ github.base_ref }}"
    assert step["env"]["PR_NUMBER"] == "${{ github.event.pull_request.number }}"
    git(repo, "update-ref", "refs/remotes/origin/main", "main")
    pr_branch(repo)
    commit(repo, {"changelog.d/x.md": "- x\n"})
    git(repo, "checkout", "-q", "main")
    commit(repo, {"VERSION": "1.0.0\n", "other.txt": "main moved on\n"}, "main moves")
    git(repo, "update-ref", "refs/remotes/origin/main", "main")
    git(repo, "checkout", "-q", "pr")
    merge_base = git(repo, "merge-base", "origin/main", "HEAD").strip()
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
