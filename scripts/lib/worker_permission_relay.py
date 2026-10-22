#!/usr/bin/env python3
"""worker_permission_relay.py — VNX worker-permission relay governance.

A DETACHED tmux worker (interactive ``claude`` on the subscription lane) has no
human at the keyboard. When Claude Code raises a permission prompt for an
off-allow-list tool/command ("Do you want to proceed?"), the worker silently
HANGS until its deadline. This module keeps the operator as the human gate:
an escalation is a durable record on disk that T0 surfaces in chat, and the
operator's answer is relayed back into the worker's pane via send-keys.

This is intentionally infrastructure-free: no popups, no IPC daemon. The
escalation records and the approve relay are plain files and explicit tmux
``send-keys`` calls.

Hard send-keys rule (proven bug — see memory feedback-tmux-sendkeys-enter):
Enter is ALWAYS a SEPARATE keystroke and send-keys ALWAYS targets the explicit
session id. A combined "1\\n" misses delivery; an empty ``-t`` target lands in
the wrong pane.

BILLING SAFETY: no Anthropic SDK; only tmux subprocess calls via an injected
runner abstraction.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from tmux_command_runner import TmuxCommandRunner

logger = logging.getLogger(__name__)

# Escalation records live under a dedicated subdir keyed by dispatch id.
ESCALATIONS_DIRNAME = "permission_escalations"

# dispatch_id becomes a filesystem path segment (escalation record). It MUST be
# validated before any path use: a traversal id ("../../etc/x", "a/b", an
# absolute path) would let an attacker-controlled dispatch id escape the state
# dir and read/write arbitrary files. Strict allow-list: one or more of
# [A-Za-z0-9._-], and never the dot-only ids "." / ".." (which match the class).
_SAFE_DISPATCH_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def _safe_dispatch_id(dispatch_id: str) -> str:
    """Return *dispatch_id* unchanged if safe as a path segment, else raise ValueError.

    Rejects empty / non-string ids, path separators, '..', '.', and absolute paths.
    Called by EVERY function that turns a dispatch_id into a filesystem path so a
    traversal id can never escape the state dir.
    """
    if not isinstance(dispatch_id, str) or not dispatch_id:
        raise ValueError("dispatch_id must be a non-empty string")
    if dispatch_id in (".", ".."):
        raise ValueError(f"unsafe dispatch_id {dispatch_id!r}: '.'/'..' are path-traversal segments")
    if not _SAFE_DISPATCH_ID.match(dispatch_id):
        raise ValueError(
            f"unsafe dispatch_id {dispatch_id!r}: must match [A-Za-z0-9._-]+ "
            "(no path separators, '..', or absolute paths)"
        )
    return dispatch_id


# Prompt markers the Claude Code TUI prints when it needs confirmation. The
# relay no longer parses panes itself: this tuple is the shared detection
# contract that worker_pane_classifier.PROMPT_MARKERS mirrors (parity test in
# tests/test_worker_pane_classifier.py). Matched case-insensitively and kept
# tolerant: never pinned to one Claude Code version's exact wording (the lane
# has been burned by version-specific TUI scraping before).
PROMPT_MARKERS = (
    "do you want to proceed",
    "do you want to make this edit",
    "do you want to create",
    "requires confirmation",
    "requires your permission",
    # OI-1007: newer Claude Code menus carry NO prose marker line — the whole
    # prompt is the numbered option list, e.g.
    #   "1. Yes / 2. Yes, and don't ask again for: rm -f /tmp/x / 3. No"
    # The "don't ask again for" option label is the distinguishing phrase.
    "don't ask again for",
)


# ---------------------------------------------------------------------------
# State-dir resolution (lazy; explicit arg always wins)
# ---------------------------------------------------------------------------
def _default_state_dir() -> Path:
    """Resolve the canonical VNX state dir without hard-importing at module load."""
    try:
        from vnx_paths import ensure_env  # noqa: PLC0415

        paths = ensure_env()
        return Path(paths["VNX_STATE_DIR"])
    except Exception:  # noqa: BLE001 — fall back to git-resolved root
        try:
            from project_root import resolve_state_dir  # noqa: PLC0415

            return resolve_state_dir(caller_file=__file__)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"cannot resolve VNX state dir: {exc}") from exc


def _coerce_state_dir(state_dir: "str | Path | None") -> Path:
    return Path(state_dir) if state_dir is not None else _default_state_dir()


def _now_iso(now: "float | None" = None) -> str:
    ts = now if now is not None else time.time()
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write JSON atomically via temp-file + os.replace (codex defense: atomic writes)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Escalation records
# ---------------------------------------------------------------------------
def _escalations_dir(state_dir: Path) -> Path:
    return state_dir / ESCALATIONS_DIRNAME


def _escalation_path(state_dir: Path, dispatch_id: str) -> Path:
    return _escalations_dir(state_dir) / f"{_safe_dispatch_id(dispatch_id)}.json"


def write_escalation(
    dispatch_id: str,
    command: str,
    reason: str,
    *,
    state_dir: "str | Path | None" = None,
    now: "float | None" = None,
) -> Path:
    """Write a pending escalation record (atomic).

    *reason* names why a prompt was escalated: ``catastrophic`` (an
    irreversible op), ``window_closed`` (no auto-accept window was open) or
    ``awaiting_permission`` (the lane's own pane detection raised it).

    Idempotent: if a pending record for the same command already exists it is
    left untouched (so a repeated write does not churn captured_at).
    """
    _safe_dispatch_id(dispatch_id)
    sd = _coerce_state_dir(state_dir)
    path = _escalation_path(sd, dispatch_id)
    existing = read_escalation(dispatch_id, state_dir=sd)
    if (
        existing
        and existing.get("status") == "pending"
        and existing.get("command") == command
    ):
        return path
    record = {
        "dispatch_id": dispatch_id,
        "command": command,
        "captured_at": _now_iso(now),
        "reason": reason,
        "status": "pending",
        "resolved_at": None,
    }
    _atomic_write_json(path, record)
    return path


def read_escalation(
    dispatch_id: str, *, state_dir: "str | Path | None" = None
) -> "dict | None":
    _safe_dispatch_id(dispatch_id)
    sd = _coerce_state_dir(state_dir)
    path = _escalation_path(sd, dispatch_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def list_escalations(
    *, state_dir: "str | Path | None" = None, pending_only: bool = True
) -> "list[dict]":
    sd = _coerce_state_dir(state_dir)
    d = _escalations_dir(sd)
    if not d.exists():
        return []
    out: "list[dict]" = []
    for path in sorted(d.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        if pending_only and (data.get("status") or "") != "pending":
            continue
        out.append(data)
    return out


def resolve_escalation(
    dispatch_id: str,
    approved: bool,
    *,
    state_dir: "str | Path | None" = None,
    now: "float | None" = None,
) -> "dict | None":
    """Update a pending escalation to approved/denied. Returns the updated record."""
    _safe_dispatch_id(dispatch_id)
    sd = _coerce_state_dir(state_dir)
    record = read_escalation(dispatch_id, state_dir=sd)
    if record is None:
        return None
    record["status"] = "approved" if approved else "denied"
    record["resolved_at"] = _now_iso(now)
    _atomic_write_json(_escalation_path(sd, dispatch_id), record)
    return record


def resolve_stale_escalation(
    dispatch_id: str,
    note: str,
    *,
    state_dir: "str | Path | None" = None,
    now: "float | None" = None,
) -> "dict | None":
    """Close a pending escalation whose worker session is no longer live.

    ``approve``/``deny`` record an operator DECISION on a prompt still waiting
    on a worker's pane. Once the worker session is long gone (dispatch
    finished, died, or was reaped days ago — OI-1414) there is nothing left to
    relay a keystroke into, and "denied" misrepresents the record as a
    considered "no". ``stale`` is the third, distinct terminal status for that
    case: it requires an explicit *note* so the audit trail records WHY the
    escalation was closed without a real approve/deny decision. Returns the
    updated record, or None if no escalation exists for *dispatch_id*.
    """
    _safe_dispatch_id(dispatch_id)
    if not note or not note.strip():
        raise ValueError("resolve_stale_escalation requires a non-empty note")
    sd = _coerce_state_dir(state_dir)
    record = read_escalation(dispatch_id, state_dir=sd)
    if record is None:
        return None
    record["status"] = "stale"
    record["resolution_note"] = note.strip()
    record["resolved_at"] = _now_iso(now)
    _atomic_write_json(_escalation_path(sd, dispatch_id), record)
    return record


# ---------------------------------------------------------------------------
# Approval keystroke
# ---------------------------------------------------------------------------
def _send_approval(runner, session_id: str) -> bool:
    """Send the "approve" selection (option 1) then Enter as a SEPARATE keystroke.

    Targets the EXPLICIT session id — never an empty target (proven wrong-pane
    bug). Enter is always its own send-keys call (combined misses delivery).
    """
    if not session_id:
        raise ValueError("send-keys requires an explicit session id; refusing empty target")
    rc1 = runner.run(["send-keys", "-t", session_id, "1"]).returncode
    if rc1 != 0:
        return False
    # Enter ALWAYS as a separate keystroke.
    return runner.run(["send-keys", "-t", session_id, "Enter"]).returncode == 0


def _default_tmux_runner():
    """Real tmux runner (the injectable transport tests replace with a fake)."""
    return TmuxCommandRunner()
