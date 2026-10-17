"""CHANGELOG fragments: the CI check (rules 1 to 4).

Every check test builds a throwaway git repository with a ``main`` and a PR
branch and runs ``scripts/ci/check_changelog_fragments.py`` on it as a
subprocess, with ``--base`` the merge base, exactly as the workflow step does.
Release PRs are built by the real assembler.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "ci"))

import check_changelog_fragments as check

CHECK = REPO_ROOT / "scripts" / "ci" / "check_changelog_fragments.py"
ASSEMBLER = REPO_ROOT / "scripts" / "release" / "assemble_changelog.py"

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


@pytest.mark.parametrize(
    "old,new,needle",
    [
        ("- legacy bullet\n\n", "", "removes or changes 1 line(s) under '## [Unreleased]'"),
        ("- legacy bullet\n", "- legacy bullet, reworded\n", "removes or changes 1 line(s) under '## [Unreleased]'"),
    ],
    ids=["dropped-bullet", "reworded-bullet"],
)
def test_non_release_pr_that_drops_or_changes_an_unreleased_line_fails(repo, old, new, needle):
    pr_branch(repo)
    commit(repo, {"CHANGELOG.md": BASE_CHANGELOG.replace(old, new, 1)})
    result = run_check(repo)
    assert result.returncode == 1
    assert needle in result.stdout
    assert "    - legacy bullet\n" in result.stdout


def test_non_release_pr_that_reorders_unreleased_lines_fails(tmp_path):
    two = BASE_CHANGELOG.replace("- legacy bullet\n", "- first\n\n- second\n")
    repo = init_repo(tmp_path / "two", {"CHANGELOG.md": two, "VERSION": "1.0.0\n", "changelog.d/README.md": "# rule\n"})
    pr_branch(repo)
    commit(repo, {"CHANGELOG.md": two.replace("- first\n\n- second\n", "- second\n\n- first\n")})
    result = run_check(repo)
    assert result.returncode == 1
    assert "reorders the lines under '## [Unreleased]'" in result.stdout


def test_blank_line_change_under_unreleased_passes(repo):
    pr_branch(repo)
    commit(repo, {"CHANGELOG.md": BASE_CHANGELOG.replace("- legacy bullet\n", "\n- legacy bullet\n")})
    result = run_check(repo)
    assert result.returncode == 0, result.stdout


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


@pytest.mark.parametrize(
    "appended,label",
    [
        ("\n## [1.0.0] - 2026-01-01\n\n- old entry, a second time\n", "1.0.0"),
        ("\n## [Unreleased]\n\n- a bullet below the first Unreleased\n", "Unreleased"),
    ],
    ids=["released-version", "unreleased"],
)
def test_a_duplicate_heading_anywhere_fails(repo, appended, label):
    pr_branch(repo)
    commit(repo, {"CHANGELOG.md": BASE_CHANGELOG + appended})
    result = run_check(repo)
    assert result.returncode == 1
    assert f"CHANGELOG.md has the heading '## [{label}]' 2 times" in result.stdout


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


def test_second_heading_for_an_existing_version_is_a_release_and_fails(repo):
    # The codex reproduction: the version set does not change, the legacy
    # bullet moves under the new heading and Unreleased reads empty.
    commit(repo, {"changelog.d/a.md": "- entry a\n"}, "add a")
    pr_branch(repo)
    commit(repo, {"CHANGELOG.md": BASE_CHANGELOG.replace(
        "## [Unreleased]\n\n", "## [Unreleased]\n\n## [1.0.0] - 2026-10-17\n\n- slipped past the check\n\n", 1
    )})
    result = run_check(repo)
    assert result.returncode == 1
    assert "release PR: compared with the assembler" in result.stdout
    assert "CHANGELOG.md has the heading '## [1.0.0]' 2 times" in result.stdout
    assert "changelog.d/a.md: a consumed fragment is left in place" in result.stdout


def test_release_pr_that_drops_prose_from_the_merge_base_unreleased_fails(tmp_path):
    # The old comparison stripped the prose from the expected side as release
    # intro, so a release PR could drop it unseen.  The fold now refuses it.
    base_cl = BASE_CHANGELOG.replace("- legacy bullet\n", "Pending note, not a bullet.\n\n- legacy bullet\n")
    repo = init_repo(tmp_path / "prose", {"CHANGELOG.md": base_cl, "VERSION": "1.0.0\n", "changelog.d/README.md": "# rule\n"})
    commit(repo, {"changelog.d/a.md": "- entry a\n"}, "add a")
    git(repo, "checkout", "-q", "-b", "release")
    released = base_cl.replace(
        "## [Unreleased]\n\nPending note, not a bullet.\n\n- legacy bullet\n\n",
        "## [Unreleased]\n\n## [1.1.0] - 2026-10-17\n\n- legacy bullet\n\n- entry a\n\n",
    )
    assert released != base_cl
    commit(repo, {"CHANGELOG.md": released, "VERSION": "1.1.0\n"}, "release without the note",
           remove=("changelog.d/a.md",))
    result = run_check(repo)
    assert result.returncode == 1
    assert "the merge base cannot be assembled" in result.stdout
    assert "'Pending note, not a bullet.'" in result.stdout


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


def test_listed_legacy_pr_that_also_drops_a_bullet_fails(tmp_path):
    entry = check.legacy_entry(77, BASE_CHANGELOG, with_bullets("- open PR bullet"))
    repo = legacy_repo(tmp_path, f"{entry}\n")
    commit(repo, {"CHANGELOG.md": with_bullets("- open PR bullet").replace("- legacy bullet\n\n", "")})
    result = run_check(repo, pr=77)
    assert result.returncode == 1
    assert "removes or changes 1 line(s) under '## [Unreleased]'" in result.stdout


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
