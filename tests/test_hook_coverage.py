"""tests/test_hook_coverage.py — does a project's own settings already run the T0 guard?

One function answers it for the user-level guard and for ``vnx guard check``
(plan t0-guard-gebruikersniveau, § "Coverage and deduplication: one function"). Every case
here is a settings file in a tmp project, parsed by the real function.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import hook_coverage as hc

GUARD = hc.GUARD_ENTRY


@pytest.fixture(autouse=True)
def _no_ambient_git(monkeypatch):
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_CEILING_DIRECTORIES"):
        monkeypatch.delenv(var, raising=False)


def _project(tmp_path: Path, name: str = "project") -> Path:
    project = tmp_path / name
    (project / ".claude").mkdir(parents=True, exist_ok=True)
    return project


def _guard_file(root: Path) -> Path:
    target = root / "scripts" / "hooks" / GUARD
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("", encoding="utf-8")
    return target


def _settings(project: Path, hooks, name: str = "settings.json") -> Path:
    path = project / ".claude" / name
    path.write_text(json.dumps({"permissions": {"allow": []}, "hooks": hooks}), encoding="utf-8")
    return path


def _command(command: str) -> dict:
    return {"type": "command", "command": command, "timeout": 10}


def _on(event: str, command: str, matcher: Optional[str] = None) -> dict:
    group = {"hooks": [_command(command)]}
    if matcher is not None:
        group["matcher"] = matcher
    return {event: [group]}


def _cov(project, event="Stop", tool=None, env=None):
    return hc.coverage(project, event, tool, env=env if env is not None else {"HOME": "/nonexistent"})


# ── the two forms ────────────────────────────────────────────────────────────


def test_a_direct_entry_to_an_existing_path_covers(tmp_path):
    project = _project(tmp_path)
    guard = _guard_file(tmp_path / "release")
    source = _settings(project, _on("Stop", f"python3 {guard}"))
    result = _cov(project)
    assert result.covered
    assert (result.state, result.form, result.source) == (hc.COVERED, hc.FORM_DIRECT, source)


def test_a_direct_entry_to_a_missing_path_does_not_cover(tmp_path):
    project = _project(tmp_path)
    _settings(project, _on("Stop", f"python3 {tmp_path / 'gone' / GUARD}"))
    result = _cov(project)
    assert not result.covered
    assert result.state == hc.MISSING


def _shim_home(root: Path) -> Path:
    """A VNX home with the shim in it."""
    shim = root / "scripts" / "hooks" / hc.SHIM_NAME
    shim.parent.mkdir(parents=True, exist_ok=True)
    shim.write_text("#!/bin/sh\n", encoding="utf-8")
    return root


def test_the_shim_form_covers(tmp_path):
    project = _project(tmp_path)
    home = _shim_home(tmp_path / "release")
    source = _settings(project, _on("Stop", hc.registered_command(GUARD, "project",
                                                                   vnx_home=str(home))))
    result = _cov(project)
    assert (result.state, result.form, result.source) == (hc.COVERED, hc.FORM_SHIM, source)


@pytest.mark.parametrize("prefix", ["sh ", "bash ", "dash ", ""])
def test_the_plain_shim_call_covers(tmp_path, prefix):
    project = _project(tmp_path)
    shim = _shim_home(tmp_path / "release") / "scripts" / "hooks" / hc.SHIM_NAME
    shim.chmod(0o755)
    for args in (GUARD, f"--scope project {GUARD}"):
        _settings(project, _on("Stop", f'{prefix}"{shim}" {args}'))
        assert _cov(project).form == hc.FORM_SHIM, (prefix, args)


def test_a_bare_shim_path_that_cannot_execute_does_not_cover(tmp_path):
    project = _project(tmp_path)
    shim = _shim_home(tmp_path / "release") / "scripts" / "hooks" / hc.SHIM_NAME
    shim.chmod(0o644)
    _settings(project, _on("Stop", f"{shim} {GUARD}"))
    assert _cov(project).state == hc.MISSING


def test_a_user_scope_shim_entry_in_project_settings_does_not_cover(tmp_path):
    """Both calls would run with scope user and each would skip for the other: no guard at all."""
    project = _project(tmp_path)
    home = _shim_home(tmp_path / "release")
    _settings(project, _on("Stop", hc.registered_command(GUARD, "user", vnx_home=str(home))))
    assert not _cov(project).covered


def test_the_shim_form_for_another_entry_does_not_cover(tmp_path):
    project = _project(tmp_path)
    home = _shim_home(tmp_path / "release")
    _settings(project, _on("Stop", hc.registered_command("pretooluse_t0_no_subagents.py",
                                                         "project", vnx_home=str(home))))
    assert not _cov(project).covered


# ── a shim registration covers only when its shim can run ───────────────────


def test_a_shim_registration_to_a_missing_shim_does_not_cover(tmp_path):
    """codex advisory and deepseek warning 1 on #2075: the project's own call exits 1 with
    "guard shim missing", so counting it as coverage would leave no guard for the event. It
    reports MISSING, as a direct entry to a missing path does."""
    project = _project(tmp_path)
    for command in (hc.registered_command(GUARD, "project", vnx_home=str(tmp_path / "gone")),
                    f"sh {tmp_path / 'gone' / 'scripts' / 'hooks' / hc.SHIM_NAME} {GUARD}"):
        _settings(project, _on("Stop", command))
        result = _cov(project)
        assert (result.covered, result.state) == (False, hc.MISSING), command


def _shim_variants(home: Path):
    """Shim calls the shim itself refuses with exit 1, in the plain and in the inline form."""
    shim = home / "scripts" / "hooks" / hc.SHIM_NAME
    inline = hc.registered_command(GUARD, "project", vnx_home=str(home))
    yield "unknown scope", f"sh {shim} --scope global {GUARD}"
    yield "unknown scope, inline", inline.replace("--scope project", "--scope global")
    yield "--scope=", f"sh {shim} --scope=project {GUARD}"
    yield "scope after the entry", f"sh {shim} {GUARD} --scope project"
    yield "two entries", f"sh {shim} --scope project {GUARD} {GUARD}"
    yield "two entries, inline", inline.replace(f" {GUARD};", f" {GUARD} {GUARD};")
    yield "scope without a value", f"sh {shim} --scope"


def test_a_shim_call_the_shim_refuses_does_not_cover(tmp_path):
    """deepseek warning 1: an unknown --scope, or an entry the shim would not accept, makes the
    project's call exit 1. Such a registration is no guard."""
    project = _project(tmp_path)
    home = _shim_home(tmp_path / "release")
    for label, command in _shim_variants(home):
        _settings(project, _on("Stop", command))
        assert not _cov(project).covered, label


