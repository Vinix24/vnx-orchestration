"""tests/test_sessionstart_hook_t0state_fires.py — OI-1552 / D1: the SessionStart
hook that rebuilds ``t0_state.json`` must actually FIRE and actually REACH its
artefact. It did neither, for two independent reasons, and both failed SILENTLY
(exit 0, no signal) — the exact class this file exists to keep closed.

MEASURED 2026-09-08 on claude 2.1.263, main ``015f3cf2``. This module docstring
is the record of that measurement: ``.claude/settings.json`` is strict JSON and
cannot carry a comment, so the finding is kept here, next to the tests that
enforce it.

Cause 1 — the gate could never open.
    The registration read::

        bash -c 'if [ -n "${VNX_HOME:-}" ] && [ -f "${VNX_HOME}/scripts/hooks/build_t0_state_hook.sh" ]; then ...'

    ``VNX_HOME`` is a RUNTIME variable. A SessionStart hook does not inherit it:
    measured with a probe hook that dumped its own environment, ``VNX_HOME`` came
    back ``UNSET``. So ``[ -n "${VNX_HOME:-}" ]`` was false in EVERY session, the
    ``else`` branch printed "MISSING" and exited 0, and the projection never
    refreshed. The six sibling SessionStart hooks in the same file already anchor
    on ``$(git rev-parse --show-toplevel 2>/dev/null || echo .)`` and do run.

    The anchor is safe here because a SessionStart hook runs with cwd set to the
    session's project directory (measured: the harness passes ``cwd`` on stdin and
    the hook's ``$PWD`` matched it), so ``git rev-parse`` resolves inside the repo.
    ``VNX_HOME`` is kept as the PRIMARY anchor when it is set (OI-1089 finding 2),
    and the ``[ -f ]`` guard stays as the fail-loud net for a consumer/pip tree
    that has no ``scripts/`` next to its project root.

Cause 2 — the hook group was never dispatched at all.
    The group carried ``"matcher": "terminals/T0"``. A SessionStart matcher does
    not match a path; it matches the session SOURCE (``startup``, ``resume``,
    ``clear``, ``compact``), or is empty for "always". Measured with three probe
    groups in one settings file, each touching a distinct marker file:

        matcher ""             -> marker written   (fires)
        matcher "startup"      -> marker written   (fires)
        matcher "terminals/T0" -> NO marker        (never fires)

    So the group was dead independently of ``VNX_HOME``: fixing only the gate
    would have left the projection just as frozen. Both causes are covered below.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SETTINGS = _ROOT / ".claude" / "settings.json"

_LIB = _ROOT / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

# A SessionStart matcher the harness can actually dispatch: empty/"*" mean
# "always", otherwise it is built from the documented session-source tokens.
# Anything else (notably a path like "terminals/T0") silently never fires.
#
# OI-1680: this list used to be a local literal here. It now comes from the
# production module that ``vnx doctor`` runs, so the test vocabulary and the
# runtime check cannot drift apart — the templates carried the dead matcher for
# months precisely because the only place that knew the rule was a test.
from t0_state_health import SESSION_START_SOURCES as _VALID_SOURCES  # noqa: E402


def _load_settings() -> dict:
    return json.loads(_SETTINGS.read_text(encoding="utf-8"))


def _t0_state_hook_entry() -> dict:
    """The SessionStart group that registers the state builder.

    Located by SUBSTRING on the command — the same way
    ``scripts/lib/t0_state_health.py`` detects it — so this helper keeps
    working when the group's matcher changes.
    """
    for entry in _load_settings()["hooks"]["SessionStart"]:
        for hook in entry.get("hooks", []):
            if "build_t0_state_hook" in hook.get("command", ""):
                return entry
    raise AssertionError("no SessionStart hook registers build_t0_state_hook")


def _t0_state_hook_command() -> str:
    entry = _t0_state_hook_entry()
    for hook in entry.get("hooks", []):
        if "build_t0_state_hook" in hook.get("command", ""):
            return hook["command"]
    raise AssertionError("unreachable: entry matched but command not found")


def _stage_engine(tmp_path: Path, *, with_hook: bool) -> tuple[Path, Path]:
    """A throwaway git repo standing in for the session's project root.

    The hook artefact is a STUB that records that it ran. The point of these
    tests is the INVOCATION contract (does the registered command reach the
    artefact), so they must not run the real 9.6s builder or touch the live
    central store.
    """
    repo = tmp_path / "engine"
    (repo / "scripts" / "hooks").mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-q"], cwd=repo, check=True, capture_output=True
    )
    marker = tmp_path / "hook_ran.marker"
    if with_hook:
        artefact = repo / "scripts" / "hooks" / "build_t0_state_hook.sh"
        artefact.write_text(
            '#!/usr/bin/env bash\nprintf ran > "$VNX_TEST_MARKER"\nexit 0\n',
            encoding="utf-8",
        )
        artefact.chmod(0o755)
    return repo, marker


def _run_registered_command(repo: Path, marker: Path, **env_overrides) -> subprocess.CompletedProcess:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", str(repo)),
        "VNX_TEST_MARKER": str(marker),
    }
    for key, value in env_overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return subprocess.run(
        shlex.split(_t0_state_hook_command()),
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


# ---------------------------------------------------------------------------
# Cause 2: the group must carry a matcher the harness actually dispatches.
# ---------------------------------------------------------------------------

def test_t0_state_hook_matcher_can_actually_fire():
    matcher = _t0_state_hook_entry().get("matcher", "")
    if matcher in ("", "*"):
        return
    tokens = {t.strip() for t in matcher.split("|") if t.strip()}
    unknown = tokens - _VALID_SOURCES
    assert not unknown, (
        f"SessionStart matcher {matcher!r} contains {sorted(unknown)}, which the "
        "harness never matches — a SessionStart matcher matches the session "
        f"SOURCE ({sorted(_VALID_SOURCES)}) or is empty for 'always'. Measured "
        "2026-09-08: a group matched on 'terminals/T0' never fired at all, so "
        "t0_state.json froze no matter what the hook command did."
    )


def test_t0_state_hook_matcher_is_not_a_path():
    """The specific regression: a path-shaped matcher reads plausible and is dead."""
    matcher = _t0_state_hook_entry().get("matcher", "")
    assert "/" not in matcher, (
        f"SessionStart matcher {matcher!r} looks like a path. Matchers are not "
        "paths; this group would never be dispatched (OI-1552)."
    )


# ---------------------------------------------------------------------------
# Cause 1: with VNX_HOME absent — the real SessionStart condition — the
# registered command must still REACH the artefact.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("vnx_home", [None, ""], ids=["unset", "empty"])
def test_registered_command_reaches_artefact_without_vnx_home(tmp_path, vnx_home):
    repo, marker = _stage_engine(tmp_path, with_hook=True)
    result = _run_registered_command(repo, marker, VNX_HOME=vnx_home)
    assert result.returncode == 0, result.stderr
    assert marker.exists(), (
        "the registered SessionStart command did not reach "
        "build_t0_state_hook.sh with VNX_HOME "
        f"{'unset' if vnx_home is None else 'empty'} — which is how EVERY "
        "SessionStart hook runs, since VNX_HOME is a runtime variable a hook "
        f"does not inherit. stderr: {result.stderr!r}"
    )


def test_vnx_home_still_wins_when_it_is_set(tmp_path):
    """OI-1089 finding 2 stays intact: an explicit VNX_HOME is the primary anchor."""
    repo, marker = _stage_engine(tmp_path, with_hook=True)
    other = tmp_path / "elsewhere"
    (other / "scripts" / "hooks").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=other, check=True, capture_output=True)
    explicit_marker = tmp_path / "explicit.marker"
    artefact = other / "scripts" / "hooks" / "build_t0_state_hook.sh"
    artefact.write_text(
        f'#!/usr/bin/env bash\nprintf ran > "{explicit_marker}"\nexit 0\n',
        encoding="utf-8",
    )
    artefact.chmod(0o755)

    result = _run_registered_command(repo, marker, VNX_HOME=str(other))
    assert result.returncode == 0, result.stderr
    assert explicit_marker.exists(), "an explicit VNX_HOME must take precedence"
    assert not marker.exists(), "VNX_HOME must win over the git anchor"


def test_missing_artefact_still_fails_loud_and_non_blocking(tmp_path):
    """Negative path: a tree without the artefact must SAY so, and not block."""
    repo, marker = _stage_engine(tmp_path, with_hook=False)
    result = _run_registered_command(repo, marker, VNX_HOME=None)
    assert result.returncode == 0, "a missing artefact must never block a session"
    assert not marker.exists()
    assert "MISSING" in result.stderr, (
        "a tree with no build_t0_state_hook.sh must announce it on stderr "
        f"(OI-1073 defect 2), got: {result.stderr!r}"
    )
