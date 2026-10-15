"""The shell boundary: no failure of the shim may leave a hook exiting 2."""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from conftest import PKG

HOOKS_JSON = json.loads((PKG / "hooks" / "hooks.json").read_text(encoding="utf-8"))
SHELLS = ["/bin/sh", "dash", "bash"]
# The shim alone, without the outer "|| exit 1" that folds every failure into 1.
BARE_COMMAND = 'sh "${CLAUDE_PLUGIN_ROOT}/hooks/run.sh" context_guard'


def hook_commands():
    found = []
    for groups in HOOKS_JSON["hooks"].values():
        for group in groups:
            for hook in group["hooks"]:
                found.append(hook)
    return found


def shell_path(name):
    path = shutil.which(name) if not name.startswith("/") else (name if os.path.exists(name) else None)
    if path is None:
        pytest.skip("shell %s not present on this machine" % name)
    return path


def run_command(shell, command, root, env_extra=None, stdin="{}"):
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = str(root)
    env.update(env_extra or {})
    return subprocess.run(
        [shell, "-c", command], input=stdin, capture_output=True, text=True, env=env, timeout=30
    )


def make_root(tmp_path, entry_body=None, with_shim=True):
    root = tmp_path / "plugin"
    (root / "hooks").mkdir(parents=True)
    if with_shim:
        shutil.copy(PKG / "hooks" / "run.sh", root / "hooks" / "run.sh")
    if entry_body is not None:
        (root / "hooks" / "context_guard.py").write_text(entry_body, encoding="utf-8")
    return root


@pytest.fixture(params=SHELLS)
def shell(request):
    return shell_path(request.param)


@pytest.fixture
def command():
    return hook_commands()[0]["command"]


def test_every_command_has_the_exact_form():
    cmds = hook_commands()
    assert cmds
    for hook in cmds:
        assert hook["type"] == "command"
        assert "args" not in hook
        command = hook["command"]
        assert command.startswith('sh "${CLAUDE_PLUGIN_ROOT}/hooks/run.sh" ')
        assert command.endswith(" || exit 1")


def test_only_user_prompt_submit_is_registered():
    assert list(HOOKS_JSON["hooks"]) == ["UserPromptSubmit"]


def test_shim_present_entry_present(shell, command):
    proc = run_command(shell, command, PKG)
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_entry_missing_exits_one_with_stderr_line(shell, command, tmp_path):
    root = make_root(tmp_path)
    proc = run_command(shell, command, root)
    assert proc.returncode == 1
    assert len(proc.stderr.strip().splitlines()) == 1


def test_shim_missing_exits_one_not_two(shell, command, tmp_path):
    root = make_root(tmp_path, with_shim=False)
    proc = run_command(shell, command, root)
    assert proc.returncode == 1


def test_entry_exiting_two_is_mapped_to_one(shell, command, tmp_path):
    root = make_root(tmp_path, "import sys\nsys.exit(2)\n")
    assert run_command(shell, command, root).returncode == 1


def test_other_exit_codes_pass_through(shell, command, tmp_path):
    root = make_root(tmp_path, "import sys\nsys.exit(3)\n")
    assert run_command(shell, BARE_COMMAND, root).returncode == 3


def test_stdin_reaches_the_entry(shell, command, tmp_path):
    root = make_root(tmp_path, "import sys\nsys.stdout.write(sys.stdin.read())\n")
    proc = run_command(shell, command, root, stdin='{"a": 1}')
    assert proc.stdout == '{"a": 1}'


def test_switch_off_is_silent_and_never_starts_python(shell, command, tmp_path):
    marker = tmp_path / "ran"
    root = make_root(tmp_path, "import sys\nopen(%r, 'w').close()\nsys.exit(2)\n" % str(marker))
    proc = run_command(shell, command, root, {"CLAUDE_CONTEXT_ROTATION": "off"})
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")
    assert not marker.exists()


def test_switch_off_wins_over_a_missing_entry(shell, command, tmp_path):
    root = make_root(tmp_path)
    proc = run_command(shell, command, root, {"CLAUDE_CONTEXT_ROTATION": "off"})
    assert (proc.returncode, proc.stderr) == (0, "")


def test_other_switch_values_do_not_disable(shell, command, tmp_path):
    root = make_root(tmp_path, "import sys\nsys.exit(3)\n")
    proc = run_command(shell, BARE_COMMAND, root, {"CLAUDE_CONTEXT_ROTATION": "OFF"})
    assert proc.returncode == 3


def test_missing_python3_is_exit_one_with_stderr(shell, command, tmp_path):
    root = make_root(tmp_path, "pass\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("sh", "dirname"):
        found = shutil.which(tool)
        if found:
            os.symlink(found, bindir / tool)
    proc = run_command(shell, command, root, {"PATH": str(bindir)})
    assert proc.returncode == 1
    assert "python3" in proc.stderr


def test_without_the_outer_guard_dash_would_exit_two(tmp_path):
    dash = shell_path("dash")
    missing = tmp_path / "gone" / "run.sh"
    bare = run_command(dash, 'dash "%s" context_guard' % missing, tmp_path)
    guarded = run_command(dash, 'dash "%s" context_guard || exit 1' % missing, tmp_path)
    assert bare.returncode == 2
    assert guarded.returncode == 1