def test_the_shim_path_expands_home_and_the_project_dir(tmp_path):
    project = _project(tmp_path)
    _shim_home(project)
    home = _shim_home(tmp_path / "home" / ".vnx-system" / "current").parents[1]
    env = {"HOME": str(home)}
    for vnx_home in ("$HOME/.vnx-system/current", "${HOME}/.vnx-system/current",
                     "$CLAUDE_PROJECT_DIR", "${CLAUDE_PROJECT_DIR}"):
        _settings(project, _on("Stop", hc.registered_command(GUARD, "project", vnx_home=vnx_home)))
        assert _cov(project, env=env).form == hc.FORM_SHIM, vnx_home
    _settings(project, _on("Stop", hc.registered_command(GUARD, "project",
                                                         vnx_home="$HOME/.vnx-system/current")))
    assert not _cov(project, env={"HOME": str(tmp_path / "elsewhere")}).covered
    assert not _cov(project, env={}).covered


def test_a_shim_path_the_guard_cannot_resolve_does_not_cover(tmp_path):
    """Only $HOME and $CLAUDE_PROJECT_DIR are expanded, and never through a shell. An unrendered
    template placeholder, any other variable, a tilde and a relative path stay unresolved."""
    project = _project(tmp_path)
    release = _shim_home(tmp_path / "release")
    _shim_home(project)
    env = {"HOME": str(tmp_path), "VNX_HOME": str(release)}
    rel = f"scripts/hooks/{hc.SHIM_NAME}"
    for command in (hc.registered_command(GUARD, "project", vnx_home="{{VNX_HOME}}"),
                    f'sh "$VNX_HOME/{rel}" {GUARD}', f"sh ~/release/{rel} {GUARD}",
                    f"sh {rel} {GUARD}", f'sh "$(echo {release})/{rel}" {GUARD}'):
        _settings(project, _on("Stop", command))
        assert _cov(project, env=env).state == hc.MISSING, command


