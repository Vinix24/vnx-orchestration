"""tests/test_regen_settings_replace_hooks.py — OI-2410 (PR #2288 round 3).

MEASURED 2026-10-27 on head ``3292047d``: ``docs/core/12_PERMISSION_SETTINGS.md``
and ``changelog.d/dlv-36aff85f5f7c.md`` both present
``vnx regen-settings --merge --replace-hooks`` as the explicit path back to the
old replace-every-hook behaviour, and ``scripts/vnx_settings_merge.py``
implements the flag — but ``scripts/commands/regen_settings.sh`` (the wrapper
behind ``vnx regen-settings``) never grew a ``--replace-hooks`` case arm and
rejected it with ``Unknown option: --replace-hooks`` (exit 1). A documented
command that exits 1 is a docs/CLI contract violation.

These tests run the REAL wrapper (``cmd_regen_settings`` sourced from the
shipped file, not reimplemented) against a fixture project under ``tmp_path``:

  * the flag reaches the merge script (a recording stub as the engine);
  * through the REAL engine (the repo's ``vnx_settings_merge.py``) the dry run
    reports ``mode: replace-hooks`` and drops the project hook, while the same
    run without the flag keeps it;
  * ``--help`` describes both behaviours and no longer claims hooks are
    "Replaced entirely";
  * ``--replace-hooks`` outside ``--merge`` is refused with a reason.

No real ``settings.json``, ``~/.claude`` or install tree is ever written.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER = REPO_ROOT / "scripts" / "commands" / "regen_settings.sh"
MERGE_SCRIPT = REPO_ROOT / "scripts" / "vnx_settings_merge.py"

STALE_VNX_COMMAND = (
    "bash /home/tester/.vnx-system/versions/v1.7.1/scripts/hooks/"
    "pretooluse_block_raw_claude_spawn.sh"
)
PROJECT_COMMAND = "$CLAUDE_PROJECT_DIR/.claude/hooks/t0-readonly-enforcer.sh"


def _fixture_project(tmp_path: Path) -> Path:
    """A project whose settings.json holds one VNX hook and one project hook."""
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    settings = {
        "env": {"VNX_CONTEXT_ROTATION_ENABLED": "0", "PROJECT_FLAG": "1"},
        "permissions": {"allow": ["Bash(ls)"], "deny": [], "ask": ["Bash(git push*)"]},
        "hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [
                {"type": "command", "command": STALE_VNX_COMMAND, "timeout": 5}]},
            {"matcher": "Write|Edit", "hooks": [
                {"type": "command", "command": PROJECT_COMMAND, "timeout": 5}]},
        ]},
        "_vnx_meta": {"version": "1.0", "managed_keys": ["hooks"]},
    }
    (project / ".claude" / "settings.json").write_text(
        json.dumps(settings, indent=2), encoding="utf-8")
    return project


def _recording_vnx_home(tmp_path: Path) -> tuple[Path, Path]:
    """A VNX_HOME whose merge script only records the argv it was handed."""
    vnx_home = tmp_path / "vnx-home"
    (vnx_home / "scripts").mkdir(parents=True)
    record = tmp_path / "merge-argv.json"
    (vnx_home / "scripts" / "vnx_settings_merge.py").write_text(
        "import json, os, sys\n"
        "with open(os.environ['RECORD_PATH'], 'w', encoding='utf-8') as handle:\n"
        "    json.dump(sys.argv[1:], handle)\n",
        encoding="utf-8",
    )
    return vnx_home, record


def _real_engine_vnx_home(tmp_path: Path) -> Path:
    """A VNX_HOME whose scripts/templates point at the repo's real engine.

    ``scripts`` is assembled from links rather than copied so the test exercises
    the shipped ``vnx_settings_merge.py`` and the shipped template, and so no
    ``vnx_worker_permissions_merge.py`` co-run is set up behind the test's back.
    """
    vnx_home = tmp_path / "vnx-home"
    (vnx_home / "scripts").mkdir(parents=True)
    (vnx_home / "scripts" / "vnx_settings_merge.py").symlink_to(MERGE_SCRIPT)
    (vnx_home / "scripts" / "lib").symlink_to(REPO_ROOT / "scripts" / "lib")
    (vnx_home / "templates").symlink_to(REPO_ROOT / "templates")
    return vnx_home


def _clean_env(**extra: str) -> dict:
    env = {key: value for key, value in os.environ.items() if not key.startswith("VNX_")}
    env.update(extra)
    return env


def _run_wrapper(vnx_home: Path, project: Path, tmp_path: Path, *args: str,
                 extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """Source the real wrapper function and call it with ``args``.

    The PR-3 guard collaborator is stubbed out: it is not what these tests pin,
    and its own contract is covered by test_wave4_regen_settings_target.py.
    """
    program = f"""
