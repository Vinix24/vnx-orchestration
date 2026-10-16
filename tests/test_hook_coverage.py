"""tests/test_hook_coverage.py — does a project's own settings already run the T0 guard?

One function answers it for the user-level guard and for ``vnx guard check``
(plan t0-guard-gebruikersniveau, § "Coverage and deduplication: one function"). Every case
here is a settings file in a tmp project, parsed by the real function. A registration covers
only in a project Claude Code trusts, so ``_cov`` records that trust in a tmp
``CLAUDE_CONFIG_DIR`` unless a test is about trust.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unicodedata
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


def _command(command: str, **meta) -> dict:
    return {"type": "command", "command": command, "timeout": 10, **meta}


def _on(event: str, command: str, matcher: Optional[str] = None, **meta) -> dict:
    group = {"hooks": [_command(command, **meta)]}
    if matcher is not None:
        group["matcher"] = matcher
    return {event: [group]}


def _trust_config(config_dir: Path, *projects: Path, trusted: bool = True) -> Path:
    """``<config dir>/.claude.json`` recording the trust decision for ``projects``, in the shape
    Claude Code 2.1.301 writes (``projects[<path>].hasTrustDialogAccepted``)."""
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / ".claude.json").write_text(json.dumps({"numStartups": 3, "projects": {
        str(project): {"allowedTools": [], "hasTrustDialogAccepted": trusted}
        for project in projects}}), encoding="utf-8")
    return config_dir


def _cov(project, event="Stop", tool=None, env=None, trusted=True):
    mapping = {"HOME": "/nonexistent"} if env is None else dict(env)
    if trusted and "CLAUDE_CONFIG_DIR" not in mapping:
        config = _trust_config(Path(project).parent / ".claude-config", Path(project))
        mapping["CLAUDE_CONFIG_DIR"] = str(config)
    return hc.coverage(project, event, tool, env=mapping)


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
    """A VNX home with the shim and the guard entry in it."""
    hooks = root / "scripts" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / hc.SHIM_NAME).write_text("#!/bin/sh\n", encoding="utf-8")
    (hooks / GUARD).write_text("", encoding="utf-8")
    return root


def _release_env(home: Path, **extra) -> dict:
    """A project-scope shim call runs the entry from ``VNX_HOME``: point it at ``home``."""
    return {"HOME": "/nonexistent", "VNX_HOME": str(home), **extra}


def test_the_shim_form_covers(tmp_path):
    project = _project(tmp_path)
    home = _shim_home(tmp_path / "release")
    source = _settings(project, _on("Stop", hc.registered_command(GUARD, "project",
                                                                   vnx_home=str(home))))
    result = _cov(project, env=_release_env(home))
    assert (result.state, result.form, result.source) == (hc.COVERED, hc.FORM_SHIM, source)


@pytest.mark.parametrize("prefix", ["sh ", "bash ", "dash ", ""])
def test_the_plain_shim_call_covers(tmp_path, prefix):
    project = _project(tmp_path)
    home = _shim_home(tmp_path / "release")
    shim = home / "scripts" / "hooks" / hc.SHIM_NAME
    shim.chmod(0o755)
    for args in (GUARD, f"--scope project {GUARD}", f'"--scope" \'project\' "{GUARD}"'):
        _settings(project, _on("Stop", f'{prefix}"{shim}" {args}'))
        assert _cov(project, env=_release_env(home)).form == hc.FORM_SHIM, (prefix, args)


def test_a_bare_shim_path_that_cannot_execute_does_not_cover(tmp_path):
    project = _project(tmp_path)
    home = _shim_home(tmp_path / "release")
    shim = home / "scripts" / "hooks" / hc.SHIM_NAME
    shim.chmod(0o644)
    _settings(project, _on("Stop", f"{shim} {GUARD}"))
    assert _cov(project, env=_release_env(home)).state == hc.MISSING


def test_a_user_scope_shim_entry_in_project_settings_does_not_cover(tmp_path):
    """Both calls would run with scope user and each would skip for the other: no guard at all."""
    project = _project(tmp_path)
    home = _shim_home(tmp_path / "release")
    _settings(project, _on("Stop", hc.registered_command(GUARD, "user", vnx_home=str(home))))
    assert not _cov(project, env=_release_env(home)).covered


def test_the_shim_form_for_another_entry_does_not_cover(tmp_path):
    project = _project(tmp_path)
    home = _shim_home(tmp_path / "release")
    _settings(project, _on("Stop", hc.registered_command("pretooluse_t0_no_subagents.py",
                                                         "project", vnx_home=str(home))))
    assert not _cov(project, env=_release_env(home)).covered


# ── a shim registration covers only when its shim can run ───────────────────


def test_a_shim_registration_to_a_missing_shim_does_not_cover(tmp_path):
    """codex advisory and deepseek warning 1 on #2075: the project's own call exits 1 with
    "guard shim missing", so counting it as coverage would leave no guard for the event. It
    reports MISSING, as a direct entry to a missing path does."""
    project = _project(tmp_path)
    home = _shim_home(tmp_path / "release")
    for command in (hc.registered_command(GUARD, "project", vnx_home=str(tmp_path / "gone")),
                    f"sh {tmp_path / 'gone' / 'scripts' / 'hooks' / hc.SHIM_NAME} {GUARD}"):
        _settings(project, _on("Stop", command))
        result = _cov(project, env=_release_env(home))
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
        assert not _cov(project, env=_release_env(home)).covered, label


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


# ── (b) quoting: what the shell does not expand, coverage does not expand ───


def _home_with_current(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    _shim_home(home / ".vnx-system" / "current")
    return home


SHIM_UNDER_HOME = f".vnx-system/current/scripts/hooks/{hc.SHIM_NAME}"


@pytest.mark.parametrize("command", [
    f"sh '$HOME/{SHIM_UNDER_HOME}' {GUARD}",
    f"sh '${{HOME}}/{SHIM_UNDER_HOME}' {GUARD}",
    f'sh "\\$HOME/{SHIM_UNDER_HOME}" {GUARD}',
    f"sh \\$HOME/{SHIM_UNDER_HOME} {GUARD}",
    f"sh '$CLAUDE_PROJECT_DIR'/scripts/hooks/{hc.SHIM_NAME} {GUARD}",
])
def test_a_variable_the_shell_keeps_literal_does_not_cover(tmp_path, command):
    """codex warning 2 on #2075: `shlex.split` dropped the quotes before `$HOME` was expanded,
    so a single-quoted `$HOME` counted as the home directory. The shell keeps it literal, the
    project's call exits 127, and the user-level guard skipped: no guard for the event."""
    project = _project(tmp_path)
    _shim_home(project)
    home = _home_with_current(tmp_path)
    _settings(project, _on("Stop", command))
    assert _cov(project, env={"HOME": str(home)}).state == hc.MISSING


@pytest.mark.parametrize("shell", ["bash", pytest.param("dash", marks=pytest.mark.skipif(
    shutil.which("dash") is None, reason="dash is not installed"))])
def test_the_shell_does_keep_a_single_quoted_home_literal(tmp_path, shell):
    """The measurement behind the case above, on the real shell: the call cannot start (bash
    exits 127 for the script it cannot open, dash 2)."""
    home = _home_with_current(tmp_path)
    proc = subprocess.run([shutil.which(shell), "-c", f"sh '$HOME/{SHIM_UNDER_HOME}' {GUARD}"],
                          env={"HOME": str(home), "PATH": os.environ["PATH"]}, cwd=tmp_path,
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode != 0, proc.stderr
    assert f"$HOME/{SHIM_UNDER_HOME}" in proc.stderr


def test_a_double_quoted_or_bare_home_still_covers(tmp_path):
    project = _project(tmp_path)
    home = _home_with_current(tmp_path)
    for command in (f'sh "$HOME/{SHIM_UNDER_HOME}" {GUARD}',
                    f"sh $HOME/{SHIM_UNDER_HOME} {GUARD}",
                    f'sh "${{HOME}}"/{SHIM_UNDER_HOME} "{GUARD}"'):
        _settings(project, _on("Stop", command))
        assert _cov(project, env={"HOME": str(home)}).form == hc.FORM_SHIM, command


def test_a_bare_home_the_shell_would_split_does_not_cover(tmp_path):
    project = _project(tmp_path)
    home = tmp_path / "a home"
    _shim_home(home / ".vnx-system" / "current")
    _settings(project, _on("Stop", f"sh $HOME/{SHIM_UNDER_HOME} {GUARD}"))
    assert _cov(project, env={"HOME": str(home)}).state == hc.MISSING
    _settings(project, _on("Stop", f'sh "$HOME/{SHIM_UNDER_HOME}" {GUARD}'))
    assert _cov(project, env={"HOME": str(home)}).form == hc.FORM_SHIM


def test_a_plain_shim_call_with_anything_around_it_does_not_cover(tmp_path):
    project = _project(tmp_path)
    home = _shim_home(tmp_path / "release")
    shim = home / "scripts" / "hooks" / hc.SHIM_NAME
    for command in (f"sh {shim} {GUARD} > /dev/null", f"sh {shim} {GUARD} | cat",
                    f"sh {shim} {GUARD} &", f"true; sh {shim} {GUARD}",
                    f'sh {shim} "$ENTRY"', f"sh {shim} '{GUARD}"):
        _settings(project, _on("Stop", command))
        assert _cov(project, env=_release_env(home, ENTRY=GUARD)).state == hc.MISSING, command


# ── (d) a shim registration covers only when the entry it starts exists ─────


def test_a_shim_registration_whose_entry_is_missing_does_not_cover(tmp_path):
    """deepseek info 3 on #2075: the shim exists, the entry it would start does not. The
    project's call exits 1 with "hook entry missing", so it is no guard for the event."""
    project = _project(tmp_path)
    home = _shim_home(tmp_path / "release")
    (home / "scripts" / "hooks" / GUARD).unlink()
    shim = home / "scripts" / "hooks" / hc.SHIM_NAME
    for command in (hc.registered_command(GUARD, "project", vnx_home=str(home)),
                    f"sh {shim} --scope project {GUARD}"):
        _settings(project, _on("Stop", command))
        result = _cov(project, env=_release_env(home))
        assert (result.covered, result.state) == (False, hc.MISSING), command


def test_the_entry_is_the_one_the_shim_would_start(tmp_path):
    """A project-scope call takes the entry from ``${VNX_HOME:-$HOME/.vnx-system/current}``,
    not from beside the shim the registration names."""
    project = _project(tmp_path)
    shim_home = _shim_home(tmp_path / "shim-only")
    (shim_home / "scripts" / "hooks" / GUARD).unlink()
    pinned = _shim_home(tmp_path / "pinned")
    _settings(project, _on("Stop", hc.registered_command(GUARD, "project",
                                                         vnx_home=str(shim_home))))
    assert _cov(project, env=_release_env(pinned)).covered
    assert _cov(project, env={"HOME": str(tmp_path / "home")}).state == hc.MISSING
    _shim_home(tmp_path / "home" / ".vnx-system" / "current")
    assert _cov(project, env={"HOME": str(tmp_path / "home")}).covered


def test_the_registered_fabric_root_entry_counts(tmp_path):
    fabric = _git_project(tmp_path / "fabric", "vnx-dev")
    (fabric / ".claude").mkdir(exist_ok=True)
    _guard_file(fabric)
    home = tmp_path / "home"
    _registry(home, ("vnx-dev", fabric))
    shim_home = _shim_home(tmp_path / "release")
    (shim_home / "scripts" / "hooks" / GUARD).unlink()
    _settings(fabric, _on("Stop", hc.registered_command(GUARD, "project",
                                                        vnx_home=str(shim_home))))
    assert _cov(fabric, env={"HOME": str(home)}).covered
    (home / ".vnx" / "projects.json").unlink()
    assert _cov(fabric, env={"HOME": str(home)}).state == hc.MISSING


# ── (a) a direct entry covers only when the command runs it at project scope ─


@pytest.mark.parametrize("template", [
    "test -f {g}", "[ -f {g} ]", "grep -c hooks {g}", "cat {g}", "echo {g}", "ls -l {g}",
    "true {g}", "python3 -c 'import sys' {g}", "python3 -m py_compile {g}",
    "python3 -W ignore {g}", "python3 --version {g}", "bash {g}",
])
def test_a_command_that_does_not_run_the_guard_does_not_cover(tmp_path, template):
    """codex warning 1 and deepseek info 3 on #2075: an existing guard path anywhere in the
    command counted as coverage. `test -f <guard>` decides nothing at 520K tokens."""
    project = _project(tmp_path)
    guard = _guard_file(tmp_path / "release")
    _settings(project, _on("Stop", template.format(g=guard)))
    assert _cov(project).state == hc.MISSING


@pytest.mark.parametrize("template", [
    "VNX_HOOK_SCOPE=user python3 {g}",
    "env VNX_HOOK_SCOPE=user python3 {g}",
    "export VNX_HOOK_SCOPE=user; python3 {g}",
    "VNX_HOOK_SCOPE=user; export VNX_HOOK_SCOPE; python3 {g}",
    "bash -c 'VNX_HOOK_SCOPE=user exec python3 \"{g}\"'",
    "VNX_HOOK_SCOPE=user bash -c 'exec python3 \"{g}\"'",
    "VNX_HOOK_SCOPE=\"user\" python3 {g}",
    "unset VNX_HOOK_SCOPE; VNX_HOOK_SCOPE=$X python3 {g}",
])
def test_a_direct_entry_at_user_scope_does_not_cover(tmp_path, template):
    """codex and deepseek on #2075, reproduced end to end: the project's own call runs with
    scope user and skips because the project covers, and so does the user-level call."""
    project = _project(tmp_path)
    guard = _guard_file(tmp_path / "release")
    _settings(project, _on("Stop", template.format(g=guard)))
    assert _cov(project).state == hc.MISSING


def test_a_direct_entry_at_project_scope_covers(tmp_path):
    project = _project(tmp_path)
    guard = _guard_file(tmp_path / "release")
    for command in (f"VNX_HOOK_SCOPE=project python3 {guard}",
                    f"export VNX_HOOK_SCOPE=project; exec python3 {guard}"):
        _settings(project, _on("Stop", command))
        assert _cov(project).form == hc.FORM_DIRECT, command


@pytest.mark.parametrize("template", [
    "python3 {g} > /dev/null", "python3 {g} >/dev/null", "python3 {g} 1>/tmp/x",
    "python3 {g} >> /tmp/x", "python3 {g} < /dev/null", "python3 {g} 2>&1",
    "python3 {g} | cat", "cat | python3 {g}", "python3 {g} &", "! python3 {g}",
    "python3 {g} <<EOF", "timeout 5 python3 {g}", "nohup python3 {g}",
])
def test_a_call_whose_decision_cannot_reach_claude_code_does_not_cover(tmp_path, template):
    """The decision is JSON on stdout of a foreground call that reads the payload on stdin.
    A redirect, a pipe, a background job or a negated exit status takes that away."""
    project = _project(tmp_path)
    guard = _guard_file(tmp_path / "release")
    _settings(project, _on("Stop", template.format(g=guard)))
    assert _cov(project).state == hc.MISSING


@pytest.mark.parametrize("template", [
    "python3 {g}", "python3 {g} 2>/dev/null", "exec python3 {g}", "command python3 {g}",
    "/usr/bin/env python3 {g}", "env python3 {g}", "python3 -I -S {g}", "python3 -u -- {g}",
    "python3.12 {g}", "/opt/homebrew/bin/python3 '{g}'", "( python3 {g} )",
    "cd /tmp && python3 {g}",
    "if [ -f {g} ]; then python3 {g}; else echo 'missing' >&2; exit 1; fi",
    "bash -c 'exec python3 \"{g}\"'", "sh -ec 'python3 {g}'",
])
def test_a_call_that_runs_the_guard_covers(tmp_path, template):
    """Among them the forms registered on 2026-10-16: SEOcrawler's `if [ -f ]; then`, the
    template's `python3 <path>`, and the init template's `bash -c 'exec python3 "<path>"'`."""
    project = _project(tmp_path)
    guard = _guard_file(tmp_path / "release")
    _settings(project, _on("Stop", template.format(g=guard)))
    assert _cov(project).form == hc.FORM_DIRECT


def test_an_executable_entry_in_command_position_covers(tmp_path):
    project = _project(tmp_path)
    guard = _guard_file(tmp_path / "release")
    _settings(project, _on("Stop", str(guard)))
    guard.chmod(0o644)
    assert _cov(project).state == hc.MISSING
    guard.chmod(0o755)
    assert _cov(project).form == hc.FORM_DIRECT


def test_a_command_that_sources_or_evals_does_not_cover(tmp_path):
    """Neither can be read without running it, and either can set the scope."""
    project = _project(tmp_path)
    guard = _guard_file(tmp_path / "release")
    for command in (f". ./hooks.env; python3 {guard}", f"source x; python3 {guard}",
                    f"eval \"$X\"; python3 {guard}"):
        _settings(project, _on("Stop", command))
        assert _cov(project).state == hc.MISSING, command


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


def test_a_leading_variable_resolves_only_from_what_the_command_assigns(tmp_path):
    """The old rule read any unset leading variable as the project dir, so
    `python3 "$NOT_SET/scripts/hooks/..."` covered while the shell ran `/scripts/hooks/...`."""
    project = _project(tmp_path)
    _guard_file(project)
    elsewhere = _guard_file(tmp_path / "elsewhere").parents[2]
    rel = "scripts/hooks/t0_context_guard.py"
    for command, expected in (
            (f'python3 "$NOT_SET/{rel}"', hc.MISSING),
            (f"bash -c 'exec python3 \"$ROOT/{rel}\"'", hc.MISSING),
            (f"bash -c 'ROOT=$(mktemp -d); exec python3 \"$ROOT/{rel}\"'", hc.MISSING),
            (f"bash -c 'ROOT={tmp_path / 'gone'}; exec python3 \"$ROOT/{rel}\"'", hc.MISSING),
            (f"bash -c 'ROOT={elsewhere}; exec python3 \"$ROOT/{rel}\"'", hc.COVERED),
            (f"ROOT={elsewhere} bash -c 'exec python3 \"$ROOT/{rel}\"'", hc.COVERED),
            (f'ROOT={elsewhere}; python3 "$ROOT/{rel}"', hc.COVERED),
            (f'ROOT={elsewhere} python3 "$ROOT/{rel}"', hc.MISSING)):
        _settings(project, _on("Stop", command))
        assert _cov(project).state == expected, command


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
    for command in ("python3 '~/.vnx-system/current/scripts/hooks/t0_context_guard.py'",
                    "python3 '$HOME/.vnx-system/current/scripts/hooks/t0_context_guard.py'"):
        _settings(project, _on("Stop", command))
        assert not _cov(project, env={"HOME": str(home)}).covered, command


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


def test_a_relative_path_after_a_cd_does_not_cover(tmp_path):
    project = _project(tmp_path)
    _guard_file(project)
    _settings(project, _on("Stop", "cd /tmp && python3 scripts/hooks/t0_context_guard.py"))
    assert _cov(project).state == hc.MISSING


def test_a_name_that_only_ends_in_the_entry_does_not_cover(tmp_path):
    project = _project(tmp_path)
    root = tmp_path / "release"
    other = root / "scripts" / "hooks" / ("my_" + GUARD)
    other.parent.mkdir(parents=True)
    other.write_text("", encoding="utf-8")
    _settings(project, _on("Stop", f"python3 {other}"))
    assert not _cov(project).covered


@pytest.mark.parametrize("command", ["python3 'unterminated", 'python3 "$(echo', "python3 `x",
                                     "case x in a) python3 g;; esac"])
def test_an_unreadable_command_does_not_cover(tmp_path, command):
    project = _project(tmp_path)
    _guard_file(tmp_path / "release")
    _settings(project, _on("Stop", command))
    assert _cov(project).state == hc.MISSING


def test_a_non_command_hook_does_not_cover(tmp_path):
    project = _project(tmp_path)
    guard = _guard_file(tmp_path / "release")
    _settings(project, {"Stop": [{"hooks": [{"type": "prompt", "prompt": f"python3 {guard}"}]}]})
    assert not _cov(project).covered


# ── (f) a registration covers only when it runs in the foreground every time ─


@pytest.mark.parametrize("meta", [
    {"async": True}, {"asyncRewake": True}, {"once": True}, {"if": "Bash(git *)"},
    {"args": []}, {"shell": "powershell"}, {"async": "true"}, {"async": 1},
])
def test_a_registration_that_cannot_block_every_event_does_not_cover(tmp_path, meta):
    """codex warning 3 on #2075: an `async` hook runs in the background and cannot block, yet
    it silenced the synchronous user guard. `asyncRewake` implies async, `once` removes the
    hook after one run, `if` runs it only when its condition matches, `args` is the exec
    form this reader does not parse, and another `shell` is not bash."""
    project = _project(tmp_path)
    guard = _guard_file(tmp_path / "release")
    home = _shim_home(tmp_path / "shim-release")
    for command in (f"python3 {guard}", hc.registered_command(GUARD, "project",
                                                              vnx_home=str(home))):
        _settings(project, _on("Stop", command, **meta))
        assert _cov(project, env=_release_env(home)).state == hc.MISSING, (meta, command)


def test_an_explicitly_synchronous_registration_covers(tmp_path):
    project = _project(tmp_path)
    guard = _guard_file(tmp_path / "release")
    _settings(project, _on("Stop", f"python3 {guard}", **{"async": False, "asyncRewake": False,
                                                          "once": False, "shell": "bash"}))
    assert _cov(project).covered


def test_a_synchronous_sibling_still_covers(tmp_path):
    project = _project(tmp_path)
    guard = _guard_file(tmp_path / "release")
    _settings(project, {"Stop": [{"hooks": [_command(f"python3 {guard}", **{"async": True}),
                                            _command(f"python3 {guard}")]}]})
    assert _cov(project).covered


# ── (g) a registration covers only in a project Claude Code trusts ───────────


def _covering(tmp_path: Path, name: str = "project") -> Path:
    project = _git_project(tmp_path / name)
    (project / ".claude").mkdir(exist_ok=True)
    _settings(project, _on("Stop", f"python3 {_guard_file(tmp_path / 'release')}"))
    return project


def test_a_registration_in_an_untrusted_project_does_not_cover(tmp_path):
    """deepseek warning on #2075: Claude Code runs a project's hooks only once the operator has
    trusted the folder. Without recorded trust the project's call may never run."""
    project = _covering(tmp_path)
    config = tmp_path / "config"
    env = {"HOME": "/nonexistent", "CLAUDE_CONFIG_DIR": str(config)}
    for trusted in (False, None):
        _trust_config(config, *([project] if trusted is not None else []), trusted=bool(trusted))
        result = hc.coverage(project, "Stop", env=env)
        assert (result.covered, result.state) == (False, hc.MISSING), trusted
    _trust_config(config, project)
    assert hc.coverage(project, "Stop", env=env).covered


@pytest.mark.parametrize("content", [None, "{not json", "[]", '{"projects": []}', "dir"])
def test_trust_that_cannot_be_read_is_unknown(tmp_path, content):
    project = _covering(tmp_path)
    config = tmp_path / "config"
    config.mkdir()
    if content == "dir":
        (config / ".claude.json").mkdir()
    elif content is not None:
        (config / ".claude.json").write_text(content, encoding="utf-8")
    result = hc.coverage(project, "Stop", env={"HOME": "/nonexistent",
                                               "CLAUDE_CONFIG_DIR": str(config)})
    assert (result.covered, result.state) == (False, hc.UNKNOWN)
    assert hc.coverage(project, "Stop", env={}).state == hc.UNKNOWN


def test_trust_is_consulted_only_for_a_covering_registration(tmp_path):
    project = _project(tmp_path)
    _settings(project, _on("Stop", "echo nothing"))
    assert hc.coverage(project, "Stop", env={}).state == hc.MISSING


def test_trust_follows_claude_codes_walk_up_to_the_git_root(tmp_path):
    """Measured on 2.1.301 (`OO`/`Wm`/`jm`): the session folder and its parents count, up to
    and including the git root; a trusted folder above the git root does not."""
    outer = tmp_path / "outer"
    project = _covering(outer, "repo")
    sub = project / "sub"
    (sub / ".claude").mkdir(parents=True)
    shutil.copy(project / ".claude" / "settings.json", sub / ".claude" / "settings.json")
    config = tmp_path / "config"
    env = {"HOME": "/nonexistent", "CLAUDE_CONFIG_DIR": str(config)}
    _trust_config(config, project)
    assert hc.coverage(sub, "Stop", env=env).covered
    _trust_config(config, outer)
    assert hc.coverage(project, "Stop", env=env).state == hc.MISSING


def test_outside_a_repository_trust_walks_to_the_root(tmp_path):
    project = _project(tmp_path / "plain")
    _settings(project, _on("Stop", f"python3 {_guard_file(tmp_path / 'release')}"))
    config = tmp_path / "config"
    _trust_config(config, tmp_path)
    assert hc.coverage(project, "Stop", env={"CLAUDE_CONFIG_DIR": str(config)}).covered


def test_the_trust_key_is_compared_in_nfc(tmp_path):
    """Claude Code normalizes the folder it looks up to NFC (`Nn`)."""
    decomposed = unicodedata.normalize("NFD", "projé")
    project = _covering(tmp_path, decomposed)
    config = tmp_path / "config"
    config.mkdir()
    nfc_key = unicodedata.normalize("NFC", str(project))
    (config / ".claude.json").write_text(json.dumps({"projects": {
        nfc_key: {"hasTrustDialogAccepted": True}}}), encoding="utf-8")
    assert hc.coverage(str(project), "Stop", env={"CLAUDE_CONFIG_DIR": str(config)}).covered


def test_the_trust_file_follows_the_config_dir_home_and_the_legacy_file(tmp_path):
    home = tmp_path / "home"
    config = tmp_path / "config"
    assert hc.trust_file({"HOME": str(home)}) == home / ".claude.json"
    assert hc.trust_file({"HOME": str(home), "CLAUDE_CONFIG_DIR": str(config)}) == (
        config / ".claude.json")
    assert hc.trust_file({"HOME": str(home), "CLAUDE_CODE_CUSTOM_OAUTH_URL": "https://x"}) == (
        home / ".claude-custom-oauth.json")
    assert hc.trust_file({}) is None
    legacy = home / ".claude" / ".config.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("", encoding="utf-8")
    (home / ".claude.json").write_text("{}", encoding="utf-8")
    legacy.chmod(0o644)
    assert hc.trust_file({"HOME": str(home)}) == home / ".claude.json"
    legacy.write_text("{}", encoding="utf-8")
    assert hc.trust_file({"HOME": str(home)}) == legacy
    config.mkdir()
    (config / ".config.json").write_text("{}", encoding="utf-8")
    assert hc.trust_file({"HOME": str(home), "CLAUDE_CONFIG_DIR": str(config)}) == (
        config / ".config.json")


def test_the_trust_file_is_read_per_account(tmp_path):
    project = _covering(tmp_path)
    home = tmp_path / "home"
    _trust_config(home, project)
    other = _trust_config(tmp_path / "salesminds", project, trusted=False)
    assert hc.coverage(project, "Stop", env={"HOME": str(home)}).covered
    assert hc.coverage(project, "Stop", env={"HOME": str(home),
                                             "CLAUDE_CONFIG_DIR": str(other)}).state == hc.MISSING


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
    assert hc.t0_project_root(str(plain / ".vnx-project-id")) is None
    assert hc.t0_project_root("") is None


def test_a_linked_worktree_and_a_submodule_are_their_own_root(tmp_path):
    """A `.git` file marks a root as well as a `.git` directory, as `git rev-parse` reads it."""
    project = _git_project(tmp_path / "project")
    (project / ".git").rename(tmp_path / "gitdir")
    (project / ".git").write_text(f"gitdir: {tmp_path / 'gitdir'}\n", encoding="utf-8")
    assert hc.t0_project_root(str(project / ".claude")) == Path(os.path.realpath(project))


def test_rule_1_starts_no_git(tmp_path, monkeypatch):
    """(h): the hook timeout is 10 s and `git rev-parse` had a 10 s timeout of its own. Rule 1
    now walks to the nearest `.git` and starts no subprocess at all."""
    project = _git_project(tmp_path / "project")

    def refuse(*args, **kwargs):
        raise AssertionError(f"rule 1 started a subprocess: {args}")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    assert hc.t0_project_root(str(project)) == Path(os.path.realpath(project))


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
    assert hc.shim_entry_file(fabric, GUARD, env, user_shim=tmp_path / "x" / hc.SHIM_NAME) == own


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


def test_a_registry_that_is_not_a_regular_file_is_never_opened(tmp_path):
    """(h): a FIFO would block the guard's open() until the hook timeout. Run in a child
    process, so a reader that does open it fails this test instead of hanging the suite."""
    fabric = _git_project(tmp_path / "fabric", "vnx-dev")
    home = tmp_path / "home"
    (home / ".vnx").mkdir(parents=True)
    os.mkfifo(home / ".vnx" / "projects.json")
    probe = ("import sys; sys.path.insert(0, sys.argv[1]); import hook_coverage as hc; "
             "env = {'HOME': sys.argv[2]}; "
             "print(hc.registered_fabric_root(env), hc.is_fabric_root(sys.argv[3], env))")
    proc = subprocess.run([sys.executable, "-c", probe, str(REPO_ROOT / "scripts" / "lib"),
                           str(home), str(fabric)], capture_output=True, text=True, timeout=10)
    assert (proc.returncode, proc.stdout.split()) == (0, ["None", "False"]), proc.stderr


def test_a_project_scope_entry_follows_vnx_home(tmp_path):
    consumer = _git_project(tmp_path / "consumer")
    env = {"HOME": str(tmp_path / "home"), "VNX_HOME": str(tmp_path / "pinned")}
    assert hc.shim_entry_file(consumer, GUARD, env) == tmp_path / "pinned" / "scripts" / "hooks" / GUARD
    env["VNX_HOME"] = ""
    assert hc.shim_entry_file(consumer, GUARD, env) == (
        tmp_path / "home" / ".vnx-system" / "current" / "scripts" / "hooks" / GUARD)


def test_a_user_scope_entry_sits_beside_its_shim_whatever_vnx_home_says(tmp_path):
    """(c): the user-level registration names the shim; an inherited VNX_HOME does not choose
    the code it starts."""
    consumer = _git_project(tmp_path / "consumer")
    shim = tmp_path / "release" / "scripts" / "hooks" / hc.SHIM_NAME
    env = {"HOME": str(tmp_path / "home"), "VNX_HOME": str(tmp_path / "pinned")}
    assert hc.shim_entry_file(consumer, GUARD, env, user_shim=shim) == shim.parent / GUARD
    assert hc.shim_entry_file(consumer, GUARD, env, user_shim=Path(hc.SHIM_NAME)) == Path(GUARD)


def test_the_registered_fabric_root_without_the_entry_runs_current(tmp_path):
    fabric = _git_project(tmp_path / "fabric", "vnx-dev")
    home = tmp_path / "home"
    _registry(home, ("vnx-dev", fabric))
    assert hc.shim_entry_file(fabric, GUARD, {"HOME": str(home)}) == (
        home / ".vnx-system" / "current" / "scripts" / "hooks" / GUARD)
