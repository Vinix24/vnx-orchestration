"""tests/test_settings_template_matcher_guard.py — OI-1680.

``tests/test_launchd_plist_guard.py`` walks every plist TEMPLATE this repo
ships and refuses a defect at the source. This module is its equivalent for
the settings templates, and it exists because the plist guard's absence-shaped
sibling let a fleet-wide defect sit unnoticed:

MEASURED 2026-09-08 on main ``f97e896f``. PR #1816 repaired the dead
SessionStart matcher in this repo's own ``.claude/settings.json`` (a group
carrying ``"matcher": "terminals/T0"`` is never dispatched — a SessionStart
matcher matches the session SOURCE, never a path). It did NOT repair the three
templates that install that group, and ``scripts/vnx_settings_merge.py``
replaces the ``hooks`` block INTEGRALLY on every ``vnx regen-settings
--merge``. So one regen — the very command ``scripts/hooks/hookpin_check.sh``
advises on a hook problem — put the dead matcher straight back, and every repo
scaffolded by ``vnx init`` carried it from birth. Consequence: those projects
never refreshed ``t0_state.json`` at all.

Two guards, both on BEHAVIOUR rather than on string shape:

1. Every SessionStart matcher in every settings template must be one the
   harness can dispatch, judged by ``t0_state_health.sessionstart_matcher_can_fire``
   -- the same predicate ``vnx doctor`` runs, so the guard and the runtime
   check can never disagree.
2. The rendered t0_state hook command must carry the same INVOCATION contract
   as the repaired ``.claude/settings.json``: an explicit ``VNX_HOME`` wins,
   an absent one falls back to the template's own install anchor, a missing
   artefact says MISSING on stderr and never blocks the session. Each template's
   command is EXECUTED against a staged fake engine to prove it, exactly the
   way ``tests/test_sessionstart_hook_t0state_fires.py`` proves it for
   ``.claude/settings.json``.

The templates use different anchor mechanisms by design -- ``.claude/settings.json``
resolves the repo it lives in via ``git rev-parse``, while the templates resolve
the ENGINE install through their own substitution token (``{{VNX_HOME}}`` /
``{{ engine_root }}``). A consumer repo's git toplevel is not the engine, so
copying the git anchor into the templates would have made the fix fail loudly in
exactly the fleet it is meant to repair. The contract asserted below is
therefore the invocation behaviour, which IS identical, not the literal string.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_LIB = REPO_ROOT / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from t0_state_health import (  # noqa: E402
    SESSION_START_SOURCES,
    sessionstart_matcher_can_fire,
)

TEMPLATES = REPO_ROOT / "templates"
VNX_KEYS_TMPL = TEMPLATES / "settings_vnx_keys.json.tmpl"
INIT_DEFAULT_J2 = TEMPLATES / "init" / "default" / "settings.json.j2"
INIT_MINIMAL_J2 = TEMPLATES / "init" / "minimal" / "settings.json.j2"

REFRESH_HOOK_MARKER = "build_t0_state_hook"


# ─────────────────────────────────────────────────────────────────────────
# Rendering — each template has its own substitution mechanism; render it the
# way its real installer does, so the guard judges what actually lands on disk.
# ─────────────────────────────────────────────────────────────────────────


def _render_vnx_keys(engine_root: str, project_root: str) -> str:
    """Render settings_vnx_keys.json.tmpl through the REAL merge engine's
    loader, so the OI-1139 render guard runs over it too."""
    scripts_dir = REPO_ROOT / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import vnx_settings_merge

    raw = VNX_KEYS_TMPL.read_text(encoding="utf-8")
    rendered = raw.replace(vnx_settings_merge._VNX_HOME_TOKEN, engine_root)
    rendered = rendered.replace("{{PROJECT_ROOT}}", project_root)
    vnx_settings_merge._assert_rendered_cleanly(
        raw, rendered, engine_root, project_root, str(VNX_KEYS_TMPL)
    )
    return rendered


def _render_j2(path: Path) -> Callable[[str, str], str]:
    def _render(engine_root: str, project_root: str) -> str:
        from jinja2 import Environment, FileSystemLoader, StrictUndefined

        env = Environment(
            loader=FileSystemLoader(str(path.parent)),
            undefined=StrictUndefined,
            keep_trailing_newline=True,
        )
        return env.get_template(path.name).render(
            project_name=Path(project_root).name,
            project_id="guard-project",
            vnx_version="0.0.0-test",
            engine_root=engine_root,
        )

    return _render


# (label, path, renderer)
SETTINGS_TEMPLATES = [
    ("settings_vnx_keys.json.tmpl", VNX_KEYS_TMPL, _render_vnx_keys),
    ("init/default/settings.json.j2", INIT_DEFAULT_J2, _render_j2(INIT_DEFAULT_J2)),
    ("init/minimal/settings.json.j2", INIT_MINIMAL_J2, _render_j2(INIT_MINIMAL_J2)),
]
TEMPLATE_IDS = [label for label, _, _ in SETTINGS_TEMPLATES]


def _rendered_settings(renderer: Callable[[str, str], str], engine_root: str, project_root: str) -> dict:
    return json.loads(renderer(engine_root, project_root))


def _sessionstart_groups(settings: dict) -> List[dict]:
    hooks = settings.get("hooks", {})
    return [g for g in hooks.get("SessionStart", []) if isinstance(g, dict)]


def _t0_state_group(settings: dict) -> dict:
    for group in _sessionstart_groups(settings):
        for hook in group.get("hooks", []):
            if REFRESH_HOOK_MARKER in str(hook.get("command") or ""):
                return group
    raise AssertionError("no SessionStart group registers build_t0_state_hook")


def _t0_state_command(settings: dict) -> str:
    group = _t0_state_group(settings)
    for hook in group.get("hooks", []):
        command = str(hook.get("command") or "")
        if REFRESH_HOOK_MARKER in command:
            return command
    raise AssertionError("unreachable: group matched but command not found")


# ─────────────────────────────────────────────────────────────────────────
# Guard 1 — every SessionStart matcher in every template can actually fire.
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("label,path,renderer", SETTINGS_TEMPLATES, ids=TEMPLATE_IDS)
def test_template_sessionstart_matchers_can_fire(label, path, renderer, tmp_path):
    settings = _rendered_settings(renderer, str(tmp_path / "engine"), str(tmp_path / "project"))
    groups = _sessionstart_groups(settings)
    assert groups, f"{label} ships no SessionStart groups — nothing was checked"

    dead = [g.get("matcher") for g in groups if not sessionstart_matcher_can_fire(g.get("matcher"))]
    assert not dead, (
        f"{label} ships SessionStart matcher(s) {dead!r} the harness never "
        "dispatches. A SessionStart matcher matches the session SOURCE "
        f"({sorted(SESSION_START_SOURCES)}) or is empty/'*' for 'always'. "
        "Measured 2026-09-08 (OI-1552/OI-1680): a group matched on "
        "'terminals/T0' never fired, so every project installed from this "
        "template never refreshed t0_state.json."
    )


@pytest.mark.parametrize("label,path,renderer", SETTINGS_TEMPLATES, ids=TEMPLATE_IDS)
def test_template_registers_a_live_t0_state_group(label, path, renderer, tmp_path):
    """nul-is-eerst-een-meetfout: prove the sweep above actually found the
    t0_state group, instead of reporting green over templates that ship none."""
    settings = _rendered_settings(renderer, str(tmp_path / "engine"), str(tmp_path / "project"))
    group = _t0_state_group(settings)
    assert sessionstart_matcher_can_fire(group.get("matcher")), group.get("matcher")


def test_guard_rejects_a_path_shaped_matcher():
    """Negative control: the predicate the sweep leans on must actually fail on
    the real defect, not accept everything."""
    assert sessionstart_matcher_can_fire("terminals/T0") is False
    assert sessionstart_matcher_can_fire("startup") is True
    assert sessionstart_matcher_can_fire("") is True


def test_guard_would_fail_on_a_template_carrying_the_dead_matcher(tmp_path):
    """Prove the SWEEP fails, not just the predicate: a synthetic template
    shaped exactly like the pre-OI-1680 real ones must turn it red."""
    broken = tmp_path / "broken_settings.json.j2"
    broken.write_text(
        json.dumps({
            "hooks": {
                "SessionStart": [{
                    "matcher": "terminals/T0",
                    "hooks": [{
                        "type": "command",
                        "command": 'bash -c \'exec bash "{{ engine_root }}/scripts/hooks/build_t0_state_hook.sh"\'',
                    }],
                }]
            }
        }),
        encoding="utf-8",
    )
    renderer = _render_j2(broken)
    with pytest.raises(AssertionError, match="never dispatches"):
        test_template_sessionstart_matchers_can_fire(
            "broken_settings.json.j2", broken, renderer, tmp_path / "scratch"
        )


# ─────────────────────────────────────────────────────────────────────────
# Guard 2 — the rendered command's INVOCATION contract, executed for real.
# ─────────────────────────────────────────────────────────────────────────


def _stage_engine(tmp_path: Path, *, with_hook: bool, marker: Path) -> Path:
    """A throwaway engine tree. The hook artefact is a STUB that records that it
    ran: this asserts the invocation contract, so it must not run the real
    builder or touch the live central store."""
    engine = tmp_path / "engine"
    (engine / "scripts" / "hooks").mkdir(parents=True, exist_ok=True)
    if with_hook:
        artefact = engine / "scripts" / "hooks" / "build_t0_state_hook.sh"
        artefact.write_text(
            f'#!/usr/bin/env bash\nprintf ran > "{marker}"\nexit 0\n', encoding="utf-8"
        )
        artefact.chmod(0o755)
    return engine


def _run(command: str, cwd: Path, **env_overrides) -> subprocess.CompletedProcess:
    env: Dict[str, str] = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", str(cwd)),
    }
    for key, value in env_overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return subprocess.run(
        shlex.split(command), cwd=cwd, env=env, capture_output=True, text=True, timeout=60
    )


@pytest.mark.parametrize("label,path,renderer", SETTINGS_TEMPLATES, ids=TEMPLATE_IDS)
def test_rendered_command_reaches_artefact_without_vnx_home(label, path, renderer, tmp_path):
    """The real SessionStart condition: VNX_HOME is a runtime variable a hook
    does not inherit (measured UNSET, OI-1552), so the rendered command must
    still reach the artefact under the template's own install anchor."""
    marker = tmp_path / "hook_ran.marker"
    engine = _stage_engine(tmp_path, with_hook=True, marker=marker)
    project = tmp_path / "project"
    project.mkdir()

    settings = _rendered_settings(renderer, str(engine), str(project))
    result = _run(_t0_state_command(settings), cwd=project, VNX_HOME=None)

    assert result.returncode == 0, result.stderr
    assert marker.exists(), (
        f"{label}: the rendered SessionStart command did not reach "
        f"build_t0_state_hook.sh with VNX_HOME unset. stderr: {result.stderr!r}"
    )


