"""The T0 folder works for a T0: role fallback, anchored commands, playbook path in the hook.

R1  no role command starts with ``python3 scripts/``, ``bash scripts/`` or ``bin/vnx`` and none
    reads ``.vnx-data/`` relative to the cwd.
R2  anchored read-only role commands exit the same from the T0 folder as from the project root.
R3  Mandatory Startup names the playbook file and the Read tool, STOP only for a missing file.
R4  the first 1,024 bytes of the hook's additionalContext for a T0 carry the playbook path.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ROLE = REPO / ".claude" / "terminals" / "T0" / "role-orchestrator.md"
HOOK = REPO / "hooks" / "sessionstart.sh"
T0_DIR = REPO / ".claude" / "terminals" / "T0"
ANCHOR = '${VNX_HOME:-$(git rev-parse --show-toplevel)}'

UNANCHORED = re.compile(r'(?:^|[`\s(])(?:python3 scripts/|bash scripts/|bin/vnx\b)')
RELATIVE_STATE = re.compile(r'(?:cat|ls|tail|head)\s+\.?/?\.vnx-data/')


def _role() -> str:
    return ROLE.read_text(encoding="utf-8")


def _startup_section() -> str:
    text = _role()
    return text[text.index("## Mandatory Startup"):text.index("### Autonomous Execution")]


# ── R1 ───────────────────────────────────────────────────────────────────────


def test_r1_no_command_starts_unanchored():
    hits = [
        f"{n}: {line.strip()[:100]}"
        for n, line in enumerate(_role().splitlines(), 1)
        if UNANCHORED.search(line)
    ]
    assert not hits, "unanchored role commands:\n" + "\n".join(hits)


def test_r1_no_command_reads_vnx_data_relative_to_cwd():
    hits = [
        f"{n}: {line.strip()[:100]}"
        for n, line in enumerate(_role().splitlines(), 1)
        if RELATIVE_STATE.search(line)
    ]
    assert not hits, "relative state reads:\n" + "\n".join(hits)


def test_r1_the_anchor_is_in_use_and_scope_limit_is_stated():
    text = _role()
    assert text.count(ANCHOR) >= 17
    assert "consumer repo without `scripts/`" in text
    assert "VNX_HOME" in text


# ── R2 ───────────────────────────────────────────────────────────────────────

READ_ONLY_COMMANDS = [
    # the state-dir resolution behind the t0_state.json read
    f'python3 "{ANCHOR}/scripts/lib/vnx_paths.py"',
    f'python3 "{ANCHOR}/scripts/validate_skill.py" --list',
    f'bash "{ANCHOR}/scripts/commands/t0_role_audit.sh"',
]
# Left out: build_t0_state.py, reconcile_queue_state.py --repair, open_items_manager.py add|digest,
# runtime_core_cli.py (check-terminal, release-on-failure), receipt_query.py decide, and every
# `bin/vnx dispatch|pool`. They write state, repair, or dispatch; a test must not run them.


def _env(tmp_path: Path) -> dict:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("VNX_")}
    env["HOME"] = str(home)
    env["VNX_DATA_HOME"] = str(tmp_path / "vnx-data")
    return env


def _run_in(cwd: Path, cmd: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", cmd], cwd=str(cwd), env=env,
                          capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("cmd", READ_ONLY_COMMANDS)
def test_r2_command_is_in_the_role(cmd):
    assert cmd.split(" --list")[0] in _role()


@pytest.mark.parametrize("cmd", READ_ONLY_COMMANDS)
def test_r2_anchored_command_exits_the_same_from_the_t0_folder(tmp_path, cmd):
    env = _env(tmp_path)
    from_root = _run_in(REPO, cmd, env)
    from_t0 = _run_in(T0_DIR, cmd, env)
    assert from_root.returncode != 127, from_root.stderr
    assert from_t0.returncode == from_root.returncode, from_t0.stderr


def test_r2_the_state_read_resolves_the_central_store_from_the_t0_folder(tmp_path):
    env = _env(tmp_path)
    cmd = f'python3 "{ANCHOR}/scripts/lib/vnx_paths.py" | sed -n \'s/^VNX_STATE_DIR=//p\''
    out = _run_in(T0_DIR, cmd, env)
    assert out.returncode == 0, out.stderr
    state_dir = out.stdout.strip()
    assert state_dir.endswith("/state")
    assert not Path(state_dir).is_relative_to(T0_DIR)
    assert state_dir.startswith(str(tmp_path / "vnx-data"))


# ── R3 ───────────────────────────────────────────────────────────────────────


def test_r3_startup_fallback_reads_the_playbook_file():
    s = _startup_section()
    assert ".claude/skills/t0-orchestrator/SKILL.md" in s
    assert "Read tool" in s
    assert "absolute path" in s


def test_r3_stop_only_for_a_missing_file():
    s = _startup_section()
    assert "STOP only when that file does not exist" in s
    assert "Unknown skill" not in s
    assert "invoke `@t0-orchestrator`" not in s


# ── R4 ───────────────────────────────────────────────────────────────────────

SKILL_BODY = "---\nname: t0-orchestrator\n---\n\n# T0 Orchestrator\n\n" + ("padding line\n" * 800)


def _project(tmp_path: Path, *, with_skill: bool) -> Path:
    root = tmp_path / "project"
    (root / ".vnx").mkdir(parents=True)
    t0 = root / ".claude" / "terminals" / "T0"
    t0.mkdir(parents=True)
    (t0 / "CLAUDE.md").write_text("@role-orchestrator.md\n", encoding="utf-8")
    if with_skill:
        skill = root / ".claude" / "skills" / "t0-orchestrator"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(SKILL_BODY, encoding="utf-8")
    return root


def _context(root: Path, tmp_path: Path) -> str:
    result = subprocess.run(
        ["bash", str(HOOK)], cwd=str(root / ".claude" / "terminals" / "T0"),
        env=_env(tmp_path), capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]


def test_r4_the_first_kilobyte_carries_the_playbook_path(tmp_path):
    root = _project(tmp_path, with_skill=True)
    ctx = _context(root, tmp_path)
    playbook = root / ".claude" / "skills" / "t0-orchestrator" / "SKILL.md"
    assert str(playbook) in ctx.encode()[:1024].decode(errors="ignore")


def test_r4_a_missing_playbook_is_said_in_the_same_place(tmp_path):
    root = _project(tmp_path, with_skill=False)
    head = _context(root, tmp_path).encode()[:1024].decode(errors="ignore")
    assert "NOT FOUND" in head
    assert str(root / ".claude" / "skills" / "t0-orchestrator" / "SKILL.md") in head


def test_r4_the_injection_still_opens_with_its_header(tmp_path):
    root = _project(tmp_path, with_skill=True)
    assert _context(root, tmp_path).startswith("T0 Master Orchestrator Active")


# ── the static sources audit reads an anchored command as the path it names ─


def test_audit_still_flags_a_missing_script_behind_the_anchor(tmp_path):
    project = tmp_path / "project"
    role = project / ".claude" / "terminals" / "T0" / "role-orchestrator.md"
    role.parent.mkdir(parents=True)
    role.write_text(f'Run `python3 "{ANCHOR}/scripts/does_not_exist_xyz.py"`.\n', encoding="utf-8")
    (project / "scripts" / "lib").mkdir(parents=True)
    result = subprocess.run(
        ["python3", str(REPO / "scripts" / "lib" / "t0_role_sources_audit.py"), str(project), str(project)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 1
    assert "SCRIPT-MISSING" in result.stdout and "does_not_exist_xyz.py" in result.stdout


def test_audit_accepts_an_anchored_script_that_exists(tmp_path):
    project = tmp_path / "project"
    role = project / ".claude" / "terminals" / "T0" / "role-orchestrator.md"
    role.parent.mkdir(parents=True)
    role.write_text(f'Run `python3 "{ANCHOR}/scripts/real_one.py"`.\n', encoding="utf-8")
    (project / "scripts" / "lib").mkdir(parents=True)
    (project / "scripts" / "real_one.py").write_text("print(1)\n", encoding="utf-8")
    (project / "scripts" / "lib" / "t0_role_state_writers.txt").write_text("", encoding="utf-8")
    result = subprocess.run(
        ["python3", str(REPO / "scripts" / "lib" / "t0_role_sources_audit.py"), str(project), str(project)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout
