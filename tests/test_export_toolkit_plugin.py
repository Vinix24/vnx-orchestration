"""Behaviour of scripts/maintenance/export_toolkit_plugin.sh (context-rotation track, E).

Every source and target here is a throwaway git repo under tmp_path. Nothing
touches a real toolkit clone and nothing uses the network.

The export builds its commit in a worktree of its own and writes nothing in
the operator's checkout: most tests compare checkout_state() before and after
a run, and read what the export wrote from the branch, never from the checkout.
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
REAL_METER = REPO / "packages" / "claude-code-toolkit" / "context-meter"
METER_OWNED = "packages/claude-code-toolkit/context-meter"
OWNED = "packages/claude-code-toolkit/context-rotation"
REAL_GIT = shutil.which("git")

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
RUN_SH = "#!/bin/sh\nexit 0\n"


def git(cwd, *args, check=True, env=None):
    result = subprocess.run(["git", *args], cwd=cwd, env=env or GIT_ENV, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise AssertionError("git %s failed: %s" % (" ".join(args), result.stderr))
    return result.stdout.strip()


def write(root, rel, text):
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def commit_source(src, version="0.1.0", files=None, remove=(), message="source", modes=None):
    base = Path(src) / OWNED
    write(base, ".claude-plugin/plugin.json", json.dumps(
        {"name": "context-rotation", "version": version, "description": "Guards session context."}, indent=2) + "\n")
    for rel, text in (files or {}).items():
        path = write(base, rel, text)
        if rel.endswith(".sh"):
            path.chmod(0o755)
    for rel, mode in (modes or {}).items():
        (base / rel).chmod(mode)
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
    commit_source(src, files={"hooks/run.sh": RUN_SH, "hooks/old.sh": "old\n", "skills/x.md": "skill\n"})
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


def export(source, target, sha=None, tag="context-rotation-v0.1.0", env=None, plugin=None):
    """Run the script with sh from PATH, and a TMPDIR of the test's own."""
    sha = sha or git(source, "rev-parse", "HEAD")
    scratch = Path(target).parent / "scratch"
    scratch.mkdir(exist_ok=True)
    run_env = {**SCRIPT_ENV, "TMPDIR": str(scratch), **(env or {})}
    return subprocess.run(
        ["sh", str(SCRIPT), "--source", str(source), "--commit", sha, "--target", str(target), "--tag", tag,
         *(["--plugin", plugin] if plugin else [])],
        env=run_env, capture_output=True, text=True,
    )


