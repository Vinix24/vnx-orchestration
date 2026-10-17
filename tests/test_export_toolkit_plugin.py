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
        write(base, rel, text)
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


def assert_untouched(target, head):
    assert git(target, "rev-parse", "HEAD") == head
    assert branches(target) == ["main"]
    assert git(target, "status", "--porcelain", "--untracked-files=all") == ""
    assert not (Path(target) / "context-rotation").exists()


def marketplace(target):
    return json.loads((Path(target) / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))


def test_first_export_creates_folder_marketplace_and_readme_row(source, target):
    sha = git(source, "rev-parse", "HEAD")
    result = export(source, target)
    assert result.returncode == 0, result.stderr
    assert git(target, "branch", "--show-current") == "export/context-rotation-0.1.0"
    root = Path(target) / "context-rotation"
    assert (root / ".claude-plugin" / "plugin.json").is_file()
    assert (root / "hooks" / "run.sh").read_text() == "#!/bin/sh\nexit 0\n"
    assert os.access(root / "hooks" / "run.sh", os.R_OK)
    assert not (Path(target) / "unrelated.txt").exists()
    assert not (Path(target) / "packages").exists()
    doc = marketplace(target)
    assert doc["name"] == "claude-code-toolkit"
    assert doc["owner"]["name"]
    assert doc["plugins"] == [{
        "name": "context-rotation", "source": "./context-rotation",
        "version": "0.1.0", "description": "Guards session context.",
    }] or [p["name"] for p in doc["plugins"]] == ["context-rotation"]
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
    assert [p["name"] for p in marketplace(fresh)["plugins"]] == ["context-rotation"]
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
    write(target, "scratch-outside.txt", "untracked, not ours\n")
    result = export(source, target, tag="context-rotation-v0.1.1")
    assert result.returncode == 0, result.stderr
    assert not (Path(target) / "context-rotation" / "hooks" / "old.sh").exists()
    assert (Path(target) / "context-rotation" / "hooks" / "run.sh").exists()
    assert (Path(target) / "timekeeper" / "tk.txt").read_text() == "tk\n"
    assert (Path(target) / "docs" / "keep.md").read_text() == "keep\n"
    assert (Path(target) / "scratch-outside.txt").read_text() == "untracked, not ours\n"


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
    leak = "/Us" + "ers/someone/project"
    commit_source(source, version="0.1.0", files={"hooks/leaky.txt": "path %s\n" % leak})
    result = export(source, target)
    assert result.returncode != 0
    assert "leak" in result.stderr.lower() and "hooks/leaky.txt" in result.stderr
    assert_untouched(target, head)


def test_leak_in_the_toolkit_readme_row_is_caught(source, target):
    head = git(target, "rev-parse", "HEAD")
    write(source, OWNED + "/.claude-plugin/plugin.json", json.dumps(
        {"name": "context-rotation", "version": "0.1.0", "description": "Mail me at a" + "@" + "b.example"}) + "\n")
    git(source, "add", "-A")
    git(source, "commit", "-q", "-m", "bad description")
    git(source, "update-ref", "refs/remotes/origin/main", git(source, "rev-parse", "HEAD"))
    result = export(source, target)
    assert result.returncode != 0 and "leak" in result.stderr.lower()
    assert_untouched(target, head)


def test_leak_on_an_existing_export_branch_resets_only_to_the_previous_tip(source, target):
    assert export(source, target).returncode == 0
    tip = git(target, "rev-parse", "HEAD")
    leak = "/Us" + "ers/someone"
    commit_source(source, version="0.1.1", files={"hooks/leaky.txt": leak + "\n"})
    result = export(source, target, tag="context-rotation-v0.1.1")
    assert result.returncode != 0
    assert git(target, "rev-parse", "HEAD") == tip
    assert git(target, "status", "--porcelain", "--untracked-files=all") == ""
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
