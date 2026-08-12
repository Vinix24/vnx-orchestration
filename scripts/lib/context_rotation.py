#!/usr/bin/env python3
"""T0 rotation handoff writer + project_id-scoped rotation paths.

This module carries the HANDOFF side of T0 context rotation:

  - write_t0_handoff(): writes the repo handoff.md contract (frontmatter +
    "Waar we middenin zitten" / "State" / "Next steps"), fail-soft per
    source (git, horizon, open items each independently guarded). Called by
    the Stop-hook safety net (scripts/hooks/session_stop_rotation.py, gated
    on VNX_T0_ROTATION=1).
  - write_ready_signal(): writes the rotation_id-stamped `.ready` ack a
    resumed session drops via `vnx handoff mark-ready` /
    `vnx handoff show --mark-ready` (vnx_cli/commands/handoff.py).
  - rotation_handoff_dir() / rotation_state_dir() / ready_signal_path():
    the project_id+terminal-scoped path contract shared with the `vnx
    handoff` CLI and scripts/lib/handoff_reader.py.

Rotation EXECUTION (deciding when to rotate, /clear, resuming the session)
is NOT here — the live mechanism is the worker rotation system
(hooks/vnx_rotate.sh + the operator /rotate flow), which emits its own
`context_rotation_continuation` receipt. An earlier in-module control-plane
(checkpoint()/decide_rotation()/RotationPolicy/respawn()) shipped default-off
and never gained a production caller; it was removed (OI-1042) rather than
left as a mechanism that reads as if it exists.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Self-bootstrap: make this module importable both as `scripts.lib.context_rotation`
# (namespace package from repo root, e.g. `python3 -c "import scripts.lib.context_rotation"`)
# and via the test convention of prepending scripts/lib to sys.path directly. Either
# way, this module's OWN sibling imports (vnx_paths, tracks) need scripts/lib and
# scripts/ on sys.path — mirrors scripts/lib/vnx_paths.py's bootstrap.
_LIB_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _LIB_DIR.parent
for _p in (str(_LIB_DIR), str(_SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from vnx_paths import resolve_central_data_dir, _resolve_state_root  # noqa: E402

log = logging.getLogger(__name__)

DEFAULT_TERMINAL = "T0"
HANDOFF_FILENAME = "handoff.md"
_ROTATION_SUBDIR = ("rotation_handovers",)
_STATE_SUBDIR = ("state", "rotation")

# Terminal names flow into path components below (rotation_handoff_dir,
# ready_signal_path). The CLI `--terminal` flag (vnx handoff show/mark-ready)
# is untrusted input — a value like "../../../../.ssh/x" would otherwise let
# a caller write files outside the central data dir (path traversal). Only a
# bare identifier is accepted; no separators, no "..".
_TERMINAL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_terminal(terminal: str) -> str:
    """Reject any terminal value that isn't a safe, bare path component."""
    if not isinstance(terminal, str) or not _TERMINAL_NAME_RE.match(terminal):
        raise ValueError(
            f"invalid terminal name {terminal!r}: must match {_TERMINAL_NAME_RE.pattern!r}"
        )
    return terminal


# ---------------------------------------------------------------------------
# Path helpers (all project_id-scoped, resolved via the SAME canonical
# resolver every other VNX surface uses — vnx_paths._resolve_state_root —
# anchored on `project_root`. This deliberately does NOT hardcode
# ~/.vnx-data/<project_id>: that central path is only used when this project
# ALREADY resolves there (existing central install). For a project that
# currently lives in project-local state, forcing the central path
# would create ~/.vnx-data/<project_id> as a side effect of the FIRST
# rotation call — after which vnx_paths' existence-gated central branch
# would prefer that now-existing (but empty) dir over the project's real
# store for every subsequent `vnx track`/`vnx horizon`/`status` call: a
# state-store split-brain (ADR-026 / central-store class). `terminal` is
# validated via _validate_terminal() in every helper below — it is the
# single choke point untrusted --terminal input passes through before
# becoming a path component.
# ---------------------------------------------------------------------------

def _project_data_root(project_id: str, project_root: Optional[Path] = None) -> Path:
    """Resolve the data root THIS project already uses (central or
    project-local) — never forces ~/.vnx-data/<project_id> into existence when
    the project doesn't already resolve there. `project_root` defaults to the
    current working directory when not supplied.
    """
    resolve_central_data_dir(project_id)  # validate project_id shape (ADR-007); raises ValueError on malformed input
    root = Path(project_root) if project_root is not None else Path.cwd()
    return _resolve_state_root(project_id, root)