def branches(target):
    return git(target, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()


def branch_tips(target):
    return git(target, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads")


def blob(target, rel, rev=BRANCH):
    return subprocess.run(["git", "show", "%s:%s" % (rev, rel)], cwd=target, env=GIT_ENV,
                          capture_output=True, text=True, check=True).stdout


def tracked(target, rev=BRANCH):
    return git(target, "ls-tree", "-r", "--name-only", rev).splitlines()


def mode_of(target, rel, rev=BRANCH):
    return git(target, "ls-tree", rev, "--", rel).split()[0]


def checkout_state(target):
    """The operator's side of the clone, which no run may change: git status byte
    for byte, the index file, HEAD, every working-tree file with its mode, and
    the worktree list. GIT_OPTIONAL_LOCKS=0 keeps status from rewriting the index."""
    root = Path(target)
    quiet = {**GIT_ENV, "GIT_OPTIONAL_LOCKS": "0"}
    files = {}
    for dirpath, dirnames, filenames in os.walk(root):
        if Path(dirpath) == root:
            dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in sorted(filenames + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]):
            path = Path(dirpath) / name
            rel = str(path.relative_to(root))
            files[rel] = ("link", os.readlink(path)) if path.is_symlink() else (path.read_bytes(), path.stat().st_mode)
    index = root / ".git" / "index"
    return {
        "status": git(target, "status", env=quiet),
        "porcelain": git(target, "status", "--porcelain", "--ignored", "--untracked-files=all", env=quiet),
        "index": index.read_bytes() if index.exists() else None,
        "head": (root / ".git" / "HEAD").read_text(encoding="utf-8"),
        "worktrees": git(target, "worktree", "list", "--porcelain", env=quiet),
        "files": files,
    }


def commit_all(repo, message):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def commit_on_branch(target, branch, edit, message):
    """Commit on the export branch the way an operator would, then go back to main."""
    git(target, "checkout", "-q", branch)
    edit()
    commit_all(target, message)
    git(target, "checkout", "-q", "main")


def release(target, branch=BRANCH):
    """What the operator does after review: the export branch merged into main."""
    git(target, "checkout", "-q", "main")
    git(target, "merge", "-q", "--ff-only", branch)


def drop_row_on_branch(target):
    """Drift on the export branch that a re-export of the same commit repairs: the
    README without the context-rotation row."""
    def edit():
        readme = Path(target) / "README.md"
        kept = [ln for ln in readme.read_text(encoding="utf-8").splitlines() if "context-rotation/" not in ln]
        readme.write_text("\n".join(kept) + "\n", encoding="utf-8")

    commit_on_branch(target, BRANCH, edit, "the row dropped on the export branch")


def marketplace(target, rev=BRANCH):
    return json.loads(blob(target, ".claude-plugin/marketplace.json", rev))


def git_in_front(tmp_path, body):
    """A git on PATH in front of the real one, for the two faults no input of the
    script can cause: a signal right after the branch is written, and a worktree
    that will not go away."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "git"
    wrapper.write_text("#!/bin/sh\n" + body.replace("@GIT@", REAL_GIT), encoding="utf-8")
    wrapper.chmod(0o755)
    return {"PATH": "%s%s%s" % (bin_dir, os.pathsep, os.environ["PATH"])}


# --- what a successful export writes


def test_first_export_writes_the_branch_and_leaves_the_checkout_alone(source, target):
    sha = git(source, "rev-parse", "HEAD")
    before = checkout_state(target)
    result = export(source, target)
    assert result.returncode == 0, result.stderr
    assert checkout_state(target) == before
    assert git(target, "branch", "--show-current") == "main"
    assert blob(target, "context-rotation/hooks/run.sh") == RUN_SH
    assert mode_of(target, "context-rotation/hooks/run.sh") == "100755"
    assert "context-rotation/.claude-plugin/plugin.json" in tracked(target)
    assert not any(p.startswith(("packages/", "unrelated")) for p in tracked(target))
    doc = marketplace(target)
    assert doc["name"] == "claude-code-toolkit" and doc["owner"]["name"]
    assert doc["plugins"] == [EXPORTED_ENTRY]
    readme = blob(target, "README.md")
    rows = [line for line in readme.splitlines() if "context-rotation/" in line]
    assert len(rows) == 1 and rows[0].startswith("| ") and "Guards session context." in rows[0]
    assert readme.index(rows[0]) < readme.index("## License")
    assert git(target, "rev-list", "--count", "main.." + BRANCH) == "1"
    assert sha in git(target, "log", "-1", "--format=%B", BRANCH)
    assert git(target, "tag") == ""
    assert "push" in result.stdout and "context-rotation-v0.1.0" in result.stdout and BRANCH in result.stdout


def test_export_into_a_fresh_empty_repo_creates_readme_and_marketplace(source, tmp_path):
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    git(fresh, "init", "-q", "-b", "main")
    git(fresh, "commit", "-q", "--allow-empty", "-m", "init")
    result = export(source, fresh)
    assert result.returncode == 0, result.stderr
    assert marketplace(fresh)["plugins"] == [EXPORTED_ENTRY]
    readme = blob(fresh, "README.md")
    assert "## Tools" in readme and len([ln for ln in readme.splitlines() if "context-rotation/" in ln]) == 1
    assert sorted(os.listdir(fresh)) == [".git"]


def test_re_export_of_the_same_commit_changes_nothing(source, target):
    assert export(source, target).returncode == 0
    tips = branch_tips(target)
    before = checkout_state(target)
    again = export(source, target)
    assert again.returncode == 0, again.stderr
    assert "nothing" in again.stdout.lower()
    assert branch_tips(target) == tips
    assert checkout_state(target) == before


def test_existing_marketplace_entries_stay_byte_for_byte(source, target):
    write(target, ".claude-plugin/marketplace.json", TOOLKIT_MARKETPLACE)
    commit_all(target, "marketplace")
    assert export(source, target).returncode == 0
    text = blob(target, ".claude-plugin/marketplace.json")
    assert TIMEKEEPER_ENTRY in text
    assert text.startswith('{\n    "name": "claude-code-toolkit",\n    "owner": {"name": "Someone"},\n')
    assert [p["name"] for p in marketplace(target)["plugins"]] == ["timekeeper", "context-rotation"]
    release(target)
    commit_source(source, version="0.1.1", files={"hooks/run.sh": "#!/bin/sh\nexit 1\n"})
    assert export(source, target, tag="context-rotation-v0.1.1").returncode == 0
    bumped = "export/context-rotation-0.1.1"
    assert TIMEKEEPER_ENTRY in blob(target, ".claude-plugin/marketplace.json", bumped)
    doc = marketplace(target, bumped)
    assert [p["name"] for p in doc["plugins"]] == ["timekeeper", "context-rotation"]
    assert doc["plugins"][1]["version"] == "0.1.1"


def test_removed_source_file_is_removed_and_outside_files_survive(source, target):
    assert export(source, target).returncode == 0
    release(target)
    write(target, "docs/main-only.md", "kept\n")
    commit_all(target, "an operator file on main")
    assert "context-rotation/hooks/old.sh" in tracked(target, "main")
    commit_source(source, version="0.1.1", remove=["hooks/old.sh"])
    bumped = "export/context-rotation-0.1.1"
    result = export(source, target, tag="context-rotation-v0.1.1")
    assert result.returncode == 0, result.stderr
    files = tracked(target, bumped)
    assert "context-rotation/hooks/old.sh" not in files and "context-rotation/hooks/run.sh" in files
    assert blob(target, "docs/main-only.md", bumped) == "kept\n"
    assert blob(target, "timekeeper/tk.txt", bumped) == "tk\n"
    changed = git(target, "show", "--name-only", "--format=", bumped).splitlines()
    assert "context-rotation/hooks/old.sh" in changed
    assert all(
        p.startswith("context-rotation/") or p in (".claude-plugin/marketplace.json", "README.md") for p in changed
    )


def test_removed_file_with_a_quoted_name_is_removed(source, target):
    odd = 'skills/a "b"\\é.md'
    commit_source(source, files={odd: "odd name\n"})
    assert export(source, target).returncode == 0
    names = git(target, "ls-tree", "-r", "-z", "--name-only", BRANCH).split("\0")
    assert "context-rotation/" + odd in names
    release(target)
    commit_source(source, version="0.1.1", remove=[odd])
    result = export(source, target, tag="context-rotation-v0.1.1")
    assert result.returncode == 0, result.stderr
    names = git(target, "ls-tree", "-r", "-z", "--name-only", "export/context-rotation-0.1.1").split("\0")
    assert "context-rotation/" + odd not in names and "context-rotation/skills/x.md" in names


def test_file_the_toolkit_ignores_is_still_committed(source, target):
    write(target, ".gitignore", "*.md\n")
    commit_all(target, "ignore markdown")
    before = checkout_state(target)
    result = export(source, target)
    assert result.returncode == 0, result.stderr
    assert "context-rotation/skills/x.md" in tracked(target)
    assert checkout_state(target) == before


def test_re_export_from_main_keeps_what_the_export_branch_already_carries(source, target):
    assert export(source, target).returncode == 0

    def edit():
        market = Path(target) / ".claude-plugin" / "marketplace.json"
        doc = json.loads(market.read_text(encoding="utf-8"))
        doc["plugins"].append({"name": "timekeeper", "source": "./timekeeper", "description": "Tracks time."})
        market.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        readme = Path(target) / "README.md"
        kept = [ln for ln in readme.read_text(encoding="utf-8").splitlines() if "context-rotation/" not in ln]
        readme.write_text("\n".join(kept + ["", "Notes kept on the export branch."]) + "\n", encoding="utf-8")

    commit_on_branch(target, BRANCH, edit, "operator edits on the export branch")
    before = checkout_state(target)
    result = export(source, target)
    assert result.returncode == 0, result.stderr
    assert checkout_state(target) == before
    assert git(target, "rev-parse", "main") == git(target, "rev-parse", BRANCH + "~3")
    plugins = marketplace(target)["plugins"]
    assert [p["name"] for p in plugins] == ["context-rotation", "timekeeper"]
    assert plugins[0] == EXPORTED_ENTRY
    text = blob(target, "README.md")
    assert "Notes kept on the export branch." in text
    assert len([ln for ln in text.splitlines() if "context-rotation/" in ln]) == 1


# --- round 3: the operator's checkout is never touched


@pytest.mark.parametrize("rerun", ["nothing to export", "row restored"])
def test_ignored_local_file_the_export_branch_tracks_survives(source, target, rerun):
    write(target, ".gitignore", "private.txt\n")
    commit_all(target, "ignore private.txt")
    assert export(source, target).returncode == 0
    git(target, "checkout", "-q", BRANCH)
    write(target, "private.txt", "tracked on the export branch\n")
    git(target, "add", "-f", "private.txt")
    git(target, "commit", "-q", "-m", "private.txt on the export branch")
    git(target, "checkout", "-q", "main")
    if rerun == "row restored":
        drop_row_on_branch(target)
    tip = git(target, "rev-parse", BRANCH)
    write(target, "private.txt", "the operator's own\n")
    before = checkout_state(target)
    result = export(source, target)
    assert result.returncode == 0, result.stderr
    assert (Path(target) / "private.txt").read_text(encoding="utf-8") == "the operator's own\n"
    assert checkout_state(target) == before
    assert blob(target, "private.txt") == "tracked on the export branch\n"
    assert (git(target, "rev-parse", BRANCH) == tip) == (rerun == "nothing to export")
    assert len([ln for ln in blob(target, "README.md").splitlines() if "context-rotation/" in ln]) == 1


def make_work_in_progress(target):
    """A feature branch with staged, unstaged, untracked and ignored work, some of
    it on the very paths the export owns."""
    write(target, ".gitignore", "build/\n__pycache__/\n")
    commit_all(target, "ignore build output")
    git(target, "checkout", "-q", "-b", "feature")
    write(target, "docs/keep.md", "keep, staged edit\n")
    git(target, "add", "docs/keep.md")
    write(target, "README.md", TOOLKIT_README + "\nunstaged edit\n")
    write(target, "context-rotation/notes.txt", "untracked, under the plugin folder\n")
    write(target, "context-rotation/hooks/__pycache__/run.cpython-312.pyc", "ignored\n")
    write(target, "build/out.bin", "ignored\n")


@pytest.mark.parametrize("outcome", ["exported", "refused", "leak rolled back"])
def test_work_in_progress_on_a_feature_branch_survives_every_outcome(source, target, outcome):
    if outcome == "refused":
        assert export(source, target).returncode == 0
        git(target, "checkout", "-q", "main")
        commit_source(source, version="0.1.0", files={"hooks/run.sh": "changed, same version\n"})
    elif outcome == "leak rolled back":
        commit_source(source, files={"hooks/leaky.txt": "path %s\n" % LEAK})
    make_work_in_progress(target)
    before, tips = checkout_state(target), branch_tips(target)
    result = export(source, target)
    assert checkout_state(target) == before
    assert git(target, "branch", "--show-current") == "feature"
    if outcome == "exported":
        assert result.returncode == 0, result.stderr
        assert "context-rotation/notes.txt" not in tracked(target)
        assert blob(target, "docs/keep.md") == "keep\n" and "unstaged edit" not in blob(target, "README.md")
    else:
        assert result.returncode == 1, result.stdout
        assert ("version" if outcome == "refused" else "hooks/leaky.txt") in result.stderr
        assert branch_tips(target) == tips


def test_ignored_file_under_the_plugin_folder_stays_out_of_the_commit(source, target):
    write(target, ".gitignore", "__pycache__/\n")
    commit_all(target, "ignore caches")
    write(target, "context-rotation/hooks/__pycache__/run.cpython-312.pyc", "cache\n")
    before = checkout_state(target)
    result = export(source, target)
    assert result.returncode == 0, result.stderr
    assert checkout_state(target) == before
    assert not any("__pycache__" in p for p in tracked(target))


def test_untracked_link_in_the_checkout_is_never_written_through(source, target, tmp_path):
    outside = tmp_path / "outside-hooks"
    outside.mkdir()
    (outside / "run.sh").write_text("theirs\n", encoding="utf-8")
    write(target, ".gitignore", "context-rotation/hooks\n")
    commit_all(target, "ignore a hooks link")
    (Path(target) / "context-rotation").mkdir()
    os.symlink(outside, Path(target) / "context-rotation" / "hooks")
    before = checkout_state(target)
    result = export(source, target)
    assert result.returncode == 0, result.stderr
    assert sorted(os.listdir(outside)) == ["run.sh"]
    assert (outside / "run.sh").read_text(encoding="utf-8") == "theirs\n"
    assert checkout_state(target) == before
    assert mode_of(target, "context-rotation/hooks/run.sh") == "100755"


@pytest.mark.parametrize("where", ["this checkout", "another worktree"])
def test_export_branch_checked_out_anywhere_is_refused(source, target, tmp_path, where):
    assert export(source, target).returncode == 0
    git(target, "checkout", "-q", "main")
    holder = Path(target)
    if where == "this checkout":
        git(target, "checkout", "-q", BRANCH)
    else:
        holder = tmp_path / "other"
        git(target, "worktree", "add", "-q", str(holder), BRANCH)
    tip = git(target, "rev-parse", BRANCH)
    before = checkout_state(target)
    result = export(source, target)
    assert result.returncode == 1, result.stdout
    assert BRANCH in result.stderr and "checked out in %s" % os.path.realpath(holder) in result.stderr
    assert git(target, "rev-parse", BRANCH) == tip
    assert checkout_state(target) == before


@pytest.mark.parametrize("operation", ["rebased", "bisected"])
def test_export_branch_in_an_operation_in_another_worktree_is_refused(source, target, tmp_path, operation):
    assert export(source, target).returncode == 0
    git(target, "checkout", "-q", "main")
    other = tmp_path / "other"
    git(target, "worktree", "add", "-q", str(other), BRANCH)
    if operation == "rebased":
        git(other, "rebase", "--exec", "false", "HEAD~1", check=False)
    else:
        git(other, "bisect", "start")
        git(other, "checkout", "-q", "--detach", "HEAD~1")
    assert "detached" in git(target, "worktree", "list", "--porcelain")
    tip = git(target, "rev-parse", BRANCH)
    before = checkout_state(target)
    result = export(source, target)
    assert result.returncode == 1, result.stdout
    assert "being %s in %s" % (operation, os.path.realpath(other)) in result.stderr
    assert git(target, "rev-parse", BRANCH) == tip
    assert checkout_state(target) == before


@pytest.mark.parametrize("operation", ["bisect", "rebase"])
def test_an_operation_in_progress_in_the_checkout_is_refused(source, target, operation):
    if operation == "bisect":
        git(target, "bisect", "start")
    else:
        write(target, "docs/second.md", "second\n")
        commit_all(target, "second")
        git(target, "rebase", "--exec", "false", "HEAD~1", check=False)
    before = checkout_state(target)
    result = export(source, target)
    assert result.returncode == 1, result.stdout
    assert operation in result.stderr
    assert checkout_state(target) == before
    assert BRANCH not in branches(target)


def test_temp_dir_inside_the_target_is_refused(source, target):
    inside = Path(target) / "scratch-inside"
    inside.mkdir()
    before = checkout_state(target)
    result = export(source, target, env={"TMPDIR": str(inside)})
    assert result.returncode == 1, result.stdout
    assert "TMPDIR" in result.stderr
    assert checkout_state(target) == before
    assert BRANCH not in branches(target)


# --- round 3: the leak scan covers the bytes that are committed, also on a no-op


def test_nothing_to_export_still_runs_the_leak_scan(source, target):
    assert export(source, target).returncode == 0
    git(target, "checkout", "-q", "main")

    def add_contact():
        readme = Path(target) / "README.md"
        readme.write_text(readme.read_text(encoding="utf-8") + CONTACT + "\n", encoding="utf-8")

    commit_on_branch(target, BRANCH, add_contact, "a contact line next to the row")
    tip = git(target, "rev-parse", BRANCH)
    before = checkout_state(target)
    result = export(source, target)
    assert result.returncode == 1, result.stdout
    assert "email" in result.stderr and "pre-existing" in result.stderr and "README.md" in result.stderr
    assert git(target, "rev-parse", BRANCH) == tip
    assert checkout_state(target) == before


HOOKS = ("pre-commit", "prepare-commit-msg", "commit-msg", "post-commit", "post-checkout",
         "reference-transaction", "post-index-change")


def test_no_hook_of_the_target_runs_and_the_commit_is_what_was_staged(source, target, tmp_path):
    log = tmp_path / "hooks.log"
    rewrite = 'printf "rewritten by a hook\\n" > context-rotation/hooks/run.sh\ngit add context-rotation/hooks/run.sh\n'
    for name in HOOKS:
        hook = Path(target) / ".git" / "hooks" / name
        hook.write_text('#!/bin/sh\necho %s >> "%s"\n%s' % (name, log, rewrite if name == "pre-commit" else ""),
                        encoding="utf-8")
        hook.chmod(0o755)
    result = export(source, target)
    assert result.returncode == 0, result.stderr
    assert not log.exists(), log.read_text(encoding="utf-8")
    assert blob(target, "context-rotation/hooks/run.sh") == RUN_SH


def test_a_hook_that_cleans_the_working_copy_cannot_hide_a_committed_leak(source, target):
    commit_source(source, files={"hooks/leaky.txt": "path %s\n" % LEAK})
    hook = Path(target) / ".git" / "hooks" / "pre-commit"
    hook.write_text('#!/bin/sh\nprintf "clean\\n" > context-rotation/hooks/leaky.txt\n', encoding="utf-8")
    hook.chmod(0o755)
    before = checkout_state(target)
    result = export(source, target)
    assert result.returncode == 1, result.stdout
    assert "hooks/leaky.txt" in result.stderr
    assert BRANCH not in branches(target)
    assert checkout_state(target) == before


# --- round 3: modes are the source's, and a mode is content


def test_file_mode_follows_the_source_both_ways(source, target):
    assert export(source, target).returncode == 0
    assert mode_of(target, "context-rotation/hooks/run.sh") == "100755"
    release(target)
    commit_source(source, version="0.1.1", modes={"hooks/run.sh": 0o644})
    result = export(source, target, tag="context-rotation-v0.1.1")
    assert result.returncode == 0, result.stderr
    assert mode_of(target, "context-rotation/hooks/run.sh", "export/context-rotation-0.1.1") == "100644"
    release(target, "export/context-rotation-0.1.1")
    commit_source(source, version="0.1.2", modes={"hooks/run.sh": 0o755})
    result = export(source, target, tag="context-rotation-v0.1.2")
    assert result.returncode == 0, result.stderr
    assert mode_of(target, "context-rotation/hooks/run.sh", "export/context-rotation-0.1.2") == "100755"


def test_toolkit_filters_and_file_mode_setting_change_no_byte_and_no_mode(source, target):
    git(target, "config", "core.fileMode", "false")
    git(target, "config", "filter.shout.clean", "tr a-z A-Z")
    write(target, ".gitattributes", "*.sh filter=shout\n*.md text eol=crlf\n")
    commit_all(target, "a clean filter and eol rules")
    result = export(source, target)
    assert result.returncode == 0, result.stderr
    assert blob(target, "context-rotation/hooks/run.sh") == RUN_SH
    assert mode_of(target, "context-rotation/hooks/run.sh") == "100755"
    assert mode_of(target, "context-rotation/skills/x.md") == "100644"


def test_mode_only_change_without_a_version_bump_is_refused(source, target):
    assert export(source, target).returncode == 0
    git(target, "checkout", "-q", "main")
    tip = git(target, "rev-parse", BRANCH)
    commit_source(source, version="0.1.0", modes={"hooks/run.sh": 0o644})
    before = checkout_state(target)
    result = export(source, target)
    assert result.returncode == 1, result.stdout
    assert "version" in result.stderr
    assert git(target, "rev-parse", BRANCH) == tip
    assert checkout_state(target) == before


# --- round 3: the README row and the marketplace merge


def test_escaped_pipe_in_a_tools_row_does_not_add_columns(source, target):
    write(target, "README.md", TOOLKIT_README.replace(
        "| Tool | What it does |\n|---|---|\n| [`timekeeper/`](timekeeper/) | Tracks time. |",
        "| Tool | What it does | Notes |\n|---|---|---|\n| [`timekeeper/`](timekeeper/) | Tracks time. | a \\| b |"))
    commit_all(target, "three columns")
    assert export(source, target).returncode == 0
    row = next(ln for ln in blob(target, "README.md").splitlines() if "context-rotation/" in ln)
    assert len(re.split(r"(?<!\\)\|", row.strip())) - 2 == 3, row


def test_row_elsewhere_in_the_readme_does_not_stop_the_tools_row(source, target):
    write(target, "README.md", TOOLKIT_README.replace(
        "## License", "## Roadmap\n\n| Item | Where |\n|---|---|\n| rotation | planned in `context-rotation/` |\n\n"
        "## License"))
    commit_all(target, "roadmap table")
    assert export(source, target).returncode == 0
    lines = blob(target, "README.md").splitlines()
    tools = lines[lines.index("## Tools"):lines.index("## Roadmap")]
    assert len([ln for ln in tools if "context-rotation/" in ln]) == 1


def test_malformed_marketplace_is_refused_with_a_message(source, target):
    write(target, ".claude-plugin/marketplace.json", TOOLKIT_MARKETPLACE + "}\n")
    commit_all(target, "a marketplace with trailing garbage")
    before = checkout_state(target)
    result = export(source, target)
    assert result.returncode == 1, result.stdout
    assert "cannot merge" in result.stderr and "Traceback" not in result.stderr
    assert checkout_state(target) == before
    assert BRANCH not in branches(target)


# --- links


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
    before = checkout_state(target)
    result = export(source, target)
    assert result.returncode == 1, result.stdout
    assert "link" in result.stderr and shown in result.stderr
    assert checkout_state(target) == before
    assert BRANCH not in branches(target)
    written = outside / "marketplace.json" if outside.is_dir() else outside
    assert written.read_text(encoding="utf-8") == "theirs\n"


def test_link_tracked_only_on_the_export_branch_is_refused(source, target, tmp_path):
    assert export(source, target).returncode == 0
    git(target, "checkout", "-q", "main")
    outside = tmp_path / "outside-readme.md"
    commit_on_branch(target, BRANCH, lambda: _link_readme(target, outside), "a link on the export branch")
    tip = git(target, "rev-parse", BRANCH)
    before = checkout_state(target)
    result = export(source, target)
    assert result.returncode == 1, result.stdout
    assert "link" in result.stderr and "README.md" in result.stderr
    assert outside.read_text(encoding="utf-8") == "theirs\n"
    assert checkout_state(target) == before
    assert git(target, "rev-parse", BRANCH) == tip


def test_source_folder_with_a_link_is_refused(source, target):
    os.symlink("run.sh", Path(source) / OWNED / "hooks" / "alias.sh")
    commit_all(source, "a link in the plugin")
    git(source, "update-ref", "refs/remotes/origin/main", git(source, "rev-parse", "HEAD"))
    before = checkout_state(target)
    result = export(source, target)
    assert result.returncode == 1, result.stdout
    assert "hooks/alias.sh" in result.stderr
    assert checkout_state(target) == before
    assert BRANCH not in branches(target)


# --- failures leave the clone as it was found, and say only what is true


def test_failed_export_onto_an_existing_branch_leaves_its_tip(source, target):
    assert export(source, target).returncode == 0
    git(target, "checkout", "-q", "main")
    readme = Path(target) / "README.md"
    kept = []

    def contact_without_row():
        kept.extend(ln for ln in readme.read_text(encoding="utf-8").splitlines() if "context-rotation/" not in ln)
        readme.write_text("\n".join(kept + [CONTACT]) + "\n", encoding="utf-8")

    commit_on_branch(target, BRANCH, contact_without_row, "a contact line on the export branch")
    tip = git(target, "rev-parse", BRANCH)
    before = checkout_state(target)
    result = export(source, target)
    assert result.returncode == 1, result.stdout
    assert "pre-existing" in result.stderr and "README.md:%d" % (len(kept) + 1) in result.stderr
    assert checkout_state(target) == before
    assert git(target, "rev-parse", BRANCH) == tip


def test_leak_already_in_the_toolkit_readme_is_named_pre_existing(source, target):
    write(target, "README.md", TOOLKIT_README + "\n" + CONTACT + "\n")
    commit_all(target, "contact line")
    before = checkout_state(target)
    result = export(source, target)
    assert result.returncode == 1, result.stdout
    assert "email" in result.stderr and "pre-existing" in result.stderr
    assert checkout_state(target) == before
    assert branches(target) == ["main"]


def test_failed_export_from_a_detached_head_leaves_it_detached(source, target):
    git(target, "checkout", "-q", "--detach", "HEAD")
    before = checkout_state(target)
    commit_source(source, files={"hooks/leaky.txt": "path %s\n" % LEAK})
    result = export(source, target)
    assert result.returncode == 1, result.stdout
    assert checkout_state(target) == before
    assert branches(target) == ["main"]


SIGNAL_AFTER_THE_BRANCH_WRITE = """\
"@GIT@" "$@"
status=$?
case " $* " in
    *" update-ref "*" refs/heads/export/"*)
        if [ "$status" = 0 ] && [ ! -e "$KILL_MARKER" ]; then : >"$KILL_MARKER"; kill -TERM "$PPID"; fi ;;
