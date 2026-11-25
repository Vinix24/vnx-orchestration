"""vnx pre-push-check: selection (incl. tests that read a file by name or glob), steps, plug-in."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("pre_push_check", REPO / "scripts/ci/pre_push_check.py")
pps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pps)

HAS_PY311 = shutil.which("python3.11") is not None
needs_py311 = pytest.mark.skipif(not HAS_PY311, reason="python3.11 not installed")


# --- selection: the six tests the shadow missed ---------------------------------------------------

@pytest.mark.parametrize("touched, test", [
    ("tests/fixtures/operator_names/findings.tsv", "tests/test_no_operator_defaults.py"),
    ("scripts/ci/some_new_gate.py", "tests/test_ci_red_on_base_selection.py"),
    ("CLAUDE.md", "tests/test_project_claude_md_build_rules.py"),
    ("schemas/migrations/0099_new.sql", "tests/test_oi_track_bridge.py"),
    ("scripts/lib/vnx_paths.sh", "tests/test_vnx_paths_shell_worktree.py"),
    ("scripts/lib/brand_new_module.py", "tests/test_receipt_ci_guard.py"),
])
def test_missed_test_is_selected_by_reason_d(touched, test):
    selected = pps.select_tests(REPO, [touched])
    assert test in selected, sorted(selected)[:5]
    assert any(r.startswith("d:") for r in selected[test])


def _tests_tree(tmp_path, files):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


def test_reason_a_touched_test_file(tmp_path):
    _tests_tree(tmp_path, {"tests/test_x.py": "def test_x(): pass\n"})
    assert "a: touched test [tests/test_x.py]" in pps.select_tests(tmp_path, ["tests/test_x.py"])["tests/test_x.py"]


def test_reason_b_neighbour_by_name(tmp_path):
    _tests_tree(tmp_path, {"tests/test_foo.py": "", "tests/test_foo_more.py": "", "tests/test_foobar.py": ""})
    sel = pps.select_tests(tmp_path, ["scripts/lib/foo.py"])
    assert set(sel) == {"tests/test_foo.py", "tests/test_foo_more.py"}


def test_reason_c_imports_module(tmp_path):
    _tests_tree(tmp_path, {
        "tests/test_a.py": "import foo\n",
        "tests/test_b.py": "from foo import bar\n",
        "tests/test_c.py": "import importlib\nimportlib.import_module('foo')\n",
        "tests/test_d.py": "import other\n",
    })
    sel = pps.select_tests(tmp_path, ["scripts/foo.py"])
    assert set(sel) == {"tests/test_a.py", "tests/test_b.py", "tests/test_c.py"}
    assert all(r.startswith("c:") for rs in sel.values() for r in rs)


def test_unparseable_test_is_still_matched_by_text(tmp_path):
    _tests_tree(tmp_path, {"tests/test_bad.py": "def (:\n 'scripts/lib/foo.py'\n"})
    assert "tests/test_bad.py" in pps.select_tests(tmp_path, ["scripts/lib/foo.py"])


def test_single_segment_dir_needs_a_glob(tmp_path):
    _tests_tree(tmp_path, {
        "tests/test_plain.py": "X = ROOT / 'scripts'\n",
        "tests/test_globs.py": "X = ROOT / 'scripts'\nlist(X.rglob('*.py'))\n",
    })
    assert set(pps.select_tests(tmp_path, ["scripts/lib/a.py"])) == {"tests/test_globs.py"}


# --- the steps, on a throwaway repo -------------------------------------------------------------

def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


def _repo(tmp_path, with_scripts=True):
    repo = tmp_path / "repo"
    repo.mkdir()
    if with_scripts:
        for rel in ("scripts/ci_lint_patterns.py", "scripts/ci/check_function_size_ratchet.py",
                    "scripts/lib/function_size_gate.py"):
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(REPO / rel, repo / rel)
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-qb", "work")
    return repo


def _commit(repo, files):
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "work")


def _run(repo, capsys):
    code = pps.main(["--repo", str(repo), "--base", "main"])
    return code, capsys.readouterr().out


@needs_py311
def test_compile_red_on_a_312_only_construct(tmp_path, capsys):
    repo = _repo(tmp_path)
    _commit(repo, {"scripts/new312.py": "type X = int\n"})
    code, out = _run(repo, capsys)
    assert code == 1
    assert "[RED] compile" in out and pps.RED_TAIL in out


def test_compile_red_when_python311_is_missing(tmp_path, capsys, monkeypatch):
    repo = _repo(tmp_path)
    _commit(repo, {"scripts/ok.py": "X = 1\n"})
    monkeypatch.setattr(pps.shutil, "which", lambda name: None)
    code, out = _run(repo, capsys)
    assert code == 1
    assert "python3.11 not found: CI compiles on 3.11" in out


@needs_py311
def test_lint_red_on_added_silent_except_and_untouched_file_does_not_count(tmp_path, capsys):
    bad = "try:\n    pass\nexcept:\n    pass\n"
    repo = _repo(tmp_path)
    _commit(repo, {"scripts/untouched.py": bad})
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "work")
    _git(repo, "checkout", "-qb", "work2")
    _commit(repo, {"scripts/clean.py": "X = 1\n"})
    code, out = _run(repo, capsys)
    assert code == 0, out
    _commit(repo, {"scripts/dirty.py": bad})
    code, out = _run(repo, capsys)
    assert code == 1
    assert "[RED] lint patterns" in out and "dirty.py" not in out.split("[RED] lint patterns")[0]


@needs_py311
def test_ratchet_red_on_function_grown_past_limit(tmp_path, capsys):
    repo = _repo(tmp_path)
    small = "def big():\n" + "".join(f"    x{i} = {i}\n" for i in range(10)) + "    return 1\n"
    _commit(repo, {"scripts/lib/grow.py": small})
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "work")
    _git(repo, "checkout", "-qb", "work2")
    _commit(repo, {"scripts/lib/grow.py": "def big():\n" + "".join(f"    x{i} = {i}\n" for i in range(90)) + "    return 1\n"})
    code, out = _run(repo, capsys)
    assert code == 1
    assert "[RED] ratchet" in out and "big" in out


@needs_py311
def test_green_run_prints_no_test_selects_and_exits_zero(tmp_path, capsys):
    repo = _repo(tmp_path)
    _commit(repo, {"scripts/lib/foo.py": "X = 1\n"})
    code, out = _run(repo, capsys)
    assert code == 0, out
    assert "no test selects scripts/lib/foo.py" in out
    assert "pre-push check: GREEN" in out


@needs_py311
def test_failing_selected_test_is_red(tmp_path, capsys):
    repo = _repo(tmp_path)
    _commit(repo, {"scripts/lib/foo.py": "X = 1\n", "tests/test_foo.py": "def test_x():\n    assert False\n"})
    code, out = _run(repo, capsys)
    assert code == 1
    assert "[RED] tests" in out and pps.RED_TAIL in out


def test_missing_scripts_are_skipped_with_a_line(tmp_path, capsys):
    repo = _repo(tmp_path)
    _commit(repo, {"scripts/lib/foo.py": "X = 1\n"})
    (repo / "scripts/ci/check_function_size_ratchet.py").unlink()
    code, out = _run(repo, capsys)
    assert "skipped: ratchet (scripts/ci/check_function_size_ratchet.py not in this repo)" in out


# --- the plug-in ----------------------------------------------------------------------------------

def _config(repo, text):
    (repo / ".vnx").mkdir(exist_ok=True)
    (repo / ".vnx/config.yml").write_text(text, encoding="utf-8")


def test_plugin_command_failure_is_red_and_named(tmp_path, capsys):
    repo = _repo(tmp_path)
    _config(repo, 'pre_push_command: "exit 3"\n')
    code, out = _run(repo, capsys)
    assert code == 1
    assert "pre_push_command: exit 3" in out and pps.RED_TAIL in out


def test_plugin_command_success_skips_default_steps(tmp_path, capsys):
    repo = _repo(tmp_path)
    _commit(repo, {"scripts/new312.py": "type X = int\n"})
    _config(repo, "other: 1\npre_push_command: 'true'\n")
    code, out = _run(repo, capsys)
    assert code == 0
    assert "compile" not in out and "pre_push_command: true" in out


@pytest.mark.parametrize("cfg", ["", "pre_push_command:\n", "pre_push_command: ''\n", "garbage line\n", "x: 1\n"])
def test_no_command_and_no_scripts_exits_zero_with_notice(tmp_path, capsys, cfg):
    repo = _repo(tmp_path, with_scripts=False)
    _config(repo, cfg)
    code, out = _run(repo, capsys)
    assert code == 0
    assert pps.NO_COMMAND in out


# --- bad input and the CLI ------------------------------------------------------------------------

def test_unknown_base_is_exit_2(tmp_path, capsys):
    repo = _repo(tmp_path)
    assert pps.main(["--repo", str(repo), "--base", "nope/none"]) == 2


def test_not_a_repo_is_exit_2(tmp_path, capsys):
    assert pps.main(["--repo", str(tmp_path)]) == 2
    assert pps.main(["--repo", str(tmp_path / "absent")]) == 2


def test_unreadable_config_is_exit_2(tmp_path, capsys):
    repo = _repo(tmp_path)
    (repo / ".vnx").mkdir()
    (repo / ".vnx/config.yml").write_bytes(b"\xff\xfe\x00bad")
    assert pps.main(["--repo", str(repo), "--base", "main"]) == 2


def test_bin_vnx_pre_push_check_reaches_the_script():
    done = subprocess.run([str(REPO / "bin" / "vnx"), "pre-push-check", "--help"],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert "pre_push_check.py" in done.stdout and "--base" in done.stdout
