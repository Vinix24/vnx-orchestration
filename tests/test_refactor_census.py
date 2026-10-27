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
        "# ---- step 1", "# ---- step 1", "banner_value = 1", "",
        "# impossible by construction", "defensive_value = 0", "",
        "# increment counter", "counter = increment(counter)", "",
    ])
    _write(repo, "scripts/big.py", source)
    _write(repo, "scripts/broken.py", "def broken(:\n")
    _write(repo, "usage.md", "big_210, short_199, dup_left and dup_right are referenced here.\n")
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
    assert json.loads(proc.stdout)["doubled_banners"]["count"] == 1
    env = {"PYTHONPATH": str(REPO_ROOT / "scripts" / "lib"), "PATH": "/usr/bin:/bin"}
    module = subprocess.run([sys.executable, "-m", "refactor_census", "--json"], cwd=str(repo), env=env,
                            capture_output=True, text=True, check=False)
    assert module.returncode == 0, module.stderr
    assert json.loads(module.stdout)["dead_module_functions"]["count"] == 1
    bad = subprocess.run([sys.executable, str(CLI), "--commit", "deadbeefdeadbeef", "--repo", str(repo)],
                         capture_output=True, text=True, check=False)
    assert bad.returncode == 2 and "error:" in bad.stderr
    table = subprocess.run(cli, capture_output=True, text=True, check=False)
    assert table.returncode == 0, table.stderr
    assert all(label in table.stdout for label in (
        "long functions", "exact duplicates", "doubled step banners", "version-suffixed paths",
        "defensive markers", "dead module functions", "restating comments", "unparsed"))