set -u
source "{WRAPPER}"
err() {{ printf '%s\\n' "$*" >&2; }}
log() {{ printf '%s\\n' "$*"; }}
PROJECT_ROOT="{project}"
VNX_HOME="{vnx_home}"
_guard_not_vnx_home() {{ return 0; }}
cmd_regen_settings "$@"
printf 'RC=%s\\n' "$?"
"""
    env = _clean_env()
    env["HOME"] = str(tmp_path / "home")
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", "-c", program, "regen", *args],
        capture_output=True, text=True, env=env,
    )


def _json_before_rc(stdout: str) -> dict:
    """The dry-run JSON, without the wrapper's trailing RC line."""
    body = stdout.split("RC=")[0]
    return json.loads(body[body.index("{"):])


def _commands(hooks: dict) -> list:
    return [handler.get("command")
            for groups in hooks.values() if isinstance(groups, list)
            for group in groups if isinstance(group, dict)
            for handler in group.get("hooks") or [] if isinstance(handler, dict)]


# ── the flag reaches the merge script ────────────────────────────────────────
def test_replace_hooks_reaches_the_merge_script(tmp_path):
    vnx_home, record = _recording_vnx_home(tmp_path)
    project = _fixture_project(tmp_path)

    result = _run_wrapper(vnx_home, project, tmp_path,
                          "--merge", "--replace-hooks", "--dry-run",
                          extra_env={"RECORD_PATH": str(record)})

    assert "RC=0" in result.stdout, result.stdout + result.stderr
    assert "Unknown option" not in result.stderr
    argv = json.loads(record.read_text(encoding="utf-8"))
    assert "--merge" in argv
    assert "--dry-run" in argv
    assert "--replace-hooks" in argv


def test_the_flag_is_not_forwarded_when_it_was_not_given(tmp_path):
    """The positive assertion above must be discriminating."""
    vnx_home, record = _recording_vnx_home(tmp_path)
    project = _fixture_project(tmp_path)

    result = _run_wrapper(vnx_home, project, tmp_path, "--merge", "--dry-run",
                          extra_env={"RECORD_PATH": str(record)})

    assert "RC=0" in result.stdout, result.stdout + result.stderr
    assert "--replace-hooks" not in json.loads(record.read_text(encoding="utf-8"))


# ── through the real engine: the flag changes the merge behaviour ────────────
def test_replace_hooks_dry_run_replaces_every_hook_through_the_real_engine(tmp_path):
    vnx_home = _real_engine_vnx_home(tmp_path)
    project = _fixture_project(tmp_path)

    result = _run_wrapper(vnx_home, project, tmp_path,
                          "--merge", "--replace-hooks", "--dry-run", "--json")

    assert "RC=0" in result.stdout, result.stdout + result.stderr
    payload = _json_before_rc(result.stdout)
    assert payload["_vnx_hook_report"]["mode"] == "replace-hooks"
    assert PROJECT_COMMAND not in _commands(payload["hooks"])
    # A dry run writes nothing.
    written = json.loads((project / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert PROJECT_COMMAND in _commands(written["hooks"])


def test_plain_merge_through_the_real_engine_keeps_the_project_hook(tmp_path):
    """Control: the same fixture without the flag keeps the project hook."""
    vnx_home = _real_engine_vnx_home(tmp_path)
    project = _fixture_project(tmp_path)

    result = _run_wrapper(vnx_home, project, tmp_path, "--merge", "--dry-run", "--json")

    assert "RC=0" in result.stdout, result.stdout + result.stderr
    payload = _json_before_rc(result.stdout)
    assert payload["_vnx_hook_report"]["mode"] == "merge"
    assert PROJECT_COMMAND in _commands(payload["hooks"])


# ── help text: both behaviours documented, the stale line gone ───────────────
def test_help_documents_both_hook_behaviours(tmp_path):
    result = _run_wrapper(tmp_path, tmp_path / "project", tmp_path, "--help")

    assert "RC=0" in result.stdout, result.stdout + result.stderr
    help_text = result.stdout
    assert "--replace-hooks" in help_text
    # Default --merge: VNX hooks refreshed, project hooks kept.
    assert "VNX-owned handlers refreshed in place, project handlers kept" in help_text
    # --replace-hooks: the old full replacement.
    assert "replaces the whole hooks block" in help_text
    assert "project hooks are dropped" in help_text
    # The stale claim that --merge replaces hooks wholesale must be gone.
    assert "Replaced entirely" not in help_text
    assert "hooks        Replaced entirely" not in help_text
    assert result.returncode == 0


# ── the flag only applies to --merge ─────────────────────────────────────────
@pytest.mark.parametrize("mode", ["--full", "--validate"])
def test_replace_hooks_outside_merge_is_refused(tmp_path, mode):
    vnx_home = _real_engine_vnx_home(tmp_path)
    project = _fixture_project(tmp_path)

    result = _run_wrapper(vnx_home, project, tmp_path, mode, "--replace-hooks", "--dry-run")

    assert "RC=1" in result.stdout, result.stdout + result.stderr
    assert "--replace-hooks only applies to --merge" in result.stderr