esac
exit "$status"
"""


@pytest.mark.parametrize("branch_before", ["new", "existing"])
def test_a_signal_after_the_branch_write_swaps_it_back(source, target, tmp_path, branch_before):
    if branch_before == "existing":
        assert export(source, target).returncode == 0
        git(target, "checkout", "-q", "main")
        drop_row_on_branch(target)
    tips = branch_tips(target)
    before = checkout_state(target)
    env = git_in_front(tmp_path, SIGNAL_AFTER_THE_BRANCH_WRITE)
    env["KILL_MARKER"] = str(tmp_path / "killed")
    result = export(source, target, env=env)
    assert (tmp_path / "killed").exists()
    assert result.returncode == 1, result.stdout + result.stderr
    assert "stopped with exit status 143" in result.stderr and "as it was found" in result.stderr
    assert ("deleted " + BRANCH if branch_before == "new" else "moved %s back" % BRANCH) in result.stderr
    assert branch_tips(target) == tips
    assert checkout_state(target) == before


WORKTREE_THAT_STAYS = """\
case " $* " in
    *" worktree remove "*) echo "refusing to remove a worktree" >&2; exit 1 ;;
esac
exec "@GIT@" "$@"
"""


def test_cleanup_that_cannot_finish_exits_3_and_names_what_is_left(source, target, tmp_path):
    commit_source(source, files={"hooks/leaky.txt": "path %s\n" % LEAK})
    result = export(source, target, env=git_in_front(tmp_path, WORKTREE_THAT_STAYS))
    assert result.returncode == 3, result.stderr
    assert "as it was found" not in result.stderr
    assert BRANCH not in branches(target)
    found = re.search(r'git -C "([^"]+)" worktree remove --force "([^"]+)"', result.stderr)
    assert found, result.stderr
    assert found.group(2) in git(target, "worktree", "list", "--porcelain")
    git(found.group(1), "worktree", "remove", "--force", found.group(2))
    assert git(target, "worktree", "list", "--porcelain").count("worktree ") == 1


# --- round 4: the export writes exactly its own branch, and only after it won


def symbolic_target(repo, ref):
    """What ref points at when it is a symbolic ref; empty when it is not."""
    return git(repo, "symbolic-ref", "-q", ref, check=False)


@pytest.mark.parametrize("points_at", ["refs/heads/main", "refs/heads/elsewhere", "refs/heads/nowhere"])
def test_symbolic_export_branch_is_refused_and_nothing_it_points_at_moves(source, target, points_at):
    """main is the clone's checkout and elsewhere a branch no worktree is on:
    both read as an existing export branch. nowhere does not exist, so the
    export branch reads as new and the write would create it."""
    git(target, "branch", "elsewhere")
    git(target, "symbolic-ref", "refs/heads/" + BRANCH, points_at)
    tips = branch_tips(target)
    before = checkout_state(target)
    result = export(source, target)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "refs/heads/%s is a symbolic ref to %s" % (BRANCH, points_at) in result.stderr
    assert checkout_state(target) == before
    assert branch_tips(target) == tips
    assert symbolic_target(target, "refs/heads/" + BRANCH) == points_at
    assert "nowhere" not in branches(target)


def test_export_branch_rebased_under_a_symbolic_alias_is_refused(source, target, tmp_path):
    """git records the alias a rebase was started on, not the branch it points
    at; finishing that rebase moves the export branch."""
    assert export(source, target).returncode == 0
    drop_row_on_branch(target)
    git(target, "symbolic-ref", "refs/heads/alias", "refs/heads/" + BRANCH)
    other = tmp_path / "other"
    git(target, "worktree", "add", "-q", "--detach", str(other), BRANCH)
    git(other, "rebase", "--exec", "false", "HEAD~1", "alias", check=False)
    head_name = other / git(other, "rev-parse", "--git-path", "rebase-merge/head-name")
    assert head_name.read_text(encoding="utf-8").strip() == "refs/heads/alias"
    tip = git(target, "rev-parse", BRANCH)
    before = checkout_state(target)
    result = export(source, target)
    assert result.returncode == 1, result.stdout
    assert "being rebased in %s" % os.path.realpath(other) in result.stderr
    assert git(target, "rev-parse", BRANCH) == tip
    assert checkout_state(target) == before


def test_symbolic_origin_main_in_the_source_is_refused(source, target):
    """The commit is checked against origin/main itself. A symbolic origin/main
    checks it against the branch it points at, here the source's own main."""
    commit_source(source, version="0.2.0", files={"hooks/run.sh": "never pushed\n"})
    git(source, "update-ref", "-d", "refs/remotes/origin/main")
    git(source, "symbolic-ref", "refs/remotes/origin/main", "refs/heads/main")
    before = checkout_state(target)
    result = export(source, target, tag="context-rotation-v0.2.0")
    assert result.returncode == 1, result.stdout
    assert "refs/remotes/origin/main is a symbolic ref to refs/heads/main" in result.stderr
    assert checkout_state(target) == before and branches(target) == ["main"]


