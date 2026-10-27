#!/usr/bin/env python3
"""refactor_census: negative control, edge cases, CLI entry points."""

from __future__ import annotations

import json, subprocess, sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import refactor_census as rc

CLI = REPO_ROOT / "scripts" / "lib" / "refactor_census.py"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    for key, value in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        git(tmp_path, "config", key, value)
    return tmp_path


def _write(repo: Path, relative: str, text: str) -> None:
    (repo / relative).parent.mkdir(parents=True, exist_ok=True)
    (repo / relative).write_text(text, encoding="utf-8")


def _sized(name: str, raw: int) -> str:
    body = [f"    value += {index}" for index in range(raw - 5)]
    return "\n".join([f"def {name}():", "    value = 0", "    # accumulator", "", *body, "    return value"])


def _pair() -> str:
    body = ["    total = 0"] + [f"    total += {index}" for index in range(28)] + ["    return total"]
    return "\n".join(["def dup_left():", '    """Left."""', *body, "",
                      "def dup_right():", '    """Right, a different docstring."""', *body])


def _fixture(repo: Path) -> str:
    source = "\n".join([
        _sized("big_210", 210), "", _sized("short_199", 199), "", _pair(), "",
        "def orphan_helper():", "    return 1", "",
        "def only_test_helper():", "    return 2", "",
        "def shell_referenced():", "    return 3", "",
        "def doc_referenced():", "    return 4", "",
        "class Holder:", "    def only_test_method(self):", "        return 5", "",
        "class BaseRunner:", "    def finish(self):", "        return 6", "",
        "class Runner:", "    def __init__(self):", "        self.value = 0", "",
        "    def run(self):", "        return self.value", "",
        "# ---- step 1", "# ---- step 1", "banner_value = 1", "",
        "# impossible by construction", "defensive_value = 0", "",
        "# increment counter", "counter = increment(counter)", "",
    ])
    _write(repo, "scripts/big.py", source)
    _write(repo, "scripts/broken.py", "def broken(:\n")
    _write(repo, "scripts/use.sh", "#!/bin/sh\nshell_referenced\n")
    _write(repo, "tests/test_uses.py", "\n".join([
        "from scripts.big import BaseRunner, Holder, Runner, only_test_helper", "",
        "class FakeRunner(BaseRunner):", "    def finish(self):", "        return 7", "",
        "def test_helpers():", "    assert only_test_helper() == 2",
        "    assert Holder().only_test_method() == 5",
        "    runner = Runner()", "    runner.__init__()", "    assert runner.run() == 0", "",
    ]))
    _write(repo, "usage.md", "big_210, short_199, dup_left, dup_right and doc_referenced are referenced here.\n")
    _write(repo, "notes_v2.txt", "legacy suffix\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fixture")
    return git(repo, "rev-parse", "HEAD").strip()


def test_negative_control_reports_one_per_class(repo: Path):
    result = rc.census(_fixture(repo), repo=str(repo))

    assert result["unparsed"] == ["scripts/broken.py"]
    assert result["long_functions"]["counts"]["raw_ge_200"] == 1
    assert (result["exact_duplicates"]["group_count"], result["exact_duplicates"]["function_count"]) == (1, 2)
    assert result["doubled_banners"]["count"] == 1
    assert result["version_suffixed_paths"]["items"] == ["notes_v2.txt"]
    assert result["defensive_markers"]["count"] == 1
    assert [item["name"] for item in result["dead_module_functions"]["items"]] == ["orphan_helper"]
    assert [item["name"] for item in result["test_only_functions"]["items"]] == ["only_test_helper"]
    assert [item["name"] for item in result["test_only_methods"]["items"]] == ["only_test_method"]
    assert [(item["path"], item["test_refs"], item["in_seed"])
            for item in result["test_only_functions"]["items"]] == [("scripts/big.py", 2, False)]
    assert [(item["path"], item["test_refs"], item["in_seed"])
            for item in result["test_only_methods"]["items"]] == [("scripts/big.py", 1, False)]
    assert result["restating_comments"]["count"] == 1
    members = {member["name"] for member in result["exact_duplicates"]["items"][0]["members"]}
    assert members == {"dup_left", "dup_right"}
    long_result = result["long_functions"]
    assert (long_result["counts"]["raw_ge_100"], long_result["counts"]["raw_ge_200"]) == (2, 1)
    assert [item["qualname"] for item in long_result["items"] if item["raw_lines"] >= 200] == ["big_210"]
    short = next(item for item in long_result["items"] if item["qualname"] == "short_199")
    assert short["raw_lines"] == 199
    big = next(item for item in long_result["items"] if item["qualname"] == "big_210")
    assert 0 < big["executable_lines"] < big["raw_lines"]
    with pytest.raises(rc.CensusError):
        rc.census("deadbeefdeadbeef", repo=str(repo))
    plain = repo.parent / "plain"
    plain.mkdir(exist_ok=True)
    with pytest.raises(rc.CensusError):
        rc.census("HEAD", repo=str(plain))


