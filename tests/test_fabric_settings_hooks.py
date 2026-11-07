"""tests/test_fabric_settings_hooks.py — G3, dispatch dlv-2f23d1dbf443.

The T0 context guard (``scripts/hooks/t0_context_guard.py``) now runs at USER
level on both Claude accounts (``vnx guard install``, operator run 2026-11-07;
``vnx guard check`` reports ``OK``). This repo's own ``.claude/settings.json``
must therefore carry no project-level guard entry: while it does,
``vnx guard check --require-user`` reports vnx-dev as ``project (direct)`` --
"covered by the project only, not at user level".

Claude Code reads a hook ``timeout`` in SECONDS. Every timeout in this file
used to be 5000 (83 minutes) from a millisecond assumption. This file pins
both fixes: no guard reference, and every declared timeout an integer between
1 and 600 seconds, while the hooks the change did not remove stay wired per
event.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

SETTINGS_PATH = Path(__file__).resolve().parents[1] / ".claude" / "settings.json"
GUARD = "t0_context_guard.py"

# A hook command is shell, not a path grammar; only the basename is needed to
# prove wiring, so this deliberately loose pattern matches .sh/.py basenames.
_SCRIPT_RE = re.compile(r"[\w.-]+\.(?:sh|py)")

# The entries this change keeps, per event (declaration order, deduplicated).
EXPECTED_SCRIPTS = {
    "PreToolUse": [
        "pretooluse_block_raw_claude_spawn.sh",
        "pretooluse_block_subagent.sh",
    ],
    "SessionEnd": [
        "session_reconcile_cleanup.sh",
        "build_current_state.py",
        "build_doc_indexes.py",
    ],
    "SessionStart": [
        "session_reconcile_autoclose.sh",
        "build_t0_state_hook.sh",
        "path_parity_check.sh",
        "monitor_tripwire.sh",
        "sessionstart.sh",
        "hookpin_check.sh",
    ],
    "Stop": [
        "stop_report_hook.sh",
        "session_stop_rotation.py",
    ],
}


def _settings() -> dict:
    return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))


def _hooks():
    """Yield (event, matcher, hook) for every hook entry in the fabric settings."""
    for event, groups in _settings().get("hooks", {}).items():
        for group in groups:
            for hook in group.get("hooks", []):
                yield event, group.get("matcher", ""), hook


def test_no_hook_command_references_the_t0_context_guard():
    offenders = [
        (event, matcher, hook.get("command", ""))
        for event, matcher, hook in _hooks()
        if GUARD in hook.get("command", "")
    ]
    assert offenders == [], (
        f"{len(offenders)} hook command(s) still reference {GUARD}; the guard "
        f"runs at user level now: {offenders}"
    )


def test_every_declared_timeout_is_an_integer_number_of_seconds():
    bad = []
    for event, matcher, hook in _hooks():
        if "timeout" not in hook:
            continue
        value = hook["timeout"]
        if isinstance(value, bool) or not isinstance(value, int) or not (1 <= value <= 600):
            bad.append((event, matcher, value))
    assert bad == [], (
        f"{len(bad)} hook timeout(s) are not an integer number of seconds in "
        f"1..600: {bad}"
    )


def test_the_remaining_hooks_are_still_wired_per_event():
    rows: dict[str, list[str]] = {}
    for event, _matcher, hook in _hooks():
        names = rows.setdefault(event, [])
        for name in _SCRIPT_RE.findall(hook.get("command", "")):
            if name not in names:
                names.append(name)
    assert rows == EXPECTED_SCRIPTS