# The export branch becomes a symbolic ref at the very update-ref that writes
# it. TURN=publication: just before the publishing write, pointing at main.
# Otherwise: the publishing write goes through, the script gets a TERM, and the
# rollback write finds the branch a symbolic ref to a bystander that holds
# this run's commit.
SYMBOLIC_AT_THE_WRITE = """\
case " $* " in
    *" update-ref "*"-m "*)
        if [ "$TURN" = publication ]; then "@GIT@" -C "$2" symbolic-ref "$EXPORT_REF" refs/heads/main; fi
        "@GIT@" "$@"
        status=$?
        if [ "$TURN" != publication ] && [ "$status" = 0 ]; then kill -TERM "$PPID"; fi
        exit "$status" ;;
    *" update-ref "*)
        for new; do :; done
        "@GIT@" -C "$2" update-ref refs/heads/bystander "$new"
        "@GIT@" -C "$2" symbolic-ref "$EXPORT_REF" refs/heads/bystander ;;
esac
exec "@GIT@" "$@"
"""


@pytest.mark.parametrize("turn", ["publication", "rollback of a new branch", "rollback of an existing branch"])
def test_a_branch_turned_symbolic_at_the_write_is_never_written_through(source, target, tmp_path, turn):
    if turn != "rollback of a new branch":
        assert export(source, target).returncode == 0
        drop_row_on_branch(target)
    if turn == "publication":
        # main at the export branch's tip, so a write through the symbolic ref
        # passes the compare-and-swap and moves the checked-out main.
        release(target)
    main = git(target, "rev-parse", "main")
    base = git(target, "rev-parse", BRANCH) if BRANCH in branches(target) else main
    before = checkout_state(target)
    env = git_in_front(tmp_path, SYMBOLIC_AT_THE_WRITE)
    env.update({"TURN": turn.split()[0], "EXPORT_REF": "refs/heads/" + BRANCH})
    result = export(source, target, env=env)
    assert git(target, "rev-parse", "main") == main
    assert checkout_state(target) == before
    assert symbolic_target(target, "refs/heads/" + BRANCH) == ""
    if turn == "publication":
        assert result.returncode == 0, result.stderr
        assert git(target, "rev-parse", BRANCH + "^") == main
        return
    assert result.returncode == 1, result.stdout + result.stderr
    assert "stopped with exit status 143" in result.stderr
    assert git(target, "rev-parse", "bystander^") == base
    if turn == "rollback of a new branch":
        assert BRANCH not in branches(target)
    else:
        assert git(target, "rev-parse", BRANCH) == base


