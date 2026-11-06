"""check_function_size_tree.py: whole-tree function size measurement at a git ref."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import function_size_gate as fsg

CLI = REPO_ROOT / "scripts" / "ci" / "check_function_size_tree.py"


def fn(name: str, n: int, indent: str = "") -> str:
    """A function with exactly n executable lines (def line included)."""
    body = "".join(f"{indent}    x{i} = {i}\n" for i in range(n - 1))
    return f"{indent}def {name}():\n{body}"


def docstring(lines: int, indent: str = "") -> str:
    """A docstring of exactly `lines` physical lines."""
    inner = "".join(f"{indent}doc line {i}\n" for i in range(lines - 2))
    return f'{indent}"""\n{inner}{indent}"""\n'


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    git(tmp_path, "config", "commit.gpgsign", "false")
    return tmp_path


def commit(repo: Path, files: dict, message: str = "c") -> None:
    for rel, content in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def run_tree(repo: Path, *args: str):
    return subprocess.run(
        [sys.executable, str(CLI), *args], cwd=repo, capture_output=True, text=True
    )


def offender_lines(result) -> list:
    return [line for line in result.stdout.splitlines() if line and not line.startswith("function size tree:")]


def test_exactly_the_max_is_clean(repo):
    commit(repo, {"scripts/a.py": fn("f", 200)})
    result = run_tree(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 above 200" in result.stdout
    assert offender_lines(result) == []


def test_one_over_the_max_is_flagged_and_named(repo):
    commit(repo, {"scripts/a.py": "x = 0\n\n" + fn("f", 201)})
    result = run_tree(repo)
    assert result.returncode == 1
    assert "1 above 200" in result.stdout
    assert offender_lines(result) == ["201 scripts/a.py::f (line 3)"]


def test_docstring_does_not_count(repo):
    source = "def f():\n" + docstring(10, indent="    ") + fn("body", 200).split("\n", 1)[1]
    commit(repo, {"scripts/a.py": source})
    result = run_tree(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 above 200" in result.stdout
    (measured,) = fsg.measure_source(source)
    assert measured.executable_lines == 200


def test_scope_excludes_tests_spikes_and_other_dirs(repo):
    big = fn("f", 300)
    commit(
        repo,
        {
            "scripts/small.py": fn("g", 3),
            "scripts/tests/t.py": big,
            "scripts/lib/spikes/s.py": big,
            "vnx_cli/big.py": big,
        },
    )
    result = run_tree(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 above 200" in result.stdout
    assert "1 functions in 1 files" in result.stdout


def test_prefix_vnx_cli_counts_that_dir(repo):
    commit(repo, {"scripts/small.py": fn("g", 3), "vnx_cli/big.py": fn("f", 300)})
    assert run_tree(repo).returncode == 0
    result = run_tree(repo, "--prefix", "vnx_cli/")
    assert result.returncode == 1
    assert offender_lines(result) == ["300 vnx_cli/big.py::f (line 1)"]


def test_unparsed_file_exits_one_and_others_are_still_measured(repo):
    commit(repo, {"scripts/bad.py": "def f(:\n", "scripts/a.py": fn("f", 201)})
    result = run_tree(repo)
    assert result.returncode == 1
    lines = result.stdout.splitlines()
    assert "unparsed scripts/bad.py" in lines
    assert "1 above 200" in result.stdout
    assert "2 files" in result.stdout
    assert "201 scripts/a.py::f (line 1)" in lines


def test_unparsed_only_is_not_a_clean_exit(repo):
    commit(repo, {"scripts/bad.py": "def f(:\n"})
    result = run_tree(repo)
    assert result.returncode == 1
    assert "0 above 200" in result.stdout
    assert "unparsed scripts/bad.py" in result.stdout.splitlines()


def test_ref_measures_the_commit_not_the_working_tree(repo):
    commit(repo, {"scripts/a.py": fn("f", 50)})
    (repo / "scripts/a.py").write_text(fn("f", 300), encoding="utf-8")
    result = run_tree(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 above 200" in result.stdout


def test_named_ref_is_measured(repo):
    commit(repo, {"scripts/a.py": fn("f", 300)}, "big")
    git(repo, "tag", "big")
    commit(repo, {"scripts/a.py": fn("f", 50)}, "small")
    at_head = run_tree(repo)
    assert at_head.returncode == 0
    at_tag = run_tree(repo, "--ref", "big")
    assert at_tag.returncode == 1
    assert offender_lines(at_tag) == ["300 scripts/a.py::f (line 1)"]


def test_above_seventy_uses_the_gate_limit(repo):
    commit(repo, {"scripts/a.py": fn("at", fsg.FUNCTION_SIZE_LIMIT) + "\n" + fn("over", fsg.FUNCTION_SIZE_LIMIT + 1)})
    result = run_tree(repo)
    assert result.returncode == 0
    assert f"1 above {fsg.FUNCTION_SIZE_LIMIT}" in result.stdout


def test_summary_line_format(repo):
    commit(repo, {"scripts/a.py": fn("f", 3)})
    sha = git(repo, "rev-parse", "HEAD").strip()
    result = run_tree(repo)
    assert result.returncode == 0
    assert result.stdout.strip().splitlines() == [
        f"function size tree: 0 above 200, 0 above 70, 1 functions in 1 files at HEAD ({sha[:8]})"
    ]


def test_json_carries_every_field(repo):
    commit(repo, {"scripts/a.py": fn("small", 201) + "\n" + fn("big", 250)})
    sha = git(repo, "rev-parse", "HEAD").strip()
    result = run_tree(repo, "--json")
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ref"] == "HEAD"
    assert payload["sha"] == sha
    assert payload["prefix"] == "scripts/"
    assert payload["max"] == 200
    assert payload["files"] == 1
    assert payload["functions"] == 2
    assert payload["above_max"] == 2
    assert payload["above_70"] == 2
    assert payload["unparsed"] == 0
    assert payload["offenders"] == [
        {"path": "scripts/a.py", "qualname": "big", "lineno": 203, "executable_lines": 250},
        {"path": "scripts/a.py", "qualname": "small", "lineno": 1, "executable_lines": 201},
    ]


def test_json_reports_unparsed_count(repo):
    commit(repo, {"scripts/bad.py": "def f(:\n", "scripts/a.py": fn("f", 3)})
    result = run_tree(repo, "--json")
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["unparsed"] == 1
    assert payload["files"] == 2
    assert payload["functions"] == 1


def test_bad_ref_is_exit_two(repo):
    commit(repo, {"scripts/a.py": fn("f", 3)})
    result = run_tree(repo, "--ref", "no-such-ref")
    assert result.returncode == 2
    assert result.stderr.strip()
    assert result.stdout == ""


def test_outside_git_is_exit_two(tmp_path):
    assert run_tree(tmp_path).returncode == 2


def test_custom_max_changes_the_offender_set(repo):
    commit(repo, {"scripts/a.py": fn("f", 5)})
    result = run_tree(repo, "--max", "4")
    assert result.returncode == 1
    assert offender_lines(result) == ["5 scripts/a.py::f (line 1)"]
