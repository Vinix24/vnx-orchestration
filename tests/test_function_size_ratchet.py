"""Function size ratchet: counting rule, base/head comparison, CLI, CI wiring."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import function_size_gate as fsg

CLI = REPO_ROOT / "scripts" / "ci" / "check_function_size_ratchet.py"
JOB_NAME = "Lint Patterns (silent-except + atomic-write)"


def fn(name: str, n: int, indent: str = "") -> str:
    """A function with exactly n executable lines (def line included)."""
    body = "".join(f"{indent}    x{i} = {i}\n" for i in range(n - 1))
    return f"{indent}def {name}():\n{body}"


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


def commit(repo: Path, files: dict, message: str = "c", remove: tuple = ()) -> None:
    for rel, content in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    for rel in remove:
        (repo / rel).unlink()
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def run_ratchet(repo: Path, base: str = "base", head: str = "HEAD"):
    return fsg.check_ratchet(repo, base, head)


def two_commits(repo: Path, base_files: dict, head_files: dict, remove: tuple = ()) -> None:
    commit(repo, base_files, "base")
    git(repo, "tag", "base")
    commit(repo, head_files, "head", remove=remove)


def kinds(violations) -> list:
    return sorted((v.qualname, v.kind) for v in violations)


def test_t1_crossed(repo):
    two_commits(repo, {"scripts/a.py": fn("f", 60)}, {"scripts/a.py": fn("f", 75)})
    assert kinds(run_ratchet(repo)) == [("f", "crossed")]


def test_t1_new_function_over_limit_crossed(repo):
    two_commits(repo, {"scripts/a.py": fn("f", 5)}, {"scripts/a.py": fn("f", 5) + "\n" + fn("g", 75)})
    assert kinds(run_ratchet(repo)) == [("g", "crossed")]


def test_t1_new_function_at_limit_ok(repo):
    two_commits(repo, {"scripts/a.py": fn("f", 5)}, {"scripts/a.py": fn("f", 5) + "\n" + fn("g", 70)})
    assert run_ratchet(repo) == []


def test_t2_grew(repo):
    two_commits(repo, {"scripts/a.py": fn("f", 80)}, {"scripts/a.py": fn("f", 81)})
    found = run_ratchet(repo)
    assert kinds(found) == [("f", "grew")]
    assert (found[0].base_lines, found[0].head_lines) == (80, 81)


def test_t2_unchanged_and_shrunk_ok(repo):
    base = {"scripts/a.py": fn("f", 80) + "\n" + fn("g", 3)}
    two_commits(repo, base, {"scripts/a.py": fn("f", 80) + "\n" + fn("g", 4)})
    assert run_ratchet(repo) == []
    commit(repo, {"scripts/a.py": fn("f", 60) + "\n" + fn("g", 4)}, "shrink")
    assert run_ratchet(repo) == []


def test_t3_counting_ignores_docstring_comments_blanks():
    doc = '    """\n' + "".join(f"    doc line {i}\n" for i in range(10)) + '    """\n'
    noise = "".join("\n    # comment\n" for _ in range(10))
    source = "def f():\n" + doc + noise + "".join(f"    x{i} = {i}\n" for i in range(59))
    (measured,) = fsg.measure_source(source)
    assert measured.executable_lines == 60


def test_t3_seventy_one_flagged(repo):
    two_commits(repo, {"scripts/a.py": fn("f", 5)}, {"scripts/a.py": fn("f", 71)})
    assert kinds(run_ratchet(repo)) == [("f", "crossed")]


def test_t4_identity_and_nesting():
    inner = fn("inner", 4, indent="    ")
    source = (
        "class C:\n    def run(self):\n        pass\n\n"
        "def run():\n    pass\n\n"
        "def outer():\n    a = 1\n" + inner + "    return a\n"
    )
    by_name = {m.qualname: m for m in fsg.measure_source(source)}
    assert set(by_name) == {"C.run", "run", "outer", "outer.<locals>.inner"}
    assert by_name["outer.<locals>.inner"].executable_lines == 4
    # def + `a = 1` + inner (4) + return
    assert by_name["outer"].executable_lines == 7


def test_t4_redefinition_told_apart_by_order():
    source = fn("f", 3) + "\n" + fn("f", 80)
    measured = fsg.measure_source(source)
    assert [(m.qualname, m.occurrence) for m in measured] == [("f", 0), ("f", 1)]


def test_t4_nested_over_limit_flagged_with_parent(repo):
    base = "def outer():\n" + fn("inner", 60, indent="    ") + "    return 1\n"
    head = "def outer():\n" + fn("inner", 75, indent="    ") + "    return 1\n"
    two_commits(repo, {"scripts/a.py": base}, {"scripts/a.py": head})
    assert kinds(run_ratchet(repo)) == [("outer", "crossed"), ("outer.<locals>.inner", "crossed")]


def test_t5_scope_tests_and_spikes_ignored(repo):
    big = fn("f", 90)
    two_commits(
        repo,
        {"scripts/a.py": fn("f", 3)},
        {"tests/test_x.py": big, "scripts/spikes/s.py": big, "scripts/lib/spikes/t.py": big},
    )
    assert run_ratchet(repo) == []


def test_t5_other_dirs_out_of_scope(repo):
    two_commits(repo, {"scripts/a.py": fn("f", 3)}, {"other/b.py": fn("f", 90), "scripts/c.txt": "x"})
    assert run_ratchet(repo) == []


def _rename(repo: Path, new_source: str) -> None:
    git(repo, "mv", "scripts/old.py", "scripts/new.py")
    (repo / "scripts/new.py").write_text(new_source, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "rename")


def test_t5_rename_compared_with_old_path(repo):
    padding = "\n# pad\n" * 3
    commit(repo, {"scripts/old.py": fn("f", 80) + padding}, "base")
    git(repo, "tag", "base")
    _rename(repo, fn("f", 80) + padding + "# more\n")
    assert run_ratchet(repo) == []


def test_t5_rename_growth_is_grew(repo):
    padding = "\n# pad\n" * 3
    commit(repo, {"scripts/old.py": fn("f", 80) + padding}, "base")
    git(repo, "tag", "base")
    _rename(repo, fn("f", 82) + padding)
    assert kinds(run_ratchet(repo)) == [("f", "grew")]


def test_t5_deleted_file_ok(repo):
    two_commits(
        repo, {"scripts/a.py": fn("f", 90)}, {"scripts/b.py": fn("g", 3)}, remove=("scripts/a.py",)
    )
    assert run_ratchet(repo) == []


def test_t5_merge_base_used_not_base_tip(repo):
    commit(repo, {"scripts/a.py": fn("f", 80)}, "root")
    git(repo, "checkout", "-q", "-b", "feature")
    commit(repo, {"scripts/b.py": fn("g", 3)}, "feat")
    git(repo, "checkout", "-q", "main")
    commit(repo, {"scripts/a.py": fn("f", 30)}, "main moved on")
    assert run_ratchet(repo, base="main", head="feature") == []


def test_t7_unparseable_head(repo):
    two_commits(repo, {"scripts/a.py": fn("f", 3)}, {"scripts/a.py": "def f(:\n"})
    assert [v.kind for v in run_ratchet(repo)] == ["unparseable"]


def test_unparseable_base_means_no_base_functions(repo):
    two_commits(repo, {"scripts/a.py": "def f(:\n"}, {"scripts/a.py": fn("f", 75)})
    assert kinds(run_ratchet(repo)) == [("f", "crossed")]


def cli(repo: Path, *args: str):
    return subprocess.run([sys.executable, str(CLI), *args], cwd=repo, capture_output=True, text=True)


def test_t6_cli_exit_zero(repo):
    two_commits(repo, {"scripts/a.py": fn("f", 60)}, {"scripts/a.py": fn("f", 61)})
    result = cli(repo, "--base", "base")
    assert result.returncode == 0
    assert len(result.stdout.strip().splitlines()) == 1


def test_t6_cli_exit_one_line_format(repo):
    head = fn("f", 75) + "\n" + fn("g", 80)
    two_commits(repo, {"scripts/a.py": fn("f", 60)}, {"scripts/a.py": head})
    result = cli(repo, "--base", "base", "--head", "HEAD")
    assert result.returncode == 1
    assert result.stdout.strip().splitlines() == [
        "scripts/a.py:1 f: base 60 -> head 75 (limit 70) [crossed]",
        "scripts/a.py:77 g: base new -> head 80 (limit 70) [crossed]",
    ]


def test_t6_cli_unknown_base_is_exit_two(repo):
    commit(repo, {"scripts/a.py": fn("f", 3)})
    result = cli(repo, "--base", "no-such-ref")
    assert result.returncode == 2
    assert result.stderr.strip()


def test_t6_cli_missing_base_is_usage_error(repo):
    commit(repo, {"scripts/a.py": fn("f", 3)})
    assert cli(repo).returncode == 2


def test_t6_cli_outside_git_is_exit_two(tmp_path):
    assert cli(tmp_path, "--base", "main").returncode == 2


def test_t7_cli_unparseable_exit_one(repo):
    two_commits(repo, {"scripts/a.py": fn("f", 3)}, {"scripts/a.py": "def f(:\n"})
    result = cli(repo, "--base", "base")
    assert result.returncode == 1
    assert "[unparseable]" in result.stdout


def test_g2_reviewer_prompt_has_no_function_size_instruction():
    text = (REPO_ROOT / "scripts/lib/prompts/roles/reviewer.md").read_text(encoding="utf-8")
    assert "## Function Size Threshold" not in text
    assert "70 lines" not in text and "70 executable" not in text
    assert "**Function size**" not in text
    assert "Function size ratchet" in text


def test_g3_workflow_step_in_required_job():
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/vnx-ci.yml").read_text(encoding="utf-8"))
    jobs = [j for j in workflow["jobs"].values() if j.get("name") == JOB_NAME]
    assert len(jobs) == 1
    steps = jobs[0]["steps"]
    names = [s.get("name") for s in steps]
    assert "Function size ratchet" in names
    assert names.index("Function size ratchet") > names.index("Run lint gate")
    step = steps[names.index("Function size ratchet")]
    assert "check_function_size_ratchet.py" in step["run"]
    assert "$VNX_HOME" in step["run"]
