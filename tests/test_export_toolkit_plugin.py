"""Behaviour of scripts/maintenance/export_toolkit_plugin.sh (context-rotation track, E).

Every source and target here is a throwaway git repo under tmp_path. Nothing
touches a real toolkit clone and nothing uses the network.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "maintenance" / "export_toolkit_plugin.sh"
REAL_PLUGIN = REPO / "packages" / "claude-code-toolkit" / "context-rotation"
OWNED = "packages/claude-code-toolkit/context-rotation"

GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "Fixture",
    "GIT_AUTHOR_EMAIL": "fixture@localhost",
    "GIT_COMMITTER_NAME": "Fixture",
    "GIT_COMMITTER_EMAIL": "fixture@localhost",
}
SCRIPT_ENV = {k: v for k, v in GIT_ENV.items() if not k.startswith(("GIT_AUTHOR", "GIT_COMMITTER"))}

TOOLKIT_README = (
    "# claude-code-toolkit\n\nSmall tools.\n\n## Tools\n\n"
    "| Tool | What it does |\n|---|---|\n"
    "| [`timekeeper/`](timekeeper/) | Tracks time. |\n\n## License\n\nMIT\n"
)
TIMEKEEPER_ENTRY = '{"name":   "timekeeper",\n      "source": "./timekeeper",   "description": "Tracks time."}'
TOOLKIT_MARKETPLACE = (
    '{\n    "name": "claude-code-toolkit",\n    "owner": {"name": "Someone"},\n'
    '    "plugins": [\n      %s\n    ]\n}\n' % TIMEKEEPER_ENTRY
)
EXPORTED_ENTRY = {
    "name": "context-rotation", "source": "./context-rotation",
    "version": "0.1.0", "description": "Guards session context.",
}
BRANCH = "export/context-rotation-0.1.0"
LEAK = "/Us" + "ers/someone/project"
CONTACT = "Contact: someone" + "@" + "example.org"
# A git hook that refuses to delete an export branch: the one way to make the
# script's own cleanup fail on demand without touching the script.
REFUSE_BRANCH_DELETE = """#!/bin/sh
[ "$1" = prepared ] || exit 0
while read -r old new ref; do
    case "$ref:$new" in
        refs/heads/export/*:0000000000000000000000000000000000000000) exit 1 ;;
    esac
done
exit 0
"""


def git(cwd, *args, check=True):
    result = subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise AssertionError("git %s failed: %s" % (" ".join(args), result.stderr))
    return result.stdout.strip()


def write(root, rel, text):
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def commit_source(src, version="0.1.0", files=None, remove=(), message="source"):
    base = Path(src) / OWNED
    write(base, ".claude-plugin/plugin.json", json.dumps(
        {"name": "context-rotation", "version": version, "description": "Guards session context."}, indent=2) + "\n")
    for rel, text in (files or {}).items():
        path = write(base, rel, text)
        if rel.endswith(".sh"):
            path.chmod(0o755)
    for rel in remove:
        (base / rel).unlink()
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", message)
    sha = git(src, "rev-parse", "HEAD")
    git(src, "update-ref", "refs/remotes/origin/main", sha)
    return sha


@pytest.fixture
def source(tmp_path):
    src = tmp_path / "source"
    src.mkdir()
    git(src, "init", "-q", "-b", "main")
    plugin_tests = Path(OWNED) / "tests"
    for name in ("test_isolation.py", "conftest.py"):
        write(src, plugin_tests / name, (REAL_PLUGIN / "tests" / name).read_text(encoding="utf-8"))
    write(src, "unrelated.txt", "not exported\n")
    commit_source(src, files={"hooks/run.sh": "#!/bin/sh\nexit 0\n", "hooks/old.sh": "old\n", "skills/x.md": "skill\n"})
    return src


@pytest.fixture
def target(tmp_path):
    tgt = tmp_path / "toolkit"
    tgt.mkdir()
    git(tgt, "init", "-q", "-b", "main")
    write(tgt, "README.md", TOOLKIT_README)
    write(tgt, "timekeeper/tk.txt", "tk\n")
    write(tgt, "docs/keep.md", "keep\n")
    git(tgt, "add", "-A")
    git(tgt, "commit", "-q", "-m", "initial")
    return tgt


def export(source, target, sha=None, tag="context-rotation-v0.1.0"):
    sha = sha or git(source, "rev-parse", "HEAD")
    return subprocess.run(
        ["sh", str(SCRIPT), "--source", str(source), "--commit", sha, "--target", str(target), "--tag", tag],
        env=SCRIPT_ENV, capture_output=True, text=True,
    )


def branches(target):
    return git(target, "branch", "--format=%(refname:short)").split()


def state(target):
    """What a failed or refused run must leave exactly as it was."""
    return {
        "head": git(target, "rev-parse", "HEAD"),
        "ref": git(target, "symbolic-ref", "-q", "--short", "HEAD", check=False),
        "branches": git(target, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads"),
        "status": git(target, "status", "--porcelain", "--untracked-files=all"),
    }


def assert_untouched(target, head):
    assert git(target, "rev-parse", "HEAD") == head
    assert branches(target) == ["main"]
    assert git(target, "status", "--porcelain", "--untracked-files=all") == ""
    assert not (Path(target) / "context-rotation").exists()


def commit_all(repo, message):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def marketplace(target):
    return json.loads((Path(target) / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))


def test_first_export_creates_folder_marketplace_and_readme_row(source, target):
    sha = git(source, "rev-parse", "HEAD")
    result = export(source, target)
    assert result.returncode == 0, result.stderr
    assert git(target, "branch", "--show-current") == BRANCH
    root = Path(target) / "context-rotation"
    assert (root / ".claude-plugin" / "plugin.json").is_file()
    assert (root / "hooks" / "run.sh").read_text() == "#!/bin/sh\nexit 0\n"
    assert os.access(root / "hooks" / "run.sh", os.X_OK)
    assert git(target, "ls-files", "-s", "context-rotation/hooks/run.sh").startswith("100755 ")
    assert not (Path(target) / "unrelated.txt").exists()
    assert not (Path(target) / "packages").exists()
    doc = marketplace(target)
    assert doc["name"] == "claude-code-toolkit"
    assert doc["owner"]["name"]
    assert doc["plugins"] == [EXPORTED_ENTRY]
    readme = (Path(target) / "README.md").read_text()
    rows = [line for line in readme.splitlines() if "context-rotation/" in line]
    assert len(rows) == 1 and rows[0].startswith("| ") and "Guards session context." in rows[0]
    assert readme.index(rows[0]) < readme.index("## License")
    assert git(target, "rev-list", "--count", "main..HEAD") == "1"
    assert sha in git(target, "log", "-1", "--format=%B")
    assert git(target, "tag") == ""
    assert "git -C" in result.stdout and "push" in result.stdout and "context-rotation-v0.1.0" in result.stdout


def test_export_into_a_fresh_empty_repo_creates_readme_and_marketplace(source, tmp_path):
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    git(fresh, "init", "-q", "-b", "main")
    git(fresh, "commit", "-q", "--allow-empty", "-m", "init")
    result = export(source, fresh)
    assert result.returncode == 0, result.stderr
    assert marketplace(fresh)["plugins"] == [EXPORTED_ENTRY]
    readme = (fresh / "README.md").read_text()
    assert "## Tools" in readme and len([ln for ln in readme.splitlines() if "context-rotation/" in ln]) == 1


def test_re_export_of_the_same_commit_changes_nothing(source, target):
    assert export(source, target).returncode == 0
    head = git(target, "rev-parse", "HEAD")
    readme = (Path(target) / "README.md").read_text()
    market = (Path(target) / ".claude-plugin" / "marketplace.json").read_text()
    again = export(source, target)
    assert again.returncode == 0, again.stderr
    assert "nothing" in again.stdout.lower()
    assert git(target, "rev-parse", "HEAD") == head
    assert (Path(target) / "README.md").read_text() == readme
    assert (Path(target) / ".claude-plugin" / "marketplace.json").read_text() == market
    assert len([ln for ln in readme.splitlines() if "context-rotation/" in ln]) == 1


def test_existing_marketplace_entries_stay_byte_for_byte(source, target):
    write(target, ".claude-plugin/marketplace.json", TOOLKIT_MARKETPLACE)
    git(target, "add", "-A")
    git(target, "commit", "-q", "-m", "marketplace")
    assert export(source, target).returncode == 0
    text = (Path(target) / ".claude-plugin" / "marketplace.json").read_text()
    assert TIMEKEEPER_ENTRY in text
    assert text.startswith('{\n    "name": "claude-code-toolkit",\n    "owner": {"name": "Someone"},\n')
    doc = marketplace(target)
    assert [p["name"] for p in doc["plugins"]] == ["timekeeper", "context-rotation"]
    commit_source(source, version="0.1.1", files={"hooks/run.sh": "#!/bin/sh\nexit 1\n"})
    assert export(source, target, tag="context-rotation-v0.1.1").returncode == 0
    text = (Path(target) / ".claude-plugin" / "marketplace.json").read_text()
    assert TIMEKEEPER_ENTRY in text
    doc = marketplace(target)
    assert [p["name"] for p in doc["plugins"]] == ["timekeeper", "context-rotation"]
    assert doc["plugins"][1]["version"] == "0.1.1"


def test_removed_source_file_is_removed_and_outside_files_survive(source, target):
    assert export(source, target).returncode == 0
    assert (Path(target) / "context-rotation" / "hooks" / "old.sh").exists()
    commit_source(source, version="0.1.1", remove=["hooks/old.sh"])
    result = export(source, target, tag="context-rotation-v0.1.1")
    assert result.returncode == 0, result.stderr
    assert not (Path(target) / "context-rotation" / "hooks" / "old.sh").exists()
    assert (Path(target) / "context-rotation" / "hooks" / "run.sh").exists()
    assert (Path(target) / "timekeeper" / "tk.txt").read_text() == "tk\n"
    assert (Path(target) / "docs" / "keep.md").read_text() == "keep\n"
    changed = git(target, "show", "--name-only", "--format=", "HEAD").splitlines()
    assert "context-rotation/hooks/old.sh" in changed
    assert all(
        p.startswith("context-rotation/") or p in (".claude-plugin/marketplace.json", "README.md") for p in changed
    )


def test_removed_file_with_a_quoted_name_is_removed(source, target):
    odd = 'skills/a "b"\\é.md'
    commit_source(source, files={odd: "odd name\n"})
    assert export(source, target).returncode == 0
    root = Path(target) / "context-rotation"
    assert (root / odd).read_text() == "odd name\n"
    commit_source(source, version="0.1.1", remove=[odd])
    result = export(source, target, tag="context-rotation-v0.1.1")
    assert result.returncode == 0, result.stderr
    assert not (root / odd).exists()
    assert (root / "skills" / "x.md").exists()
    tracked = git(target, "ls-files", "-z", "--", "context-rotation").split("\0")
    assert "context-rotation/" + odd not in tracked
    assert git(target, "status", "--porcelain", "--untracked-files=all") == ""


def test_local_changes_under_the_owned_paths_are_refused(source, target):
    head = git(target, "rev-parse", "HEAD")
    write(target, "context-rotation/stray.txt", "mine\n")
    result = export(source, target)
    assert result.returncode != 0 and "local changes" in result.stderr
    assert branches(target) == ["main"] and git(target, "rev-parse", "HEAD") == head
    assert not (Path(target) / "context-rotation" / "hooks").exists()
    assert not (Path(target) / ".claude-plugin").exists()
    (Path(target) / "context-rotation" / "stray.txt").unlink()
    assert export(source, target).returncode == 0
    write(target, "context-rotation/hooks/run.sh", "edited\n")
    commit_source(source, version="0.1.1", files={"hooks/run.sh": "new\n"})
    after = export(source, target, tag="context-rotation-v0.1.1")
    assert after.returncode != 0 and "local changes" in after.stderr
    assert (Path(target) / "context-rotation" / "hooks" / "run.sh").read_text() == "edited\n"


def test_local_changes_to_the_readme_are_refused(source, target):
    head = git(target, "rev-parse", "HEAD")
    write(target, "README.md", TOOLKIT_README + "\nlocal edit\n")
    result = export(source, target)
    assert result.returncode != 0 and "local changes" in result.stderr
    assert git(target, "rev-parse", "HEAD") == head and branches(target) == ["main"]


# --- finding 1: only the export's own changes, and a rollback to a known clean start


def test_staged_change_outside_the_owned_paths_is_refused_and_stays_staged(source, target):
    write(target, "docs/keep.md", "keep, edited\n")
    git(target, "add", "docs/keep.md")
    before = state(target)
    result = export(source, target)
    assert result.returncode == 1, result.stdout
    assert "local changes" in result.stderr and "docs/keep.md" in result.stderr
    assert state(target) == before
    assert git(target, "diff", "--cached", "--name-only") == "docs/keep.md"
    assert (Path(target) / "docs" / "keep.md").read_text() == "keep, edited\n"
    assert not (Path(target) / "context-rotation").exists()


def test_staged_change_is_not_lost_when_the_export_fails(source, target):
    write(target, "docs/keep.md", "keep, edited\n")
    git(target, "add", "docs/keep.md")
    before = state(target)
    commit_source(source, files={"hooks/leaky.txt": "path %s\n" % LEAK})
    result = export(source, target)
    assert result.returncode == 1
    assert state(target) == before
    assert git(target, "diff", "--cached", "--name-only") == "docs/keep.md"
    assert (Path(target) / "docs" / "keep.md").read_text() == "keep, edited\n"


@pytest.mark.parametrize("change", ["unstaged", "untracked"])
def test_any_local_change_in_the_clone_is_refused(source, target, change):
    if change == "unstaged":
        name = "docs/keep.md"
        write(target, name, "edited, not staged\n")
    else:
        name = "notes/scratch.txt"
        write(target, name, "mine\n")
    before = state(target)
    result = export(source, target)
    assert result.returncode == 1, result.stdout
    assert "local changes" in result.stderr and name in result.stderr
    assert state(target) == before
    assert not (Path(target) / "context-rotation").exists()


@pytest.mark.parametrize("operation", ["bisect", "rebase"])
def test_an_operation_in_progress_is_refused(source, target, operation):
    if operation == "bisect":
        git(target, "bisect", "start")
    else:
        write(target, "docs/second.md", "second\n")
        commit_all(target, "second")
        git(target, "rebase", "--exec", "false", "HEAD~1", check=False)
    assert git(target, "status", "--porcelain", "--untracked-files=all") == ""
    before = state(target)
    result = export(source, target)
    assert result.returncode == 1, result.stdout
    assert operation in result.stderr
    assert state(target) == before
    assert not (Path(target) / "context-rotation").exists()


def test_file_the_toolkit_ignores_is_still_committed(source, target):
    write(target, ".gitignore", "*.md\n")
    commit_all(target, "ignore markdown")
    result = export(source, target)
    assert result.returncode == 0, result.stderr
    committed = git(target, "ls-tree", "-r", "--name-only", "HEAD", "--", "context-rotation").splitlines()
    assert "context-rotation/skills/x.md" in committed
    assert git(target, "status", "--porcelain", "--untracked-files=all", "--ignored") == ""


def test_rollback_also_removes_exported_files_the_toolkit_ignores(source, target):
    write(target, ".gitignore", "*.md\n")
    commit_all(target, "ignore markdown")
    head = git(target, "rev-parse", "HEAD")
    before = state(target)
    commit_source(source, files={"hooks/leaky.txt": "path %s\n" % LEAK})
    result = export(source, target)
    assert result.returncode == 1, result.stdout
    assert_untouched(target, head)
    assert state(target) == before
    assert git(target, "status", "--porcelain", "--untracked-files=all", "--ignored") == ""


def test_ignored_file_under_the_plugin_folder_is_refused(source, target):
    write(target, ".gitignore", "__pycache__/\n")
    commit_all(target, "ignore caches")
    assert export(source, target).returncode == 0
    write(target, "context-rotation/hooks/__pycache__/run.cpython-312.pyc", "cache\n")
    commit_source(source, version="0.1.1", files={"hooks/run.sh": "#!/bin/sh\nexit 1\n"})
    before = state(target)
    result = export(source, target, tag="context-rotation-v0.1.1")
    assert result.returncode == 1, result.stdout
    assert "context-rotation/hooks/__pycache__" in result.stderr
    assert state(target) == before
    assert (Path(target) / "context-rotation/hooks/__pycache__/run.cpython-312.pyc").read_text() == "cache\n"


# --- finding 2: everything the export writes is built from the export branch's own base


def test_re_export_from_main_keeps_what_the_export_branch_already_carries(source, target):
    assert export(source, target).returncode == 0
    market = Path(target) / ".claude-plugin" / "marketplace.json"
    doc = json.loads(market.read_text(encoding="utf-8"))
    doc["plugins"].append({"name": "timekeeper", "source": "./timekeeper", "description": "Tracks time."})
    market.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    readme = Path(target) / "README.md"
    kept = [ln for ln in readme.read_text(encoding="utf-8").splitlines() if "context-rotation/" not in ln]
    readme.write_text("\n".join(kept + ["", "Notes kept on the export branch."]) + "\n", encoding="utf-8")
    commit_all(target, "operator edits on the export branch")
    git(target, "checkout", "-q", "main")
    result = export(source, target)
    assert result.returncode == 0, result.stderr
    assert git(target, "branch", "--show-current") == BRANCH
    assert git(target, "rev-parse", "main") == git(target, "rev-parse", "HEAD~3")
    plugins = json.loads(git(target, "show", "HEAD:.claude-plugin/marketplace.json"))["plugins"]
    assert [p["name"] for p in plugins] == ["context-rotation", "timekeeper"]
    assert plugins[0] == EXPORTED_ENTRY
    text = git(target, "show", "HEAD:README.md")
    assert "Notes kept on the export branch." in text
    assert len([ln for ln in text.splitlines() if "context-rotation/" in ln]) == 1
    assert git(target, "status", "--porcelain", "--untracked-files=all") == ""


# --- finding 3: no write through a link


def _link_readme(target, outside):
    outside.write_text("theirs\n", encoding="utf-8")
    (Path(target) / "README.md").unlink()
    os.symlink(outside, Path(target) / "README.md")


def _link_plugin_dir(target, outside):
    outside.mkdir()
    (outside / "marketplace.json").write_text("theirs\n", encoding="utf-8")
    os.symlink(outside, Path(target) / ".claude-plugin")


def _link_plugin_file(target, outside):
    outside.write_text("theirs\n", encoding="utf-8")
    (Path(target) / "context-rotation" / "hooks").mkdir(parents=True)
    os.symlink(outside, Path(target) / "context-rotation" / "hooks" / "run.sh")


@pytest.mark.parametrize("link, shown", [
    (_link_readme, "README.md"),
    (_link_plugin_dir, ".claude-plugin"),
    (_link_plugin_file, "context-rotation/hooks/run.sh"),
])
def test_tracked_link_where_the_export_writes_is_refused(source, target, tmp_path, link, shown):
    outside = tmp_path / "outside"
    link(target, outside)
    commit_all(target, "a link")
    before = state(target)
    result = export(source, target)
    assert result.returncode == 1, result.stdout
    assert "link" in result.stderr and shown in result.stderr
    assert state(target) == before
    written = outside / "marketplace.json" if outside.is_dir() else outside
    assert written.read_text(encoding="utf-8") == "theirs\n"


def test_linked_directory_under_the_plugin_folder_is_refused(source, target, tmp_path):
    outside = tmp_path / "outside-hooks"
    outside.mkdir()
    (outside / "run.sh").write_text("theirs\n", encoding="utf-8")
    write(target, ".gitignore", "context-rotation/hooks\n")
    commit_all(target, "ignore a hooks link")
    (Path(target) / "context-rotation").mkdir()
    os.symlink(outside, Path(target) / "context-rotation" / "hooks")
    before = state(target)
    assert before["status"] == ""
    result = export(source, target)
    assert result.returncode == 1, result.stdout
    assert "link" in result.stderr and "context-rotation/hooks" in result.stderr
    assert sorted(os.listdir(outside)) == ["run.sh"]
    assert (outside / "run.sh").read_text(encoding="utf-8") == "theirs\n"
    assert state(target) == before


def test_link_tracked_only_on_the_export_branch_is_refused(source, target, tmp_path):
    assert export(source, target).returncode == 0
    outside = tmp_path / "outside-readme.md"
    _link_readme(target, outside)
    commit_all(target, "a link on the export branch")
    tip = git(target, "rev-parse", "HEAD")
    git(target, "checkout", "-q", "main")
    before = state(target)
    result = export(source, target)
    assert result.returncode == 1, result.stdout
    assert "link" in result.stderr and "README.md" in result.stderr
    assert outside.read_text(encoding="utf-8") == "theirs\n"
    assert state(target) == before
    assert git(target, "rev-parse", BRANCH) == tip


def test_source_folder_with_a_link_is_refused(source, target):
    os.symlink("run.sh", Path(source) / OWNED / "hooks" / "alias.sh")
    commit_all(source, "a link in the plugin")
    git(source, "update-ref", "refs/remotes/origin/main", git(source, "rev-parse", "HEAD"))
    before = state(target)
    result = export(source, target)
    assert result.returncode == 1, result.stdout
    assert "hooks/alias.sh" in result.stderr
    assert state(target) == before
    assert not (Path(target) / "context-rotation").exists()


# --- finding 5: leave the target exactly as found, and say only what is true


def test_nothing_to_export_returns_to_the_ref_the_clone_was_on(source, target):
    assert export(source, target).returncode == 0
    git(target, "checkout", "-q", "main")
    before = state(target)
    result = export(source, target)
    assert result.returncode == 0, result.stderr
    assert "nothing" in result.stdout.lower()
    assert state(target) == before


def test_failed_export_onto_an_existing_branch_returns_to_the_ref_the_clone_was_on(source, target):
    assert export(source, target).returncode == 0
    readme = Path(target) / "README.md"
    kept = [ln for ln in readme.read_text(encoding="utf-8").splitlines() if "context-rotation/" not in ln]
    readme.write_text("\n".join(kept + [CONTACT]) + "\n", encoding="utf-8")
    commit_all(target, "a contact line on the export branch")
    tip = git(target, "rev-parse", "HEAD")
    base_line = len(kept) + 1
    git(target, "checkout", "-q", "main")
    before = state(target)
    result = export(source, target)
    assert result.returncode == 1, result.stdout
    assert "pre-existing" in result.stderr and "README.md:%d" % base_line in result.stderr
    assert state(target) == before
    assert git(target, "rev-parse", BRANCH) == tip


def test_leak_already_in_the_toolkit_readme_is_named_pre_existing(source, target):
    write(target, "README.md", TOOLKIT_README + "\n" + CONTACT + "\n")
    commit_all(target, "contact line")
    head = git(target, "rev-parse", "HEAD")
    result = export(source, target)
    assert result.returncode == 1, result.stdout
    assert "email" in result.stderr and "pre-existing" in result.stderr
    assert_untouched(target, head)


def test_failed_export_from_a_detached_head_returns_to_that_commit(source, target):
    git(target, "checkout", "-q", "--detach", "HEAD")
    before = state(target)
    assert before["ref"] == ""
    commit_source(source, files={"hooks/leaky.txt": "path %s\n" % LEAK})
    result = export(source, target)
    assert result.returncode == 1, result.stdout
    assert state(target) == before
    assert git(target, "for-each-ref", "--format=%(refname:short)", "refs/heads") == "main"


def test_a_signal_while_writing_undoes_the_export(source, target):
    # The pre-commit hook sends TERM to the script's shell (its grandparent:
    # hook -> git -> script) and lets the commit finish, so the stop lands
    # after the export commit exists and no check in the script foresaw it.
    hook = Path(target) / ".git" / "hooks" / "pre-commit"
    hook.write_text('#!/bin/sh\nkill -TERM "$(ps -o ppid= -p "$PPID" | tr -d " ")"\nexit 0\n', encoding="utf-8")
    hook.chmod(0o755)
    before = state(target)
    result = export(source, target)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "stopped with exit status 143" in result.stderr
    assert "removed the export commit" in result.stderr and "as it was found" in result.stderr
    assert state(target) == before
    assert not (Path(target) / "context-rotation").exists()
    assert not (Path(target) / ".claude-plugin").exists()


def test_cleanup_that_cannot_finish_exits_3_and_names_what_is_left(source, target):
    hook = Path(target) / ".git" / "hooks" / "reference-transaction"
    hook.write_text(REFUSE_BRANCH_DELETE, encoding="utf-8")
    hook.chmod(0o755)
    commit_source(source, files={"hooks/leaky.txt": "path %s\n" % LEAK})
    result = export(source, target)
    assert result.returncode == 3, result.stderr
    assert BRANCH in result.stderr
    assert "as it was found" not in result.stderr
    assert branches(target) == [BRANCH, "main"]


def test_target_that_is_not_a_git_repo_is_refused(source, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    result = export(source, plain)
    assert result.returncode != 0 and "git" in result.stderr
    assert list(plain.iterdir()) == []


def test_commit_not_on_origin_main_is_refused(source, target):
    head = git(target, "rev-parse", "HEAD")
    first = git(source, "rev-parse", "HEAD")
    commit_source(source, version="0.2.0", files={"hooks/run.sh": "unmerged\n"})
    git(source, "update-ref", "refs/remotes/origin/main", first)
    result = export(source, target, tag="context-rotation-v0.2.0")
    assert result.returncode != 0 and "origin/main" in result.stderr
    assert_untouched(target, head)


def test_unknown_commit_is_refused(source, target):
    head = git(target, "rev-parse", "HEAD")
    result = export(source, target, sha="0" * 40)
    assert result.returncode != 0
    assert_untouched(target, head)


def test_tag_version_differing_from_plugin_json_is_refused(source, target):
    head = git(target, "rev-parse", "HEAD")
    result = export(source, target, tag="context-rotation-v0.9.0")
    assert result.returncode != 0 and "version" in result.stderr
    assert_untouched(target, head)
    bad = export(source, target, tag="timekeeper-v0.1.0")
    assert bad.returncode != 0 and "tag" in bad.stderr
    assert_untouched(target, head)


def test_changed_content_with_the_same_version_is_refused(source, target):
    assert export(source, target).returncode == 0
    head = git(target, "rev-parse", "HEAD")
    commit_source(source, version="0.1.0", files={"hooks/run.sh": "changed, same version\n"})
    result = export(source, target)
    assert result.returncode != 0 and "version" in result.stderr
    assert git(target, "rev-parse", "HEAD") == head
    assert (Path(target) / "context-rotation" / "hooks" / "run.sh").read_text() == "#!/bin/sh\nexit 0\n"


def test_changed_content_with_a_lower_version_is_refused(source, target):
    commit_source(source, version="0.3.0", files={"hooks/run.sh": "x\n"})
    assert export(source, target, tag="context-rotation-v0.3.0").returncode == 0
    commit_source(source, version="0.2.0", files={"hooks/run.sh": "y\n"})
    result = export(source, target, tag="context-rotation-v0.2.0")
    assert result.returncode != 0 and "version" in result.stderr


def test_planted_leak_removes_the_export_commit(source, target):
    head = git(target, "rev-parse", "HEAD")
    before = state(target)
    commit_source(source, version="0.1.0", files={"hooks/leaky.txt": "path %s\n" % LEAK})
    result = export(source, target)
    assert result.returncode == 1
    assert "leak" in result.stderr.lower() and "hooks/leaky.txt" in result.stderr
    assert "pre-existing" not in result.stderr
    assert_untouched(target, head)
    assert state(target) == before


def test_leak_in_the_toolkit_readme_row_is_caught(source, target):
    head = git(target, "rev-parse", "HEAD")
    write(source, OWNED + "/.claude-plugin/plugin.json", json.dumps(
        {"name": "context-rotation", "version": "0.1.0", "description": "Mail me at a" + "@" + "b.example"}) + "\n")
    git(source, "add", "-A")
    git(source, "commit", "-q", "-m", "bad description")
    git(source, "update-ref", "refs/remotes/origin/main", git(source, "rev-parse", "HEAD"))
    result = export(source, target)
    assert result.returncode != 0 and "leak" in result.stderr.lower()
    assert "pre-existing" not in result.stderr
    assert_untouched(target, head)


def test_leak_on_an_existing_export_branch_resets_only_to_the_previous_tip(source, target):
    assert export(source, target).returncode == 0
    tip = git(target, "rev-parse", "HEAD")
    before = state(target)
    commit_source(source, version="0.1.1", files={"hooks/leaky.txt": LEAK + "\n"})
    result = export(source, target, tag="context-rotation-v0.1.1")
    assert result.returncode == 1
    assert "removed the export commit" in result.stderr and "as it was found" in result.stderr
    assert git(target, "rev-parse", "HEAD") == tip
    assert state(target) == before
    assert not (Path(target) / "context-rotation" / "hooks" / "leaky.txt").exists()


def test_script_never_pushes_or_tags(source, target):
    assert export(source, target).returncode == 0
    assert git(target, "tag") == ""
    assert git(target, "remote") == ""


def test_script_lives_outside_the_wheel():
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    section = text.split("[tool.setuptools.exclude-package-data]", 1)[1]
    section = re.split(r"\n\[", section, 1)[0]
    assert '"scripts/maintenance/export_toolkit_plugin.sh"' in section


def test_script_is_executable_and_syntax_clean():
    assert os.access(SCRIPT, os.X_OK)
    sh = shutil.which("dash") or shutil.which("sh")
    assert subprocess.run([sh, "-n", str(SCRIPT)]).returncode == 0
