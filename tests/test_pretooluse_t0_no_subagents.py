"""Tests for scripts/hooks/pretooluse_t0_no_subagents.py.

Dispatch-ID: 20260924-t0-geen-subagents-en-rol-alarm

T0 orchestrates and never builds, and a Claude Code subagent started from T0
leaves no dispatch, no report and no receipt. The rule lived only in
role-orchestrator.md, so a T0 that did not load that file had no rule at all
(SEOcrawler_v2: 280 Agent calls in 13 T0 transcripts, measured 24-09).

The hook turns the rule into policy. What these tests pin, and why each one
exists:

1. It denies BOTH tool names. The subagent tool is ``Agent`` in current Claude
   Code (448 calls, zero ``Task`` in 143 real T0 transcripts) and ``Task`` in
   older ones. The T0 enforcer SEOcrawler_v2 already carried blocked only
   ``Task``, so it stopped nothing.
2. It denies in the form Claude Code honours on PreToolUse,
   ``hookSpecificOutput.permissionDecision``. That enforcer also exited 1, a
   non-blocking error, and read the tool name from an environment variable
   instead of the payload.
3. It fires for T0 ONLY. Project settings apply to every session in a repo,
   headless workers included, so a hook that also caught a worker would break
   dispatches. T0 is recognised by the session's cwd AND by the launch
   directory encoded in transcript_path, because the current cwd is not stable
   (3 of 143 real T0 sessions also carry ``/Users/vincentvandeth`` as cwd).
4. Other tools are left completely alone.
5. The three settings templates ship it, the merge engine writes it into a
   project without touching the project's own settings, and the command string
   that lands on disk actually works when executed.

Isolation: nothing here reads or writes the real ``~/.claude``, a real project's
settings or the central store. Every subprocess runs with HOME pointed at a tmp
dir, and every project is a tmp_path.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "scripts" / "hooks" / "pretooluse_t0_no_subagents.py"
HOOK_REL = "scripts/hooks/pretooluse_t0_no_subagents.py"
TEMPLATES = REPO_ROOT / "templates"
VNX_KEYS_TMPL = TEMPLATES / "settings_vnx_keys.json.tmpl"
INIT_DEFAULT_J2 = TEMPLATES / "init" / "default" / "settings.json.j2"
INIT_MINIMAL_J2 = TEMPLATES / "init" / "minimal" / "settings.json.j2"

DENY_MESSAGE = "T0 gebruikt geen subagents: stage een dispatch via `vnx dispatch`"

# Launch directory of a real T0 session, as Claude Code encodes it for the
# transcript path (every non-alphanumeric character becomes "-").
T0_CWD = "/Users/someone/Development/proj/.claude/terminals/T0"
T0_TRANSCRIPT = "/Users/someone/.claude/projects/-Users-someone-Development-proj--claude-terminals-T0/abc.jsonl"

WORKER_CWD = "/Users/someone/Development/proj/.vnx-data/worktrees/dispatch-20260924-x"
WORKER_TRANSCRIPT = (
    "/Users/someone/.claude/projects/"
    "-Users-someone-Development-proj--vnx-data-worktrees-dispatch-20260924-x/abc.jsonl"
)


def _payload(tool: str, cwd: str | None, transcript: str | None) -> str:
    body: dict = {"tool_name": tool, "tool_input": {"prompt": "do a thing"}, "session_id": "s-1"}
    if cwd is not None:
        body["cwd"] = cwd
    if transcript is not None:
        body["transcript_path"] = transcript
    return json.dumps(body)


def _isolated_env(tmp_path: Path) -> dict:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("VNX_")}
    env["HOME"] = str(home)
    return env


def _run_hook(payload: str, tmp_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        env=_isolated_env(tmp_path),
        timeout=10,
    )


def _assert_denied(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    decision = out["hookSpecificOutput"]
    assert decision["hookEventName"] == "PreToolUse"
    assert decision["permissionDecision"] == "deny"
    assert decision["permissionDecisionReason"] == DENY_MESSAGE
    assert "decision" not in out, "the flat deprecated form must not be emitted"


def _assert_untouched(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, result.stderr
    assert result.stdout == "", f"the hook must stay silent, it printed: {result.stdout!r}"


# ── the block, in a T0 session ────────────────────────────────────────────────


@pytest.mark.parametrize("tool", ["Agent", "Task"])
def test_t0_session_is_denied_for_both_tool_names(tool, tmp_path):
    _assert_denied(_run_hook(_payload(tool, T0_CWD, T0_TRANSCRIPT), tmp_path))


def test_t0_recognised_by_cwd_alone(tmp_path):
    _assert_denied(_run_hook(_payload("Agent", T0_CWD, None), tmp_path))


def test_t0_recognised_below_the_terminal_dir(tmp_path):
    _assert_denied(_run_hook(_payload("Agent", T0_CWD + "/scratch/deeper", None), tmp_path))


def test_t0_still_denied_after_the_cwd_drifted_out_of_the_terminal_dir(tmp_path):
    """3 of 143 real T0 sessions also carry /Users/<user> as cwd. The launch
    directory in transcript_path is what keeps that moment closed."""
    _assert_denied(_run_hook(_payload("Agent", "/Users/someone", T0_TRANSCRIPT), tmp_path))


def test_t0_recognised_by_transcript_alone(tmp_path):
    _assert_denied(_run_hook(_payload("Task", None, T0_TRANSCRIPT), tmp_path))


def test_deny_output_is_a_single_json_object_on_stdout(tmp_path):
    result = _run_hook(_payload("Agent", T0_CWD, T0_TRANSCRIPT), tmp_path)
    assert result.stdout.count("\n") == 1 and result.stdout.endswith("\n")
    assert result.stderr == ""


# ── the hook must not touch anything that is not T0 ──────────────────────────


@pytest.mark.parametrize("tool", ["Agent", "Task"])
@pytest.mark.parametrize(
    "cwd, transcript",
    [
        pytest.param(WORKER_CWD, WORKER_TRANSCRIPT, id="headless-worker-worktree"),
        pytest.param("/Users/someone/Development/proj", "/Users/someone/.claude/projects/-Users-someone-Development-proj/a.jsonl", id="project-root"),
        pytest.param("/Users/someone/Development/proj/.claude/terminals/T1", "/Users/someone/.claude/projects/-Users-someone-Development-proj--claude-terminals-T1/a.jsonl", id="T1-terminal"),
        pytest.param("/Users/someone/Development/proj/.claude/terminals/T2", None, id="T2-terminal-cwd-only"),
        pytest.param("/Users/someone/Development/proj/.claude/terminals/T3", None, id="T3-terminal-cwd-only"),
        pytest.param(
            "/private/tmp/claude-501/-Users-someone-Development-proj--claude-terminals-T0/6da8164a/scratchpad/hooktest-a",
            "/Users/someone/.claude/projects/-private-tmp-claude-501--Users-someone-Development-proj--claude-terminals-T0-6da8164a-scratchpad-hooktest-a/a.jsonl",
            id="scratch-session-T0-started-from-its-scratchpad",
        ),
        pytest.param("/Users/someone/Development/T0", None, id="a-dir-called-T0-outside-terminals"),
        pytest.param("/Users/someone/Development/proj/.claude/terminals/T0x", None, id="T0x-is-not-T0"),
        pytest.param("/Users/someone/Development/proj/terminals/T0", None, id="terminals-T0-without-dot-claude"),
    ],
)
def test_non_t0_session_is_left_alone(tool, cwd, transcript, tmp_path):
    _assert_untouched(_run_hook(_payload(tool, cwd, transcript), tmp_path))


def test_payload_without_cwd_and_transcript_is_left_alone(tmp_path):
    _assert_untouched(_run_hook(_payload("Agent", None, None), tmp_path))


# ── other tools are never touched, not even in T0 ────────────────────────────


@pytest.mark.parametrize(
    "tool",
    [
        "Bash", "Read", "Edit", "Write", "Grep", "Glob", "Skill", "SendMessage", "Monitor",
        # Same word, different tools: background tasks and their todo list. The
        # matcher in settings must not sweep these in, and neither may the hook.
        "TaskCreate", "TaskUpdate", "TaskStop", "TaskOutput", "ListAgents",
        "mcp__supabase__execute_sql",
    ],
)
def test_other_tools_in_t0_are_left_alone(tool, tmp_path):
    _assert_untouched(_run_hook(_payload(tool, T0_CWD, T0_TRANSCRIPT), tmp_path))


@pytest.mark.parametrize(
    "raw",
    ["", "   \n", "not json at all", "[]", "null", '"Agent"', "42", '{"tool_input": {}}'],
)
def test_input_that_is_not_a_tool_call_payload_is_left_alone(raw, tmp_path):
    _assert_untouched(_run_hook(raw, tmp_path))


def test_a_non_string_cwd_or_transcript_does_not_crash_the_hook(tmp_path):
    payload = json.dumps({"tool_name": "Agent", "cwd": 5, "transcript_path": ["x"]})
    _assert_untouched(_run_hook(payload, tmp_path))


# ── shipping: the three templates ─────────────────────────────────────────────


def _render_vnx_keys(engine_root: Path, project_root: Path) -> dict:
    scripts_dir = REPO_ROOT / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import vnx_settings_merge

    return vnx_settings_merge.load_template(str(engine_root), str(project_root))


def _render_j2(path: Path) -> Callable[[Path, Path], dict]:
    def _render(engine_root: Path, project_root: Path) -> dict:
        from jinja2 import Environment, FileSystemLoader, StrictUndefined

        env = Environment(
            loader=FileSystemLoader(str(path.parent)),
            undefined=StrictUndefined,
            keep_trailing_newline=True,
        )
        rendered = env.get_template(path.name).render(
            project_name=project_root.name,
            project_id="guard-project",
            vnx_version="0.0.0-test",
            engine_root=str(engine_root),
        )
        return json.loads(rendered)

    return _render


SETTINGS_TEMPLATES: List[Tuple[str, Callable[[Path, Path], dict]]] = [
    ("settings_vnx_keys.json.tmpl", _render_vnx_keys),
    ("init/default/settings.json.j2", _render_j2(INIT_DEFAULT_J2)),
    ("init/minimal/settings.json.j2", _render_j2(INIT_MINIMAL_J2)),
]
TEMPLATE_IDS = [label for label, _ in SETTINGS_TEMPLATES]


def _guard_groups(settings: dict) -> List[dict]:
    """PreToolUse groups that register this hook."""
    return [
        group
        for group in (settings.get("hooks") or {}).get("PreToolUse", [])
        if any(HOOK.name in str(h.get("command") or "") for h in group.get("hooks", []))
    ]


def _guard_command(settings: dict) -> str:
    groups = _guard_groups(settings)
    assert len(groups) == 1, f"expected exactly one group registering the T0 subagent block, got {groups!r}"
    commands = [h["command"] for h in groups[0]["hooks"] if h.get("type") == "command"]
    assert len(commands) == 1, commands
    return commands[0]


def _run_command(command: str, payload: str, tmp_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", command],
        input=payload,
        capture_output=True,
        text=True,
        env=_isolated_env(tmp_path),
        timeout=10,
    )


@pytest.mark.parametrize("render", [r for _, r in SETTINGS_TEMPLATES], ids=TEMPLATE_IDS)
def test_every_template_registers_the_block_for_both_tool_names(render, tmp_path):
    settings = render(REPO_ROOT, tmp_path / "project")
    (group,) = _guard_groups(settings)
    tools = set(group["matcher"].split("|"))
    assert {"Agent", "Task"} <= tools, group["matcher"]
    assert "TaskCreate" not in tools and "Bash" not in tools


@pytest.mark.parametrize("render", [r for _, r in SETTINGS_TEMPLATES], ids=TEMPLATE_IDS)
def test_every_template_keeps_the_existing_bash_guard(render, tmp_path):
    """The new group must sit next to the raw-spawn guard, not replace it."""
    settings = render(REPO_ROOT, tmp_path / "project")
    matchers = [g["matcher"] for g in settings["hooks"]["PreToolUse"]]
    assert "Bash" in matchers and any("Agent" in m for m in matchers)


@pytest.mark.parametrize("render", [r for _, r in SETTINGS_TEMPLATES], ids=TEMPLATE_IDS)
def test_the_rendered_command_points_at_a_file_that_exists(render, tmp_path):
    command = _guard_command(render(REPO_ROOT, tmp_path / "project"))
    assert str(REPO_ROOT / HOOK_REL) in command
    assert (REPO_ROOT / HOOK_REL).is_file()
    assert "{{" not in command


@pytest.mark.parametrize("render", [r for _, r in SETTINGS_TEMPLATES], ids=TEMPLATE_IDS)
def test_the_rendered_command_denies_in_t0_and_stays_silent_for_a_worker(render, tmp_path):
    """Executes the exact string that lands in a project's settings.json, with
    its own quoting, so a template that renders to a dead command is caught."""
    command = _guard_command(render(REPO_ROOT, tmp_path / "project"))

    _assert_denied(_run_command(command, _payload("Agent", T0_CWD, T0_TRANSCRIPT), tmp_path))
    _assert_untouched(_run_command(command, _payload("Agent", WORKER_CWD, WORKER_TRANSCRIPT), tmp_path))
    _assert_untouched(_run_command(command, _payload("Bash", T0_CWD, T0_TRANSCRIPT), tmp_path))


# ── shipping: regen-settings --merge on a tmp project ────────────────────────


def _git_init(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=path, check=True)
    return path.resolve()


def _make_install(tmp_path: Path, *, with_hook: bool = False) -> Path:
    """A standalone install carrying exactly what `vnx regen-settings` touches.
    The hook script is only shipped along when the test executes the generated
    command: the merge itself never opens it."""
    install = _git_init(tmp_path / "vnx-install")
    files = [
        "bin/vnx",
        "scripts/lib/vnx_paths.sh",
        "scripts/commands/regen_settings.sh",
        "scripts/vnx_settings_merge.py",
        "templates/settings_vnx_keys.json.tmpl",
    ]
    if with_hook:
        files.append(HOOK_REL)
    for rel in files:
        dest = install / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO_ROOT / rel, dest)
    os.chmod(install / "bin" / "vnx", 0o755)
    (install / ".vnx-install-mode").write_text("central\n", encoding="utf-8")
    return install


def _regen_merge(install: Path, project: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(install / "bin" / "vnx"), "regen-settings", "--merge", "--no-backup"],
        cwd=project,
        env=_isolated_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
    )


USER_SETTINGS = {
    "model": "opus",
    "env": {"MY_PROJECT_VAR": "keep-me"},
    "permissions": {
        "allow": ["Bash(make *)"],
        "ask": ["Bash(git push*)"],
        "additionalDirectories": ["../shared"],
    },
    "statusLine": {"type": "command", "command": "echo status"},
}


def test_regen_merge_writes_the_block_and_keeps_the_projects_own_settings(tmp_path):
    install = _make_install(tmp_path)
    project = _git_init(tmp_path / "project")
    settings_path = project / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps(USER_SETTINGS), encoding="utf-8")

    result = _regen_merge(install, project, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr

    merged = json.loads(settings_path.read_text(encoding="utf-8"))

    # the block is there, pointing into the install, for both tool names
    (group,) = _guard_groups(merged)
    assert {"Agent", "Task"} <= set(group["matcher"].split("|"))
    assert str(install / HOOK_REL) in _guard_command(merged)

    # the project's own settings survived the merge
    assert merged["model"] == "opus"
    assert merged["statusLine"] == USER_SETTINGS["statusLine"]
    assert merged["env"]["MY_PROJECT_VAR"] == "keep-me"
    assert "Bash(make *)" in merged["permissions"]["allow"]
    assert merged["permissions"]["ask"] == ["Bash(git push*)"]
    assert merged["permissions"]["additionalDirectories"] == ["../shared"]


def test_regen_merge_is_idempotent_for_the_block(tmp_path):
    install = _make_install(tmp_path)
    project = _git_init(tmp_path / "project")
    for _ in range(2):
        result = _regen_merge(install, project, tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
    merged = json.loads((project / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert len(_guard_groups(merged)) == 1


def test_the_generated_settings_command_blocks_t0_and_spares_a_worker(tmp_path):
    install = _make_install(tmp_path, with_hook=True)
    project = _git_init(tmp_path / "project")
    assert _regen_merge(install, project, tmp_path).returncode == 0
    merged = json.loads((project / ".claude" / "settings.json").read_text(encoding="utf-8"))
    command = _guard_command(merged)

    t0_cwd = str(project / ".claude" / "terminals" / "T0")
    _assert_denied(_run_command(command, _payload("Agent", t0_cwd, None), tmp_path))
    _assert_untouched(_run_command(command, _payload("Agent", str(project), None), tmp_path))
