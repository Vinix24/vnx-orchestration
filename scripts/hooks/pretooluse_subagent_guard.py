#!/usr/bin/env python3
"""pretooluse_subagent_guard.py — PreToolUse hook core: subagent (Task tool) gate.

OI-1643: the previous bash-only hook (pretooluse_block_subagent.sh) emitted
the deprecated flat ``{"decision":"block",...}`` form. Claude Code's
PreToolUse event reads ``hookSpecificOutput.permissionDecision`` instead, so
that output was silently ignored and every Task call was allowed through —
the rule existed on paper only. This module is the corrected core:

  - No marker, an unreadable/malformed marker, an expired marker, or a marker
    with an empty ``reason`` all deny. Deny is the only default.
  - A present, unexpired marker with a non-empty ``reason`` allows the call
    AND appends one line to ``<state_dir>/subagent_use.ndjson`` so an allowed
    subagent still leaves a governance trail. If the audit write itself fails
    the decision flips back to deny (fail-closed) — an "allow" that leaves no
    trace is exactly the bypass this hook exists to close.

Marker file: ``<state_dir>/subagents_allowed.json``, written by
``scripts/subagents_allow.py`` (reason, granted_at, expires_at, granted_by).
State dir is resolved via ``vnx_paths.resolve_paths()['VNX_STATE_DIR']`` —
same central-store resolution every other hook in this repo uses (see
``path_parity_check.sh``), not a hardcoded ``.vnx-data/`` literal.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Tuple

_HOOK_DIR = Path(__file__).resolve().parent
_LIB_DIR = _HOOK_DIR.parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from vnx_paths import resolve_paths  # noqa: E402
from atomic_io import audit_event_append  # noqa: E402

MARKER_FILENAME = "subagents_allowed.json"
AUDIT_EVENT_TYPE = "subagent_use"
PROMPT_EXCERPT_LEN = 200

_ALLOW_CLI_HINT = 'python3 scripts/subagents_allow.py --reason "..." --hours N'


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(ts: Any) -> Optional[datetime]:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def resolve_state_dir() -> Path:
    """Central per-project state dir. Honors VNX_STATE_DIR (tests override this)."""
    paths = resolve_paths()
    return Path(paths["VNX_STATE_DIR"])


def _load_marker(state_dir: Path) -> Tuple[Optional[dict], Optional[str]]:
    """Return (marker, load_error). marker is None when absent or unreadable."""
    marker_path = state_dir / MARKER_FILENAME
    if not marker_path.is_file():
        return None, None
    try:
        raw = marker_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"onleesbaar ({exc})"
    if not isinstance(data, dict):
        return None, "onleesbaar (geen JSON-object)"
    return data, None


def evaluate(marker: Optional[dict], load_error: Optional[str]) -> Tuple[bool, str, Optional[str]]:
    """Return (allowed, reason, marker_reason). marker_reason is set only when
    the marker was structurally valid enough to carry a reason string."""
    if load_error:
        return (
            False,
            f"Subagent-marker {load_error}; behandeld als geblokkeerd. Zet een nieuwe met: {_ALLOW_CLI_HINT}",
            None,
        )

    if marker is None:
        return (
            False,
            "Subagents (Task tool) zijn standaard geblokkeerd — Task-aanroepen laten geen "
            f"governance-receipt achter. Sta tijdelijk toe met: {_ALLOW_CLI_HINT}",
            None,
        )

    reason = str(marker.get("reason") or "").strip()
    if not reason:
        return (
            False,
            "Subagent-marker ongeldig: 'reason' ontbreekt of is leeg. "
            f"Zet een reden met: {_ALLOW_CLI_HINT}",
            None,
        )

    expires_raw = marker.get("expires_at")
    expiry_dt = _parse_iso(expires_raw)
    if expiry_dt is None:
        return (
            False,
            f"Subagent-marker ongeldig: 'expires_at' ({expires_raw!r}) is geen geldige ISO-8601 datum. "
            f"Zet een nieuwe met: {_ALLOW_CLI_HINT}",
            reason,
        )

    if _utc_now() >= expiry_dt:
        return (
            False,
            f"Subagent-marker verlopen op {expires_raw} (reden was: {reason}). "
            f"Vernieuw met: {_ALLOW_CLI_HINT}",
            reason,
        )

    return (
        True,
        f"Subagents tijdelijk toegestaan tot {expires_raw} (reden: {reason}). "
        "Gebruik wordt gelogd in state/subagent_use.ndjson.",
        reason,
    )


def _append_audit(state_dir: Path, session_id: str, prompt: str, marker_reason: str) -> None:
    audit_event_append(
        state_dir,
        AUDIT_EVENT_TYPE,
        {
            "session_id": session_id,
            "prompt_excerpt": prompt[:PROMPT_EXCERPT_LEN],
            "reason": marker_reason,
        },
    )


def build_decision(payload: dict) -> dict:
    state_dir = resolve_state_dir()
    marker, load_error = _load_marker(state_dir)
    allowed, reason, marker_reason = evaluate(marker, load_error)

    if allowed:
        session_id = str(payload.get("session_id") or "")
        tool_input = payload.get("tool_input")
        prompt = ""
        if isinstance(tool_input, dict):
            prompt = str(tool_input.get("prompt") or "")
        try:
            _append_audit(state_dir, session_id, prompt, marker_reason or "")
        except OSError as exc:
            allowed = False
            reason = (
                f"Subagent-marker geldig maar audit-log kon niet geschreven worden ({exc}); "
                "geweigerd (fail-closed) — een subagent zonder spoor is exact wat dit "
                "mechanisme voorkomt."
            )

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow" if allowed else "deny",
            "permissionDecisionReason": reason,
        }
    }


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}

    if not isinstance(payload, dict) or payload.get("tool_name") != "Task":
        return 0  # Not our concern — allow silently, no stdout.

    decision = build_decision(payload)
    sys.stdout.write(json.dumps(decision) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
