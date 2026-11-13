"""The impact shadow in CI (ADR-074 besluit 15): the workflow wiring and the scripts that must never fail.

The workflow tests read ``.github/workflows/vnx-ci.yml``. The behaviour tests run the real shell scripts
on a small git repo in ``tmp_path``, with ``impact_select.py`` and ``impact_shadow.py`` replaced by stubs
where a crash is the case under test.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
CI_DIR = REPO / "scripts" / "ci"
WORKFLOW = REPO / ".github" / "workflows" / "vnx-ci.yml"
SCRIPTS = ("impact_shadow_inputs.sh", "impact_shadow_step.sh", "impact_shadow_job.sh")
STEP = "Impact shadow record (never fails the job)"
COMPLETENESS = "Check the xdist completeness (required)"
EVIDENCE_UPLOAD = "Upload Profile A evidence (JUnit XML + manifest + completeness verdict)"
LIGHT = "steps.prelude.outputs.profile != 'light'"


@pytest.fixture(scope="module")
def jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def names(job: dict) -> list:
    return [s.get("name") for s in job["steps"]]


def step_named(job: dict, name: str) -> dict:
    return next(s for s in job["steps"] if s.get("name") == name)


def test_step_sits_directly_after_completeness_and_before_the_evidence_upload(jobs):
    order = names(jobs["profile-a"])
    assert order.index(STEP) == order.index(COMPLETENESS) + 1
    assert order.index(STEP) < order.index(EVIDENCE_UPLOAD)


def test_step_cannot_turn_the_job_red(jobs):
    step = step_named(jobs["profile-a"], STEP)
    assert step["continue-on-error"] is True
    assert step["timeout-minutes"] == 5
    assert LIGHT in step["if"] and "always()" in step["if"]
    assert "impact_shadow_step.sh" in step["run"]
    assert {"VNX_HOME", "VNX_DATA_DIR", "PROFILE_A_EVIDENCE"} <= set(step["env"])


def test_step_upload_is_non_fatal(jobs):
    uploads = [s for s in jobs["profile-a"]["steps"] if (s.get("with") or {}).get("name") == "impact-shadow"]
    assert len(uploads) == 1
    upload = uploads[0]
    assert upload["continue-on-error"] is True and upload["timeout-minutes"] == 5
    assert LIGHT in upload["if"] and "always()" in upload["if"]
    assert upload["with"]["if-no-files-found"] == "warn" and upload["with"]["overwrite"] is True


def test_shadow_job_cannot_turn_a_check_red(jobs):
    job = jobs["impact-shadow"]
    assert job["name"] == "Impact shadow (runs only S, never required)"
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["continue-on-error"] is True
    assert job["timeout-minutes"] == 38
    assert "needs" not in job
    for other, body in jobs.items():
        needs = body.get("needs") or []
        assert "impact-shadow" not in ([needs] if isinstance(needs, str) else needs), other


def test_shadow_job_runs_only_on_pull_requests(jobs):
    """The conclusion of VNX CI on main paces the merge chain, so the shadow job must not run there."""
    assert jobs["impact-shadow"]["if"] == "${{ github.event_name == 'pull_request' }}"


def test_shadow_job_keeps_continue_on_error_no_needs_and_is_in_no_required_list(jobs):
    job = jobs["impact-shadow"]
    assert job["continue-on-error"] is True
    assert "needs" not in job
    for other, body in jobs.items():
        needs = body.get("needs") or []
        assert "impact-shadow" not in ([needs] if isinstance(needs, str) else needs), other
    text = WORKFLOW.read_text(encoding="utf-8")
    assert text.count("Impact shadow (runs only S, never required)") == 1


def test_shadow_step_in_profile_a_has_no_event_condition(jobs):
    step = step_named(jobs["profile-a"], STEP)
    assert "github.event_name" not in step["if"]
    upload = [s for s in jobs["profile-a"]["steps"] if (s.get("with") or {}).get("name") == "impact-shadow"][0]
    assert "github.event_name" not in upload["if"]


def test_shadow_job_steps(jobs):
    job = jobs["impact-shadow"]
    assert job["steps"][0]["with"]["fetch-depth"] == 0
    assert job["steps"][1]["uses"] == "./.github/actions/profile-a-prelude"
    run = [s for s in job["steps"] if "impact_shadow_job.sh" in s.get("run", "")]
    assert len(run) == 1 and LIGHT in run[0]["if"] and run[0]["continue-on-error"] is True
    uploads = [s for s in job["steps"] if (s.get("with") or {}).get("name") == "impact-shadow-job"]
    assert len(uploads) == 1 and uploads[0]["continue-on-error"] is True
    assert uploads[0]["with"]["if-no-files-found"] == "warn"


def test_other_profile_a_steps_keep_their_conditions(jobs):
    """The shadow adds two steps and changes no `if` or `continue-on-error` of the others."""
    shown = subprocess.run(["git", "show", "origin/main:.github/workflows/vnx-ci.yml"], cwd=REPO, capture_output=True, text=True)
    if shown.returncode != 0:
        pytest.skip("origin/main is not available")
    before = yaml.safe_load(shown.stdout)["jobs"]["profile-a"]["steps"]
    now = {s.get("name"): s for s in jobs["profile-a"]["steps"]}
    for old in before:
        new = now[old.get("name")]
        assert new.get("if") == old.get("if"), old.get("name")
        assert new.get("continue-on-error") == old.get("continue-on-error"), old.get("name")


# --- behaviour on a small git repo -------------------------------------------------------------

def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo: Path, name: str, text: str = "x") -> str:
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", f"add {name}")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def repo(tmp_path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    commit(path, "README.md", "root")
    return path


@pytest.fixture()
def tree(tmp_path) -> Path:
    """A copy of the shadow scripts with a stub selector that echoes the paths it was given."""
    ci = tmp_path / "scripts" / "ci"
    ci.mkdir(parents=True)
    for name in SCRIPTS:
        shutil.copy(CI_DIR / name, ci / name)
    (ci / "impact_select.py").write_text(
        "import json, sys\npaths = open(sys.argv[sys.argv.index('--paths-file') + 1]).read().split()\nprint(json.dumps({'paths': paths}))\n",
        encoding="utf-8")
    (ci / "impact_shadow.py").write_text("import sys\nsys.stderr.write('boom\\n')\nsys.exit(3)\n", encoding="utf-8")
    return ci


def run(script: Path, cwd: Path, *args: str, **env: str):
    clean = {k: v for k, v in os.environ.items() if not k.startswith("GITHUB_")}
    return subprocess.run(["bash", str(script), *args], cwd=cwd, env={**clean, **env}, capture_output=True, text=True)


def test_inputs_on_pull_request_use_the_merge_base_and_the_diff_of_the_pr(tree, repo, tmp_path):
    git(repo, "checkout", "-q", "-b", "feature")
    commit(repo, "a.txt")
    commit(repo, "b/c.txt")
    git(repo, "checkout", "-q", "main")
    base = commit(repo, "main-only.txt")  # main moved on after the branch point
    git(repo, "update-ref", "refs/remotes/origin/main", base)
    git(repo, "checkout", "-q", "feature")
    git(repo, "merge", "-q", "--no-ff", "-m", "merge", "main")
    out = tmp_path / "out"
    done = run(tree / "impact_shadow_inputs.sh", repo, str(out), GITHUB_EVENT_NAME="pull_request", GITHUB_BASE_REF="main")
    assert done.returncode == 0, done.stderr
    assert (out / "base").read_text().strip() == git(repo, "merge-base", "origin/main", "HEAD") == base
    assert (out / "candidate").read_text().strip() == git(repo, "rev-parse", "HEAD")
    assert sorted((out / "paths.txt").read_text().split()) == ["a.txt", "b/c.txt"]  # the PR, not what main added
    assert '"paths"' in (out / "selection.json").read_text()


def test_inputs_on_push_use_the_parent_commit(tree, repo, tmp_path):
    parent = git(repo, "rev-parse", "HEAD")
    head = commit(repo, "scripts/lib/x.py")
    out = tmp_path / "out"
    done = run(tree / "impact_shadow_inputs.sh", repo, str(out), GITHUB_EVENT_NAME="push")
    assert done.returncode == 0
    assert (out / "base").read_text().strip() == parent
    assert (out / "candidate").read_text().strip() == head
    assert (out / "paths.txt").read_text().split() == ["scripts/lib/x.py"]


def test_inputs_without_a_resolvable_base_give_an_empty_base_and_paths(tree, repo, tmp_path):
    commit(repo, "a.txt")
    out = tmp_path / "out"
    done = run(tree / "impact_shadow_inputs.sh", repo, str(out), GITHUB_EVENT_NAME="pull_request", GITHUB_BASE_REF="no-such-branch")
    assert done.returncode == 0
    assert (out / "base").read_text().strip() == ""
    assert (out / "paths.txt").read_text() == ""


def test_inputs_on_a_push_of_a_first_commit_have_no_base(tree, tmp_path):
    first = tmp_path / "first"
    first.mkdir()
    git(first, "init", "-q", "-b", "main")
    commit(first, "a.txt")
    out = tmp_path / "out"
    assert run(tree / "impact_shadow_inputs.sh", first, str(out), GITHUB_EVENT_NAME="push").returncode == 0
    assert (out / "base").read_text().strip() == ""
    assert (out / "paths.txt").read_text() == ""


@pytest.mark.parametrize("body", ["import sys\nsys.exit(7)\n", "pass\n", "import sys\nsys.stderr.write('bad graph\\n')\nsys.exit(1)\n"])
def test_inputs_remove_the_selection_when_the_selector_crashes_or_prints_nothing(tree, repo, tmp_path, body):
    (tree / "impact_select.py").write_text(body, encoding="utf-8")
    commit(repo, "a.txt")
    out = tmp_path / "out"
    done = run(tree / "impact_shadow_inputs.sh", repo, str(out), GITHUB_EVENT_NAME="push")
    assert done.returncode == 0
    assert not (out / "selection.json").exists()
    assert (out / "selection-error.txt").read_text().startswith("selector exit ")


def test_inputs_exit_zero_without_an_argument(tree, repo):
    assert run(tree / "impact_shadow_inputs.sh", repo).returncode == 0


def test_step_writes_no_record_and_exits_zero_when_the_selector_crashes(tree, repo, tmp_path):
    (tree / "impact_select.py").write_text("import sys\nsys.exit(2)\n", encoding="utf-8")
    commit(repo, "a.txt")
    summary = tmp_path / "summary.md"
    done = run(tree / "impact_shadow_step.sh", repo, str(tmp_path / "ev"), str(tmp_path / "out"), GITHUB_EVENT_NAME="push", GITHUB_STEP_SUMMARY=str(summary))
    assert done.returncode == 0
    assert summary.read_text().startswith("impact shadow: no record (selector exit 2")
    assert not (tmp_path / "out" / "record.json").exists()


def test_step_writes_no_record_and_exits_zero_when_impact_shadow_crashes(tree, repo, tmp_path):
    commit(repo, "a.txt")
    summary = tmp_path / "summary.md"
    done = run(tree / "impact_shadow_step.sh", repo, str(tmp_path / "ev"), str(tmp_path / "out"), GITHUB_EVENT_NAME="push", GITHUB_STEP_SUMMARY=str(summary))
    assert done.returncode == 0
    assert summary.read_text().strip() == "impact shadow: no record (impact_shadow.py exit 3: boom)"


def test_step_exits_zero_without_arguments(tree, repo):
    done = run(tree / "impact_shadow_step.sh", repo)
    assert done.returncode == 0 and "no record" in done.stdout


def test_job_exits_zero_and_says_so_when_the_record_cannot_be_written(tree, repo, tmp_path):
    """The stub shadow crashes on `job` and on `plan`: the job still exits 0 and names the failure."""
    commit(repo, "a.txt")
    summary = tmp_path / "summary.md"
    done = run(tree / "impact_shadow_job.sh", repo, str(tmp_path / "out"), GITHUB_EVENT_NAME="push", GITHUB_STEP_SUMMARY=str(summary))
    assert done.returncode == 0
    assert summary.read_text().startswith("impact shadow job: no record (impact_shadow.py job:")
