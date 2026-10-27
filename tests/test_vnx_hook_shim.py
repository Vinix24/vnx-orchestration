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
import time
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


def _logging_git(tmp: Path, log: Path) -> Path:
    """A bin dir whose `git` records every call in ``log`` and then runs the real git."""
    bin_dir = tmp / "bin-logging-git"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "git").write_text(f'#!/bin/sh\necho "$*" >> "{log}"\nexec "{shutil.which("git")}" "$@"\n',
                                 encoding="utf-8")
    (bin_dir / "git").chmod(0o755)
    return bin_dir


def _env(world, shell: str, **extra) -> Dict[str, str]:
    flags = {k: extra.pop(k) for k in ("fake_sh", "fake_python") if k in extra}
    git_log = extra.pop("git_log", None)
    bin_dirs = [_bin_dir(world["tmp"], shell, **flags)]
    if git_log is not None:
        bin_dirs.insert(0, _logging_git(world["tmp"], git_log))
    path = os.pathsep.join([*map(str, bin_dirs), os.environ.get("PATH", "/usr/bin:/bin")])
    env = {"PATH": path, "HOME": str(world["home"]), "GIT_CONFIG_NOSYSTEM": "1"}
    env.update({k: str(v) for k, v in extra.items()})
    return env


def _released_shim(world) -> Path:
    """The shim as the user-level command starts it: through `~/.vnx-system/current`."""
    return world["home"] / ".vnx-system" / "current" / "scripts" / "hooks" / SHIM.name


def _shim(world, shell: str, *args: str, cwd: Optional[Path] = None, stdin: str = "{}",
          shim: Path = SHIM, **env_extra) -> subprocess.CompletedProcess:
    """The shim with ``args``. A user-scope call runs the entry beside the shim it was started
    as, so user-scope tests pass ``shim=_released_shim(world)``."""
    return subprocess.run([shutil.which(shell), str(shim), *args], input=stdin,
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
    out = _ran(_shim(world, shell, "--scope", "user", PROBE, stdin='{"hook_event_name": "Stop"}',
                     shim=_released_shim(world)))
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


def _registry(home: Path, *projects) -> Optional[Path]:
    """``~/.vnx/projects.json`` in the shape measured on 2026-10-16; no projects removes it."""
    registry = home / ".vnx" / "projects.json"
    if not projects:
        if registry.exists():
            registry.unlink()
        return None
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps({"projects": [
        {"name": Path(str(path)).name, "path": str(path), "layout": "unknown", "project_id": pid}
        for pid, path in projects]}, indent=2), encoding="utf-8")
    return registry


@pytest.mark.parametrize("shell", SHELLS)
def test_the_registered_fabric_repo_runs_its_own_code(world, shell):
    fabric = _vnx_project(world["tmp"] / "fabric", "vnx-dev")
    own = _write_probe(fabric / "scripts" / "hooks")
    _registry(world["home"], ("consumer", world["project"]), ("vnx-dev", fabric))
    assert _ran(_shim(world, shell, PROBE, cwd=fabric))["ran"] == os.path.realpath(own)


@pytest.mark.parametrize("shell", SHELLS)
def test_the_fabric_repo_without_the_entry_runs_current(world, shell):
    fabric = _vnx_project(world["tmp"] / "fabric", "vnx-dev")
    _registry(world["home"], ("vnx-dev", fabric))
    assert _ran(_shim(world, shell, PROBE, cwd=fabric))["ran"] == os.path.realpath(
        world["hooks"] / PROBE)


def _spoof_repo(tmp: Path, marker: Path) -> Path:
    """A cloned repo that calls itself vnx-dev, has a T0 folder, and ships its own guard."""
    spoof = _vnx_project(tmp / "spoof", "vnx-dev")
    hooks = spoof / "scripts" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / hook_coverage.GUARD_ENTRY).write_text(
        f"open({str(marker)!r}, 'w').write('repo code ran')\n", encoding="utf-8")
    return spoof