def test_the_fabric_repo_form_resolves_root_against_the_project(tmp_path):
    """vnx-orchestration registers `$ROOT/scripts/hooks/t0_context_guard.py` with ROOT from
    git; until G3 the user-level call skips vnx-dev per event because of it."""
    project = _project(tmp_path)
    command = ("bash -c 'ROOT=$(git rev-parse --show-toplevel 2>/dev/null || echo .); "
               "exec python3 \"$ROOT/scripts/hooks/t0_context_guard.py\"'")
    _settings(project, _on("Stop", command))
    assert not _cov(project).covered
    _guard_file(project)
    assert _cov(project).form == hc.FORM_DIRECT


def test_claude_project_dir_and_home_expand(tmp_path):
    project = _project(tmp_path)
    _guard_file(project)
    home = tmp_path / "home"
    _guard_file(home / ".vnx-system" / "current")
    _settings(project, _on("Stop", 'python3 "$CLAUDE_PROJECT_DIR/scripts/hooks/t0_context_guard.py"'))
    assert _cov(project).covered
    for command in ("python3 ~/.vnx-system/current/scripts/hooks/t0_context_guard.py",
                    'python3 "${HOME}/.vnx-system/current/scripts/hooks/t0_context_guard.py"'):
        _settings(project, _on("Stop", command))
        assert _cov(project, env={"HOME": str(home)}).covered, command
        assert not _cov(project, env={"HOME": str(tmp_path / "elsewhere")}).covered, command


def test_an_unresolvable_variable_inside_the_path_does_not_cover(tmp_path):
    project = _project(tmp_path)
    _guard_file(project)
    _settings(project, _on("Stop", 'python3 "/x/$UNSET_THING/scripts/hooks/t0_context_guard.py"'))
    assert not _cov(project).covered


def test_a_relative_path_is_read_from_the_project(tmp_path):
    project = _project(tmp_path)
    _settings(project, _on("Stop", "python3 scripts/hooks/t0_context_guard.py"))
    assert not _cov(project).covered
    _guard_file(project)
    assert _cov(project).covered


def test_a_name_that_only_ends_in_the_entry_does_not_cover(tmp_path):
    project = _project(tmp_path)
    root = tmp_path / "release"
    other = root / "scripts" / "hooks" / ("my_" + GUARD)
    other.parent.mkdir(parents=True)
    other.write_text("", encoding="utf-8")
    _settings(project, _on("Stop", f"python3 {other}"))
    assert not _cov(project).covered


def test_a_non_command_hook_does_not_cover(tmp_path):
    project = _project(tmp_path)
    guard = _guard_file(tmp_path / "release")
    _settings(project, {"Stop": [{"hooks": [{"type": "prompt", "prompt": f"python3 {guard}"}]}]})
    assert not _cov(project).covered


# ── per event and per tool ───────────────────────────────────────────────────


def test_coverage_is_per_event(tmp_path):
    project = _project(tmp_path)
    guard = _guard_file(tmp_path / "release")
    _settings(project, _on("Stop", f"python3 {guard}"))
    assert _cov(project, "Stop").covered
    assert _cov(project, "UserPromptSubmit").state == hc.MISSING


def test_a_bash_only_matcher_covers_bash_and_not_skill(tmp_path):
    project = _project(tmp_path)
    guard = _guard_file(tmp_path / "release")
    _settings(project, _on("PreToolUse", f"python3 {guard}", matcher="Bash"))
    assert _cov(project, "PreToolUse", "Bash").covered
    assert not _cov(project, "PreToolUse", "Skill").covered
    assert not _cov(project, "PreToolUse", "BashOutput").covered


