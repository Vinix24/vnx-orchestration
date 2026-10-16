"""tests/test_vnx_hook_shim.py — the hook shim and the registered commands never exit 2 (OI-2071).

Claude Code treats exit 2 from a hook as blocking. ``python3 /missing.py`` exits 2, and so does a
missing script started through dash, which is ``/bin/sh`` on Debian and Ubuntu (CI runs there).
A user-level registration that exits 2 when a file is missing blocks every session on the
machine (plan t0-guard-gebruikersniveau, problem 3).

Every test runs the real shim, or the real registered command from ``hook_coverage``, under
``bash`` and under ``dash``. A tmp ``bin`` directory comes first on PATH: ``sh`` there is the
shell under test, so the registered command's inner ``sh`` is that shell too, and ``python3``
is the interpreter running this test. dash is skipped only when it is not installed.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIM = REPO_ROOT / "scripts" / "hooks" / "vnx_hook_shim.sh"
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import hook_coverage

SHELLS = [
    pytest.param("bash", id="bash"),
    pytest.param("dash", id="dash", marks=pytest.mark.skipif(
        shutil.which("dash") is None,
        reason="dash is not installed on this machine; CI on Ubuntu has it as /bin/sh")),
]

PROBE_SOURCE = """\
import json, os, sys
payload = sys.stdin.read()
print(json.dumps({"ran": os.path.realpath(__file__), "scope": os.environ.get("VNX_HOOK_SCOPE"),
                  "stdin": payload}))