WORKTREE_HEAD_TURNED_SYMBOLIC = """\
case " $* " in
    *" commit "* | *" commit-tree "*) "@GIT@" -C "$2" symbolic-ref HEAD refs/heads/main ;;
esac
exec "@GIT@" "$@"
"""


def test_the_exports_own_worktree_turned_symbolic_moves_no_branch(source, target, tmp_path):
    """The export's own worktree HEAD made a symbolic ref to the checked-out
    main just before the commit is made: the commit goes in without a ref."""
    main = git(target, "rev-parse", "main")
    before = checkout_state(target)
    result = export(source, target, env=git_in_front(tmp_path, WORKTREE_HEAD_TURNED_SYMBOLIC))
    assert result.returncode == 0, result.stderr
    assert git(target, "rev-parse", "main") == main
    assert checkout_state(target) == before
    assert git(target, "rev-parse", BRANCH + "^") == main


# Another export publishes the same commit just before this run's
# compare-and-swap, which then fails.
COMPETING_EXPORT_FIRST = """\
case " $* " in
    *" update-ref "*"-m "*)
        ref= new= old=
        for arg; do ref=$new; new=$old; old=$arg; done
        "@GIT@" -C "$2" update-ref -m "a competing export" "$ref" "$new" ;;
esac
exec "@GIT@" "$@"
"""