@pytest.mark.parametrize("shell", SHELLS)
def test_a_repo_that_names_itself_vnx_dev_runs_current(world, shell):
    """deepseek warning 2 on #2075: a user-scope hook must never run code the repository
    selects. Through the user-form command such a repo runs `current`'s guard, whatever the
    registry says about other roots, and its own file never runs."""
    marker = world["tmp"] / "marker"
    spoof = _spoof_repo(world["tmp"], marker)
    real = _vnx_project(world["tmp"] / "real-fabric", "vnx-dev")
    command = hook_coverage.registered_command(hook_coverage.GUARD_ENTRY)
    for projects in ((), (("vnx-dev", real),), (("other", spoof),),
                     (("vnx-dev", real), ("vnx-dev", spoof))):
        _registry(world["home"], *projects)
        proc = _registered(world, shell, command, cwd=spoof)
        assert not marker.exists(), ("the repo's own guard ran", projects)
        out = _ran(proc)
        assert out["ran"] == os.path.realpath(world["hooks"] / hook_coverage.GUARD_ENTRY), projects


@pytest.mark.parametrize("shell", SHELLS)
def test_an_unreadable_registry_runs_current(world, shell):
    fabric = _vnx_project(world["tmp"] / "fabric", "vnx-dev")
    _write_probe(fabric / "scripts" / "hooks")
    registry = world["home"] / ".vnx" / "projects.json"
    registry.parent.mkdir(parents=True)
    for content in ('{"projects": [', "[]", json.dumps({"projects": [
            {"project_id": "vnx-dev", "path": fabric.name}]})):
        registry.write_text(content, encoding="utf-8")
        assert _ran(_shim(world, shell, PROBE, cwd=fabric))["ran"] == os.path.realpath(
            world["hooks"] / PROBE), content
    registry.unlink()
    registry.mkdir()
    assert _ran(_shim(world, shell, PROBE, cwd=fabric))["ran"] == os.path.realpath(
        world["hooks"] / PROBE)


@pytest.mark.parametrize("shell", SHELLS)
def test_the_registry_check_ignores_a_planted_python_module(world, shell):
    """The registry is read by `python3 -I`: the repo is the cwd, and a `json.py` it plants
    must not be imported."""
    fabric = _vnx_project(world["tmp"] / "fabric", "vnx-dev")
    own = _write_probe(fabric / "scripts" / "hooks")
    marker = world["tmp"] / "planted"
    for name in ("json.py", "os.py", "sitecustomize.py"):
        (fabric / name).write_text(f"open({str(marker)!r}, 'w').write('x')\n", encoding="utf-8")
    _registry(world["home"], ("vnx-dev", fabric))
    assert _ran(_shim(world, shell, PROBE, cwd=fabric))["ran"] == os.path.realpath(own)
    assert not marker.exists()


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
        proc = _shim(world, shell, "--scope", "user", PROBE, cwd=cwd, shim=_released_shim(world))
        assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", ""), label


# ── a drifted cwd follows the session's project ─────────────────────────────


@pytest.mark.parametrize("shell", SHELLS)
def test_a_drifted_cwd_follows_claude_project_dir(world, shell):
    """OI-2119 (d): step 1 used to exit 0 on a pwd that named no project, before step 2 could
    start anything. Claude Code sets CLAUDE_PROJECT_DIR to the folder the session started in, and
    step 1 is the same two steps as ``hook_coverage.session_project_root``."""
    home = world["tmp"] / "drifted-home"
    home.mkdir()
    proc = _shim(world, shell, PROBE, cwd=home, CLAUDE_PROJECT_DIR=world["project"])
    assert _ran(proc)["ran"] == os.path.realpath(world["hooks"] / PROBE)


@pytest.mark.parametrize("shell", SHELLS)
def test_a_dispatch_worktree_cwd_stays_silent_with_the_main_project(world, shell):
    worktree = world["project"] / ".vnx-data" / "worktrees" / "dispatch-x"
    worktree.mkdir(parents=True)
    proc = _shim(world, shell, PROBE, cwd=worktree, CLAUDE_PROJECT_DIR=world["project"])
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")


@pytest.mark.parametrize("shell", SHELLS)
def test_a_worktree_claude_project_dir_stays_silent(world, shell):
    home = world["tmp"] / "drifted-home"
    home.mkdir()
    proc = _shim(world, shell, PROBE, cwd=home,
                 CLAUDE_PROJECT_DIR=world["project"] / ".vnx-data" / "worktrees" / "dispatch-x")
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")