sys.exit(int(os.environ.get("PROBE_EXIT", "0")))
"""
PROBE = "probe_entry.py"
NO_SUBAGENTS = "pretooluse_t0_no_subagents.py"


def _git_env(home: Path) -> Dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home),
            "GIT_CONFIG_NOSYSTEM": "1"}


def _vnx_project(path: Path, project_id: Optional[str] = "consumer", *, t0: bool = True,
                 git: bool = True) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if git and not (path / ".git").exists():
        subprocess.run(["git", "init", "-q", str(path)], env=_git_env(path.parent), check=True,
                       capture_output=True)
    if project_id is not None:
        (path / ".vnx-project-id").write_text(project_id + "\ndev-T0\n\n", encoding="utf-8")
    if t0:
        t0_dir = path / ".claude" / "terminals" / "T0"
        t0_dir.mkdir(parents=True, exist_ok=True)
        (t0_dir / "CLAUDE.md").write_text("# T0\n", encoding="utf-8")
    return path


def _write_probe(hooks_dir: Path, name: str = PROBE) -> Path:
    hooks_dir.mkdir(parents=True, exist_ok=True)
    target = hooks_dir / name
    target.write_text(PROBE_SOURCE, encoding="utf-8")
    return target


@pytest.fixture
def world(tmp_path):
    """A home with ``~/.vnx-system/current`` pointing at a release dir, a consumer project
    with a T0 folder, and a bin dir that the per-shell runner fills."""
    home = tmp_path / "home"
    release = tmp_path / "release"
    hooks = release / "scripts" / "hooks"
    hooks.mkdir(parents=True)
    shutil.copy2(SHIM, hooks / SHIM.name)
    _write_probe(hooks)
    _write_probe(hooks, hook_coverage.GUARD_ENTRY)
    _write_probe(hooks, NO_SUBAGENTS)
    (home / ".vnx-system").mkdir(parents=True)
    (home / ".vnx-system" / "current").symlink_to(release, target_is_directory=True)
    project = _vnx_project(tmp_path / "project")
    return {"tmp": tmp_path, "home": home, "release": release, "hooks": hooks,
            "project": project}


def _bin_dir(tmp: Path, shell: str, *, fake_sh: bool = False, fake_python: bool = False) -> Path:
    real_shell = shutil.which(shell)
    assert real_shell, shell
    bin_dir = tmp / f"bin-{shell}-{int(fake_sh)}{int(fake_python)}"
    if bin_dir.exists():
        return bin_dir
    bin_dir.mkdir()
    if fake_sh:
        # The script exists when the registered command tests it and is gone when `sh` starts it.
        (bin_dir / "sh").write_text(
            f'#!{real_shell}\nif [ "$1" != "-c" ] && [ -n "$1" ]; then rm -f "$1"; fi\n'
            f'exec {real_shell} "$@"\n', encoding="utf-8")
        (bin_dir / "sh").chmod(0o755)
    else:
        (bin_dir / "sh").symlink_to(real_shell)
    if fake_python:
        # The entry exists when the shim tests it; python3 then finds it gone and exits 2.
        (bin_dir / "python3").write_text(
            f'#!{real_shell}\nrm -f "$1"\necho "python3: can\'t open file \'$1\'" >&2\nexit 2\n',
            encoding="utf-8")
        (bin_dir / "python3").chmod(0o755)
    else:
        (bin_dir / "python3").symlink_to(sys.executable)
    return bin_dir


def _env(world, shell: str, **extra) -> Dict[str, str]:
    flags = {k: extra.pop(k) for k in ("fake_sh", "fake_python") if k in extra}
    bin_dir = _bin_dir(world["tmp"], shell, **flags)
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
           "HOME": str(world["home"]), "GIT_CONFIG_NOSYSTEM": "1"}
    env.update({k: str(v) for k, v in extra.items()})
    return env


def _shim(world, shell: str, *args: str, cwd: Optional[Path] = None, stdin: str = "{}",
          **env_extra) -> subprocess.CompletedProcess:
    return subprocess.run([shutil.which(shell), str(SHIM), *args], input=stdin,
                          env=_env(world, shell, **env_extra), cwd=cwd or world["project"],
                          capture_output=True, text=True, timeout=60)


def _registered(world, shell: str, command: str, *, cwd: Optional[Path] = None,
                stdin: str = "{}", **env_extra) -> subprocess.CompletedProcess:
    return subprocess.run([shutil.which(shell), "-c", command], input=stdin,
                          env=_env(world, shell, **env_extra), cwd=cwd or world["project"],
                          capture_output=True, text=True, timeout=60)


def _ran(proc: subprocess.CompletedProcess) -> dict:
    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    return json.loads(proc.stdout)


# ── the hazard the shim exists for (problem 3) ──────────────────────────────


DIRECT_FORMS = (
    'python3 "{missing}"',
    "bash -c 'exec python3 \"{missing}\"'",
    "bash -c 'ROOT=$(git rev-parse --show-toplevel 2>/dev/null || echo .); "
    "exec python3 \"$ROOT/scripts/hooks/t0_context_guard.py\"'",
)


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("form", DIRECT_FORMS)
def test_the_direct_registration_exits_2_when_its_target_is_missing(world, shell, form):
    """The registrations of 2026-10-16 (template and vnx-orchestration) block on a missing file.
    This pins the measurement the shim answers; it is true of python3, not of VNX code."""
    command = form.format(missing=world["tmp"] / "gone" / "t0_context_guard.py")
    assert _registered(world, shell, command).returncode == 2


# ── the shim ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("shell", SHELLS)
def test_a_consumer_runs_the_entry_from_current(world, shell):
    out = _ran(_shim(world, shell, "--scope", "user", PROBE, stdin='{"hook_event_name": "Stop"}'))
    assert out["ran"] == os.path.realpath(world["hooks"] / PROBE)
    assert out["scope"] == "user"
    assert json.loads(out["stdin"]) == {"hook_event_name": "Stop"}


@pytest.mark.parametrize("shell", SHELLS)
def test_the_default_scope_is_project(world, shell):
    assert _ran(_shim(world, shell, PROBE))["scope"] == "project"
    assert _ran(_shim(world, shell, "--scope", "project", PROBE))["scope"] == "project"


@pytest.mark.parametrize("shell", SHELLS)
def test_the_t0_folder_resolves_to_the_project(world, shell):
    t0_dir = world["project"] / ".claude" / "terminals" / "T0"
    assert _ran(_shim(world, shell, PROBE, cwd=t0_dir))["ran"] == os.path.realpath(
        world["hooks"] / PROBE)


@pytest.mark.parametrize("shell", SHELLS)
def test_the_fabric_repo_runs_its_own_code(world, shell):
    fabric = _vnx_project(world["tmp"] / "fabric", "vnx-dev")
    own = _write_probe(fabric / "scripts" / "hooks")
    assert _ran(_shim(world, shell, PROBE, cwd=fabric))["ran"] == os.path.realpath(own)


@pytest.mark.parametrize("shell", SHELLS)
def test_the_fabric_repo_without_the_entry_runs_current(world, shell):
    fabric = _vnx_project(world["tmp"] / "fabric", "vnx-dev")
    assert _ran(_shim(world, shell, PROBE, cwd=fabric))["ran"] == os.path.realpath(
        world["hooks"] / PROBE)


@pytest.mark.parametrize("shell", SHELLS)
def test_a_consumer_never_runs_project_code(world, shell):
    """Only the fabric repo (first line `vnx-dev`) runs its own copy; a consumer that carries a
    file of the same name still runs the released one."""
    _write_probe(world["project"] / "scripts" / "hooks")
    assert _ran(_shim(world, shell, PROBE))["ran"] == os.path.realpath(world["hooks"] / PROBE)


@pytest.mark.parametrize("shell", SHELLS)
def test_vnx_home_replaces_current(world, shell):
    other = world["tmp"] / "other-home"
    own = _write_probe(other / "scripts" / "hooks")
    assert _ran(_shim(world, shell, PROBE, VNX_HOME=other))["ran"] == os.path.realpath(own)


def _not_guarded_cases(tmp: Path):
    yield "not a git repository", _vnx_project(tmp / "plain", git=False)
    project = _vnx_project(tmp / "wt-project")
    worktree = project / ".vnx-data" / "worktrees" / "dispatch-x"
    worktree.mkdir(parents=True)
    yield "dispatch worktree", worktree
    yield "project id without a T0 folder", _vnx_project(tmp / "clients", "vincent-clients", t0=False)
    yield "T0 folder without a project id", _vnx_project(tmp / "no-id", None)
    empty = _vnx_project(tmp / "empty-id", None)
    (empty / ".vnx-project-id").write_text("\nvnx-dev\n", encoding="utf-8")
    yield "empty first line", empty
    blank = _vnx_project(tmp / "blank-id", None)
    (blank / ".vnx-project-id").write_text("   \t\n", encoding="utf-8")
    yield "blank first line", blank


@pytest.mark.parametrize("shell", SHELLS)
def test_outside_a_guarded_project_the_shim_is_silent(world, shell):
    for label, cwd in _not_guarded_cases(world["tmp"] / shell):
        proc = _shim(world, shell, "--scope", "user", PROBE, cwd=cwd)
        assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", ""), label


@pytest.mark.parametrize("shell", SHELLS)
def test_the_shim_and_hook_coverage_agree_on_rule_1(world, shell, monkeypatch):
    """Rule 1 exists twice, in sh and in Python. Both must say the same for every case."""
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_CEILING_DIRECTORIES"):
        monkeypatch.delenv(var, raising=False)
    cases = list(_not_guarded_cases(world["tmp"] / f"parity-{shell}"))
    cases.append(("project root", world["project"]))
    cases.append(("T0 folder", world["project"] / ".claude" / "terminals" / "T0"))
    for label, cwd in cases:
        ran = _shim(world, shell, PROBE, cwd=cwd).stdout != ""
        assert ran == (hook_coverage.t0_project_root(str(cwd)) is not None), label


@pytest.mark.parametrize("shell", SHELLS)
def test_a_missing_entry_names_the_path_and_exits_1(world, shell):
    proc = _shim(world, shell, "--scope", "user", "absent_entry.py")
    assert proc.returncode == 1
    assert str(world["home"] / ".vnx-system" / "current" / "scripts" / "hooks" /
               "absent_entry.py") in proc.stderr
    assert proc.stdout == ""


@pytest.mark.parametrize("shell", SHELLS)
def test_an_entry_exit_2_becomes_1(world, shell):
    assert _shim(world, shell, PROBE, PROBE_EXIT=2).returncode == 1


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("code", [0, 1, 3])
def test_other_entry_exit_codes_pass_through(world, shell, code):
    assert _shim(world, shell, PROBE, PROBE_EXIT=code).returncode == code


@pytest.mark.parametrize("shell", SHELLS)
def test_an_entry_removed_between_test_and_start_exits_1(world, shell):
    proc = _shim(world, shell, "--scope", "user", PROBE, fake_python=True)
    assert proc.returncode == 1
    assert not (world["hooks"] / PROBE).exists()


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("args", [(), ("--scope",), ("--scope", "user"), ("--scope", "bogus", PROBE),
                                  ("../escape.py",), ("sub/entry.py",), ("-x",), (".hidden.py",),
                                  (PROBE, PROBE)])
def test_a_malformed_call_exits_1(world, shell, args):
    proc = _shim(world, shell, *args)
    assert proc.returncode == 1, (args, proc.stderr)
    assert proc.stderr.strip()


def test_the_shim_is_posix_sh():
    assert SHIM.read_text(encoding="utf-8").startswith("#!/bin/sh\n")
    for shell in ("bash", "dash"):
        if shutil.which(shell):
            check = subprocess.run([shutil.which(shell), "-n", str(SHIM)], capture_output=True,
                                   text=True)
            assert check.returncode == 0, (shell, check.stderr)


def test_the_shim_sources_no_file():
    """A second file that can be missing is the failure this script contains."""
    code = [line for line in SHIM.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]
    assert not [line for line in code if re.match(r"\s*(?:\.|source)\s+\S", line)]


_EXIT_2_RE = re.compile(r"\b(?:sys\.exit|_exit|exit|SystemExit)\s*\(\s*2\s*\)|\breturn\s+2\b"
                        r"|\bexit\s+2\b")


@pytest.mark.parametrize("entry", hook_coverage.SHIM_ENTRIES)
def test_no_shim_routed_entry_exits_2(entry):
    """The shim contract: an entry decides through JSON on stdout, never through exit 2."""
    source = (REPO_ROOT / "scripts" / "hooks" / entry).read_text(encoding="utf-8")
    assert not _EXIT_2_RE.findall(source), entry


# ── the registered commands ──────────────────────────────────────────────────


USER_COMMAND = (
    "sh -c 'f=\"$HOME/.vnx-system/current/scripts/hooks/vnx_hook_shim.sh\"; "
    "if [ ! -f \"$f\" ]; then echo \"vnx: guard shim missing: $f\" >&2; exit 1; fi; "
    "sh \"$f\" --scope user t0_context_guard.py; rc=$?; [ \"$rc\" -eq 0 ] || rc=1; exit \"$rc\"'"
)


def test_the_user_command_is_the_inline_form():
    assert hook_coverage.registered_command(hook_coverage.GUARD_ENTRY) == USER_COMMAND
    assert hook_coverage.registered_command(hook_coverage.GUARD_ENTRY, "user") == USER_COMMAND


def test_the_project_form_names_its_home_and_scope():
    command = hook_coverage.registered_command(NO_SUBAGENTS, "project", vnx_home="{{VNX_HOME}}")
    assert 'f="{{VNX_HOME}}/scripts/hooks/vnx_hook_shim.sh"' in command
    assert f'--scope project {NO_SUBAGENTS};' in command
    assert "exec" not in command


@pytest.mark.parametrize("kwargs", [
    {"entry": "../x.py"}, {"entry": ""}, {"entry": "a b.py"}, {"scope": "global"},
    {"vnx_home": ""}, {"vnx_home": "/x'y"}, {"vnx_home": '/x"y'}, {"vnx_home": "/x\ny"},
    {"vnx_home": "/x`id`"}, {"vnx_home": "/x\\y"},
])
def test_registered_command_refuses_what_it_cannot_quote(kwargs):
    args = {"entry": hook_coverage.GUARD_ENTRY, "scope": "user", **kwargs}
    with pytest.raises(ValueError):
        hook_coverage.registered_command(args.pop("entry"), **args)


def _forms(world):
    yield "user", hook_coverage.registered_command(hook_coverage.GUARD_ENTRY), \
        world["home"] / ".vnx-system" / "current" / "scripts" / "hooks", hook_coverage.GUARD_ENTRY
    yield "project", hook_coverage.registered_command(NO_SUBAGENTS, "project",
                                                     vnx_home=str(world["release"])), \
        world["hooks"], NO_SUBAGENTS


@pytest.mark.parametrize("shell", SHELLS)
def test_every_registered_form_runs_its_entry_when_present(world, shell):
    for scope, command, _, entry in _forms(world):
        out = _ran(_registered(world, shell, command))
        assert out["ran"] == os.path.realpath(world["hooks"] / entry), scope
        assert out["scope"] == scope


@pytest.mark.parametrize("shell", SHELLS)
def test_every_registered_form_is_silent_outside_a_project(world, shell):
    plain = world["tmp"] / "plain"
    plain.mkdir()
    for scope, command, _, _ in _forms(world):
        proc = _registered(world, shell, command, cwd=plain)
        assert (proc.returncode, proc.stdout) == (0, ""), scope


@pytest.mark.parametrize("shell", SHELLS)
def test_a_missing_shim_exits_1(world, shell):
    (world["hooks"] / SHIM.name).unlink()
    for scope, command, _, _ in _forms(world):
        proc = _registered(world, shell, command)
        assert proc.returncode == 1, (scope, proc.stderr)
        assert "vnx: guard shim missing" in proc.stderr


@pytest.mark.parametrize("shell", SHELLS)
def test_a_missing_current_exits_1(world, shell):
    (world["home"] / ".vnx-system" / "current").unlink()
    proc = _registered(world, shell, hook_coverage.registered_command(hook_coverage.GUARD_ENTRY))
    assert proc.returncode == 1
    assert "vnx: guard shim missing" in proc.stderr


@pytest.mark.parametrize("shell", SHELLS)
def test_a_missing_entry_through_a_registered_form_exits_1(world, shell):
    for scope, command, _, entry in _forms(world):
        (world["hooks"] / entry).unlink()
        proc = _registered(world, shell, command)
        assert proc.returncode == 1, (scope, proc.stderr)
        assert entry in proc.stderr


@pytest.mark.parametrize("shell", SHELLS)
def test_a_shim_removed_between_test_and_start_exits_1(world, shell):
    for scope, command, _, _ in _forms(world):
        shutil.copy2(SHIM, world["hooks"] / SHIM.name)
        proc = _registered(world, shell, command, fake_sh=True)
        assert proc.returncode == 1, (scope, proc.returncode, proc.stderr)
        assert not (world["hooks"] / SHIM.name).exists()


@pytest.mark.parametrize("shell", SHELLS)
def test_an_entry_removed_between_test_and_start_exits_1_through_a_registered_form(world, shell):
    for scope, command, _, entry in _forms(world):
        proc = _registered(world, shell, command, fake_python=True)
        assert proc.returncode == 1, (scope, proc.returncode, proc.stderr)
        assert not (world["hooks"] / entry).exists()


# ── the real entries behind the registered commands ─────────────────────────


def _real_world(world) -> None:
    current = world["home"] / ".vnx-system" / "current"
    current.unlink()
    current.symlink_to(REPO_ROOT, target_is_directory=True)


@pytest.mark.parametrize("shell", SHELLS)
def test_the_project_form_still_denies_an_agent_call(world, shell):
    _real_world(world)
    t0_dir = world["project"] / ".claude" / "terminals" / "T0"
    command = hook_coverage.registered_command(NO_SUBAGENTS, "project", vnx_home=str(REPO_ROOT))
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Agent", "cwd": str(t0_dir),
               "tool_input": {"prompt": "x"}}
    proc = _registered(world, shell, command, cwd=t0_dir, stdin=json.dumps(payload))
    assert proc.returncode == 0, proc.stderr
    decision = json.loads(proc.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"


def _guard_transcript(path: Path, tokens: int) -> Path:
    entry = {"type": "assistant", "message": {"model": "claude-opus-5-5", "usage": {
        "input_tokens": 1, "cache_read_input_tokens": tokens - 1,
        "cache_creation_input_tokens": 0}}}
    path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    return path


MEASURED_T0_ENV = {"CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_CODE_SESSION_ATTENDED": "1",
                   "CLAUDE_CODE_CHILD_SESSION": "1", "TMUX_PANE": "%7"}


def _guard_stop(world, shell, **env_extra) -> subprocess.CompletedProcess:
    _real_world(world)
    t0_dir = world["project"] / ".claude" / "terminals" / "T0"
    payload = {"hook_event_name": "Stop", "session_id": "s-1", "cwd": str(t0_dir),
               "transcript_path": str(_guard_transcript(world["tmp"] / "t.jsonl", 520_000))}
    env = {**MEASURED_T0_ENV, "CLAUDE_PROJECT_DIR": str(world["project"]),
           "VNX_T0_ROTATION_STATE_DIR": str(world["tmp"] / "state"),
           "VNX_T0_ROTATION_RECEIPTS_FILE": str(world["tmp"] / "receipts" / "r.ndjson"),
           **env_extra}
    return _registered(world, shell, hook_coverage.registered_command(hook_coverage.GUARD_ENTRY),
                       cwd=t0_dir, stdin=json.dumps(payload), **env)


@pytest.mark.parametrize("shell", SHELLS)
def test_the_user_command_runs_the_guard_in_a_project_without_a_registration(world, shell):
    proc = _guard_stop(world, shell)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["decision"] == "block"


@pytest.mark.parametrize("shell", SHELLS)
def test_the_user_command_is_silent_for_a_claude_p_child(world, shell):
    proc = _guard_stop(world, shell, CLAUDE_CODE_ENTRYPOINT="sdk-cli",
                       CLAUDE_CODE_SESSION_ATTENDED="0")
    assert (proc.returncode, proc.stdout) == (0, "")


@pytest.mark.parametrize("shell", SHELLS)
def test_the_user_command_is_silent_where_the_project_covers_the_event(world, shell):
    settings = world["project"] / ".claude" / "settings.json"
    settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{
        "type": "command",
        "command": f'python3 "{REPO_ROOT / "scripts" / "hooks" / hook_coverage.GUARD_ENTRY}"',
    }]}]}}), encoding="utf-8")
    proc = _guard_stop(world, shell)
    assert (proc.returncode, proc.stdout) == (0, ""), proc.stderr