@pytest.mark.parametrize("branch_before", ["new", "existing"])
def test_a_lost_compare_and_swap_leaves_the_winners_branch(source, target, tmp_path, branch_before):
    if branch_before == "existing":
        assert export(source, target).returncode == 0
        drop_row_on_branch(target)
    base = git(target, "rev-parse", BRANCH if branch_before == "existing" else "main")
    before = checkout_state(target)
    result = export(source, target, env=git_in_front(tmp_path, COMPETING_EXPORT_FIRST))
    assert result.returncode == 1, result.stdout + result.stderr
    winner = git(target, "rev-parse", BRANCH)
    assert git(target, "rev-parse", BRANCH + "^") == base
    assert "could not write %s" % BRANCH in result.stderr and "points at %s" % winner in result.stderr
    assert "deleted" not in result.stderr and "moved" not in result.stderr
    assert checkout_state(target) == before


# --- refusals before anything is built


def test_target_that_is_not_a_git_repo_is_refused(source, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    result = export(source, plain)
    assert result.returncode != 0 and "git" in result.stderr
    assert list(plain.iterdir()) == []


def test_commit_not_on_origin_main_is_refused(source, target):
    first = git(source, "rev-parse", "HEAD")
    commit_source(source, version="0.2.0", files={"hooks/run.sh": "unmerged\n"})
    git(source, "update-ref", "refs/remotes/origin/main", first)
    before = checkout_state(target)
    result = export(source, target, tag="context-rotation-v0.2.0")
    assert result.returncode != 0 and "origin/main" in result.stderr
    assert checkout_state(target) == before and branches(target) == ["main"]


def test_unknown_commit_is_refused(source, target):
    before = checkout_state(target)
    result = export(source, target, sha="0" * 40)
    assert result.returncode != 0
    assert checkout_state(target) == before and branches(target) == ["main"]


def test_tag_version_differing_from_plugin_json_is_refused(source, target):
    before = checkout_state(target)
    result = export(source, target, tag="context-rotation-v0.9.0")
    assert result.returncode != 0 and "version" in result.stderr
    bad = export(source, target, tag="timekeeper-v0.1.0")
    assert bad.returncode != 0 and "tag" in bad.stderr
    assert checkout_state(target) == before and branches(target) == ["main"]


def test_changed_content_with_the_same_version_is_refused(source, target):
    assert export(source, target).returncode == 0
    git(target, "checkout", "-q", "main")
    tip = git(target, "rev-parse", BRANCH)
    commit_source(source, version="0.1.0", files={"hooks/run.sh": "changed, same version\n"})
    result = export(source, target)
    assert result.returncode == 1 and "version" in result.stderr
    assert git(target, "rev-parse", BRANCH) == tip
    assert blob(target, "context-rotation/hooks/run.sh") == RUN_SH


def test_changed_content_with_a_lower_version_is_refused(source, target):
    commit_source(source, version="0.3.0", files={"hooks/run.sh": "x\n"})
    assert export(source, target, tag="context-rotation-v0.3.0").returncode == 0
    release(target, "export/context-rotation-0.3.0")
    commit_source(source, version="0.2.0", files={"hooks/run.sh": "y\n"})
    result = export(source, target, tag="context-rotation-v0.2.0")
    assert result.returncode == 1 and "version" in result.stderr


def test_planted_leak_writes_no_branch(source, target):
    before = checkout_state(target)
    commit_source(source, version="0.1.0", files={"hooks/leaky.txt": "path %s\n" % LEAK})
    result = export(source, target)
    assert result.returncode == 1
    assert "leak" in result.stderr.lower() and "hooks/leaky.txt" in result.stderr
    assert "pre-existing" not in result.stderr
    assert checkout_state(target) == before
    assert branches(target) == ["main"]


def test_leak_in_the_toolkit_readme_row_is_caught(source, target):
    write(source, OWNED + "/.claude-plugin/plugin.json", json.dumps(
        {"name": "context-rotation", "version": "0.1.0", "description": "Mail me at a" + "@" + "b.example"}) + "\n")
    commit_all(source, "bad description")
    git(source, "update-ref", "refs/remotes/origin/main", git(source, "rev-parse", "HEAD"))
    before = checkout_state(target)
    result = export(source, target)
    assert result.returncode != 0 and "leak" in result.stderr.lower()
    assert "pre-existing" not in result.stderr
    assert checkout_state(target) == before
    assert branches(target) == ["main"]


def test_leak_on_an_existing_export_branch_leaves_its_tip(source, target):
    # An export branch for 0.1.1 that still holds the 0.1.0 release, so the
    # 0.1.1 export is a real commit onto a branch that already exists.
    assert export(source, target).returncode == 0
    git(target, "checkout", "-q", "main")
    git(target, "branch", "-q", "-m", BRANCH, "export/context-rotation-0.1.1")
    tips = branch_tips(target)
    before = checkout_state(target)
    commit_source(source, version="0.1.1", files={"hooks/leaky.txt": LEAK + "\n"})
    result = export(source, target, tag="context-rotation-v0.1.1")
    assert result.returncode == 1
    assert "as it was found" in result.stderr
    assert branch_tips(target) == tips
    assert checkout_state(target) == before


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


# --- the second plugin: --plugin context-meter

METER_BRANCH = "export/context-meter-1.0.0"
METER_ENTRY = {
    "name": "context-meter", "source": "./context-meter",
    "version": "1.0.0", "description": "Shows how full the context is.",
}


def commit_meter(src, version="1.0.0", files=None, name="context-meter"):
    base = Path(src) / METER_OWNED
    write(base, ".claude-plugin/plugin.json", json.dumps(
        {"name": name, "version": version, "description": "Shows how full the context is."}, indent=2) + "\n")
    for rel, text in (files or {}).items():
        write(base, rel, text)
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "meter")
    sha = git(src, "rev-parse", "HEAD")
    git(src, "update-ref", "refs/remotes/origin/main", sha)
    return sha