@pytest.mark.parametrize("shell", SHELLS)
def test_no_project_on_either_step_stays_silent(world, shell):
    home = world["tmp"] / "drifted-home"
    home.mkdir()
    no_project = world["tmp"] / "no-project-here"
    no_project.mkdir()
    proc = _shim(world, shell, PROBE, cwd=home, CLAUDE_PROJECT_DIR=no_project)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")


def _code_choice_cases(world, tmp: Path):
    """(label, cwd, registry projects, extra env) for shim step 2."""
    fabric = _vnx_project(tmp / "fabric", "vnx-dev")
    _write_probe(fabric / "scripts" / "hooks")
    bare_fabric = _vnx_project(tmp / "bare-fabric", "vnx-dev")
    spoof = _vnx_project(tmp / "spoof", "vnx-dev")
    _write_probe(spoof / "scripts" / "hooks")
    link = tmp / "fabric-link"
    link.symlink_to(fabric, target_is_directory=True)
    consumer = world["project"]
    _write_probe(consumer / "scripts" / "hooks")
    other_home = tmp / "other-home"
    _write_probe(other_home / "scripts" / "hooks")
    yield "consumer", consumer, (), {}
    yield "consumer with VNX_HOME", consumer, (), {"VNX_HOME": world["release"]}
    yield "consumer with another VNX_HOME", consumer, (), {"VNX_HOME": other_home}
    yield "registered fabric", fabric, (("vnx-dev", fabric),), {}
    yield "fabric registered through a symlink", fabric, (("vnx-dev", link),), {}
    yield "fabric from its T0 folder", fabric / ".claude" / "terminals" / "T0", \
        (("vnx-dev", fabric),), {}
    yield "registered fabric without the entry", bare_fabric, (("vnx-dev", bare_fabric),), {}
    yield "fabric without a registry", fabric, (), {}
    yield "spoof, registry names another root", spoof, (("vnx-dev", fabric),), {}
    yield "spoof registered under another id", spoof, (("other", spoof),), {}
    yield "spoof in a later vnx-dev entry", spoof, (("vnx-dev", fabric), ("vnx-dev", spoof)), {}
    yield "consumer path registered as vnx-dev", consumer, (("vnx-dev", consumer),), {}


@pytest.mark.parametrize("shell", SHELLS)
def test_the_shim_and_hook_coverage_agree_on_rule_1_and_the_code_choice(world, shell,
                                                                         monkeypatch):
    """Rule 1 and step 2 exist twice, in sh and in Python. Both must say the same for every
    case and both scopes: whether anything runs, and which file."""
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_CEILING_DIRECTORIES"):
        monkeypatch.delenv(var, raising=False)
    cases = [(label, cwd, (), {}) for label, cwd in
             _not_guarded_cases(world["tmp"] / f"parity-{shell}")]
    cases.append(("project root", world["project"], (), {}))
    cases.append(("T0 folder", world["project"] / ".claude" / "terminals" / "T0", (), {}))
    drifted = world["tmp"] / f"drifted-{shell}"
    drifted.mkdir()
    worktree = world["project"] / ".vnx-data" / "worktrees" / "dispatch-x"
    worktree.mkdir(parents=True, exist_ok=True)
    cases.append(("drifted cwd", drifted, (), {"CLAUDE_PROJECT_DIR": str(world["project"])}))
    cases.append(("worktree cwd with the main project", worktree, (),
                  {"CLAUDE_PROJECT_DIR": str(world["project"])}))
    cases.extend(_code_choice_cases(world, world["tmp"] / f"choice-{shell}"))
    released = _released_shim(world)
    for label, cwd, projects, extra in cases:
        _registry(world["home"], *projects)
        env = _env(world, shell, **extra)
        project_call = _shim(world, shell, PROBE, cwd=cwd, **extra)
        user_call = _shim(world, shell, "--scope", "user", PROBE, cwd=cwd, shim=released, **extra)
        root = hook_coverage.session_project_root(str(cwd), env)
        assert (project_call.stdout != "") == (root is not None), label
        assert (user_call.stdout != "") == (root is not None), label
        if root is not None:
            expected = hook_coverage.shim_entry_file(root, PROBE, env)
            assert _ran(project_call)["ran"] == os.path.realpath(expected), label
            expected = hook_coverage.shim_entry_file(root, PROBE, env, user_shim=released)
            assert _ran(user_call)["ran"] == os.path.realpath(expected), label


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("scope", hook_coverage.SCOPES)
def test_a_missing_entry_names_the_path_and_exits_1(world, shell, scope):
    proc = _shim(world, shell, "--scope", scope, "absent_entry.py", shim=_released_shim(world))
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
    proc = _shim(world, shell, "--scope", "user", PROBE, fake_python=True,
                 shim=_released_shim(world))
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