def rotation_state_dir(project_id: str, project_root: Optional[Path] = None) -> Path:
    return _project_data_root(project_id, project_root).joinpath(*_STATE_SUBDIR)


def rotation_handoff_dir(
    project_id: str, terminal: str = DEFAULT_TERMINAL, project_root: Optional[Path] = None
) -> Path:
    terminal = _validate_terminal(terminal)
    return _project_data_root(project_id, project_root).joinpath(*_ROTATION_SUBDIR, terminal)


def ready_signal_path(
    project_id: str, terminal: str = DEFAULT_TERMINAL, project_root: Optional[Path] = None
) -> Path:
    terminal = _validate_terminal(terminal)
    return rotation_state_dir(project_id, project_root) / f"{terminal}.ready"


# ---------------------------------------------------------------------------
# Small JSON/atomic-write helpers (Codex Defense Checklist: atomic writes on
# canonical state — write <path>.tmp then os.replace).
# ---------------------------------------------------------------------------

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# write_t0_handoff — REAL, project_id-scoped, fail-soft per source
# ---------------------------------------------------------------------------

def _git_snapshot(project_root: Path) -> Dict[str, Any]:
    def _run(*args: str) -> str:
        try:
            return subprocess.check_output(
                ["git", "-C", str(project_root), *args],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
            ).strip()
        except Exception:  # noqa: BLE001 - fail-soft, this is best-effort context
            return ""

    branch = _run("rev-parse", "--abbrev-ref", "HEAD") or "unknown"
    status_out = _run("status", "--porcelain")
    status_lines = [l for l in status_out.splitlines() if l.strip()] if status_out else []
    log_out = _run("log", "--oneline", "-5")
    commits = [l.strip() for l in log_out.splitlines() if l.strip()] if log_out else []
    return {"branch": branch, "status_lines": status_lines, "commits": commits}


