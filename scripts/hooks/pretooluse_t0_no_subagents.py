#!/usr/bin/env python3
"""pretooluse_t0_no_subagents.py: PreToolUse hook, T0 does not spawn subagents.

T0 orchestrates and never builds. A Claude Code subagent started from a T0
session leaves no dispatch, no report and no receipt, so the work it does is
invisible to governance. The rule lived in ``role-orchestrator.md`` only, and
a T0 that did not load that file had no rule at all. SEOcrawler_v2 ran T0
sessions without the role from 16-07 (operator finding, 24-09). Its 13 T0
transcripts hold 280 Agent calls, 139 of them in one session (measured 24-09).

This hook makes it policy instead of text. It denies the ``Agent`` and ``Task``
tools when, and only when, the session is a T0 session.

Why the tool names are ``Agent`` and ``Task``: the subagent tool is called
``Agent`` in current Claude Code (448 calls, zero ``Task`` across 143 real T0
transcripts, measured 24-09) and ``Task`` in older versions. The two share one
guard.

Why the deny form is ``hookSpecificOutput.permissionDecision``: measured in all
local session transcripts, that form produced 42 blocked calls (33 of them in T0
sessions), rendered to the model as an ``is_error`` tool result carrying the
reason. The flat ``{"decision": "block"}`` form that ``vnx_context_monitor.sh`` documents
has no trace of ever blocking anything, and OI-1643 found it ignored on
PreToolUse. Exit code 1 (what ``t0-readonly-enforcer.sh`` in SEOcrawler_v2 used)
is a non-blocking error: the call goes through.

How a session is recognised as T0. Project settings apply to EVERY session in
a repo, headless workers included, so the block must not fire for them. Two
facts, both in the hook payload, both measured:

- ``cwd``: a T0 session is launched in ``<project>/.claude/terminals/T0``, and
  project-root settings are loaded for it (176 PreToolUse hook runs from the
  project's own settings in one real T0 transcript; both ``settings.json`` and
  ``settings.local.json`` carry that hook). A worker runs in an isolated
  worktree root or a scratch dir, never in a terminal dir.
- ``transcript_path``: the current ``cwd`` is not stable. 3 of 143 T0 sessions
  also carry the operator's home directory as cwd, and a cwd check alone lets
  a subagent through at exactly that moment. The transcript lives in
  ``~/.claude/projects/<launch cwd, non-alphanumerics as "-">/``, so its
  parent directory names the launch directory for the whole session.
  ``.claude/terminals/T0`` encodes to ``--claude-terminals-T0`` and the name
  must END with it: the scratch sessions T0 itself starts from its own
  scratchpad carry that text in the middle and are not T0.

Either fact makes the session T0. Anything else, and any payload that is not a
JSON object naming one of the two tools, is left alone (empty stdout, exit 0).
"""

from __future__ import annotations

import json
import sys
from pathlib import PurePosixPath
from typing import Any

BLOCKED_TOOLS = frozenset({"Agent", "Task"})

DENY_REASON = "T0 gebruikt geen subagents: stage een dispatch via `vnx dispatch`"

_T0_DIR_SEGMENTS = (".claude", "terminals", "T0")
_T0_TRANSCRIPT_DIR_SUFFIX = "--claude-terminals-T0"


def _cwd_is_t0(cwd: Any) -> bool:
    """True when ``cwd`` is a T0 terminal directory or sits below one."""
    if not isinstance(cwd, str) or not cwd:
        return False
    parts = PurePosixPath(cwd).parts
    width = len(_T0_DIR_SEGMENTS)
    return any(parts[i:i + width] == _T0_DIR_SEGMENTS for i in range(len(parts) - width + 1))


def _launched_in_t0(transcript_path: Any) -> bool:
    """True when the transcript belongs to a session launched in a T0 terminal dir."""
    if not isinstance(transcript_path, str) or not transcript_path:
        return False
    return PurePosixPath(transcript_path).parent.name.endswith(_T0_TRANSCRIPT_DIR_SUFFIX)


def is_t0_session(payload: dict) -> bool:
    return _cwd_is_t0(payload.get("cwd")) or _launched_in_t0(payload.get("transcript_path"))


def decide(payload: Any) -> dict | None:
    """Return the deny decision for this payload, or None to leave the call alone."""
    if not isinstance(payload, dict):
        return None
    if payload.get("tool_name") not in BLOCKED_TOOLS:
        return None
    if not is_t0_session(payload):
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": DENY_REASON,
        }
    }


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else None
    except json.JSONDecodeError:
        return 0  # not a Claude Code payload: nothing to decide on
    decision = decide(payload)
    if decision is not None:
        sys.stdout.write(json.dumps(decision) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