@pytest.mark.parametrize("label,path,renderer", SETTINGS_TEMPLATES, ids=TEMPLATE_IDS)
def test_rendered_command_honours_explicit_vnx_home(label, path, renderer, tmp_path):
    """OI-1089 finding 2 stays intact in the templates too: an explicit
    VNX_HOME is the PRIMARY anchor, ahead of the baked-in install root."""
    template_marker = tmp_path / "template_anchor.marker"
    engine = _stage_engine(tmp_path, with_hook=True, marker=template_marker)
    project = tmp_path / "project"
    project.mkdir()

    explicit_marker = tmp_path / "explicit.marker"
    other = tmp_path / "elsewhere"
    (other / "scripts" / "hooks").mkdir(parents=True)
    artefact = other / "scripts" / "hooks" / "build_t0_state_hook.sh"
    artefact.write_text(
        f'#!/usr/bin/env bash\nprintf ran > "{explicit_marker}"\nexit 0\n', encoding="utf-8"
    )
    artefact.chmod(0o755)

    settings = _rendered_settings(renderer, str(engine), str(project))
    result = _run(_t0_state_command(settings), cwd=project, VNX_HOME=str(other))

    assert result.returncode == 0, result.stderr
    assert explicit_marker.exists(), f"{label}: an explicit VNX_HOME must take precedence"
    assert not template_marker.exists(), f"{label}: VNX_HOME must win over the baked anchor"


