"""tests/test_settings_template_hook_contract.py: OI-1816.

MEASURED 2026-09-23 on main. ``templates/settings_vnx_keys.json.tmpl`` shipped a
UserPromptSubmit hook whose fallback branch was

    else echo '{"decision": "allow"}'; fi

That branch fires wherever ``$PWD`` does not end in T0..T3, so in every worktree.
Two things are wrong with it:

1. A top-level ``decision`` is the deprecated hook shape. The current contract
   is ``hookSpecificOutput``, or exit 0 with empty stdout for a no-op.
2. Stdout of a UserPromptSubmit hook is either parsed as a decision or put
   verbatim into the model's context. The visible result was the literal text
   ``{"decision": "allow"}`` in the context of every worker.

sales-copilot caught it with its own ``test_claude_hook_contract.py`` after a
v1.6.2 sync. A project without such a test gets it silently, so the guard lives
here, at the source.

The two scripts the hook routes to (``userpromptsubmit_intelligence_inject.sh``
for T0, ``userpromptsubmit_worker_intelligence_inject.sh`` for T1..T3) carried
the same shape on every stdout path, so they are held to the same contract:
silence for a no-op, ``hookSpecificOutput.additionalContext`` for context.
Both scripts are executed for real; only the collaborator that feeds the worker
script (``gather_intelligence.py``) is a fixture.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
TEMPLATES = REPO_ROOT / "templates"
VNX_KEYS_TMPL = TEMPLATES / "settings_vnx_keys.json.tmpl"
INIT_DEFAULT_J2 = TEMPLATES / "init" / "default" / "settings.json.j2"
INIT_MINIMAL_J2 = TEMPLATES / "init" / "minimal" / "settings.json.j2"

T0_INJECT = "userpromptsubmit_intelligence_inject.sh"
WORKER_INJECT = "userpromptsubmit_worker_intelligence_inject.sh"

# A top-level "decision" key inside a hook command string: the shape of an
# inline ``echo '{"decision": "allow"}'``.
_DECISION_KEY = re.compile(r"""["']decision["']\s*:""")


# ─────────────────────────────────────────────────────────────────────────
# Rendering: each template has its own substitution mechanism. Render it the
# way its real installer does, so the guard judges what actually lands on disk.
# ─────────────────────────────────────────────────────────────────────────


def _render_vnx_keys(engine_root: Path, project_root: Path) -> dict:
    """Render through the REAL merge-engine loader (it also runs the OI-1139
    render guard). The loader reads ``<engine_root>/templates/...``."""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
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


def _command_hooks(settings: dict) -> List[Tuple[str, str]]:
    """Every (event, command) pair the rendered settings register."""
    found: List[Tuple[str, str]] = []
    for event, groups in (settings.get("hooks") or {}).items():
        for group in groups:
            for hook in group.get("hooks", []):
                if hook.get("type") == "command":
                    found.append((event, str(hook.get("command") or "")))
    return found


def _decision_emitters(settings: dict) -> List[Tuple[str, str]]:
    return [(event, cmd) for event, cmd in _command_hooks(settings) if _DECISION_KEY.search(cmd)]


def _user_prompt_submit_command(settings: dict) -> str:
    """The terminal-routing intelligence-inject hook. Other UserPromptSubmit hooks may sit
    next to it (the T0 context guard, dispatch 20260925-t0-context-rotation-enforced); the
    guard is held to the same no-decision contract by the test above, so this selects by
    what the hook routes to rather than by count."""
    commands = [
        cmd for event, cmd in _command_hooks(settings)
        if event == "UserPromptSubmit" and (T0_INJECT in cmd or WORKER_INJECT in cmd)
    ]
    assert len(commands) == 1, f"expected exactly one intelligence-inject UserPromptSubmit hook, got {commands!r}"
    return commands[0]


def _bash(command: str, cwd: Path, env: Dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run a rendered hook command the way the harness does: through a shell,
    with only the environment the test hands it."""
    base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(cwd)}
    base.update(env or {})
    return subprocess.run(
        ["bash", "-c", command], cwd=cwd, env=base, capture_output=True, text=True, timeout=60
    )


# ─────────────────────────────────────────────────────────────────────────
# Guard (a): no command hook in any settings template emits a top-level
# decision object.
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("label,renderer", SETTINGS_TEMPLATES, ids=TEMPLATE_IDS)
def test_no_command_hook_emits_a_top_level_decision(label, renderer, tmp_path):
    settings = renderer(REPO_ROOT, tmp_path / "project")
    assert _command_hooks(settings), f"{label} ships no command hooks: nothing was checked"

    offenders = _decision_emitters(settings)
    assert not offenders, (
        f"{label} ships a hook that writes a top-level decision object to stdout: "
        + "; ".join(f"{event}: {cmd}" for event, cmd in offenders)
        + ". A top-level decision is the deprecated hook shape, and a "
        "UserPromptSubmit stdout lands in the model's context. Use exit 0 with "
        "empty stdout for a no-op, or hookSpecificOutput for a decision or context "
        "(OI-1816)."
    )


def test_guard_flags_the_old_userpromptsubmit_fallback():
    """Negative control: the sweep must go red on the real defect, not accept
    everything."""
    old = (
        "if [[ \"$PWD\" == */T0 ]]; then /x/t0.sh; else echo '{\"decision\": \"allow\"}'; fi"
    )
    settings = {"hooks": {"UserPromptSubmit": [{"matcher": "*", "hooks": [
        {"type": "command", "command": old}
    ]}]}}
    assert _decision_emitters(settings) == [("UserPromptSubmit", old)]

    fixed = old.replace("echo '{\"decision\": \"allow\"}'", "exit 0")
    settings["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"] = fixed
    assert _decision_emitters(settings) == []


# ─────────────────────────────────────────────────────────────────────────
# Guard (b): the rendered UserPromptSubmit hook is a silent no-op outside the
# terminal directories.
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "relative_cwd",
    [
        ".vnx-data/worktrees/dispatch-20260923-example",
        ".",
        "terminals/T10",
    ],
    ids=["worktree", "project-root", "T10-is-not-T1"],
)
def test_userpromptsubmit_fallback_is_a_silent_noop_outside_terminals(relative_cwd, tmp_path):
    project = tmp_path / "project"
    cwd = project / relative_cwd
    cwd.mkdir(parents=True)

    command = _user_prompt_submit_command(_render_vnx_keys(REPO_ROOT, project))
    result = _bash(command, cwd)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "", (
        f"the UserPromptSubmit fallback wrote {result.stdout!r} to stdout in {relative_cwd!r}. "
        "That text ends up in the model's context."
    )


def _stage_engine(tmp_path: Path) -> Path:
    """An engine tree with the REAL template and two routing targets that only
    print which one ran. This asserts the routing, so it must not run the
    real intelligence scripts against the live central store."""
    engine = tmp_path / "engine"
    (engine / "templates").mkdir(parents=True)
    shutil.copy(VNX_KEYS_TMPL, engine / "templates" / VNX_KEYS_TMPL.name)
    (engine / "scripts").mkdir()
    for name in (T0_INJECT, WORKER_INJECT):
        target = engine / "scripts" / name
        target.write_text(f"#!/usr/bin/env bash\nprintf 'routed:{name}'\n", encoding="utf-8")
        target.chmod(0o755)
    return engine


@pytest.mark.parametrize(
    "terminal,expected",
    [("T0", T0_INJECT), ("T1", WORKER_INJECT), ("T2", WORKER_INJECT), ("T3", WORKER_INJECT)],
)
def test_userpromptsubmit_still_routes_terminals_to_their_script(terminal, expected, tmp_path):
    """The fix touches the else branch only: T0 and T1..T3 must keep reaching
    their own injection script."""
    engine = _stage_engine(tmp_path)
    cwd = tmp_path / "project" / ".claude" / "terminals" / terminal
    cwd.mkdir(parents=True)

    command = _user_prompt_submit_command(_render_vnx_keys(engine, tmp_path / "project"))
    result = _bash(command, cwd)

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"routed:{expected}"


def test_userpromptsubmit_command_routes_to_exactly_the_scripts_under_test():
    """The behavioural tests below cover these two scripts. A third script added
    to the hook must be covered too, so this fails until it is."""
    command = _user_prompt_submit_command(_render_vnx_keys(REPO_ROOT, REPO_ROOT))
    invoked = {Path(token).name for token in re.findall(r"\S+\.sh", command)}
    assert invoked == {T0_INJECT, WORKER_INJECT}


# ─────────────────────────────────────────────────────────────────────────
# Guard (c): the two scripts obey the same contract on every stdout path.
# ─────────────────────────────────────────────────────────────────────────


def _assert_context_payload(stdout: str) -> str:
    """The only JSON a UserPromptSubmit hook may write: hookSpecificOutput with
    additionalContext. Returns the context text."""
    payload = json.loads(stdout)
    assert "decision" not in payload, f"top-level decision on stdout: {stdout!r}"
    assert set(payload) == {"hookSpecificOutput"}, f"unexpected top-level keys: {sorted(payload)}"
    specific = payload["hookSpecificOutput"]
    assert specific["hookEventName"] == "UserPromptSubmit"
    assert specific["additionalContext"], "empty additionalContext should have been a silent no-op"
    return specific["additionalContext"]


def test_t0_script_is_silent_when_nothing_changed(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    result = subprocess.run(
        ["bash", str(SCRIPTS / T0_INJECT)],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path), "VNX_STATE_DIR": str(state)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_t0_script_emits_hook_specific_output_once_then_goes_silent(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "t0_recommendations.json").write_text(
        json.dumps({
            "total_recommendations": 1,
            "recommendations": [{"trigger": "receipt-arrived", "gate": "gate_x", "action": "review"}],
        }),
        encoding="utf-8",
    )
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path), "VNX_STATE_DIR": str(state)}

    def run() -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(SCRIPTS / T0_INJECT)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60
        )

    first = run()
    assert first.returncode == 0, first.stderr
    context = _assert_context_payload(first.stdout)
    assert "Recommendations Available" in context
    assert "receipt-arrived: gate_x" in context

    second = run()
    assert second.returncode == 0, second.stderr
    assert second.stdout == "", "an unchanged digest must be a silent no-op, not a decision object"


_CANNED_INTELLIGENCE = {
    "pattern_count": 1,
    "suggested_patterns": [{"title": "Atomic writes", "description": "write to tmp then os.replace"}],
    "prevention_rule_count": 1,
    "prevention_rules": [{"rule": "No bare open(w)", "recommendation": "use the atomic helper"}],
    "session_insights": ["prior round flagged a missing null guard"],
    "offered_pattern_hashes": ["abc123"],
    "dispatch_blocked": False,
}


def _stage_worker_engine(tmp_path: Path, intelligence: dict) -> Tuple[Path, Path]:
    """The REAL worker script in its own engine tree. Only gather_intelligence.py
    is a fixture: it prints a canned payload instead of querying the live
    quality database. The script derives its project root from its own
    location, so the dispatch file lives inside this tree."""
    engine = tmp_path / "engine"
    (engine / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPTS / WORKER_INJECT, engine / "scripts" / WORKER_INJECT)
    (engine / "scripts" / "gather_intelligence.py").write_text(
        "import json\nprint(json.dumps(" + repr(intelligence) + "))\n", encoding="utf-8"
    )
    dispatch_id = "20260923-contract-fixture"
    active = engine / ".vnx-data" / "dispatches" / "active"
    active.mkdir(parents=True)
    (active / f"{dispatch_id}.md").write_text(
        "Gate: gate_contract\nRole: backend-developer\nInstruction:\nFix the hook contract\n",
        encoding="utf-8",
    )
    state = tmp_path / "state"
    state.mkdir()
    (state / "terminal_state.json").write_text(
        json.dumps({"terminals": {"T1": {"claimed_by": dispatch_id}}}), encoding="utf-8"
    )
    return engine, state


def _run_worker(engine: Path, state: Path, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(engine / "scripts" / WORKER_INJECT)],
        cwd=cwd,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(cwd),
            "VNX_TERMINAL": "T1",
            "VNX_STATE_DIR": str(state),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_worker_script_emits_hook_specific_output_once_then_goes_silent(tmp_path):
    engine, state = _stage_worker_engine(tmp_path, _CANNED_INTELLIGENCE)

    first = _run_worker(engine, state, tmp_path)
    assert first.returncode == 0, first.stderr
    context = _assert_context_payload(first.stdout)
    assert "Dispatch: 20260923-contract-fixture" in context
    assert "Atomic writes" in context
    assert len(context) <= 1600, f"additionalContext over the 1600-char budget: {len(context)}"

    second = _run_worker(engine, state, tmp_path)
    assert second.returncode == 0, second.stderr
    assert second.stdout == "", "unchanged intelligence must be a silent no-op, not a decision object"


def test_worker_script_is_silent_without_terminal_context(tmp_path):
    """No VNX_TERMINAL and a cwd outside T1..T3: the same situation the
    template fallback covers, one layer down."""
    engine, state = _stage_worker_engine(tmp_path, _CANNED_INTELLIGENCE)
    result = subprocess.run(
        ["bash", str(engine / "scripts" / WORKER_INJECT)],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path), "VNX_STATE_DIR": str(state)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_worker_script_is_silent_when_intelligence_is_empty(tmp_path):
    empty = {"pattern_count": 0, "prevention_rule_count": 0, "session_insights": [], "dispatch_blocked": False}
    engine, state = _stage_worker_engine(tmp_path, empty)
    result = _run_worker(engine, state, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
