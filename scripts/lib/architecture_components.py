"""architecture_components — generates the "Supervised Components" and
"Hooks" sections of ``docs/core/00_VNX_ARCHITECTURE.md`` from the real
startup registry and the real hook wiring, instead of a hand-maintained
checklist that silently drifts (D6, dispatch 20260830-090600).

The old doc hand-listed 15 "Active Components", carrying a stale
``Last Updated`` stamp. Two measured examples of the drift this caused:
``Smart Tap V7`` / ``Unified State Manager V2`` named launcher-era aliases
that no longer match the scripts ``start_all()`` actually runs
(``smart_tap_json_translator.sh`` / ``unified_state_manager.py`` -- no "v7"
or "v2" in either filename), and "Worker Intelligence Injection" claimed a
hook (``userpromptsubmit_worker_intelligence_inject.sh``) that
``.claude/settings.json`` never wires -- its only other reference in the
tree is its own test.

Two independently-generated sections, each sourced from the one place that
actually decides the fact it displays:
  - Supervised Components: ``daemon_register.read_daemon_register()`` (D2),
    which itself parses ``vnx_supervisor_simple.sh``'s ``start_all()`` -- the
    only place that decides which processes VNX starts. D6 reads this same
    module rather than parsing ``start_all()`` a second time, so there is
    exactly one register, not two (D2's own dispatch note).
  - Hooks: ``.claude/settings.json``'s own ``hooks`` block -- the only place
    that decides which hook scripts Claude Code actually runs.

``DAEMON_DESCRIPTIONS`` is a curated map to prose, checked both ways by
``build_daemon_rows``: a daemon in the live register with no entry here, or
an entry here for a daemon no longer in the register, is a build error --
the table stays a checked map, not a hand list that can silently drift from
the registry it is supposed to describe.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import daemon_register  # noqa: E402
import project_root  # noqa: E402

SUPERVISOR_RELATIVE_PATH = "scripts/vnx_supervisor_simple.sh"
SETTINGS_RELATIVE_PATH = ".claude/settings.json"

# name (daemon_register.DaemonSpec.name) -> doc prose. Every start_all() daemon
# must have exactly one entry; every entry must name a real daemon (see
# build_daemon_rows). Names, not scripts, are the key: queue_watcher's two
# candidate scripts (VNX_QUEUE_POPUP_ENABLED branch) share one description.
DAEMON_DESCRIPTIONS: Dict[str, str] = {
    "dispatcher": "Dispatcher (native skills, multi-provider dispatch).",
    "smart_tap": "Smart Tap (JSON/Markdown auto-translation).",
    "receipt_processor": "Receipt Processor (report -> receipt -> T0 delivery, adoption tracking).",
    "heartbeat_ack_monitor": "Heartbeat ACK Monitor (ACK processing + timeout tracking).",
    "queue_watcher": (
        "Queue Watcher (dispatch review popup; falls back to auto-accept when "
        "VNX_QUEUE_POPUP_ENABLED=0)."
    ),
    "dashboard": "Dashboard Generator (real-time metrics -> dashboard_status.json).",
    "state_manager": "Unified State Manager (state consolidation, 5s cycle).",
    "intelligence_daemon": "Intelligence Daemon (real-time intelligence updates).",
    "recommendations_engine": "Recommendations Engine (T0 dispatch suggestions, max 5 pending).",
}


def build_daemon_rows(supervisor_script: Optional[Path] = None) -> List[Dict[str, Any]]:
    """One row per daemon ``start_all()`` declares, in declaration order.

    Raises ``ValueError`` when ``DAEMON_DESCRIPTIONS`` and the live register
    disagree in either direction -- a daemon renamed or removed in
    ``start_all()`` must not silently vanish from (or linger in) the
    generated doc; it must fail generation instead (dispatch requirement).
    """
    register = daemon_register.read_daemon_register(supervisor_script)

    live_names = {spec.name for spec in register}
    stale_descriptions = set(DAEMON_DESCRIPTIONS) - live_names
    if stale_descriptions:
        raise ValueError(
            "architecture_components.DAEMON_DESCRIPTIONS names daemons no "
            f"longer in start_all(): {sorted(stale_descriptions)} -- remove "
            "the stale entry, or the daemon was renamed and this key must "
            "follow it."
        )
    missing_descriptions = live_names - set(DAEMON_DESCRIPTIONS)
    if missing_descriptions:
        raise ValueError(
            f"start_all() declares daemons with no description: "
            f"{sorted(missing_descriptions)} -- add an entry to "
            "architecture_components.DAEMON_DESCRIPTIONS before it can render."
        )

    return [
        {
            "name": spec.name,
            "description": DAEMON_DESCRIPTIONS[spec.name],
            "scripts": spec.scripts,
            "conditional": spec.conditional,
            "line": spec.line,
        }
        for spec in register
    ]


def render_daemon_md(rows: List[Dict[str, Any]]) -> str:
    lines = []
    for row in rows:
        scripts = " or ".join(f"`{s}`" for s in row["scripts"])
        cond = " (conditional)" if row["conditional"] else ""
        cite = f"{SUPERVISOR_RELATIVE_PATH}:{row['line']}"
        lines.append(f"- {row['description']} — {scripts}{cond} — `{cite}`")
    return "\n".join(lines)


# Matches a bare filename with a known hook-script extension, no path
# separators -- deliberately loose (a command string is shell, not a path
# grammar) since only the basename is needed to prove wiring.
_SCRIPT_BASENAME_RE = re.compile(r"[\w-]+\.(?:sh|py)")


def build_hook_rows(settings_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """One row per Claude Code hook event in ``.claude/settings.json``,
    listing every script basename actually referenced by that event's
    commands (declaration order, deduplicated)."""
    path = Path(settings_path) if settings_path else (
        project_root.resolve_project_root(__file__) / SETTINGS_RELATIVE_PATH
    )
    data = json.loads(path.read_text(encoding="utf-8"))

    rows: List[Dict[str, Any]] = []
    for event, matcher_blocks in data.get("hooks", {}).items():
        scripts: List[str] = []
        for block in matcher_blocks:
            for hook in block.get("hooks", []):
                command = hook.get("command", "")
                for match in _SCRIPT_BASENAME_RE.finditer(command):
                    name = match.group(0)
                    if name not in scripts:
                        scripts.append(name)
        rows.append({"event": event, "scripts": scripts})
    return rows


def render_hooks_md(rows: List[Dict[str, Any]]) -> str:
    lines = []
    for row in sorted(rows, key=lambda r: r["event"]):
        scripts = ", ".join(f"`{s}`" for s in row["scripts"]) or "_none wired_"
        lines.append(f"- **{row['event']}**: {scripts}")
    return "\n".join(lines)