@pytest.fixture
def meter_source(source):
    for name in ("test_isolation.py", "conftest.py"):
        write(source, Path(METER_OWNED) / "tests" / name, (REAL_METER / "tests" / name).read_text(encoding="utf-8"))
    commit_meter(source, files={"hooks/register.tsx": "export const register = () => {}\n", "README.md": "# meter\n"})
    return source


def export_meter(source, target, **kwargs):
    kwargs.setdefault("tag", "context-meter-v1.0.0")
    return export(source, target, plugin="context-meter", **kwargs)


def test_context_meter_exports_on_its_own_branch_and_leaves_the_other_plugin_alone(meter_source, target):
    before = checkout_state(target)
    result = export_meter(meter_source, target)
    assert result.returncode == 0, result.stderr
    assert checkout_state(target) == before
    names = tracked(target, METER_BRANCH)
    assert "context-meter/hooks/register.tsx" in names
    assert not any(n.startswith(("context-rotation/", "packages/", "unrelated")) for n in names)
    assert marketplace(target, METER_BRANCH)["plugins"] == [METER_ENTRY]
    readme = blob(target, "README.md", METER_BRANCH)
    rows = [line for line in readme.splitlines() if "context-meter/" in line]
    assert len(rows) == 1 and rows[0].startswith("| [`context-meter/`](context-meter/)")
    assert "Shows how full the context is." in rows[0]
    assert "context-meter-v1.0.0" in result.stdout and METER_BRANCH in result.stdout
    assert "export/context-rotation-0.1.0" not in branches(target)
    assert git(target, "log", "-1", "--format=%s", METER_BRANCH) == "feat(context-meter): export 1.0.0"