@pytest.mark.parametrize("matcher,tool,expected", [
    (None, "Skill", True), ("", "Skill", True), ("*", "SlashCommand", True),
    ("Bash|Skill|SlashCommand", "SlashCommand", True), ("Bash|Skill", "SlashCommand", False),
    ("Sla.*", "SlashCommand", True), ("^Bash$", "Skill", False), ("mcp__.*", "Bash", False),
    ("(", "Bash", False),
])
def test_the_pretooluse_matcher_is_evaluated(tmp_path, matcher, tool, expected):
    project = _project(tmp_path)
    guard = _guard_file(tmp_path / "release")
    _settings(project, _on("PreToolUse", f"python3 {guard}", matcher=matcher))
    assert _cov(project, "PreToolUse", tool).covered is expected


def test_the_matcher_is_ignored_for_events_without_tools(tmp_path):
    project = _project(tmp_path)
    guard = _guard_file(tmp_path / "release")
    _settings(project, _on("Stop", f"python3 {guard}", matcher="Bash"))
    assert _cov(project, "Stop").covered


def test_matcher_matches_directly():
    assert hc.matcher_matches(None, "Bash")
    assert hc.matcher_matches("Bash|Skill", "Skill")
    assert not hc.matcher_matches("Bash", "Skill")
    assert not hc.matcher_matches(["Bash"], "Bash")


# ── which settings count ─────────────────────────────────────────────────────


def test_settings_local_counts(tmp_path):
    project = _project(tmp_path)
    guard = _guard_file(tmp_path / "release")
    source = _settings(project, _on("Stop", f"python3 {guard}"), name="settings.local.json")
    assert _cov(project).source == source


def test_the_settings_are_read_from_the_project_dir_not_the_toplevel(tmp_path):
    """The guard passes CLAUDE_PROJECT_DIR. A session started in a subdirectory loaded no
    project settings, even when the git toplevel has them."""
    project = _project(tmp_path)
    subprocess.run(["git", "init", "-q", str(project)], check=True, capture_output=True)
    guard = _guard_file(tmp_path / "release")
    _settings(project, _on("Stop", f"python3 {guard}"))
    sub = project / "docs"
    sub.mkdir()
    assert _cov(project).covered
    assert _cov(sub).state == hc.MISSING


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", '{"hooks": []}',
                                     '{"hooks": {"Stop": {"hooks": []}}}'])
def test_invalid_settings_do_not_cover(tmp_path, content):
    project = _project(tmp_path)
    guard = _guard_file(tmp_path / "release")
    (project / ".claude" / "settings.json").write_text(content, encoding="utf-8")
    result = _cov(project)
    assert not result.covered
    assert result.state == hc.UNKNOWN
    # a valid sibling that covers still covers: Claude Code loads that one
    _settings(project, _on("Stop", f"python3 {guard}"), name="settings.local.json")
    assert _cov(project).covered


def test_an_unreadable_settings_file_does_not_cover(tmp_path):
    project = _project(tmp_path)
    (project / ".claude" / "settings.json").mkdir()
    assert _cov(project).state == hc.UNKNOWN


@pytest.mark.parametrize("hooks", [
    {"Stop": ["not a group"]},
    {"Stop": [{"hooks": "not a list"}]},
    {"Stop": [{"hooks": ["not a hook"]}]},
    {"Stop": [{"matcher": 7, "hooks": []}]},
    {"Stop": [{"hooks": [{"type": "command", "command": 7}]}]},
    {"PreToolUse": "not a list"},
])
def test_a_malformed_hooks_tree_makes_the_whole_file_not_cover(tmp_path, hooks):
    """Unsure what Claude Code loads from such a file, so the user-level guard runs: a double
    run is the smaller fault than no run."""
    project = _project(tmp_path)
    guard = _guard_file(tmp_path / "release")
    _settings(project, {**hooks, "UserPromptSubmit": [{"hooks": [_command(f"python3 {guard}")]}],
                        **({"Stop": hooks["Stop"] + [{"hooks": [_command(f"python3 {guard}")]}]}
                           if "Stop" in hooks else {})})
    for event in ("Stop", "UserPromptSubmit"):
        result = _cov(project, event)
        assert (result.covered, result.state) == (False, hc.UNKNOWN), event