CODEX_REPRO = "/safe/$(printf VNX_REVIEW_EXECUTED >&2)"


@pytest.mark.parametrize("shell", SHELLS)
def test_a_command_substitution_in_the_home_runs_when_rendered(world, shell):
    """codex_gate on #2075 (blocking): the home is interpolated into shell source, so a
    substitution in it runs before the shim is even tested. This pins the hazard on the command
    text the unvalidated renderer produced."""
    command = hook_coverage.registered_command(
        hook_coverage.GUARD_ENTRY, vnx_home="/safe/X").replace("/safe/X", CODEX_REPRO)
    proc = _registered(world, shell, command)
    assert "VNX_REVIEW_EXECUTED" in proc.stderr
    assert proc.returncode == 1


@pytest.mark.parametrize("vnx_home", [
    CODEX_REPRO, "${HOME}$(id)", "$HOME/$(x)", "/x/{{VNX_HOME}}", "{{VNX_HOME}}x",
    "x{{VNX_HOME}}", "$(id)", "/a/$(id)/b", "/a/${HOME}", "$HOMEX/x", "$HOME_x",
    "${HOME:-/tmp}", "${HOME}${HOME}", "$VNX_HOME/x", "$PWD", "relative/path", "./x", "~/x",
    "/a;b", "/a&&b", "/a|b", "/a>b", "/a*", "/a\tb", "/a\rb", "/a\x00b", "/a$", "/{a,b}",
    "{{VNX_HOME}}{{VNX_HOME}}", "$CLAUDE_PROJECT_DIRX", "${CLAUDE_PROJECT_DIR}x",
])
def test_the_home_is_one_of_the_documented_forms(vnx_home):
    with pytest.raises(ValueError) as caught:
        hook_coverage.registered_command(hook_coverage.GUARD_ENTRY, "project", vnx_home=vnx_home)
    assert repr(vnx_home) in str(caught.value)


def _allowed_homes(world):
    """(label, vnx_home, extra env, the release the rendered command names)."""
    release = str(world["release"])
    yield "absolute", release, {}, world["release"]
    yield "absolute with spaces and plain marks", str(world["spaced"]), {}, world["spaced"]
    yield "$HOME", "$HOME/.vnx-system/current", {}, world["release"]
    yield "${HOME}", "${HOME}/.vnx-system/current", {}, world["release"]
    yield "default", hook_coverage.DEFAULT_VNX_HOME, {}, world["release"]
    yield "$CLAUDE_PROJECT_DIR", "$CLAUDE_PROJECT_DIR", {"CLAUDE_PROJECT_DIR": release}, \
        world["release"]
    yield "${CLAUDE_PROJECT_DIR}", "${CLAUDE_PROJECT_DIR}/", {"CLAUDE_PROJECT_DIR": release}, \
        world["release"]
    yield "{{VNX_HOME}}", "{{VNX_HOME}}", {}, world["release"]


@pytest.mark.parametrize("shell", SHELLS)
def test_every_documented_home_form_runs_the_shim(world, shell):
    """Each allowed form renders, and the rendered command (with the template placeholder
    substituted the way the template renderer does) runs the shim and its entry: at user scope
    the entry beside that shim, at project scope the one under `current` (VNX_HOME unset)."""
    world["spaced"] = world["tmp"] / "a b+c@d,e:f" / "release-1.7.2"
    shutil.copytree(world["release"], world["spaced"])
    for label, vnx_home, extra, named in _allowed_homes(world):
        for scope in hook_coverage.SCOPES:
            command = hook_coverage.registered_command(PROBE, scope, vnx_home=vnx_home)
            command = command.replace("{{VNX_HOME}}", str(world["release"]))
            out = _ran(_registered(world, shell, command, **extra))
            assert out["scope"] == scope, label
            hooks = named / "scripts" / "hooks" if scope == "user" else world["hooks"]
            assert out["ran"] == os.path.realpath(hooks / PROBE), (label, scope)


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


