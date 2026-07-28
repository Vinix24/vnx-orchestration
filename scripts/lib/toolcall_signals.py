"""toolcall_signals.py — per-dispatch tool-call signal aggregation (receipt-quality PR-B2).

The PreToolUse hooks already registered for Bash and Task in .claude/settings.json
(``scripts/hooks/pretooluse_block_raw_claude_spawn.sh``,
``scripts/hooks/pretooluse_block_subagent.sh``) fire on every Bash/Task tool call a
dispatch's Claude Code session makes -- a live, already-wired channel -- but until now
nothing recorded those firings anywhere, so "how many tool calls, how many were
blocked, how many were retried" was unobservable per dispatch.

This module is the write+read side of that channel:

  - ``record_toolcall_event()`` is called from the tail of each PreToolUse hook
    (best-effort, fail-open, never touches the hook's stdout/decision). It appends
    one NDJSON line to ``<signal_dir>/toolcalls.ndjson`` -- ``signal_dir`` is the
    same per-dispatch ``$VNX_TMUX_SIGNAL_DIR`` scratch directory the tmux-signal
    hooks (``tmux_signal_stop_receipt.sh`` et al.) already use.
  - ``aggregate_toolcall_signals()`` is called once at receipt-emit time
    (``provider_dispatch._emit_governance``) to fold the log into three additive
    receipt fields (``receipt_schema.ReceiptV2.tool_call_count`` /
    ``tool_call_failures`` / ``tool_call_retries``).

Scoped to dispatches that export ``VNX_TMUX_SIGNAL_DIR`` (tmux-spawn dispatches
today): the same scoping convention ``tmux_signal_stop_receipt.sh`` already uses.
Non-Claude-Code providers (codex, kimi, gemini CLIs) never go through Claude Code's
hook system at all, so this channel is silent for them by construction --
``aggregate_toolcall_signals()`` returns ``None`` and the receipt fields stay omitted
(``ReceiptV2``'s existing None-omission contract).

Fail-open everywhere: ``record_toolcall_event`` never raises (hook context -- a
failure here must never block or alter the PreToolUse decision); ``aggregate_toolcall_
signals`` never raises (receipt-emit context -- an aggregation failure must never
break receipt emission).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_SIGNAL_FILENAME = "toolcalls.ndjson"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _input_signature(tool_input: Any) -> str:
    """Stable short hash of a tool call's input, used to detect retries (the same
    tool_name reissued with the same input). ``sort_keys`` makes key-order
    variation in an equivalent dict not spuriously break a retry match.
    """
    try:
        blob = json.dumps(tool_input, sort_keys=True, default=str)
    except (TypeError, ValueError):
        blob = str(tool_input)
    return hashlib.sha256(blob.encode("utf-8", errors="ignore")).hexdigest()[:16]


def record_toolcall_event(
    signal_dir: "str | Path",
    hook_payload: Dict[str, Any],
    *,
    blocked: bool,
) -> None:
    """Append one tool-call signal line to ``<signal_dir>/toolcalls.ndjson``.

    ``hook_payload`` is the raw PreToolUse hook stdin JSON
    (``{tool_name, tool_input, session_id, cwd, transcript_path}``); only
    ``tool_name``/``tool_input`` are used. ``blocked`` records whether the hook's
    OWN decision for this invocation was to block the call.

    Best-effort: never raises. A hook that logs a signal must never fail the tool
    call it is observing.
    """
    try:
        signal_dir = Path(signal_dir)
        signal_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "tool_name": hook_payload.get("tool_name") or "",
            "signature": _input_signature(hook_payload.get("tool_input")),
            "blocked": bool(blocked),
            "timestamp": _utc_now_iso(),
        }
        path = signal_dir / _SIGNAL_FILENAME
        with path.open("a", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                fh.write(json.dumps(entry) + "\n")
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
    except Exception:  # noqa: BLE001 — hook context: must never raise or block the tool call
        logger.debug("toolcall_signals: record failed (non-fatal)", exc_info=True)


def aggregate_toolcall_signals(signal_dir: "str | Path") -> Optional[Dict[str, int]]:
    """Fold a dispatch's ``toolcalls.ndjson`` into receipt-ready counts.

    Returns ``{tool_call_count, tool_call_failures, tool_call_retries}``, or
    ``None`` when the signal file is absent/unreadable/empty (distinct from
    "confirmed zero calls" -- mirrors ``token_harvest.py``'s None-vs-zero
    convention). A malformed line is skipped rather than aborting the whole
    aggregation. Never raises.
    """
    try:
        path = Path(signal_dir) / _SIGNAL_FILENAME
        if not path.is_file():
            return None
        seen: Dict[tuple, int] = {}
        total = 0
        failures = 0
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                total += 1
                if entry.get("blocked"):
                    failures += 1
                key = (entry.get("tool_name"), entry.get("signature"))
                seen[key] = seen.get(key, 0) + 1
        if total == 0:
            return None
        retries = sum(n - 1 for n in seen.values() if n > 1)
        return {
            "tool_call_count": total,
            "tool_call_failures": failures,
            "tool_call_retries": retries,
        }
    except OSError:
        logger.debug("toolcall_signals: aggregate failed (non-fatal)", exc_info=True)
        return None


def _cli_main() -> int:
    """CLI entry so the (bash) PreToolUse hooks can record a signal without a
    Python import: ``echo "$INPUT" | python3 toolcall_signals.py --signal-dir DIR
    --blocked {0,1}``. Fail-open: malformed/absent stdin never breaks the hook.
    """
    import argparse
    import sys

    parser = argparse.ArgumentParser()
    parser.add_argument("--signal-dir", required=True)
    parser.add_argument("--blocked", choices=("0", "1"), required=True)
    args = parser.parse_args()

    try:
        payload = json.load(sys.stdin)
    except Exception:  # noqa: BLE001 — fail-open: never break the calling hook
        return 0
    if not isinstance(payload, dict):
        return 0

    record_toolcall_event(args.signal_dir, payload, blocked=(args.blocked == "1"))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli_main())


__all__ = ["record_toolcall_event", "aggregate_toolcall_signals"]