def test_both_plugins_end_up_in_the_marketplace_once_each(meter_source, target):
    assert export(meter_source, target).returncode == 0
    release(target)
    assert export_meter(meter_source, target).returncode == 0
    release(target, METER_BRANCH)
    plugins = marketplace(target, "main")["plugins"]
    assert [p["name"] for p in plugins] == ["context-rotation", "context-meter"]
    readme = blob(target, "README.md", "main")
    assert len([ln for ln in readme.splitlines() if "context-rotation/" in ln]) == 1
    assert len([ln for ln in readme.splitlines() if "context-meter/" in ln]) == 1
    assert export_meter(meter_source, target).returncode == 0
    assert "Nothing to export" in export_meter(meter_source, target).stdout


def test_explicit_context_rotation_is_the_default(meter_source, target):
    result = export(meter_source, target, plugin="context-rotation")
    assert result.returncode == 0, result.stderr
    assert BRANCH in branches(target)
    assert marketplace(target)["plugins"][-1] == EXPORTED_ENTRY


def test_unknown_plugin_is_refused(meter_source, target):
    before = checkout_state(target)
    result = export(meter_source, target, plugin="timekeeper", tag="timekeeper-v1.0.0")
    assert result.returncode == 1 and "unknown plugin" in result.stderr
    assert checkout_state(target) == before and branches(target) == ["main"]


def test_a_tag_of_the_other_plugin_is_refused(meter_source, target):
    before = checkout_state(target)
    result = export_meter(meter_source, target, tag="context-rotation-v1.0.0")
    assert result.returncode == 1 and "context-meter-v" in result.stderr
    assert checkout_state(target) == before and branches(target) == ["main"]


def test_a_manifest_that_names_another_plugin_is_refused(meter_source, target):
    commit_meter(meter_source, name="context-rotation", files={"hooks/other.txt": "x\n"})
    before = checkout_state(target)
    result = export_meter(meter_source, target)
    assert result.returncode == 1 and "expected 'context-meter'" in result.stderr
    assert checkout_state(target) == before and branches(target) == ["main"]


def test_a_leak_in_the_meter_writes_no_branch(meter_source, target):
    commit_meter(meter_source, files={"hooks/leaky.txt": "path %s\n" % LEAK})
    before = checkout_state(target)
    result = export_meter(meter_source, target)
    assert result.returncode == 1 and "leak" in result.stderr.lower() and "hooks/leaky.txt" in result.stderr
    assert checkout_state(target) == before and branches(target) == ["main"]


def test_the_real_meter_has_what_the_export_needs():
    manifest = json.loads((REAL_METER / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "context-meter" and manifest["description"]
    assert (REAL_METER / "tests" / "test_isolation.py").is_file()