def _trust(world, trusted: Optional[bool] = True) -> None:
    """``~/.claude.json`` with the project's trust decision; None leaves no file at all."""
    config = world["home"] / ".claude.json"
    if trusted is None:
        if config.exists():
            config.unlink()
        return
    config.write_text(json.dumps({"projects": {str(world["project"]): {
        "allowedTools": [], "hasTrustDialogAccepted": trusted}}}), encoding="utf-8")


def _guard_stop(world, shell, *, trusted: Optional[bool] = True,
                **env_extra) -> subprocess.CompletedProcess:
    _real_world(world)
    _trust(world, trusted)
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


def _project_stop_hook(world, command: str, **meta) -> None:
    settings = world["project"] / ".claude" / "settings.json"
    settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{
        "type": "command", "command": command, **meta}]}]}}), encoding="utf-8")


@pytest.mark.parametrize("shell", SHELLS)
def test_the_user_command_is_silent_where_the_project_covers_the_event(world, shell):
    _project_stop_hook(
        world, f'python3 "{REPO_ROOT / "scripts" / "hooks" / hook_coverage.GUARD_ENTRY}"')
    proc = _guard_stop(world, shell)
    assert (proc.returncode, proc.stdout) == (0, ""), proc.stderr


@pytest.mark.parametrize("shell", SHELLS)
def test_the_user_command_decides_where_the_project_shim_is_missing(world, shell):
    """codex advisory on #2075: the project registers the guard through a shim that is gone.
    Its own call exits 1, so the user-level guard must decide, not skip."""
    _project_stop_hook(world, hook_coverage.registered_command(
        hook_coverage.GUARD_ENTRY, "project", vnx_home=str(world["tmp"] / "pruned-release")))
    project_call = _registered(world, shell, hook_coverage.registered_command(
        hook_coverage.GUARD_ENTRY, "project", vnx_home=str(world["tmp"] / "pruned-release")))
    assert project_call.returncode == 1
    proc = _guard_stop(world, shell)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout, "no decision: the user-level guard skipped, so no guard ran for Stop"
    assert json.loads(proc.stdout)["decision"] == "block"


@pytest.mark.parametrize("shell", SHELLS)
def test_the_user_command_is_silent_where_the_project_shim_exists(world, shell):
    _project_stop_hook(world, hook_coverage.registered_command(
        hook_coverage.GUARD_ENTRY, "project", vnx_home=str(REPO_ROOT)))
    proc = _guard_stop(world, shell)
    assert (proc.returncode, proc.stdout) == (0, ""), proc.stderr


# ── fix-forward on #2075: the user command decides where the project cannot ─


def _pruned_release(world) -> Path:
    """A release with the shim and without the guard entry."""
    pruned = world["tmp"] / "pruned"
    (pruned / "scripts" / "hooks").mkdir(parents=True, exist_ok=True)
    shutil.copy2(SHIM, pruned / "scripts" / "hooks" / SHIM.name)
    return pruned


REPO_GUARD = REPO_ROOT / "scripts" / "hooks" / hook_coverage.GUARD_ENTRY
UNCOVERING = {
    "(a) a direct entry at user scope": (f'VNX_HOOK_SCOPE=user python3 "{REPO_GUARD}"', {}, True),
    "(b) a single-quoted $HOME": (
        f"sh '$HOME/.vnx-system/current/scripts/hooks/{SHIM.name}' {hook_coverage.GUARD_ENTRY}",
        {}, True),
    "(f) an async registration": (f'python3 "{REPO_GUARD}"', {"async": True}, True),
    "(g) an untrusted project": (f'python3 "{REPO_GUARD}"', {}, False),
    "(g) unreadable trust": (f'python3 "{REPO_GUARD}"', {}, None),
}


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("case", sorted(UNCOVERING))
def test_the_user_command_decides_where_the_project_registration_cannot(world, shell, case):
    """codex and deepseek on #2075: each of these counted as coverage, so the user-level guard
    skipped while the project's own call decided nothing. No guard ran at 520K tokens."""
    command, meta, trusted = UNCOVERING[case]
    _project_stop_hook(world, command, **meta)
    proc = _guard_stop(world, shell, trusted=trusted)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout, f"{case}: no decision, so no guard ran for Stop"
    assert json.loads(proc.stdout)["decision"] == "block"


