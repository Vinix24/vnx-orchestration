"""The native claude destination of the skill materializer (skilldisc R3, ADR-059)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import agent_skill_materialize as asm
import envelope_adapters_claude as eac
from envelope_types import EnvelopeSpec

ROLE = "backend-developer"
IGNORE = ".vnx-agent-skills/\n.claude/skills/*/SKILL.md\n"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout


def _make_worktree(tmp_path: Path, gitignore: str) -> Path:
    main = tmp_path / "main"
    main.mkdir(parents=True)
    _git(main, "init", "-q", "-b", "main")
    (main / ".gitignore").write_text(gitignore)
    for slug in ("alpha", "beta"):
        (main / "skills" / slug).mkdir(parents=True)
        (main / "skills" / slug / "SKILL.md").write_text(f"{slug}\n")
    agent = main / "agents" / ROLE
    agent.mkdir(parents=True)
    (agent / "CLAUDE.md").write_text("role\n")
    (agent / "config.yaml").write_text("skills:\n  - alpha\n  - beta\n")
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "init")
    wt = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", "-b", "dispatch/x", str(wt))
    return wt


@pytest.fixture
def worktree(tmp_path):
    return _make_worktree(tmp_path, IGNORE)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    for key in ("VNX_AGENT_LAYOUT", "VNX_OVERRIDE_AGENT_LAYOUT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        "dispatch_worktree_isolation.resolve_consumer_project_root", lambda: tmp_path / "main"
    )


def _call(wt, tmp_path, **over):
    args = dict(worktree=wt, provider="claude", mode="on", dispatch_id="dlv-r3",
                state_dir=tmp_path / "state", project_id="proj")
    args.update(over)
    return asm.materialize_skill_set(args.pop("role", ROLE), **args)


def _dest(wt, slug="alpha"):
    return wt / ".claude" / "skills" / slug / "SKILL.md"


def _run(tmp_path, cwd, role=ROLE):
    spec = EnvelopeSpec(
        dispatch_id="dlv-r3", terminal_id="T1", provider="claude", model="sonnet",
        instruction="do it", role=role, pr_id=None, data_dir=tmp_path / "data",
        state_dir=tmp_path / "state",
    )
    fake = mock.Mock(return_value=SimpleNamespace(
        returncode=0, completion_text="", token_usage={}, session_id=None, spawn_refused=None,
        model_resolved=None, model="sonnet", timed_out=False, event_writer_failures=0,
        error=None, stopped_early=False))
    with mock.patch("provider_spawns.claude_spawn.spawn_claude", fake), \
         mock.patch("provider_switch.worker_env_stamps", return_value={}), \
         mock.patch("agent_layout.layout_mode", return_value="on"), \
         mock.patch("project_scope.resolve_stamp_project_id", return_value="proj"):
        eac.ClaudeSubprocessAdapter().run(spec, cwd=cwd)
    return fake


def test_run_materializes_native_files_before_spawn(tmp_path, worktree):
    fake = _run(tmp_path, worktree)
    assert fake.call_count == 1
    for slug in ("alpha", "beta"):
        assert _dest(worktree, slug).is_file()
        assert not os.path.islink(_dest(worktree, slug))
        assert _dest(worktree, slug).read_text() == f"{slug}\n"
    assert _git(worktree, "status", "--porcelain") == ""


def test_existing_native_skill_is_kept(tmp_path, worktree):
    _dest(worktree).parent.mkdir(parents=True)
    _dest(worktree).write_text("mine\n")
    result = _call(worktree, tmp_path)
    assert _dest(worktree).read_text() == "mine\n"
    assert [e["ownership"] for e in result["skills"]] == ["native", "materialized"]
    only_native = _call(worktree, tmp_path, dispatch_id="dlv-r3b")
    assert only_native["status"] == "ok" and only_native["reason"] == "already_native"


def test_tracked_native_skill_is_never_overwritten(tmp_path):
    wt = _make_worktree(tmp_path / "t", IGNORE)
    _dest(wt).parent.mkdir(parents=True)
    _dest(wt).write_text("tracked\n")
    _git(wt, "add", "-f", ".claude/skills/alpha/SKILL.md")
    result = _call(wt, tmp_path)
    assert _dest(wt).read_text() == "tracked\n"
    assert result["skills"][0]["ownership"] == "native"


def test_not_ignored_destination_writes_nothing(tmp_path):
    wt = _make_worktree(tmp_path / "t", ".vnx-agent-skills/\n")
    result = _call(wt, tmp_path)
    assert (result["status"], result["reason"]) == ("skipped", "not_ignored")
    assert not (wt / ".claude").exists()


def test_project_root_is_refused(tmp_path, worktree, monkeypatch):
    monkeypatch.setattr("dispatch_worktree_isolation.resolve_consumer_project_root", lambda: worktree)
    result = _call(worktree, tmp_path)
    assert (result["status"], result["reason"]) == ("skipped", "path_outside_worktree")
    assert not (worktree / ".claude").exists()


def test_home_claude_dir_is_refused(tmp_path, worktree, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: worktree.parent))
    inner = worktree.parent / ".claude" / "w"
    inner.mkdir(parents=True)
    assert _call(inner, tmp_path)["reason"] == "path_outside_worktree"


@pytest.mark.parametrize("field,reason", [
    ("role", "no_role"), ("worktree", "no_cwd"), ("state_dir", "no_state_dir"),
    ("dispatch_id", "no_dispatch_id"), ("project_id", "no_project_id"),
])
def test_missing_field_skips_with_one_warning(tmp_path, worktree, caplog, field, reason):
    with caplog.at_level(logging.WARNING):
        result = _call(worktree, tmp_path, **{field: None})
    assert (result["status"], result["reason"]) == ("skipped", reason)
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
    assert not (worktree / ".claude").exists()


def test_unknown_role_and_provider_and_off_write_nothing(tmp_path, worktree):
    assert _call(worktree, tmp_path, provider="kimi")["status"] == "skipped"
    assert _call(worktree, tmp_path, mode="off")["status"] == "skipped"
    assert not (tmp_path / "state").exists()
    assert _call(worktree, tmp_path, role="no-such-role")["reason"] == "no_role"
    assert not (worktree / ".claude").exists()


def test_two_runs_write_two_attempt_records(tmp_path, worktree):
    _call(worktree, tmp_path)
    _call(worktree, tmp_path)
    folder = tmp_path / "state" / "agent_skill_materializations" / "dlv-r3"
    assert sorted(p.name for p in folder.iterdir()) == ["claude.attempt-1.json", "claude.attempt-2.json"]
    record = json.loads((folder / "claude.attempt-2.json").read_text())
    assert record["attempt"] == 2 and record["dispatch_id"] == "dlv-r3" and record["project_id"] == "proj"
    for key in ("schema", "status", "reason", "role", "provider", "worktree", "session_id",
                "captured_at", "skills", "manifest_sha256"):
        assert key in record
    assert record["skills"][0].keys() == {"slug", "dest", "sha256", "bytes", "ownership"}


def test_status_and_reason_sets_are_pinned():
    assert asm.STATUSES == frozenset({"ok", "partial", "error", "skipped"})
    assert asm.REASONS == frozenset({
        "no_role", "no_declared_set", "no_cwd", "no_state_dir", "no_dispatch_id", "no_project_id",
        "not_isolated", "not_ignored", "already_native", "source_missing", "copy_failed:<Class>",
        "not_owned:<slug>", "path_outside_worktree", "symlink_in_destination",
    })


def test_copy_failure_is_partial_and_fail_open(tmp_path, worktree):
    real = asm._copy_skill

    def boom(base, root, slug, src):
        if slug == "beta":
            raise PermissionError("no")
        return real(base, root, slug, src)

    with mock.patch.object(asm, "_copy_skill", boom):
        result = _call(worktree, tmp_path)
    assert (result["status"], result["reason"]) == ("partial", "copy_failed:PermissionError")
    assert _dest(worktree, "alpha").is_file()


def test_unknown_declared_slug_is_source_missing(tmp_path, worktree):
    (worktree / "agents" / ROLE / "config.yaml").write_text("skills:\n  - alpha\n  - nope\n")
    result = _call(worktree, tmp_path)
    assert (result["status"], result["reason"]) == ("partial", "source_missing")


def test_shadow_mode_is_byte_equal_to_materialize_role_skills(tmp_path, worktree, monkeypatch):
    monkeypatch.setenv("VNX_AGENT_LAYOUT", "shadow")
    direct = asm.materialize_role_skills(worktree, ROLE, ["alpha", "beta"])
    via = _call(worktree, tmp_path, mode="shadow")
    assert via == direct and via["status"] == "ok"
    assert not (worktree / ".claude").exists()


def _ship_assets(wt):
    (wt / "skills" / "alpha" / "assets").mkdir()
    (wt / "skills" / "alpha" / "assets" / "data.txt").write_text("shipped\n")


def _outside(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data.txt").write_text("original")
    return outside


def _assert_refused(result, outside=None):
    assert (result["status"], result["reason"]) == ("skipped", "symlink_in_destination")
    assert result["skills"] == []
    if outside is not None:
        assert (outside / "data.txt").read_text() == "original"


def test_linked_subdirectory_of_destination_refuses(tmp_path, worktree):
    _ship_assets(worktree)
    outside = _outside(tmp_path)
    dest = worktree / ".claude" / "skills" / "alpha"
    dest.mkdir(parents=True)
    (dest / "assets").symlink_to(outside, target_is_directory=True)
    result = _call(worktree, tmp_path)
    assert (outside / "data.txt").read_text() == "original"
    assert result["status"] == "skipped"
    assert result["reason"] == "symlink_in_destination"
    assert result["skills"] == []
    assert not _dest(worktree).exists()


def test_linked_slug_directory_refuses(tmp_path, worktree):
    outside = _outside(tmp_path)
    (worktree / ".claude" / "skills").mkdir(parents=True)
    (worktree / ".claude" / "skills" / "alpha").symlink_to(outside, target_is_directory=True)
    _assert_refused(_call(worktree, tmp_path), outside)
    assert not (outside / "SKILL.md").exists()


def test_linked_skills_directory_refuses(tmp_path, worktree):
    real = worktree / "elsewhere"
    real.mkdir()
    (worktree / ".claude").mkdir()
    (worktree / ".claude" / "skills").symlink_to(real, target_is_directory=True)
    _assert_refused(_call(worktree, tmp_path))
    assert list(real.iterdir()) == []


def test_file_where_directory_belongs_refuses(tmp_path, worktree):
    _ship_assets(worktree)
    dest = worktree / ".claude" / "skills" / "alpha"
    dest.mkdir(parents=True)
    (dest / "assets").write_text("a file\n")
    _assert_refused(_call(worktree, tmp_path))
    assert (dest / "assets").read_text() == "a file\n"


def test_link_planted_after_the_check_fails_the_write(tmp_path, worktree):
    _ship_assets(worktree)
    outside = _outside(tmp_path)
    dest = worktree / ".claude" / "skills" / "alpha"
    real_refuse = asm._refuse_dest_tree

    def check_then_plant(wt, slug, src, *parts):
        real_refuse(wt, slug, src, *parts)
        if slug == "alpha":
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "assets").symlink_to(outside, target_is_directory=True)

    with mock.patch.object(asm, "_refuse_dest_tree", check_then_plant):
        result = _call(worktree, tmp_path)
    _assert_refused(result, outside)


def test_file_link_planted_after_the_check_is_not_followed(tmp_path, worktree):
    outside = _outside(tmp_path)
    dest = worktree / ".claude" / "skills" / "alpha"
    real_refuse = asm._refuse_dest_tree

    def check_then_plant(wt, slug, src, *parts):
        real_refuse(wt, slug, src, *parts)
        if slug == "alpha":
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "SKILL.md").symlink_to(outside / "data.txt")

    with mock.patch.object(asm, "_refuse_dest_tree", check_then_plant):
        result = _call(worktree, tmp_path)
    assert result["status"] != "ok"
    assert (outside / "data.txt").read_text() == "original"


def _empty_outside(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    return outside


def _swap_after(monkeypatch, name, swap):
    """Run the original validation, then let *swap* replace an ancestor (the check-then-use window)."""
    real = getattr(asm, name)
    seen: list[str] = []

    def wrapper(*args, **kwargs):
        out = real(*args, **kwargs)
        seen.append(name)
        # the native ignore check is the last validation and runs once per slug (alpha, beta): swap after the last one
        if name != "_dest_ignored" or len(seen) == 2:
            swap()
        return out

    monkeypatch.setattr(asm, name, wrapper)


def test_swapped_dot_claude_ancestor_is_not_followed(tmp_path, worktree, monkeypatch):
    outside = _empty_outside(tmp_path)
    (outside / "skills").mkdir()

    def swap():
        (worktree / ".claude").symlink_to(outside, target_is_directory=True)

    _swap_after(monkeypatch, "_dest_ignored", swap)
    result = _call(worktree, tmp_path)
    assert result["status"] != "ok"
    assert list(outside.rglob("*")) == [outside / "skills"]


def test_swapped_existing_dot_claude_ancestor_is_not_followed(tmp_path, worktree, monkeypatch):
    outside = _empty_outside(tmp_path)
    (outside / "skills").mkdir()
    (worktree / ".claude" / "skills").mkdir(parents=True)

    def swap():
        moved = tmp_path / "moved-claude"
        (worktree / ".claude").rename(moved)
        (worktree / ".claude").symlink_to(outside, target_is_directory=True)

    _swap_after(monkeypatch, "_dest_ignored", swap)
    result = _call(worktree, tmp_path)
    assert result["status"] != "ok"
    assert list(outside.rglob("*")) == [outside / "skills"]


def test_swapped_shadow_dir_is_not_followed(tmp_path, worktree, monkeypatch):
    monkeypatch.setenv("VNX_AGENT_LAYOUT", "shadow")
    outside = _empty_outside(tmp_path)

    def swap():
        (worktree / ".vnx-agent-skills").symlink_to(outside, target_is_directory=True)

    _swap_after(monkeypatch, "_checked_base", swap)
    result = asm.materialize_role_skills(worktree, ROLE, ["alpha", "beta"])
    assert result["status"] == "error"
    assert list(outside.rglob("*")) == []


def test_swapped_shadow_ancestor_blocks_the_manifest_write(tmp_path, worktree, monkeypatch):
    monkeypatch.setenv("VNX_AGENT_LAYOUT", "shadow")
    outside = _empty_outside(tmp_path)
    real_write = asm._write_manifest

    def swap_then_write(role_fd, manifest):
        shadow = worktree / ".vnx-agent-skills"
        moved = tmp_path / "moved"
        shadow.rename(moved)
        shadow.symlink_to(outside, target_is_directory=True)
        real_write(role_fd, manifest)

    monkeypatch.setattr(asm, "_write_manifest", swap_then_write)
    result = asm.materialize_role_skills(worktree, ROLE, ["alpha"])
    assert result["status"] == "ok"
    assert list(outside.rglob("*")) == []
    assert (tmp_path / "moved" / ROLE / "manifest.json").is_file()


def test_swapped_ancestor_of_the_error_manifest_is_not_followed(tmp_path, worktree, monkeypatch):
    monkeypatch.setenv("VNX_AGENT_LAYOUT", "shadow")
    outside = _empty_outside(tmp_path)

    def swap():
        (worktree / ".vnx-agent-skills").symlink_to(outside, target_is_directory=True)

    _swap_after(monkeypatch, "_checked_base", swap)
    result = asm.materialize_role_skills(worktree, ROLE, ["nonexistent"])
    assert result["status"] == "error"
    assert list(outside.rglob("*")) == []


# --- r5: every destination file is new, untracked and ignored before the copy writes it ---

IGNORE_ASSETS = IGNORE + ".claude/skills/*/assets/\n"


def _asset(wt, slug="alpha"):
    return wt / ".claude" / "skills" / slug / "assets" / "data.txt"


def _track_asset(wt, content=b"existing tracked asset\n"):
    _asset(wt).parent.mkdir(parents=True)
    _asset(wt).write_bytes(content)
    _git(wt, "add", "-f", ".claude/skills/alpha/assets/data.txt")
    _git(wt, "commit", "-q", "-m", "tracked asset")


def _assert_alpha_refused(result, reason):
    assert (result["status"], result["reason"]) == ("partial", reason)
    assert [e["slug"] for e in result["skills"]] == ["beta"]


@pytest.mark.parametrize("gitignore", [IGNORE, IGNORE_ASSETS], ids=["assets-not-ignored", "assets-ignored"])
def test_tracked_asset_without_skill_md_is_never_overwritten(tmp_path, gitignore):
    wt = _make_worktree(tmp_path / "t", gitignore)
    _ship_assets(wt)
    _track_asset(wt)
    result = _call(wt, tmp_path)
    assert _asset(wt).read_bytes() == b"existing tracked asset\n"
    assert not _dest(wt).exists()
    _assert_alpha_refused(result, "not_owned:alpha")


def test_tracked_asset_missing_from_the_worktree_is_still_not_ours(tmp_path):
    wt = _make_worktree(tmp_path / "t", IGNORE_ASSETS)
    _ship_assets(wt)
    _track_asset(wt)
    _asset(wt).unlink()
    result = _call(wt, tmp_path)
    assert not _asset(wt).exists()
    assert not _dest(wt).exists()
    _assert_alpha_refused(result, "not_owned:alpha")


def test_asset_that_git_does_not_ignore_is_never_written(tmp_path, worktree):
    _ship_assets(worktree)
    result = _call(worktree, tmp_path)
    assert ".claude" not in _git(worktree, "status", "--porcelain", "--untracked-files=all")
    assert not (worktree / ".claude" / "skills" / "alpha").exists()
    _assert_alpha_refused(result, "not_ignored")


def test_existing_untracked_asset_is_never_overwritten(tmp_path):
    wt = _make_worktree(tmp_path / "t", IGNORE_ASSETS)
    _ship_assets(wt)
    _asset(wt).parent.mkdir(parents=True)
    _asset(wt).write_bytes(b"user-owned\n")
    result = _call(wt, tmp_path)
    assert _asset(wt).read_bytes() == b"user-owned\n"
    assert not _dest(wt).exists()
    _assert_alpha_refused(result, "not_owned:alpha")


def test_asset_planted_after_the_last_check_is_never_overwritten(tmp_path, monkeypatch):
    wt = _make_worktree(tmp_path / "t", IGNORE_ASSETS)
    _ship_assets(wt)

    def plant():
        _asset(wt).parent.mkdir(parents=True, exist_ok=True)
        _asset(wt).write_bytes(b"planted\n")

    _swap_after(monkeypatch, "_dest_ignored", plant)
    result = _call(wt, tmp_path)
    assert _asset(wt).read_bytes() == b"planted\n"
    _assert_alpha_refused(result, "not_owned:alpha")
    assert not _dest(wt).exists()


def test_skill_md_only_skills_in_a_clean_worktree_are_ok(tmp_path, worktree):
    result = _call(worktree, tmp_path)
    assert (result["status"], result["reason"]) == ("ok", None)
    assert [e["ownership"] for e in result["skills"]] == ["materialized", "materialized"]
    assert result["skills"][0]["sha256"] == hashlib.sha256(b"alpha\n").hexdigest()
    assert result["skills"][0]["bytes"] == len(b"alpha\n")
    assert _git(worktree, "status", "--porcelain", "--untracked-files=all") == ""


def test_failed_copy_leaves_no_skill_md_and_the_next_attempt_copies_again(tmp_path):
    wt = _make_worktree(tmp_path / "t", IGNORE_ASSETS)
    _ship_assets(wt)
    real_mkdir = os.mkdir

    def refuse_alpha(path, mode=0o777, *, dir_fd=None):
        if path == "alpha":
            raise PermissionError("no")
        return real_mkdir(path, mode, dir_fd=dir_fd)

    with mock.patch.object(asm.os, "mkdir", refuse_alpha):
        first = _call(wt, tmp_path)
    assert not os.path.lexists(_dest(wt).parent)
    _assert_alpha_refused(first, "copy_failed:PermissionError")
    second = _call(wt, tmp_path, dispatch_id="dlv-r3b")
    assert (second["status"], second["reason"]) == ("ok", None)
    assert [e["ownership"] for e in second["skills"]] == ["materialized", "native"]
    assert _asset(wt).read_text() == "shipped\n"


def test_source_link_leaving_the_skill_is_source_missing(tmp_path, worktree):
    secret = tmp_path / "secret.txt"
    secret.write_text("s\n")
    (worktree / "skills" / "alpha" / "leak.md").symlink_to(secret)
    result = _call(worktree, tmp_path)
    _assert_alpha_refused(result, "source_missing")
    assert result["reason"] in asm.REASONS
    assert not (worktree / ".claude" / "skills" / "alpha").exists()


def test_linked_native_skill_md_refuses_before_any_copy(tmp_path, worktree):
    (worktree / "agents" / ROLE / "config.yaml").write_text("skills:\n  - beta\n  - alpha\n")
    _dest(worktree).parent.mkdir(parents=True)
    _dest(worktree).symlink_to(worktree / "skills" / "alpha" / "SKILL.md")
    result = _call(worktree, tmp_path)
    assert (result["status"], result["reason"], result["skills"]) == ("skipped", "symlink_in_destination", [])
    assert not (worktree / ".claude" / "skills" / "beta").exists()


def test_duplicate_declared_slug_is_copied_once(tmp_path, worktree):
    (worktree / "agents" / ROLE / "config.yaml").write_text("skills:\n  - alpha\n  - beta\n  - alpha\n")
    result = _call(worktree, tmp_path)
    assert (result["status"], result["reason"]) == ("ok", None)
    assert [e["slug"] for e in result["skills"]] == ["alpha", "beta"]


def _git_spy(monkeypatch, answer=None):
    """Record each git subcommand; *answer* maps a subcommand to a forced (returncode, stdout)."""
    real_run = subprocess.run
    calls: list[str] = []

    def run(cmd, *args, **kwargs):
        sub = cmd[3] if cmd[:2] == ["git", "-C"] else None
        calls.append(sub)
        if answer and sub in answer:
            code, out = answer[sub]
            return subprocess.CompletedProcess(cmd, code, out, b"")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(asm.subprocess, "run", run)
    return calls


def test_one_git_call_each_covers_every_file_of_every_slug(tmp_path, monkeypatch):
    wt = _make_worktree(tmp_path / "t", IGNORE_ASSETS)
    _ship_assets(wt)
    (wt / "skills" / "alpha" / "assets" / "more.txt").write_text("more\n")
    calls = _git_spy(monkeypatch)
    result = _call(wt, tmp_path)
    assert (result["status"], result["reason"]) == ("ok", None)
    assert sorted(c for c in calls if c in ("check-ignore", "ls-files")) == ["check-ignore", "ls-files"]
    assert (_asset(wt).parent / "more.txt").read_text() == "more\n"


ALL_IGNORED = b".claude/skills/alpha/SKILL.md\0.claude/skills/beta/SKILL.md\0"


@pytest.mark.parametrize("answer", [
    {"check-ignore": (128, b"")},
    {"check-ignore": (1, ALL_IGNORED)},
    {"check-ignore": (0, b"")},
    {"check-ignore": (0, b"garbage\0")},
    {"ls-files": (128, b"")},
], ids=["check-ignore-fatal", "check-ignore-rc1-with-output", "check-ignore-empty",
        "check-ignore-unknown-path", "ls-files-fatal"])
def test_git_without_positive_evidence_writes_nothing(tmp_path, worktree, monkeypatch, answer):
    _git_spy(monkeypatch, answer)
    result = _call(worktree, tmp_path)
    assert (result["status"], result["reason"], result["skills"]) == ("skipped", "not_ignored", [])
    assert not (worktree / ".claude").exists()


class _ReadBlocked(Exception):
    """Raised by the alarm; deliberately not an OSError, so no except-OSError branch can absorb it."""


@pytest.mark.parametrize("kind", ["fifo", "directory"])
def test_native_skill_md_that_is_not_a_file_refuses_without_blocking(tmp_path, worktree, kind):
    import signal

    _dest(worktree).parent.mkdir(parents=True)
    if kind == "fifo":
        os.mkfifo(_dest(worktree))
    else:
        _dest(worktree).mkdir()

    def interrupt(signum, frame):
        raise _ReadBlocked("the native read blocked")

    previous = signal.signal(signal.SIGALRM, interrupt)
    signal.setitimer(signal.ITIMER_REAL, 5)
    try:
        result = _call(worktree, tmp_path)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    assert (result["status"], result["reason"], result["skills"]) == ("skipped", "symlink_in_destination", [])
    assert not (worktree / ".claude" / "skills" / "beta").exists()


# --- r6: a skill file appears at its final name only when it is complete ---

PARTIAL_RE = r"^\.(SKILL\.md|data\.txt)\.vnx-partial-[0-9a-f]{32}$"


def _partials(wt):
    """Every private copy name left anywhere under ``.claude``; links are listed, never followed."""
    found = []
    for dirpath, dirs, names in os.walk(wt / ".claude"):
        found += [Path(dirpath) / n for n in dirs + names if ".vnx-partial-" in n]
    return sorted(found)


def _break_write(target: bytes, kind: str):
    """Patch the ``wb`` handle so the write of *target* stops after two bytes.

    ``raises``: two bytes reach the file, then ENOSPC (disk full halfway). ``short``: two
    bytes reach the file and the write returns as if it was done. Returns the patch and
    the list that records each injection, so a test proves the fault really fired.
    """
    import errno

    real_fdopen = os.fdopen
    fired: list[int] = []

    class Handle:
        def __init__(self, inner):
            self._inner = inner

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._inner.close()
            return False

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def write(self, data):
            if bytes(data) != target:
                return self._inner.write(data)
            fired.append(len(data))
            self._inner.write(data[:2])
            self._inner.flush()
            if kind == "raises":
                raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
            return 2

    def fdopen(fd, mode="r", *args, **kwargs):
        inner = real_fdopen(fd, mode, *args, **kwargs)
        return Handle(inner) if mode == "wb" else inner

    return mock.patch.object(asm.os, "fdopen", fdopen), fired


@pytest.mark.parametrize("kind,reason", [("raises", "copy_failed:OSError"),
                                         ("short", "copy_failed:IncompleteWrite")])
def test_failed_skill_md_write_leaves_nothing_and_the_next_run_copies(tmp_path, worktree, kind, reason):
    patch, fired = _break_write(b"alpha\n", kind)
    with patch:
        first = _call(worktree, tmp_path)
    assert fired == [len(b"alpha\n")]
    assert not os.path.lexists(_dest(worktree))
    assert _partials(worktree) == []
    _assert_alpha_refused(first, reason)
    second = _call(worktree, tmp_path, dispatch_id="dlv-r3b")
    assert (second["status"], second["reason"]) == ("ok", None)
    assert [e["ownership"] for e in second["skills"]] == ["materialized", "native"]
    assert second["skills"][0]["sha256"] == hashlib.sha256(b"alpha\n").hexdigest()
    assert _dest(worktree).read_bytes() == b"alpha\n"


def test_failed_asset_write_leaves_nothing_and_the_next_run_copies(tmp_path):
    wt = _make_worktree(tmp_path / "t", IGNORE_ASSETS)
    _ship_assets(wt)
    patch, fired = _break_write(b"shipped\n", "raises")
    with patch:
        first = _call(wt, tmp_path)
    assert fired == [len(b"shipped\n")]
    assert not os.path.lexists(_asset(wt).parent)
    assert not os.path.lexists(_dest(wt).parent)
    assert _partials(wt) == []
    _assert_alpha_refused(first, "copy_failed:OSError")
    second = _call(wt, tmp_path, dispatch_id="dlv-r3b")
    assert (second["status"], second["reason"]) == ("ok", None)
    assert _asset(wt).read_bytes() == b"shipped\n"
    assert _dest(wt).read_bytes() == b"alpha\n"


def _plant_before_link(target: bytes, plant):
    """Let *plant* create an entry at the final name after the private copy is written, just before the link."""
    real_link = os.link
    sources: list[str] = []

    def link(src, dst, *, src_dir_fd=None, dst_dir_fd=None, follow_symlinks=True):
        fd = os.open(src, os.O_RDONLY, dir_fd=src_dir_fd)
        try:
            content = os.read(fd, 4096)
        finally:
            os.close(fd)
        if content == target:
            sources.append(src)
            plant(dst, dst_dir_fd)
        return real_link(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd, follow_symlinks=follow_symlinks)

    return mock.patch.object(asm.os, "link", link), sources


def _plant_file(name, dir_fd):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=dir_fd)
    try:
        os.write(fd, b"planted\n")
    finally:
        os.close(fd)


@pytest.mark.parametrize("target", [b"alpha\n", b"shipped\n"], ids=["skill-md", "asset"])
def test_file_that_appears_before_the_link_is_never_replaced(tmp_path, target):
    import re

    wt = _make_worktree(tmp_path / "t", IGNORE_ASSETS)
    _ship_assets(wt)
    patch, sources = _plant_before_link(target, _plant_file)
    with patch:
        result = _call(wt, tmp_path)
    assert len(sources) == 1 and re.match(PARTIAL_RE, sources[0])
    final = _dest(wt) if target == b"alpha\n" else _asset(wt)
    assert final.read_bytes() == b"planted\n"
    assert _partials(wt) == []
    _assert_alpha_refused(result, "not_owned:alpha")
    if target == b"shipped\n":
        assert not os.path.lexists(_dest(wt))


def test_link_that_appears_before_the_link_is_never_followed(tmp_path, worktree):
    outside = _outside(tmp_path)

    def plant_link(name, dir_fd):
        os.symlink(str(outside / "data.txt"), name, dir_fd=dir_fd)

    patch, sources = _plant_before_link(b"alpha\n", plant_link)
    with patch:
        result = _call(worktree, tmp_path)
    assert len(sources) == 1
    _assert_refused(result, outside)
    assert os.path.islink(_dest(worktree))
    assert _partials(worktree) == []
    assert not (worktree / ".claude" / "skills" / "beta").exists()


@pytest.mark.parametrize("err,reason", [
    ("EPERM", "copy_failed:LinkUnsupported"),
    ("ENOTSUP", "copy_failed:LinkUnsupported"),
    ("EOPNOTSUPP", "copy_failed:LinkUnsupported"),
    ("EIO", "copy_failed:OSError"),
])
def test_filesystem_without_hard_links_never_writes_the_final_name(tmp_path, worktree, err, reason):
    import errno

    code = getattr(errno, err)

    def link(*args, **kwargs):
        raise OSError(code, os.strerror(code))

    with mock.patch.object(asm.os, "link", link):
        result = _call(worktree, tmp_path)
    assert (result["status"], result["reason"], result["skills"]) == ("error", reason, [])
    assert reason.split(":")[0] + ":<Class>" in asm.REASONS
    for slug in ("alpha", "beta"):
        assert not os.path.lexists(_dest(worktree, slug))
    assert _partials(worktree) == []


def _leftover(wt, content=None, link_to=None):
    folder = _dest(wt).parent
    folder.mkdir(parents=True)
    leftover = folder / (".SKILL.md.vnx-partial-" + "0" * 32)
    if link_to is not None:
        leftover.symlink_to(link_to)
    else:
        leftover.write_bytes(content)
    return leftover


def test_leftover_partial_of_a_run_killed_after_the_link_is_not_read_counted_or_removed(tmp_path, worktree):
    leftover = _leftover(worktree, b"alpha\n")
    os.link(leftover, _dest(worktree))
    result = _call(worktree, tmp_path)
    assert (result["status"], result["reason"]) == ("ok", None)
    assert [e["ownership"] for e in result["skills"]] == ["native", "materialized"]
    assert result["skills"][0]["sha256"] == hashlib.sha256(b"alpha\n").hexdigest()
    assert _dest(worktree).read_bytes() == b"alpha\n"
    assert leftover.read_bytes() == b"alpha\n"
    assert _partials(worktree) == [leftover]


def test_folder_of_a_run_killed_before_the_link_is_foreign_and_stays_untouched(tmp_path, worktree):
    leftover = _leftover(worktree, b"al")
    result = _call(worktree, tmp_path)
    assert (result["status"], result["reason"]) == ("partial", "not_owned:alpha")
    assert [e["slug"] for e in result["skills"]] == ["beta"]
    assert not os.path.lexists(_dest(worktree))
    assert leftover.read_bytes() == b"al"
    assert _partials(worktree) == [leftover]


def test_leftover_partial_that_is_a_link_is_never_followed(tmp_path, worktree):
    outside = _outside(tmp_path)
    leftover = _leftover(worktree, link_to=outside / "data.txt")
    result = _call(worktree, tmp_path)
    _assert_refused(result, outside)
    assert leftover.is_symlink()
    assert not os.path.lexists(_dest(worktree))
    assert not (worktree / ".claude" / "skills" / "beta").exists()


def test_repo_gitignore_ignores_a_leftover_partial():
    names = [".claude/skills/zzz-probe/.SKILL.md.vnx-partial-" + "0" * 32,
             ".claude/skills/zzz-probe/assets/.data.txt.vnx-partial-" + "0" * 32]
    proc = subprocess.run(["git", "-C", str(REPO), "check-ignore", "--no-index", *names],
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == names