@pytest.mark.parametrize("label,path,renderer", SETTINGS_TEMPLATES, ids=TEMPLATE_IDS)
def test_rendered_command_fails_loud_and_non_blocking_without_artefact(
    label, path, renderer, tmp_path
):
    """Negative path: an install tree with no artefact must SAY so on stderr and
    must never block a session (OI-1073 defect 2)."""
    marker = tmp_path / "hook_ran.marker"
    engine = _stage_engine(tmp_path, with_hook=False, marker=marker)
    project = tmp_path / "project"
    project.mkdir()

    settings = _rendered_settings(renderer, str(engine), str(project))
    result = _run(_t0_state_command(settings), cwd=project, VNX_HOME=None)

    assert result.returncode == 0, f"{label}: a missing artefact must never block a session"
    assert not marker.exists()
    assert "MISSING" in result.stderr, (
        f"{label}: a tree with no build_t0_state_hook.sh must announce it on "
        f"stderr, got: {result.stderr!r}"
    )


# ─────────────────────────────────────────────────────────────────────────
# The rendered t0_state command must not make hookpin_check cry wolf.
#
# MEASURED 2026-09-08 on main f97e896f: it already does. #1816's fail-loud
# message spells a path inside the printf text, and
# ``hookpin_check.extract_path_tokens`` tokenizes ANY ``/…​.sh`` run in the
# command string — including one inside a message — so it reported
# ``/hooks/build_t0_state_hook.sh`` as a DEAD pin on the live repo. The
# SessionStart beacon in every session of this repo carried that warning, and
# ``vnx doctor``'s remediation advice for it is ``vnx regen-settings --merge``,
# which is the very command that reinstalls this hook. A false DEAD next to a
# real fix trains the reader to ignore the surface that OI-1123 built.
#
# The templates therefore keep the message free of path-shaped text. This test
# is the guard: it runs the REAL checker over a project staged from each
# template, so a future reword that reintroduces a path in the message turns
# red here instead of in the next session's beacon.
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("label,path,renderer", SETTINGS_TEMPLATES, ids=TEMPLATE_IDS)
def test_rendered_settings_produce_no_false_dead_hook_pin(label, path, renderer, tmp_path):
    if str(_LIB) not in sys.path:  # pragma: no cover - defensive
        sys.path.insert(0, str(_LIB))
    import hookpin_check

    marker = tmp_path / "hook_ran.marker"
    engine = _stage_engine(tmp_path, with_hook=True, marker=marker)
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    # The engine reachable the way a real install is: <project>/.vnx, one of
    # hookpin_check's own ${VNX_HOME} candidate bases.
    (project / ".vnx").symlink_to(engine)

    settings = _rendered_settings(renderer, str(engine), str(project))
    command = _t0_state_command(settings)
    (project / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": {"SessionStart": [{
            "matcher": _t0_state_group(settings).get("matcher", ""),
            "hooks": [{"type": "command", "command": command}],
        }]}}),
        encoding="utf-8",
    )

    findings = hookpin_check.check_project_hook_pins(project)
    assert findings, f"{label}: hookpin_check found no pin to check at all"
    dead = [f for f in findings if f.status == hookpin_check.STATUS_MISSING]
    assert not dead, (
        f"{label}: hookpin_check reports a DEAD pin for a hook whose artefact "
        "is present. Every token it extracted:\n"
        + "\n".join(f"  {f.status}: {f.raw_path} -> {f.resolved_path}" for f in findings)
    )


