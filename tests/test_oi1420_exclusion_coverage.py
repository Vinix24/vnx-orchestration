"""OI-1420: the full-sweep promise names its exclusions, and Profile A states its coverage.

Two behaviours are pinned here:

1. every tracked claim about the Profile A full sweep names
   ``scripts/ci/test_exclusions.txt`` in the SAME sentence, because the sweep
   skips every file in that list (OI-906);
2. ``check_test_exclusions.py --summary`` states how many files and test
   functions sit outside the sweep, and names any other deselection the
   Profile A job applies. It never fails the job: the counting line is
   informational, and a missing or unreadable input reads ``unmeasured``
   rather than a number that covers only part of the list.

The guard test reads the tracked tree with ``git grep`` (not a fixed list of
three files), so a fourth copy of the claim cannot appear unnoticed.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
CHECK = REPO / "scripts" / "ci" / "check_test_exclusions.py"
WORKFLOW = REPO / ".github" / "workflows" / "vnx-ci.yml"

# Built from two halves so this file is not itself a tracked hit for the claim.
_CLAIM = "sweeps the " + "whole tree"
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_COUNTED_LINE = re.compile(
    r"^test exclusions: (?P<files>\d+) files, (?P<functions>\d+) test functions "
    r"outside the Profile A sweep \(scripts/ci/test_exclusions\.txt\)$",
    re.MULTILINE,
)


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CHECK), str(repo), *args],
        capture_output=True,
        text=True,
        cwd=str(repo),
        env={**os.environ, "PYTHONPATH": str(repo)},
    )


def _repo(tmp_path: Path, exclusions: str | None, files: dict[str, str] | None = None) -> Path:
    repo = tmp_path / "repo"
    (repo / "scripts" / "ci").mkdir(parents=True)
    (repo / "scripts" / "ci" / "profile_a_pytest_args.sh").write_text(
        'QUARANTINE_K="not flaky"\n', encoding="utf-8"
    )
    if exclusions is not None:
        (repo / "scripts" / "ci" / "test_exclusions.txt").write_text(exclusions, encoding="utf-8")
    (repo / "tests").mkdir()
    for rel, content in (files or {}).items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return repo


# ---------------------------------------------------------------------------
# The promise names its exclusions
# ---------------------------------------------------------------------------


def test_every_full_sweep_claim_names_the_exclusions_file():
    """Every tracked full-sweep claim must name test_exclusions.txt in its sentence."""
    result = subprocess.run(
        ["git", "-C", str(REPO), "grep", "-n", "-F", _CLAIM],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0 and result.stdout.strip(), (
        "no tracked file promises the Profile A full sweep any more; the "
        f"guard would pass vacuously. stderr:\n{result.stderr}"
    )

    offenders: list[str] = []
    for line in result.stdout.splitlines():
        path, _, rest = line.partition(":")
        _lineno, _, content = rest.partition(":")
        sentences = [s for s in _SENTENCE_SPLIT.split(content) if _CLAIM in s]
        if not any("test_exclusions.txt" in sentence for sentence in sentences):
            offenders.append(line)

    assert not offenders, (
        "a full-sweep claim does not name scripts/ci/test_exclusions.txt in the "
        "same sentence, so a worker cannot know the sweep skips files:\n"
        + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# --summary: the counted line
# ---------------------------------------------------------------------------


def test_summary_counts_files_and_test_functions(tmp_path: Path):
    repo = _repo(
        tmp_path,
        "tests/test_a.py  # OI-1: red solo\n"
        "tests/test_b.py  # OI-1: red solo\n",
        {
            "tests/test_a.py": (
                "def test_one(): pass\n"
                "def test_two(): pass\n"
                "async def test_three(): pass\n"
            ),
            "tests/test_b.py": (
                "class Tests:\n"
                "    def test_four(self): pass\n"
                "    async def test_five(self): pass\n"
            ),
        },
    )

    result = _run(repo, "--summary")

    assert result.returncode == 0, result.stderr
    assert (
        "test exclusions: 2 files, 5 test functions outside the Profile A sweep "
        "(scripts/ci/test_exclusions.txt)" in result.stdout
    ), result.stdout


def test_summary_names_other_deselections_found_in_the_workflow(tmp_path: Path):
    repo = _repo(
        tmp_path,
        "tests/test_a.py  # OI-1: red solo\n",
        {"tests/test_a.py": "def test_one(): pass\n"},
    )
    workflow = repo / ".github" / "workflows" / "vnx-ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "jobs:\n"
        "  profile-a:\n"
        "    steps:\n"
        "      - run: pytest tests -m \"not slow\" --deselect tests/test_x.py::test_y --ignore=tests/test_z.py\n"
        "  unrelated:\n"
        "    steps:\n"
        "      - run: pytest tests -m \"not this job\"\n",
        encoding="utf-8",
    )

    result = _run(repo, "--summary")
    out = result.stdout

    assert result.returncode == 0, result.stderr
    assert _COUNTED_LINE.search(out), out
    assert 'also not swept: -m "not slow" deselects tests' in out, out
    assert "also not swept: --deselect tests/test_x.py::test_y deselects tests" in out, out
    assert "also not swept: --ignore tests/test_z.py deselects tests" in out, out
    assert "not this job" not in out, "a deselection outside the profile-a job is not ours to name"
    # The counted line always comes first; the qualifiers follow it.
    assert out.index("test exclusions:") < out.index("also not swept:")


def test_summary_names_the_light_profile_path_filter(tmp_path: Path):
    repo = _repo(
        tmp_path,
        "tests/test_a.py  # OI-1: red solo\n",
        {"tests/test_a.py": "def test_one(): pass\n"},
    )
    workflow = repo / ".github" / "workflows" / "vnx-ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "jobs:\n"
        "  profile-a:\n"
        "    steps:\n"
        "      - run: |\n"
        "          if [ \"$CI_PROFILE\" = \"light\" ]; then\n"
        "            pytest -q \"$GITHUB_WORKSPACE/tests/test_docs_index.py\"\n"
        "            exit 0\n"
        "          fi\n",
        encoding="utf-8",
    )

    result = _run(repo, "--summary")

    assert result.returncode == 0, result.stderr
    assert "also not swept when CI_PROFILE=light:" in result.stdout, result.stdout
    assert "runs a fixed file list, not the tree" in result.stdout, result.stdout


# ---------------------------------------------------------------------------
# --summary: fail closed, never a partial number
# ---------------------------------------------------------------------------


def test_summary_is_unmeasured_when_the_exclusions_file_is_missing(tmp_path: Path):
    repo = _repo(tmp_path, None)

    result = _run(repo, "--summary")

    assert result.returncode == 0, result.stderr
    assert re.search(r"^test exclusions: unmeasured \(.+\)$", result.stdout, re.MULTILINE), result.stdout
    assert not _COUNTED_LINE.search(result.stdout), "a partial list must never print a number"


def test_summary_is_unmeasured_when_a_listed_file_cannot_be_read(tmp_path: Path):
    repo = _repo(tmp_path, "tests/not_a_file.py  # OI-1: red solo\n")
    (repo / "tests" / "not_a_file.py").mkdir()

    result = _run(repo, "--summary")

    assert result.returncode == 0, result.stderr
    assert "test exclusions: unmeasured" in result.stdout, result.stdout
    assert "tests/not_a_file.py" in result.stdout, result.stdout
    assert not _COUNTED_LINE.search(result.stdout), "a partial list must never print a number"


def test_summary_is_unmeasured_when_a_listed_file_is_not_utf8(tmp_path: Path):
    repo = _repo(tmp_path, "tests/test_bad.py  # OI-1: red solo\n")
    (repo / "tests" / "test_bad.py").write_bytes(b"def test_ok(): pass\n\xff\xfe\n")

    result = _run(repo, "--summary")

    assert result.returncode == 0, result.stderr
    assert "test exclusions: unmeasured" in result.stdout, result.stdout
    assert "tests/test_bad.py" in result.stdout, result.stdout
    assert not _COUNTED_LINE.search(result.stdout), "a partial list must never print a number"


def test_summary_is_unmeasured_when_the_exclusions_file_is_not_utf8(tmp_path: Path):
    repo = _repo(tmp_path, None)
    (repo / "scripts" / "ci" / "test_exclusions.txt").write_bytes(b"tests/test_a.py  # \xff\xfe\n")

    result = _run(repo, "--summary")

    assert result.returncode == 0, result.stderr
    assert "test exclusions: unmeasured" in result.stdout, result.stdout
    assert not _COUNTED_LINE.search(result.stdout), "a partial list must never print a number"


def test_summary_ignores_selection_flags_in_yaml_comments(tmp_path: Path):
    repo = _repo(
        tmp_path,
        "tests/test_a.py  # OI-1: red solo\n",
        {"tests/test_a.py": "def test_one(): pass\n"},
    )
    workflow = repo / ".github" / "workflows" / "vnx-ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "jobs:\n"
        "  profile-a:\n"
        "    steps:\n"
        "      # --ignore=tests/not_really_ignored.py is prose, not a selection\n"
        "      - run: pytest tests\n",
        encoding="utf-8",
    )

    result = _run(repo, "--summary")

    assert result.returncode == 0, result.stderr
    assert _COUNTED_LINE.search(result.stdout), result.stdout
    assert "not_really_ignored" not in result.stdout, result.stdout


def test_summary_is_unmeasured_when_a_listed_file_is_gone(tmp_path: Path):
    repo = _repo(tmp_path, "tests/test_gone.py  # OI-1: red solo\n")

    summary = _run(repo, "--summary")
    assert "test exclusions: unmeasured" in summary.stdout, summary.stdout
    assert "tests/test_gone.py" in summary.stdout, summary.stdout

    # The existing check still names the stale entry; the summary does not
    # quietly count the rest as if the list were whole.
    check = _run(repo)
    assert check.returncode == 1, check.stdout
    assert "path does not exist" in check.stdout, check.stdout


# ---------------------------------------------------------------------------
# --summary: the -k quarantine and the serial selections of the args script
# ---------------------------------------------------------------------------


def test_summary_names_the_profile_a_quarantine_expression():
    """The real -k expression, read from the args script, not copied into the checker."""
    script = REPO / "scripts" / "ci" / "profile_a_pytest_args.sh"
    match = re.search(r'^QUARANTINE_K="([^"]*)"', script.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, "profile_a_pytest_args.sh no longer defines QUARANTINE_K"

    result = _run(REPO, "--summary")

    assert result.returncode == 0, result.stderr
    assert f"also not swept: -k {match.group(1)} deselects tests" in result.stdout, result.stdout


def test_summary_names_the_args_script_quarantine_and_extra_selections(tmp_path: Path):
    repo = _repo(
        tmp_path,
        "tests/test_a.py  # OI-1: red solo\n",
        {"tests/test_a.py": "def test_one(): pass\n"},
    )
    (repo / "scripts" / "ci" / "profile_a_pytest_args.sh").write_text(
        'QUARANTINE_K="not flaky"\n'
        'ARGS=(-k "$QUARANTINE_K" --ignore=tests/test_extra.py '
        '--deselect=tests/test_x.py::test_y )\n',
        encoding="utf-8",
    )

    result = _run(repo, "--summary")
    out = result.stdout

    assert result.returncode == 0, result.stderr
    assert "also not swept: -k not flaky deselects tests" in out, out
    assert "also not swept: --ignore tests/test_extra.py deselects tests" in out, out
    assert "also not swept: --deselect tests/test_x.py::test_y deselects tests" in out, out


def test_summary_names_serial_entries_as_ignore_and_deselect(tmp_path: Path):
    repo = _repo(
        tmp_path,
        "tests/test_a.py  # OI-1: red solo\n",
        {"tests/test_a.py": "def test_one(): pass\n"},
    )
    (repo / "scripts" / "ci" / "serial_tests.txt").write_text(
        "tests/test_serial.py  # OI-2: xdist-only failure\n"
        "tests/test_z.py::test_node  # OI-3: order dependent\n",
        encoding="utf-8",
    )

    result = _run(repo, "--summary")
    out = result.stdout

    assert result.returncode == 0, result.stderr
    assert "also not swept: --ignore tests/test_serial.py deselects tests" in out, out
    assert "also not swept: --deselect tests/test_z.py::test_node deselects tests" in out, out


def test_summary_is_unmeasured_when_the_args_script_cannot_be_read(tmp_path: Path):
    repo = _repo(
        tmp_path,
        "tests/test_a.py  # OI-1: red solo\n",
        {"tests/test_a.py": "def test_one(): pass\n"},
    )
    (repo / "scripts" / "ci" / "profile_a_pytest_args.sh").write_bytes(b'QUARANTINE_K="\xff\xfe"\n')

    result = _run(repo, "--summary")

    assert result.returncode == 0, result.stderr
    assert re.search(r"^test exclusions: unmeasured \(.+\)$", result.stdout, re.MULTILINE), result.stdout
    assert not _COUNTED_LINE.search(result.stdout), "a partial list must never print a number"
    assert "also not swept" not in result.stdout, result.stdout


def test_summary_does_not_treat_a_non_pytest_dash_m_as_a_deselection(tmp_path: Path):
    repo = _repo(
        tmp_path,
        "tests/test_a.py  # OI-1: red solo\n",
        {"tests/test_a.py": "def test_one(): pass\n"},
    )
    workflow = repo / ".github" / "workflows" / "vnx-ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "jobs:\n"
        "  profile-a:\n"
        "    steps:\n"
        "      - run: |\n"
        '          git commit -m "x"\n'
        "          pytest tests\n",
        encoding="utf-8",
    )

    result = _run(repo, "--summary")

    assert result.returncode == 0, result.stderr
    assert _COUNTED_LINE.search(result.stdout), result.stdout
    assert not re.search(r"^also not swept: -m ", result.stdout, re.MULTILINE), result.stdout
    assert '-m "x"' not in result.stdout, result.stdout


# ---------------------------------------------------------------------------
# The real tree and the real workflow
# ---------------------------------------------------------------------------


def test_current_tree_summary_reports_the_exclusion_list():
    result = _run(REPO, "--summary")

    assert result.returncode == 0, result.stderr
    match = _COUNTED_LINE.search(result.stdout)
    assert match, result.stdout
    assert int(match.group("files")) > 0
    assert int(match.group("functions")) > 0


def test_profile_a_runs_the_summary_into_the_step_summary():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["profile-a"]

    summary_steps = [
        step
        for step in job["steps"]
        if "check_test_exclusions.py" in str(step.get("run", ""))
        and "--summary" in str(step.get("run", ""))
    ]
    assert summary_steps, (
        "the Profile A job has no step that runs check_test_exclusions.py --summary"
    )

    step = summary_steps[0]
    run = str(step["run"])
    assert "GITHUB_STEP_SUMMARY" in run, run
    assert step.get("continue-on-error") is True or "|| true" in run, (
        "the summary step must not be able to red the job"
    )
