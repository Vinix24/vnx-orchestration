#!/usr/bin/env python3
"""T0 context-rotation control-plane (default OFF).

Design authority: claudedocs/plans/t0-context-rotation-revival.md (rev 3).
Full contract: docs/operations/CONTEXT_ROTATION.md.

Native Claude Code compaction stays the baseline. This module is an OPTIONAL,
T0-INITIATED, NON-DESTRUCTIVE control-plane on top of it:

  - decide_rotation(): pure decision (enabled-gate, governance boundary,
    durable boundary-count debounce, optional pct backstop, never mid-action).
  - checkpoint(): the integration point a running T0 calls at a governance
    boundary. Loads policy + durable state, decides, and on a decided
    rotation writes a handoff.md + a request marker, then (if
    policy.respawn == "tmux_new_session") calls respawn(). Debounce state
    only advances after a CONFIRMED respawn success (or, when respawn is
    "off", after the handoff/marker write itself succeeds) — an aborted
    respawn leaves the counter untouched so the next boundary can retry.
  - write_t0_handoff(): writes the repo handoff.md contract (frontmatter +
    "Waar we middenin zitten" / "State" / "Next steps"), fail-soft per
    source (git, horizon, open items each independently guarded).
  - respawn(): NON-DESTRUCTIVE `tmux new-session -d` of a fresh, bare
    interactive `claude` (never -p/--print/--dangerously-skip-permissions —
    the exact form scripts/hooks/pretooluse_block_raw_claude_spawn.sh's own
    header comment marks "Always allowed"; argv[0] is always `tmux`, never
    `claude`, since `claude` only ever appears as a later positional/typed
    argument, so pretooluse_spawn_detector.py's exe-basename classifier
    never matches it as the executable being hard-blocked). Waits (bounded)
    for a rotation_id-stamped `.ready` file from the successor; on timeout it
    ABORTs — reaps the orphan tmux session it just created (never the
    caller's own session), leaves the handoff/marker/durable state in place,
    and logs loudly. The tmux call is injectable for tests.

DEFAULT OFF: RotationPolicy.enabled is False unless configs/context_rotation.yaml
sets `enabled: true` or the VNX_T0_ROTATION env var is exactly "1". With either
absent, checkpoint()/decide_rotation() are a proven no-op (see
tests/test_context_rotation.py).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

# Self-bootstrap: make this module importable both as `scripts.lib.context_rotation`
# (namespace package from repo root, e.g. `python3 -c "import scripts.lib.context_rotation"`)
# and via the test convention of prepending scripts/lib to sys.path directly. Either
# way, this module's OWN sibling imports (vnx_paths, tracks, append_receipt) need
# scripts/lib and scripts/ on sys.path — mirrors scripts/lib/vnx_paths.py's bootstrap.
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
_CONFIG_RELATIVE_PATH = Path("configs") / "context_rotation.yaml"

# Terminal names flow into path components below (rotation_handoff_dir,
# durable_state_path, request_marker_path, ready_signal_path). The CLI
# `--terminal` flag (vnx handoff show/mark-ready) is untrusted input — a
# value like "../../../../.ssh/x" would otherwise let a caller write files
# outside the central data dir (path traversal). Only a bare identifier is
# accepted; no separators, no "..".
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
# currently lives in project-local or XDG state, forcing the central path
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
    """Resolve the data root THIS project already uses (central, project-local,
    or XDG) — never forces ~/.vnx-data/<project_id> into existence when the
    project doesn't already resolve there. `project_root` defaults to the
    current working directory (matching checkpoint()'s own default) when not
    supplied.
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


def durable_state_path(
    project_id: str, terminal: str = DEFAULT_TERMINAL, project_root: Optional[Path] = None
) -> Path:
    terminal = _validate_terminal(terminal)
    return rotation_state_dir(project_id, project_root) / f"{terminal}_durable.json"


def request_marker_path(
    project_id: str, terminal: str = DEFAULT_TERMINAL, project_root: Optional[Path] = None
) -> Path:
    terminal = _validate_terminal(terminal)
    return rotation_state_dir(project_id, project_root) / f"{terminal}_request.json"


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


def _load_json_safe(path: Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _seconds_since(iso_ts: str, now: datetime) -> float:
    try:
        parsed = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return float("inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (now - parsed).total_seconds()


# ---------------------------------------------------------------------------
# RotationPolicy
# ---------------------------------------------------------------------------

def _default_config_path() -> Path:
    return _SCRIPTS_DIR.parent / _CONFIG_RELATIVE_PATH


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


@dataclass
class RotationPolicy:
    enabled: bool = False
    trigger: str = "governance_boundary"
    min_boundaries_between_rotations: int = 3
    pct_ceiling: Optional[float] = None
    respawn: str = "off"
    handoff_template: str = "default"

    @classmethod
    def load(
        cls,
        *,
        config_path: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> "RotationPolicy":
        """Load policy from yaml (configs/context_rotation.yaml) + env overrides.

        Env always wins when set. With no yaml file and no env vars, this
        returns the dataclass defaults (enabled=False) — the DEFAULT-OFF
        guarantee.
        """
        env = env if env is not None else os.environ
        path = Path(config_path) if config_path is not None else _default_config_path()
        data: Dict[str, Any] = {}
        if path.is_file():
            try:
                loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, yaml.YAMLError) as exc:
                log.warning("context_rotation: failed to load %s: %s", path, exc)

        enabled = _coerce_bool(data.get("enabled", False))
        if "VNX_T0_ROTATION" in env:
            enabled = env.get("VNX_T0_ROTATION") == "1"

        trigger = env.get("VNX_T0_ROTATION_TRIGGER") or data.get("trigger") or "governance_boundary"

        min_boundaries_raw = env.get("VNX_T0_ROTATION_MIN_BOUNDARIES")
        if min_boundaries_raw is not None:
            min_boundaries = int(min_boundaries_raw)
        else:
            min_boundaries = int(data.get("min_boundaries_between_rotations", 3))

        pct_ceiling_raw = env.get("VNX_T0_ROTATION_PCT_CEILING")
        if pct_ceiling_raw is not None:
            pct_ceiling: Optional[float] = float(pct_ceiling_raw)
        else:
            yaml_pct = data.get("pct_ceiling")
            pct_ceiling = float(yaml_pct) if yaml_pct is not None else None

        respawn = env.get("VNX_T0_ROTATION_RESPAWN") or data.get("respawn") or "off"
        if respawn not in ("tmux_new_session", "off"):
            log.warning("context_rotation: invalid respawn mode %r, defaulting to 'off'", respawn)
            respawn = "off"

        handoff_template = data.get("handoff_template", "default")

        return cls(
            enabled=enabled,
            trigger=trigger,
            min_boundaries_between_rotations=min_boundaries,
            pct_ceiling=pct_ceiling,
            respawn=respawn,
            handoff_template=handoff_template,
        )


# ---------------------------------------------------------------------------
# decide_rotation — pure
# ---------------------------------------------------------------------------

@dataclass
class RotationDecision:
    should_rotate: bool
    reason: str


def decide_rotation(
    *,
    policy: RotationPolicy,
    at_governance_boundary: bool,
    boundaries_since_last_rotation: int,
    context_pct: Optional[float] = None,
    mid_action: bool = False,
) -> RotationDecision:
    """Pure decision function — no I/O, no side effects.

    Gates, in order: enabled -> never mid-action -> must be at a governance
    boundary -> durable boundary-count debounce (bypassable only by the
    optional pct_ceiling backstop, which still requires being at a boundary).
    """
    if not policy.enabled:
        return RotationDecision(False, "disabled")
    if mid_action:
        return RotationDecision(False, "mid_action")
    if not at_governance_boundary:
        return RotationDecision(False, "not_at_boundary")
    if policy.trigger != "governance_boundary":
        # Only governance_boundary is implemented (verified round 1: no
        # reliable live-% signal for interactive T0).
        return RotationDecision(False, f"unsupported_trigger:{policy.trigger}")

    debounced = boundaries_since_last_rotation < policy.min_boundaries_between_rotations
    pct_backstop = (
        policy.pct_ceiling is not None
        and context_pct is not None
        and context_pct >= policy.pct_ceiling
    )

    if not debounced:
        return RotationDecision(True, "boundary_debounce_cleared")
    if pct_backstop:
        return RotationDecision(True, "pct_ceiling_backstop")
    return RotationDecision(False, "debounced")


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


# ---------------------------------------------------------------------------
# respawn — NON-DESTRUCTIVE tmux new-session + bounded readiness wait
# ---------------------------------------------------------------------------

@dataclass
class RespawnResult:
    success: bool
    reason: str
    session_name: Optional[str] = None
    rotation_id: Optional[str] = None
    waited_seconds: float = 0.0


def _build_resume_prompt(
    *, terminal: str, rotation_id: str, project_id: str, handoff_path: Path, project_root: Path,
) -> str:
    # --project-dir is passed explicitly (OI-619 finding #1): the successor's
    # tmux cwd is the T0 terminal workdir, not project_root, so `vnx handoff
    # show`'s cwd-default resolution can no longer be relied on to find it.
    return (
        "Context rotation resume. Read the handoff, then run: "
        f"vnx handoff show --mark-ready --terminal {terminal} "
        f"--rotation-id {rotation_id} --project-id {project_id} "
        f"--project-dir {project_root}\n"
        f"(handoff: {handoff_path})"
    )


class SpawnPartialFailure(RuntimeError):
    """Raised by a tmux_spawn_fn when the tmux session was already created
    before a LATER step (send-keys, etc.) failed. Distinct from a spawn that
    raises before anything was created — this tells respawn() the session
    may exist and is worth reaping (round-3-follow-up finding: an exception
    after `tmux new-session` succeeds must not leave an orphan session)."""

    def __init__(self, session_name: str, cause: BaseException) -> None:
        super().__init__(f"partial spawn failure for session {session_name!r}: {cause}")
        self.session_name = session_name
        self.cause = cause


def _default_tmux_spawn(
    session_name: str,
    project_root: str,
    resume_prompt: str,
    *,
    boot_delay_seconds: float = 3.0,
) -> None:
    """Spawn a fresh, bare interactive `claude` in a new detached tmux session.

    The session's cwd is `<project_root>/.claude/terminals/T0`, NOT
    project_root itself (OI-619 finding #1): Claude Code's memory-file
    discovery and this repo's T0-only SessionStart hooks (matcher
    "terminals/T0" in .claude/settings.json) only load the canonical
    orchestrator role when cwd is under that subdirectory — a bare `-c
    project_root` launch silently booted the successor into the generic
    project CLAUDE.md instead. Since cwd no longer doubles as the project
    root, `resume_prompt` carries `--project-dir project_root` explicitly
    (see _build_resume_prompt) so `vnx handoff show` still resolves the right
    project.

    Guard-safety (round-3 finding #5): every subprocess call here has argv[0]
    == "tmux" — `claude` only ever appears as a later positional argument to
    `tmux send-keys` (a single quoted string), never as the executable being
    run in the outer command. scripts/hooks/pretooluse_spawn_detector.py's
    _classify_argv() hard-blocks on `exe == "claude"` with a dangerous flag in
    the SAME argv — that condition structurally never occurs here, and the
    interactive `claude` invocation itself carries no -p/--print/
    --dangerously-skip-permissions flag (the "Always allowed... claude
    (benign/interactive)" case documented in that hook's own header comment).
    In production this call happens inside an already-running python process
    (not as a literal Bash-tool-call string the guard inspects), so the guard
    is not even in the invocation path — this structure is defense-in-depth
    on top of that.

    If `tmux new-session` itself fails, the session was never created and the
    exception propagates as-is (nothing to reap). If any LATER step fails,
    the session already exists — that failure is wrapped in
    SpawnPartialFailure so respawn() knows to kill it before returning.
    """
    t0_workdir = str(Path(project_root) / ".claude" / "terminals" / "T0")
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session_name, "-c", t0_workdir],
        check=True,
        timeout=10,
    )
    try:
        subprocess.run(["tmux", "send-keys", "-t", session_name, "-l", "claude"], check=True, timeout=10)
        subprocess.run(["tmux", "send-keys", "-t", session_name, "Enter"], check=True, timeout=10)
        if boot_delay_seconds > 0:
            time.sleep(boot_delay_seconds)
        subprocess.run(["tmux", "send-keys", "-t", session_name, "-l", resume_prompt], check=True, timeout=10)
        subprocess.run(["tmux", "send-keys", "-t", session_name, "Enter"], check=True, timeout=10)
    except Exception as exc:  # noqa: BLE001 - re-raised wrapped, not swallowed
        raise SpawnPartialFailure(session_name, exc) from exc


def _default_tmux_kill(session_name: str) -> None:
    """Reap an orphan tmux session. Only ever called with a session_name this
    module itself just created for a failed respawn attempt — never the
    caller's own/current session (non-destructive guarantee)."""
    subprocess.run(["tmux", "kill-session", "-t", session_name], check=False, timeout=10)


def _check_ready(ready_path: Path, rotation_id: str) -> bool:
    data = _load_json_safe(ready_path)
    if not data:
        return False
    return data.get("rotation_id") == rotation_id


def write_ready_signal(
    project_id: str, terminal: str, rotation_id: str, project_root: Optional[Path] = None
) -> Path:
    """Write the rotation_id-stamped `.ready` signal a waiting respawn() call
    checks for. Called by the successor session (`vnx handoff mark-ready` /
    `vnx handoff show --mark-ready`) once it has resumed — round-3 finding
    #6: the rotation_id is what lets the waiter reject a stale `.ready` left
    over from a previous rotation.
    """
    ready_path = ready_signal_path(project_id, terminal, project_root)
    _write_json_atomic(ready_path, {
        "rotation_id": rotation_id,
        "terminal": terminal,
        "marked_at": _iso(_utc_now()),
    })
    return ready_path


def respawn(
    *,
    handoff_path: Path,
    terminal: str,
    project_id: str,
    project_root: Path,
    rotation_id: Optional[str] = None,
    timeout_seconds: float = 60.0,
    poll_interval_seconds: float = 0.5,
    tmux_spawn_fn: Optional[Callable[[str, str, str], None]] = None,
    tmux_kill_fn: Optional[Callable[[str], None]] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    time_fn: Callable[[], float] = time.monotonic,
) -> RespawnResult:
    """NON-DESTRUCTIVE respawn: tmux new-session -d a fresh T0, wait bounded
    for its rotation_id-stamped `.ready` signal.

    Never touches the caller's own session — no `vnx start`, no kill of an
    existing session. On timeout, ABORTs: reaps the orphan session THIS call
    spawned (round-3 finding #4) and returns success=False; the caller is
    responsible for leaving all other state (handoff, durable counter) intact
    so the next boundary can retry (round-3 finding #3). A spawn that fails
    AFTER the tmux session was already created (SpawnPartialFailure) is
    reaped the same way — a raw spawn failure before anything existed is not
    (there is nothing to reap).
    """
    spawn = tmux_spawn_fn or _default_tmux_spawn
    kill = tmux_kill_fn or _default_tmux_kill
    rotation_id = rotation_id or uuid.uuid4().hex[:12]

    ready_path = ready_signal_path(project_id, terminal, project_root)
    ready_path.parent.mkdir(parents=True, exist_ok=True)

    session_name = f"vnx-t0-rotation-{terminal.lower()}-{rotation_id[:8]}"
    resume_prompt = _build_resume_prompt(
        terminal=terminal, rotation_id=rotation_id, project_id=project_id, handoff_path=handoff_path,
        project_root=project_root,
    )

    try:
        spawn(session_name, str(project_root), resume_prompt)
    except SpawnPartialFailure as exc:
        log.error(
            "context_rotation: respawn spawn partially failed for %s (session may exist): %s "
            "— reaping to avoid an orphan T0",
            session_name, exc,
        )
        try:
            kill(session_name)
        except Exception as kill_exc:  # noqa: BLE001
            log.error("context_rotation: failed to reap partially-spawned session %s: %s", session_name, kill_exc)
        return RespawnResult(
            success=False, reason=f"spawn_partial_failure:{exc.cause}",
            session_name=session_name, rotation_id=rotation_id,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("context_rotation: respawn spawn failed for %s: %s", session_name, exc)
        return RespawnResult(success=False, reason=f"spawn_failed:{exc}", session_name=session_name, rotation_id=rotation_id)

    start = time_fn()
    while True:
        if _check_ready(ready_path, rotation_id):
            return RespawnResult(
                success=True, reason="ready", session_name=session_name,
                rotation_id=rotation_id, waited_seconds=time_fn() - start,
            )
        elapsed = time_fn() - start
        if elapsed >= timeout_seconds:
            log.error(
                "context_rotation: ABORT — no ready signal for terminal=%s rotation_id=%s "
                "within %.1fs; reaping orphan session %s (old session retained)",
                terminal, rotation_id, timeout_seconds, session_name,
            )
            try:
                kill(session_name)
            except Exception as exc:  # noqa: BLE001
                log.error("context_rotation: failed to reap orphan session %s: %s", session_name, exc)
            return RespawnResult(
                success=False, reason="timeout_no_ready", session_name=session_name,
                rotation_id=rotation_id, waited_seconds=elapsed,
            )
        sleep_fn(poll_interval_seconds)


# ---------------------------------------------------------------------------
# checkpoint — THE integration point
# ---------------------------------------------------------------------------

@dataclass
class RotationOutcome:
    rotated: bool
    reason: str
    handoff_path: Optional[Path] = None
    marker_path: Optional[Path] = None
    respawn_result: Optional[RespawnResult] = None
    rotation_id: Optional[str] = None


def _emit_continuation_receipt(
    *, terminal: str, dispatch_id: str, handoff_path: str, context_pct: Optional[float], project_id: str,
) -> None:
    """Emit the EXISTING context_rotation_continuation event (round-3 finding
    #1) that scripts/lib/conversation_read_model.py chains on by dispatch_id.
    Same field shape as the legacy worker rotation emitter
    (hooks/vnx_rotate.sh) so both producers feed one read model. Fail-soft:
    a receipt-emission failure must never undo an already-successful
    rotation, so this only logs loudly on failure.
    """
    receipt = {
        "event_type": "context_rotation_continuation",
        "terminal": terminal,
        "dispatch_id": dispatch_id,
        "handover_path": handoff_path,
        "skill": "t0-orchestrator",
        "context_used_pct_at_rotation": int(context_pct) if context_pct is not None else 0,
        "timestamp": _iso(_utc_now()),
        "project_id": project_id,
        "source": "context_rotation",
    }
    try:
        from append_receipt import append_receipt_payload
        append_receipt_payload(receipt)
    except Exception as exc:  # noqa: BLE001
        log.error("context_rotation: failed to emit context_rotation_continuation receipt: %s", exc)


def _load_durable(path: Path) -> Dict[str, Any]:
    data = _load_json_safe(path)
    if not isinstance(data, dict):
        data = {}
    data.setdefault("boundaries_since_last_rotation", 0)
    data.setdefault("last_rotation_at", None)
    return data


def checkpoint(
    *,
    at_governance_boundary: bool = True,
    project_id: str,
    context_pct: Optional[float] = None,
    terminal: str = DEFAULT_TERMINAL,
    project_root: Optional[Path] = None,
    policy: Optional[RotationPolicy] = None,
    respawn_fn: Optional[Callable[..., RespawnResult]] = None,
    now_fn: Callable[[], datetime] = _utc_now,
    request_ttl_seconds: float = 120.0,
) -> RotationOutcome:
    """The integration point a running T0 calls at a governance boundary.

    Loads policy + durable state (both project_id/terminal-scoped), decides
    via decide_rotation(), and on a decided rotation writes handoff.md + a
    request marker, then (if policy.respawn == "tmux_new_session") invokes
    respawn(). The durable debounce counter only resets — and
    context_rotation_continuation only fires — after a CONFIRMED success
    (respawn ready, or immediately when respawn is "off"). An ABORTed respawn
    leaves the counter untouched (round-3 finding #3) so a later boundary can
    retry, and does not emit the continuation receipt (round-3 finding #1:
    "on a successful rotate" only).

    Idempotent: a request marker with status "in_progress" younger than
    request_ttl_seconds short-circuits a duplicate call to a no-op instead of
    writing a second handoff/marker or invoking respawn() twice.
    """
    policy = policy or RotationPolicy.load()
    if not policy.enabled:
        return RotationOutcome(rotated=False, reason="disabled")

    resolved_project_root = Path(project_root) if project_root else Path.cwd()
    state_dir = rotation_state_dir(project_id, resolved_project_root)
    state_dir.mkdir(parents=True, exist_ok=True)

    durable_path = durable_state_path(project_id, terminal, resolved_project_root)
    durable = _load_durable(durable_path)

    request_path = request_marker_path(project_id, terminal, resolved_project_root)
    now = now_fn()
    in_flight = _load_json_safe(request_path)
    if in_flight and in_flight.get("status") == "in_progress":
        created_at = in_flight.get("created_at")
        if created_at and _seconds_since(created_at, now) < request_ttl_seconds:
            return RotationOutcome(
                rotated=False, reason="already_in_progress", rotation_id=in_flight.get("rotation_id"),
            )
        # Stale in_progress marker (a previous attempt crashed mid-flight
        # without ever writing an outcome) — fall through and retry.

    decision = decide_rotation(
        policy=policy,
        at_governance_boundary=at_governance_boundary,
        boundaries_since_last_rotation=durable.get("boundaries_since_last_rotation", 0),
        context_pct=context_pct,
    )

    if not decision.should_rotate:
        if at_governance_boundary:
            durable["boundaries_since_last_rotation"] = durable.get("boundaries_since_last_rotation", 0) + 1
            _write_json_atomic(durable_path, durable)
        return RotationOutcome(rotated=False, reason=decision.reason)

    rotation_id = uuid.uuid4().hex[:12]
    _write_json_atomic(request_path, {
        "rotation_id": rotation_id, "status": "in_progress", "created_at": _iso(now),
    })

    handoff_dir = rotation_handoff_dir(project_id, terminal, resolved_project_root)
    try:
        handoff_path = write_t0_handoff(
            logdir=handoff_dir, project_root=resolved_project_root, project_id=project_id,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("context_rotation: handoff write failed, aborting rotation: %s", exc)
        _write_json_atomic(request_path, {
            "rotation_id": rotation_id, "status": "aborted", "created_at": _iso(now),
            "reason": f"handoff_write_failed:{exc}",
        })
        return RotationOutcome(rotated=False, reason="handoff_write_failed", rotation_id=rotation_id)

    respawn_result: Optional[RespawnResult] = None
    confirmed = True
    if policy.respawn == "tmux_new_session":
        respawn_impl = respawn_fn or respawn
        respawn_result = respawn_impl(
            handoff_path=handoff_path, terminal=terminal, project_id=project_id,
            project_root=resolved_project_root, rotation_id=rotation_id,
        )
        confirmed = respawn_result.success

    if confirmed:
        durable["boundaries_since_last_rotation"] = 0
        durable["last_rotation_at"] = _iso(now_fn())
        _write_json_atomic(durable_path, durable)
        _write_json_atomic(request_path, {
            "rotation_id": rotation_id, "status": "success", "created_at": _iso(now),
        })
        _emit_continuation_receipt(
            terminal=terminal, dispatch_id=rotation_id, handoff_path=str(handoff_path),
            context_pct=context_pct, project_id=project_id,
        )
        return RotationOutcome(
            rotated=True, reason=decision.reason, handoff_path=handoff_path,
            marker_path=request_path, respawn_result=respawn_result, rotation_id=rotation_id,
        )

    abort_reason = respawn_result.reason if respawn_result else "unknown"
    _write_json_atomic(request_path, {
        "rotation_id": rotation_id, "status": "aborted", "created_at": _iso(now), "reason": abort_reason,
    })
    log.error(
        "context_rotation: ABORT rotation_id=%s terminal=%s reason=%s — old session retained, "
        "handoff kept at %s, debounce counter NOT reset (retry eligible next boundary)",
        rotation_id, terminal, abort_reason, handoff_path,
    )
    return RotationOutcome(
        rotated=False, reason=f"abort:{abort_reason}", handoff_path=handoff_path,
        marker_path=request_path, respawn_result=respawn_result, rotation_id=rotation_id,
    )