@pytest.mark.parametrize("project_dir", [None, ""])
def test_no_project_dir_is_unknown(project_dir):
    result = hc.coverage(project_dir, "Stop")
    assert (result.covered, result.state) == (False, hc.UNKNOWN)


def test_no_settings_at_all_is_missing(tmp_path):
    assert _cov(tmp_path / "bare").state == hc.MISSING


# ── who the registration is for: rule 1 ─────────────────────────────────────


def _git_project(path: Path, project_id: Optional[str] = "consumer", t0: bool = True) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    if project_id is not None:
        (path / ".vnx-project-id").write_text(project_id + "\n", encoding="utf-8")
    if t0:
        (path / ".claude" / "terminals" / "T0").mkdir(parents=True)
        (path / ".claude" / "terminals" / "T0" / "CLAUDE.md").write_text("", encoding="utf-8")
    return path


def test_t0_project_root_from_the_root_and_the_t0_folder(tmp_path):
    project = _git_project(tmp_path / "project")
    expected = Path(os.path.realpath(project))
    assert hc.t0_project_root(str(project)) == expected
    assert hc.t0_project_root(str(project / ".claude" / "terminals" / "T0")) == expected


def test_t0_project_root_refuses_what_rule_1_refuses(tmp_path):
    assert hc.t0_project_root(str(_git_project(tmp_path / "clients", "vincent-clients",
                                               t0=False))) is None
    assert hc.t0_project_root(str(_git_project(tmp_path / "no-id", None))) is None
    empty = _git_project(tmp_path / "empty", None)
    (empty / ".vnx-project-id").write_text("\nvnx-dev\n", encoding="utf-8")
    assert hc.t0_project_root(str(empty)) is None
    worktree = _git_project(tmp_path / "wt") / ".vnx-data" / "worktrees" / "dispatch-x"
    worktree.mkdir(parents=True)
    assert hc.t0_project_root(str(worktree)) is None
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / ".vnx-project-id").write_text("x\n", encoding="utf-8")
    assert hc.t0_project_root(str(plain)) is None
    assert hc.t0_project_root(str(tmp_path / "does-not-exist")) is None
    assert hc.t0_project_root("") is None


def test_project_id_is_the_first_line(tmp_path):
    (tmp_path / ".vnx-project-id").write_text(" vnx-dev \ndev-T0\n\n", encoding="utf-8")
    assert hc.project_id(tmp_path) == "vnx-dev"
    (tmp_path / ".vnx-project-id").write_text("", encoding="utf-8")
    assert hc.project_id(tmp_path) is None
    assert hc.project_id(tmp_path / "absent") is None


# ── which code the shim runs: step 2 ────────────────────────────────────────


def _registry(home: Path, *projects) -> Path:
    """``~/.vnx/projects.json`` in the shape measured on 2026-10-16."""
    registry = home / ".vnx" / "projects.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps({"projects": [
        {"name": Path(str(path)).name, "path": str(path), "layout": "unknown", "project_id": pid}
        for pid, path in projects]}, indent=2), encoding="utf-8")
    return registry


def test_the_registered_fabric_root_runs_its_own_code(tmp_path):
    fabric = _git_project(tmp_path / "fabric", "vnx-dev")
    own = _guard_file(fabric)
    home = tmp_path / "home"
    _registry(home, ("seocrawler-v2", tmp_path / "other"), ("vnx-dev", fabric))
    env = {"HOME": str(home)}
    assert hc.is_fabric_root(fabric, env)
    assert hc.shim_entry_file(fabric, GUARD, env) == own


def test_the_fabric_root_compares_real_paths(tmp_path):
    fabric = _git_project(tmp_path / "fabric", "vnx-dev")
    link = tmp_path / "link-to-fabric"
    link.symlink_to(fabric, target_is_directory=True)
    home = tmp_path / "home"
    _registry(home, ("vnx-dev", link))
    assert hc.is_fabric_root(fabric, {"HOME": str(home)})
    _registry(home, ("vnx-dev", fabric))
    assert hc.is_fabric_root(link, {"HOME": str(home)})