@pytest.mark.parametrize("shell", SHELLS)
def test_the_user_command_decides_where_the_project_shim_lacks_its_entry(world, shell):
    """(d) with (c): the project's shim call takes its entry from VNX_HOME, which names a release
    without the guard, so it exits 1. The user-level call runs the guard beside its own shim
    and decides."""
    pruned = _pruned_release(world)
    _project_stop_hook(world, hook_coverage.registered_command(
        hook_coverage.GUARD_ENTRY, "project", vnx_home=str(REPO_ROOT)))
    _trust(world)
    _real_world(world)
    project_call = _registered(world, shell, hook_coverage.registered_command(
        hook_coverage.GUARD_ENTRY, "project", vnx_home=str(REPO_ROOT)), VNX_HOME=pruned)
    assert project_call.returncode == 1
    assert "hook entry missing" in project_call.stderr
    proc = _guard_stop(world, shell, VNX_HOME=pruned)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["decision"] == "block"


@pytest.mark.parametrize("shell", SHELLS)
def test_a_user_scope_call_ignores_an_inherited_vnx_home(world, shell):
    """(c), deepseek warning 2 on #2075: a settings `env` block, or the `vnx` copy that launched
    the session, sets VNX_HOME. It must not choose the code the user-level registration starts."""
    marker = world["tmp"] / "foreign-code-ran"
    foreign = world["tmp"] / "foreign-home" / "scripts" / "hooks"
    foreign.mkdir(parents=True)
    (foreign / hook_coverage.GUARD_ENTRY).write_text(
        f"open({str(marker)!r}, 'w').write('VNX_HOME chose the code')\n", encoding="utf-8")
    proc = _registered(world, shell, hook_coverage.registered_command(hook_coverage.GUARD_ENTRY),
                       VNX_HOME=foreign.parents[1])
    assert not marker.exists(), "the user-scope call ran the entry VNX_HOME named"
    assert _ran(proc)["ran"] == os.path.realpath(world["hooks"] / hook_coverage.GUARD_ENTRY)
    assert _ran(proc)["scope"] == "user"


@pytest.mark.parametrize("shell", SHELLS)
def test_a_project_scope_call_still_follows_vnx_home(world, shell):
    other = world["tmp"] / "other-home"
    own = _write_probe(other / "scripts" / "hooks")
    command = hook_coverage.registered_command(PROBE, "project", vnx_home=str(world["release"]))
    assert _ran(_registered(world, shell, command, VNX_HOME=other))["ran"] == os.path.realpath(own)


@pytest.mark.parametrize("shell", SHELLS)
def test_the_shim_and_the_guard_start_no_git(world, shell):
    """(h): the hook has 10 s and `git rev-parse` waited up to 10 s (Python) or without any
    bound (the shim). Neither starts git now, on any path of a Stop at 520K tokens."""
    log = world["tmp"] / "git.log"
    proc = _guard_stop(world, shell, git_log=log)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["decision"] == "block"
    assert not log.exists(), log.read_text(encoding="utf-8")


@pytest.mark.parametrize("shell", SHELLS)
def test_a_registry_that_blocks_costs_the_shim_at_most_two_seconds(world, shell):
    """(h): the registry reader ends itself after 2 s, so a FIFO registry (or a stalled mount)
    cannot hold the hook until its timeout. The fabric root then runs the released code."""
    fabric = _vnx_project(world["tmp"] / "fabric", "vnx-dev")
    _write_probe(fabric / "scripts" / "hooks")
    registry = world["home"] / ".vnx" / "projects.json"
    registry.parent.mkdir(parents=True)
    os.mkfifo(registry)
    started = time.monotonic()
    try:
        proc = subprocess.run([shutil.which(shell), str(SHIM), PROBE], input="{}",
                              env=_env(world, shell), cwd=fabric, capture_output=True,
                              text=True, timeout=15)
    finally:
        try:
            os.close(os.open(registry, os.O_WRONLY | os.O_NONBLOCK))
        except OSError:
            pass  # vnx-silent-except: no reader left on the FIFO, nothing to release
    assert time.monotonic() - started < 6
    assert _ran(proc)["ran"] == os.path.realpath(world["hooks"] / PROBE)