def test_test_only_classes_ignore_production_references_and_overrides(repo: Path):
    result = rc.census(_fixture(repo), repo=str(repo))

    functions = [item["name"] for item in result["test_only_functions"]["items"]]
    methods = [item["name"] for item in result["test_only_methods"]["items"]]
    # Referenced from a .sh and a .md file outside tests/: production callers.
    assert "shell_referenced" not in functions
    assert "doc_referenced" not in functions
    # Same name on two classes (the test double overrides the production base).
    assert "finish" not in methods
    # A dunder and a name of three characters, both referenced only by tests.
    assert "__init__" not in methods
    assert "run" not in methods
    assert result["dead_module_functions"]["count"] == 1


def test_paths_narrowing_and_worktree_mode(repo: Path):
    commit = _fixture(repo)
    scripts = rc.census(commit, paths=["scripts"], repo=str(repo))
    markdown = rc.census(commit, paths="usage.md", repo=str(repo))
    worktree = rc.census(repo)
    assert scripts["python_files"] == 2 and scripts["version_suffixed_paths"]["count"] == 0
    assert markdown["python_files"] == 0 and markdown["functions"] == 0
    assert worktree["version_suffixed_paths"]["items"] == ["notes_v2.txt"]
    assert worktree["dead_module_functions"]["count"] == 1


def test_cli_json_module_error_and_table(repo: Path):
    commit = _fixture(repo)
    cli = [sys.executable, str(CLI), "--commit", commit, "--repo", str(repo)]
    proc = subprocess.run([*cli, "--json"], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["doubled_banners"]["count"] == 1
    assert (payload["test_only_functions"]["count"], payload["test_only_methods"]["count"]) == (1, 1)
    env = {"PYTHONPATH": str(REPO_ROOT / "scripts" / "lib"), "PATH": "/usr/bin:/bin"}
    module = subprocess.run([sys.executable, "-m", "refactor_census", "--json"], cwd=str(repo), env=env,
                            capture_output=True, text=True, check=False)
    assert module.returncode == 0, module.stderr
    module_payload = json.loads(module.stdout)
    assert module_payload["dead_module_functions"]["count"] == 1
    assert (module_payload["test_only_functions"]["count"], module_payload["test_only_methods"]["count"]) == (1, 1)
    bad = subprocess.run([sys.executable, str(CLI), "--commit", "deadbeefdeadbeef", "--repo", str(repo)],
                         capture_output=True, text=True, check=False)
    assert bad.returncode == 2 and "error:" in bad.stderr
    table = subprocess.run(cli, capture_output=True, text=True, check=False)
    assert table.returncode == 0, table.stderr
    assert all(label in table.stdout for label in (
        "long functions", "exact duplicates", "doubled step banners", "version-suffixed paths",
        "defensive markers", "dead module functions", "test-only functions", "test-only methods",
        "restating comments", "unparsed"))
    assert table.stdout.count("not proven dead") == 2