def _horizon_snapshot(project_id: str, project_root: Optional[Path] = None) -> Dict[str, Any]:
    """Best-effort NOW/NEXT tracks + their unresolved open items.

    Fail-soft: any failure (missing DB, schema mismatch, import error)
    returns an empty-but-well-formed snapshot rather than raising, so a
    handoff is still written.
    """
    empty: Dict[str, Any] = {"now": [], "next": [], "open_items": [], "error": None}
    try:
        import tracks as _tracks  # scripts/lib/tracks.py
    except Exception as exc:  # noqa: BLE001
        log.warning("context_rotation: tracks module unavailable: %s", exc)
        empty["error"] = str(exc)
        return empty

    state_dir = _project_data_root(project_id, project_root) / "state"
    try:
        rows = _tracks.list_tracks(state_dir, project_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("context_rotation: horizon read failed: %s", exc)
        empty["error"] = str(exc)
        return empty

    now_tracks = [r for r in rows if r.get("horizon") == "now"]
    next_tracks = [r for r in rows if r.get("horizon") == "next"]

    open_items: List[Dict[str, Any]] = []
    for row in now_tracks + next_tracks:
        track_id = row.get("track_id")
        if not track_id:
            continue
        try:
            ois = _tracks.get_linked_open_items(state_dir, track_id, project_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("context_rotation: open-items read failed for %s: %s", track_id, exc)
            continue
        for oi in ois:
            open_items.append({"track_id": track_id, **oi})

    return {"now": now_tracks, "next": next_tracks, "open_items": open_items, "error": None}


def write_t0_handoff(*, logdir: Path, project_root: Path, project_id: str) -> Path:
    """Write the repo handoff.md contract to <logdir>/handoff.md.

    Contract (docs/operations/CONTEXT_ROTATION.md): frontmatter (context,
    project, date, branch) + `## Waar we middenin zitten` / `## State` /
    `## Next steps`. Fail-soft per source — a git or horizon-read failure
    degrades that section's content, it never prevents the handoff from
    being written.
    """
    logdir = Path(logdir)
    logdir.mkdir(parents=True, exist_ok=True)
    project_root = Path(project_root)

    try:
        git = _git_snapshot(project_root)
    except Exception as exc:  # noqa: BLE001
        log.warning("context_rotation: git snapshot failed: %s", exc)
        git = {"branch": "unknown", "status_lines": [], "commits": []}

    try:
        horizon = _horizon_snapshot(project_id, project_root)
    except Exception as exc:  # noqa: BLE001
        log.warning("context_rotation: horizon snapshot failed: %s", exc)
        horizon = {"now": [], "next": [], "open_items": [], "error": str(exc)}

    now_iso = _iso(_utc_now())
    branch = git.get("branch") or "unknown"
    status_lines = git.get("status_lines") or []
    commits = git.get("commits") or []
    now_tracks = horizon.get("now") or []
    next_tracks = horizon.get("next") or []
    open_items = horizon.get("open_items") or []

    lines: List[str] = []
    lines.append("---")
    lines.append("context: t0-rotation")
    lines.append(f"project: {project_id}")
    lines.append(f"date: {now_iso}")
    lines.append(f"branch: {branch}")
    lines.append("---")
    lines.append("")
    lines.append("# T0 Context Rotation Handoff")
    lines.append("")

    lines.append("## Waar we middenin zitten")
    lines.append("")
    if status_lines:
        lines.append(f"Uncommitted changes present ({len(status_lines)} file(s)) on branch `{branch}`.")
    else:
        lines.append(f"Working tree clean on branch `{branch}`.")
    if now_tracks:
        titles = ", ".join(
            f"{t.get('track_id', '?')} ({t.get('title', '?')})" for t in now_tracks[:5]
        )
        lines.append(f"Active NOW-horizon tracks: {titles}.")
    else:
        lines.append("No tracks currently in the NOW horizon.")
    if open_items:
        lines.append(f"{len(open_items)} unresolved open item(s) linked to active tracks — see State below.")
    lines.append("")

    lines.append("## State")
    lines.append("")
    lines.append(f"- Branch: `{branch}`")
    lines.append(f"- Uncommitted files: {len(status_lines)}")
    for sl in status_lines[:20]:
        lines.append(f"  - `{sl}`")
    lines.append("- Last commits:")
    for c in commits[:5]:
        lines.append(f"  - {c}")
    if not commits:
        lines.append("  - (none available)")
    lines.append(f"- Horizon NOW tracks: {len(now_tracks)}")
    for t in now_tracks[:10]:
        lines.append(f"  - `{t.get('track_id')}` — {t.get('title', '')} (phase={t.get('phase')})")
    lines.append(f"- Horizon NEXT tracks: {len(next_tracks)}")
    for t in next_tracks[:10]:
        lines.append(f"  - `{t.get('track_id')}` — {t.get('title', '')} (phase={t.get('phase')})")
    lines.append(f"- Unresolved open items: {len(open_items)}")
    for oi in open_items[:15]:
        lines.append(f"  - `{oi.get('track_id')}` / {oi.get('oi_id')} ({oi.get('link_type')})")
    lines.append("")

    lines.append("## Next steps")
    lines.append("")
    if open_items:
        lines.append("Unresolved open items on active tracks:")
        for oi in open_items[:10]:
            lines.append(f"- `{oi.get('track_id')}` / {oi.get('oi_id')} ({oi.get('link_type')})")
    elif now_tracks:
        lines.append("Continue work on the active NOW-horizon tracks:")
        for t in now_tracks[:10]:
            lines.append(f"- `{t.get('track_id')}` — {t.get('title', '')}")
    else:
        lines.append("No pending horizon items detected. Run `vnx horizon list` to check for newly queued work.")
    lines.append("")

    handoff_path = logdir / HANDOFF_FILENAME
    tmp = handoff_path.with_suffix(handoff_path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, handoff_path)
    return handoff_path


def write_ready_signal(
    project_id: str, terminal: str, rotation_id: str, project_root: Optional[Path] = None
) -> Path:
    """Write the rotation_id-stamped `.ready` ack for a resumed session.

    Called via `vnx handoff mark-ready` / `vnx handoff show --mark-ready`
    (vnx_cli/commands/handoff.py) once the successor session has read the
    handoff — the rotation_id stamp is what lets any waiter distinguish this
    ack from a stale `.ready` left over from a previous rotation.
    """
    ready_path = ready_signal_path(project_id, terminal, project_root)
    _write_json_atomic(ready_path, {
        "rotation_id": rotation_id,
        "terminal": terminal,
        "marked_at": _iso(_utc_now()),
    })
    return ready_path
