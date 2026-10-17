"""CHANGELOG fragments: the shared rule module and the release assembler.

The module (``scripts/release/changelog_fragments.py``) is tested on its pure
functions; the assembler (``scripts/release/assemble_changelog.py``) runs as a
subprocess against throwaway git repositories, except for the injected delete
failure, which needs the real ``main`` in-process with one failing call.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "release"))

import assemble_changelog
import changelog_fragments as cf

ASSEMBLER = REPO_ROOT / "scripts" / "release" / "assemble_changelog.py"

BASE_CHANGELOG = (
    "# Changelog\n"
    "\n"
    "Intro text.\n"
    "\n"
    "## [Unreleased]\n"
    "\n"
    "- legacy one\n"
    "\n"
    "- legacy two\n"
    "  continued\n"
    "\n"
    "## [1.0.0] - 2026-01-01\n"
    "\n"
    "- old entry\n"
)


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def git(repo: Path, *args: str, env: dict | None = None) -> str:
    full_env = None if env is None else {**os.environ, **env}
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, env=full_env
    ).stdout


def commit(repo: Path, files: dict, message: str = "c", remove: tuple = (), env: dict | None = None) -> str:
    for rel, content in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    for rel in remove:
        (repo / rel).unlink()
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message, env=env)
    return git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "t")
    git(path, "config", "commit.gpgsign", "false")
    commit(path, {"CHANGELOG.md": BASE_CHANGELOG, "changelog.d/README.md": "# rule\n"}, "base")
    return path


def run_assembler(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ASSEMBLER), "--rev", "main", "--date", "2026-10-17", *args],
        cwd=repo, capture_output=True, text=True, check=False,
    )


def changelog(repo: Path) -> str:
    return (repo / "CHANGELOG.md").read_text(encoding="utf-8")


def release_body(repo: Path, version: str = "1.1.0") -> list:
    """The non-blank lines under the new release heading."""
    section = cf.release_section(changelog(repo), version)
    return [line for line in section.split("\n")[1:] if line.strip()]


def snapshot(repo: Path) -> dict:
    """Every file under CHANGELOG.md and changelog.d/, byte for byte."""
    files = {"CHANGELOG.md": (repo / "CHANGELOG.md").read_bytes()}
    for entry in sorted((repo / "changelog.d").iterdir()):
        files[f"changelog.d/{entry.name}"] = entry.read_bytes()
    return files


# ---------------------------------------------------------------------------
# is_fragment_path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected",
    [
        ("changelog.d/20261017-clf-f1.md", True),
        ("changelog.d/a.b_c-1.md", True),
        ("changelog.d/README.md", False),
        ("changelog.d/sub/x.md", False),
        ("changelog.d/x.txt", False),
        ("changelog.d/x.MD", False),
        ("changelog.d/my fix.md", False),
        ("changelog.d/.hidden.md", False),
        ("changelog.d/-dash.md", False),
        ("changelog.d/.md", False),
        ("changelog.d", False),
        ("docs/changelog.d/x.md", False),
        ("", False),
    ],
    ids=[
        "dispatch-id-name", "dots-underscores-dashes", "readme-reserved", "nested-path",
        "not-markdown", "uppercase-extension", "space-in-name", "leading-dot", "leading-dash",
        "empty-name", "directory-itself", "other-directory", "empty-path",
    ],
)
def test_is_fragment_path(path, expected):
    assert cf.is_fragment_path(path) is expected


def test_select_fragments_skips_symlink_directory_readme_and_non_fragments(tmp_path):
    directory = tmp_path / "changelog.d"
    directory.mkdir()
    (directory / "real.md").write_text("- x\n", encoding="utf-8")
    (directory / "README.md").write_text("# rule\n", encoding="utf-8")
    (directory / "notes.txt").write_text("- x\n", encoding="utf-8")
    (directory / "nested.md").mkdir()
    (directory / "link.md").symlink_to(directory / "real.md")
    assert cf.select_fragments(tmp_path) == ["real.md"]


def test_select_fragments_without_directory_is_empty(tmp_path):
    assert cf.select_fragments(tmp_path) == []


# ---------------------------------------------------------------------------
# validate_fragment
# ---------------------------------------------------------------------------


def test_validate_accepts_continuation_and_sub_bullets():
    data = b"- first entry\n  continued here\n  - a sub-bullet\n\n- second entry\n"
    assert cf.validate_fragment("ok.md", data) is None


def test_validate_accepts_leading_blank_line():
    assert cf.validate_fragment("ok.md", b"\n- entry\n") is None


@pytest.mark.parametrize(
    "data,needle",
    [
        (b"- entry\n# Heading\n", "ok.md:2: a heading line"),
        (b"Some prose\n- entry\n", "ok.md:1: the first non-blank line must start with '- '"),
        (b"- entry\n  more\n- last", "ok.md:3: does not end with a newline"),
        (b"- entry\n- caf\xe9\n", "ok.md:2: not valid UTF-8"),
        (b"- entry\nunindented\n", "ok.md:2: a line must start with '- ' or with at least two spaces"),
        (b"- entry\n one space\n", "ok.md:2: a line must start with '- '"),
        (b"- entry\r\n", "ok.md:1: carriage return"),
        (b"", "ok.md: is empty"),
        (b"\n\n", "ok.md: holds no bullet"),
    ],
    ids=[
        "heading-line", "first-line-not-bullet", "no-final-newline", "not-utf8",
        "unindented-continuation", "one-space-indent", "crlf", "empty-file", "only-blank-lines",
    ],
)
def test_validate_refuses_with_the_line(data, needle):
    reason = cf.validate_fragment("ok.md", data)
    assert reason is not None
    assert needle in reason


# ---------------------------------------------------------------------------
# assemble (pure)
# ---------------------------------------------------------------------------


def test_assemble_puts_legacy_first_and_leaves_unreleased_empty():
    text, names = cf.assemble(BASE_CHANGELOG, [("b.md", "- frag b\n"), ("a.md", "- frag a\n  more\n")], "1.1.0", "2026-10-17")
    assert names == ["b.md", "a.md"]
    assert text == (
        "# Changelog\n"
        "\n"
        "Intro text.\n"
        "\n"
        "## [Unreleased]\n"
        "\n"
        "## [1.1.0] - 2026-10-17\n"
        "\n"
        "- legacy one\n"
        "\n"
        "- legacy two\n"
        "  continued\n"
        "\n"
        "- frag b\n"
        "\n"
        "- frag a\n"
        "  more\n"
        "\n"
        "## [1.0.0] - 2026-01-01\n"
        "\n"
        "- old entry\n"
    )
    assert cf.unreleased_lines(text) == [""]


def test_assemble_keeps_two_fragments_with_identical_content():
    text, _ = cf.assemble(BASE_CHANGELOG, [("a.md", "- same\n"), ("b.md", "- same\n")], "1.1.0", "2026-10-17")
    assert cf.release_section(text, "1.1.0").count("- same\n") == 2


def test_assemble_without_following_section_ends_with_one_newline():
    text, _ = cf.assemble("# C\n\n## [Unreleased]\n\n- x\n", [("a.md", "- y\n")], "0.1.0", "2026-10-17")
    assert text == "# C\n\n## [Unreleased]\n\n## [0.1.0] - 2026-10-17\n\n- x\n\n- y\n"


def test_assemble_refuses_missing_unreleased():
    with pytest.raises(cf.ChangelogError, match=r"no '## \[Unreleased\]'"):
        cf.assemble("# C\n\n## [1.0.0] - 2026-01-01\n", [], "1.1.0", "2026-10-17")


def test_assemble_refuses_existing_version():
    with pytest.raises(cf.ChangelogError, match=r"already has a '## \[1.0.0\]'"):
        cf.assemble(BASE_CHANGELOG, [], "1.0.0", "2026-10-17")


@pytest.mark.parametrize(
    "body,needle",
    [
        ("Pending note.\n\n- legacy\n", "CHANGELOG.md:5: under '## [Unreleased]' the first non-blank line must start with '- ': 'Pending note.'"),
        ("  indented first\n- legacy\n", "CHANGELOG.md:5: under '## [Unreleased]' the first non-blank line must start with '- '"),
        ("- legacy\n### Added\n", "CHANGELOG.md:6: under '## [Unreleased]' a heading line"),
        ("- legacy\nprose after a bullet\n", "CHANGELOG.md:6: under '## [Unreleased]' a line must start with '- ' or with at least two spaces"),
    ],
    ids=["prose-first", "continuation-first", "subheading", "prose-after-bullet"],
)
def test_assemble_refuses_an_unreleased_line_that_is_no_bullet(body, needle):
    # Such a line would land between the new heading and its first bullet,
    # where the CI check reads the release intro and compares nothing.
    text = f"# C\n\n## [Unreleased]\n\n{body}\n## [1.0.0] - 2026-01-01\n\n- old\n"
    with pytest.raises(cf.ChangelogError) as refused:
        cf.assemble(text, [("a.md", "- a\n")], "1.1.0", "2026-10-17")
    assert needle in str(refused.value)


# ---------------------------------------------------------------------------
# The assembler: order
# ---------------------------------------------------------------------------


def test_three_commits_land_in_first_parent_order_even_with_reversed_dates(repo):
    # Commit order c, a, b; author and committer dates run backwards.
    for name, date in (("c", "2026-03-01T00:00:00"), ("a", "2026-02-01T00:00:00"), ("b", "2026-01-01T00:00:00")):
        env = {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        commit(repo, {f"changelog.d/{name}.md": f"- entry {name}\n"}, f"add {name}", env=env)
    result = run_assembler(repo, "--version", "1.1.0")
    assert result.returncode == 0, result.stderr
    assert release_body(repo) == ["- legacy one", "- legacy two", "  continued", "- entry c", "- entry a", "- entry b"]


def test_two_fragments_in_one_commit_sort_by_name(repo):
    commit(repo, {"changelog.d/zeta.md": "- zeta\n", "changelog.d/alpha.md": "- alpha\n"})
    assert run_assembler(repo, "--version", "1.1.0").returncode == 0
    assert release_body(repo)[-2:] == ["- alpha", "- zeta"]


def test_merge_commit_is_the_first_parent_introduction(repo):
    git(repo, "checkout", "-q", "-b", "side")
    commit(repo, {"changelog.d/side.md": "- side\n"}, "side work, older than main's next commit")
    git(repo, "checkout", "-q", "main")
    commit(repo, {"changelog.d/first.md": "- first\n"}, "first on main")
    git(repo, "merge", "-q", "--no-ff", "-m", "merge side", "side")
    commit(repo, {"changelog.d/last.md": "- last\n"}, "last on main")
    assert run_assembler(repo, "--version", "1.1.0").returncode == 0
    assert release_body(repo)[-3:] == ["- first", "- side", "- last"]


def test_reused_name_sorts_by_its_newest_addition(repo):
    commit(repo, {"changelog.d/again.md": "- first life\n"}, "add again")
    assert run_assembler(repo, "--version", "1.1.0").returncode == 0
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "release 1.1.0")
    commit(repo, {"changelog.d/between.md": "- between\n"}, "add between")
    commit(repo, {"changelog.d/again.md": "- second life\n"}, "add again, second time")
    result = run_assembler(repo, "--version", "1.2.0")
    assert result.returncode == 0, result.stderr
    assert release_body(repo, "1.2.0") == ["- between", "- second life"]


def test_branch_only_fragment_is_refused_by_name(repo):
    commit(repo, {"changelog.d/on-main.md": "- main\n"})
    git(repo, "checkout", "-q", "-b", "release")
    commit(repo, {"changelog.d/branch-only.md": "- branch\n"})
    before = snapshot(repo)
    result = run_assembler(repo, "--version", "1.1.0")
    assert result.returncode == 1
    assert "changelog.d/branch-only.md" in result.stderr
    assert "on-main.md" not in result.stderr
    assert snapshot(repo) == before


def test_fragment_deleted_on_the_release_branch_is_refused_by_name(repo):
    commit(repo, {"changelog.d/kept.md": "- kept\n", "changelog.d/dropped.md": "- dropped\n"})
    git(repo, "checkout", "-q", "-b", "release")
    git(repo, "rm", "-q", "changelog.d/dropped.md")
    git(repo, "commit", "-q", "-m", "drop a fragment by hand")
    before = snapshot(repo)
    result = run_assembler(repo, "--version", "1.1.0")
    assert result.returncode == 1
    assert "present at main but missing from the tree: changelog.d/dropped.md" in result.stderr
    assert snapshot(repo) == before


def test_fragment_deleted_on_main_and_recreated_on_the_release_branch_is_refused(repo):
    # The name landed on main once, so its newest addition is in the history;
    # the bytes on the release branch never landed there.
    commit(repo, {"changelog.d/again.md": "- first life\n"}, "add again")
    commit(repo, {}, "drop again on main", remove=("changelog.d/again.md",))
    commit(repo, {"changelog.d/kept.md": "- kept\n"}, "add kept")
    git(repo, "checkout", "-q", "-b", "release")
    commit(repo, {"changelog.d/again.md": "- second life, branch only\n"}, "recreate again")
    before = snapshot(repo)
    result = run_assembler(repo, "--version", "1.1.0")
    assert result.returncode == 1
    assert "not in main: changelog.d/again.md" in result.stderr
    assert "kept.md" not in result.stderr
    assert snapshot(repo) == before


def test_fragment_edited_on_the_release_branch_is_refused(repo):
    commit(repo, {"changelog.d/edited.md": "- as merged\n", "changelog.d/kept.md": "- kept\n"})
    git(repo, "checkout", "-q", "-b", "release")
    commit(repo, {"changelog.d/edited.md": "- rewritten on the release branch\n"}, "edit a fragment")
    before = snapshot(repo)
    result = run_assembler(repo, "--version", "1.1.0")
    assert result.returncode == 1
    assert "differs from the blob at main: changelog.d/edited.md" in result.stderr
    assert "kept.md" not in result.stderr
    assert snapshot(repo) == before


def test_uncommitted_fragment_is_refused_by_name(repo):
    (repo / "changelog.d" / "loose.md").write_text("- loose\n", encoding="utf-8")
    before = snapshot(repo)
    result = run_assembler(repo, "--version", "1.1.0")
    assert result.returncode == 1
    assert "changelog.d/loose.md" in result.stderr
    assert snapshot(repo) == before


def test_rev_that_is_not_an_ancestor_is_refused(repo):
    git(repo, "checkout", "-q", "-b", "elsewhere")
    commit(repo, {"changelog.d/x.md": "- x\n"})
    git(repo, "checkout", "-q", "main")
    result = subprocess.run(
        [sys.executable, str(ASSEMBLER), "--rev", "elsewhere", "--version", "1.1.0"],
        cwd=repo, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1
    assert "not an ancestor of HEAD" in result.stderr


# ---------------------------------------------------------------------------
# The assembler: effects and refusals
# ---------------------------------------------------------------------------


def test_fragments_are_deleted_and_readme_survives(repo):
    commit(repo, {"changelog.d/one.md": "- one\n", "changelog.d/two.md": "- two\n"})
    result = run_assembler(repo, "--version", "1.1.0")
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in (repo / "changelog.d").iterdir()) == ["README.md"]
    assert "deleted:" in result.stdout and "changelog.d/one.md" in result.stdout
    assert cf.parse_heading("## [1.1.0] - 2026-10-17") is not None
    assert "## [1.1.0] - 2026-10-17" in changelog(repo)


def test_check_mode_changes_nothing(repo):
    commit(repo, {"changelog.d/one.md": "- one\n"})
    before = snapshot(repo)
    result = run_assembler(repo, "--version", "1.1.0", "--check")
    assert result.returncode == 0, result.stderr
    assert snapshot(repo) == before
    assert git(repo, "status", "--porcelain") == ""
    assert "## [1.1.0] - 2026-10-17" in result.stdout
    assert "would delete:" in result.stdout and "changelog.d/one.md" in result.stdout


@pytest.mark.parametrize(
    "dirty_path,content",
    [("CHANGELOG.md", BASE_CHANGELOG + "- extra\n"), ("changelog.d/one.md", "- one, edited\n")],
    ids=["dirty-changelog", "dirty-fragment"],
)
def test_dirty_tree_is_refused_with_the_rollback_command(repo, dirty_path, content):
    commit(repo, {"changelog.d/one.md": "- one\n"})
    (repo / dirty_path).write_text(content, encoding="utf-8")
    before = snapshot(repo)
    result = run_assembler(repo, "--version", "1.1.0")
    assert result.returncode == 1
    assert "git checkout -- CHANGELOG.md changelog.d" in result.stderr
    assert dirty_path in result.stderr
    assert snapshot(repo) == before


def test_existing_version_heading_is_refused(repo):
    commit(repo, {"changelog.d/one.md": "- one\n"})
    before = snapshot(repo)
    result = run_assembler(repo, "--version", "1.0.0")
    assert result.returncode == 1
    assert "already has a '## [1.0.0]'" in result.stderr
    assert snapshot(repo) == before


def test_invalid_fragment_is_refused_before_any_write(repo):
    commit(repo, {"changelog.d/good.md": "- good\n", "changelog.d/bad.md": "# heading\n"})
    before = snapshot(repo)
    result = run_assembler(repo, "--version", "1.1.0")
    assert result.returncode == 1
    assert "bad.md:1: a heading line" in result.stderr
    assert snapshot(repo) == before


def test_failing_delete_restores_every_file_byte_for_byte(repo, monkeypatch, capsys):
    commit(repo, {"changelog.d/a.md": "- a\n", "changelog.d/b.md": "- b\n", "changelog.d/c.md": "- c\n"})
    before = snapshot(repo)
    real_remove = os.remove
    calls = []

    def remove_failing_on_second_call(path, *args, **kwargs):
        calls.append(str(path))
        if len(calls) == 2:
            raise PermissionError(13, "injected failure", str(path))
        return real_remove(path, *args, **kwargs)

    monkeypatch.chdir(repo)
    monkeypatch.setattr(assemble_changelog.os, "remove", remove_failing_on_second_call)
    status = assemble_changelog.main(["--version", "1.1.0", "--date", "2026-10-17", "--rev", "main"])
    monkeypatch.undo()
    assert status == 1
    assert len(calls) == 2
    assert "restored the original files" in capsys.readouterr().err
    assert snapshot(repo) == before
    assert git(repo, "status", "--porcelain") == ""


def test_rollback_after_a_killed_run_then_rerun_is_byte_identical(repo):
    commit(repo, {"changelog.d/a.md": "- a\n", "changelog.d/b.md": "- b\n"})
    assert run_assembler(repo, "--version", "1.1.0").returncode == 0
    clean_output = snapshot(repo)
    git(repo, "checkout", "--", "CHANGELOG.md", "changelog.d")
    # The state a kill between the steps leaves: CHANGELOG.md written, one fragment deleted.
    (repo / "CHANGELOG.md").write_bytes(clean_output["CHANGELOG.md"])
    (repo / "changelog.d" / "a.md").unlink()
    refused = run_assembler(repo, "--version", "1.1.0")
    assert refused.returncode == 1
    assert "git checkout -- CHANGELOG.md changelog.d" in refused.stderr
    git(repo, "checkout", "--", "CHANGELOG.md", "changelog.d")
    rerun = run_assembler(repo, "--version", "1.1.0")
    assert rerun.returncode == 0, rerun.stderr
    assert snapshot(repo) == clean_output


# ---------------------------------------------------------------------------
# The assembler: one run at a time
# ---------------------------------------------------------------------------

LOCK_NAME = "vnx-assemble-changelog.lock"

# One assembler run in its own process.  It reports "ready" and waits for "go"
# on stdin: the start barrier.  After its preparation it reports "prepared" and
# waits for "apply", so the test decides when a prepared run may write.
CONTENDER = r"""
import sys
sys.path.insert(0, sys.argv[1])
import assemble_changelog