def _not_the_fabric_root(tmp_path: Path):
    """deepseek warning 2 on #2075: the repo writes `.vnx-project-id` itself, so the id never
    selects repo code. Only the user-owned registry does."""
    spoof = _git_project(tmp_path / "spoof", "vnx-dev")
    real = _git_project(tmp_path / "real-fabric", "vnx-dev")
    home = tmp_path / "home"
    registry = home / ".vnx" / "projects.json"
    yield "no registry", spoof, lambda: None
    yield "registry names another root", spoof, lambda: _registry(home, ("vnx-dev", real))
    yield "root registered under another id", spoof, lambda: _registry(home, ("other", spoof))
    yield "only a later vnx-dev entry names the root", spoof, lambda: _registry(
        home, ("vnx-dev", real), ("vnx-dev", spoof))
    yield "relative registry path", spoof, lambda: _registry(home, ("vnx-dev", "spoof"))
    yield "invalid JSON", spoof, lambda: registry.write_text('{"projects": [', encoding="utf-8")
    yield "not a dict", spoof, lambda: registry.write_text("[]", encoding="utf-8")
    yield "projects not a list", spoof, lambda: registry.write_text(
        json.dumps({"projects": {"project_id": "vnx-dev", "path": str(spoof)}}), encoding="utf-8")
    yield "path not a string", spoof, lambda: registry.write_text(
        json.dumps({"projects": [{"project_id": "vnx-dev", "path": [str(spoof)]}]}),
        encoding="utf-8")
    other_id = _git_project(tmp_path / "other-id", "consumer")
    yield "registered path whose id is not vnx-dev", other_id, lambda: _registry(
        home, ("vnx-dev", other_id))


def test_a_repo_that_names_itself_vnx_dev_is_not_the_fabric_root(tmp_path):
    home = tmp_path / "home"
    env = {"HOME": str(home)}
    for label, root, write_registry in _not_the_fabric_root(tmp_path):
        own = _guard_file(root)
        if (home / ".vnx").exists():
            (home / ".vnx" / "projects.json").unlink()
        write_registry()
        assert not hc.is_fabric_root(root, env), label
        assert hc.shim_entry_file(root, GUARD, env) == (
            home / ".vnx-system" / "current" / "scripts" / "hooks" / GUARD), label
        assert hc.shim_entry_file(root, GUARD, env) != own, label


def test_an_unreadable_registry_or_no_home_is_not_the_fabric_root(tmp_path):
    fabric = _git_project(tmp_path / "fabric", "vnx-dev")
    home = tmp_path / "home"
    (home / ".vnx" / "projects.json").mkdir(parents=True)
    assert not hc.is_fabric_root(fabric, {"HOME": str(home)})
    assert not hc.is_fabric_root(fabric, {})
    assert not hc.is_fabric_root(fabric, {"HOME": ""})


def test_the_shim_entry_file_follows_vnx_home(tmp_path):
    consumer = _git_project(tmp_path / "consumer")
    env = {"HOME": str(tmp_path / "home"), "VNX_HOME": str(tmp_path / "pinned")}
    assert hc.shim_entry_file(consumer, GUARD, env) == tmp_path / "pinned" / "scripts" / "hooks" / GUARD
    env["VNX_HOME"] = ""
    assert hc.shim_entry_file(consumer, GUARD, env) == (
        tmp_path / "home" / ".vnx-system" / "current" / "scripts" / "hooks" / GUARD)


def test_the_registered_fabric_root_without_the_entry_runs_current(tmp_path):
    fabric = _git_project(tmp_path / "fabric", "vnx-dev")
    home = tmp_path / "home"
    _registry(home, ("vnx-dev", fabric))
    assert hc.shim_entry_file(fabric, GUARD, {"HOME": str(home)}) == (
        home / ".vnx-system" / "current" / "scripts" / "hooks" / GUARD)