# ─────────────────────────────────────────────────────────────────────────
# The templates and the repaired .claude/settings.json must not drift apart:
# same matcher class, same guard-before-exec ordering.
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("label,path,renderer", SETTINGS_TEMPLATES, ids=TEMPLATE_IDS)
def test_template_hookform_matches_live_settings(label, path, renderer, tmp_path):
    live = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    live_command = _t0_state_command(live)
    live_matcher = _t0_state_group(live).get("matcher")

    settings = _rendered_settings(renderer, str(tmp_path / "engine"), str(tmp_path / "project"))
    command = _t0_state_command(settings)
    matcher = _t0_state_group(settings).get("matcher")

    assert sessionstart_matcher_can_fire(live_matcher), (
        "the live .claude/settings.json regressed to a dead matcher "
        f"({live_matcher!r}) — fix that before trusting this comparison"
    )
    assert matcher == live_matcher, (
        f"{label} carries matcher {matcher!r} while .claude/settings.json "
        f"carries {live_matcher!r}: one regen-settings would flip the live file "
        "to the template's form, so they must agree"
    )

    # Structural parity of the mechanism (the anchor VALUE differs by design:
    # the live file resolves its own repo, a template resolves the engine).
    for fragment in (
        'VNX_HOME="${VNX_HOME:-',
        '[ -f "${VNX_HOME}/scripts/hooks/build_t0_state_hook.sh" ]',
        'exec bash "${VNX_HOME}/scripts/hooks/build_t0_state_hook.sh"',
        "MISSING",
        "exit 0",
    ):
        assert fragment in command, f"{label} lost the {fragment!r} step"
        assert fragment in live_command, (
            f".claude/settings.json lost the {fragment!r} step — the templates "
            "are now the stricter form, which means the live file regressed"
        )

    guard = command.index('if [ -f "${VNX_HOME}/scripts/hooks/build_t0_state_hook.sh" ]')
    execute = command.index('exec bash "${VNX_HOME}/scripts/hooks/build_t0_state_hook.sh"')
    assert guard < execute, (
        f"{label}: the [ -f ] guard must precede exec, or a consumer tree with "
        "no artefact fails silently instead of loudly (OI-1089)"
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