def wait_for(word):
    if sys.stdin.readline().strip() != word:
        sys.exit(f"contender: expected {word!r} on stdin")


real_prepare = assemble_changelog.prepare


def held_prepare(*args, **kwargs):
    plan = real_prepare(*args, **kwargs)
    print("prepared", flush=True)
    wait_for("apply")
    return plan


assemble_changelog.prepare = held_prepare
print("ready", flush=True)
wait_for("go")
sys.exit(assemble_changelog.main(sys.argv[2:]))
"""


def tell(proc: subprocess.Popen, word: str) -> None:
    proc.stdin.write(f"{word}\n")
    proc.stdin.flush()


def test_two_runs_released_by_one_barrier_one_assembles_and_the_other_refuses(repo):
    commit(repo, {"changelog.d/a.md": "- a\n", "changelog.d/b.md": "- b\n"})
    assembled, _ = cf.assemble(BASE_CHANGELOG, [("a.md", "- a\n"), ("b.md", "- b\n")], "1.1.0", "2026-10-17")
    argv = [sys.executable, "-c", CONTENDER, str(ASSEMBLER.parent),
            "--version", "1.1.0", "--date", "2026-10-17", "--rev", "main"]
    procs = [
        subprocess.Popen(argv, cwd=repo, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
        for _ in range(2)
    ]
    try:
        assert [proc.stdout.readline() for proc in procs] == ["ready\n", "ready\n"]
        for proc in procs:
            tell(proc, "go")
        # A run that got through its preparation waits there; a refused run
        # exits, which ends its stdout.  Only then does any prepared run write,
        # one after the other: on a tree without the lock both prepare first.
        prepared = [proc for proc in procs if proc.stdout.readline() == "prepared\n"]
        for proc in prepared:
            tell(proc, "apply")
            proc.wait(timeout=60)
        outcomes = [(proc.wait(timeout=60), proc.communicate(timeout=60)[1]) for proc in procs]
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    assert sorted(code for code, _err in outcomes) == [0, 1], outcomes
    refusal = next(err for code, err in outcomes if code == 1)
    assert "another assemble_changelog.py run holds the lock" in refusal
    assert LOCK_NAME in refusal
    assert changelog(repo) == assembled
    assert sorted(p.name for p in (repo / "changelog.d").iterdir()) == ["README.md"]


def test_a_run_refuses_while_another_holds_the_lock_and_runs_once_it_is_released(repo):
    commit(repo, {"changelog.d/a.md": "- a\n"})
    before = snapshot(repo)
    fd = os.open(repo / ".git" / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        held = run_assembler(repo, "--version", "1.1.0")
    finally:
        os.close(fd)
    assert held.returncode == 1
    assert "another assemble_changelog.py run holds the lock" in held.stderr
    assert LOCK_NAME in held.stderr
    assert snapshot(repo) == before
    released = run_assembler(repo, "--version", "1.1.0")
    assert released.returncode == 0, released.stderr
    assert release_body(repo) == ["- legacy one", "- legacy two", "  continued", "- a"]


def test_identical_fragments_both_appear(repo):
    commit(repo, {"changelog.d/a.md": "- same text\n", "changelog.d/b.md": "- same text\n"})
    assert run_assembler(repo, "--version", "1.1.0").returncode == 0
    assert release_body(repo).count("- same text") == 2


def test_no_fragment_directory_is_a_no_op(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    git(tmp_path, "config", "commit.gpgsign", "false")
    commit(tmp_path, {"CHANGELOG.md": BASE_CHANGELOG})
    result = run_assembler(tmp_path, "--version", "1.1.0")
    assert result.returncode == 0
    assert "nothing to assemble" in result.stdout
    assert (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8") == BASE_CHANGELOG


@pytest.mark.parametrize(
    "args,needle",
    [(("--version", "1.1"), "is not X.Y.Z"), (("--version", "1.1.0", "--date", "17-10-2026"), "is not YYYY-MM-DD")],
    ids=["bad-version", "bad-date"],
)
def test_usage_errors_exit_two(repo, args, needle):
    result = subprocess.run(
        [sys.executable, str(ASSEMBLER), *args], cwd=repo, capture_output=True, text=True, check=False
    )
    assert result.returncode == 2
    assert needle in result.stderr
