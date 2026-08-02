"""token_harvest.py — per-session token-usage harvester from Claude Code
transcripts (receipt-quality PR-B1).

The claude/opus/sonnet tmux subscription lane has no live token-usage API —
today a claude-lane receipt's ``token_usage`` is either a best-effort pane-TUI
regex parse (report frontmatter only) or an explicit ``unavailable`` marker.
The real per-turn accounting already exists locally: the Claude Code CLI
persists every session's transcript to
``~/.claude/projects/<encoded-cwd>/<session_id>.jsonl``, and each assistant
message carries a ``usage`` block (``input_tokens``, ``output_tokens``,
``cache_creation_input_tokens`` with an ``ephemeral_5m``/``ephemeral_1h``
split under ``cache_creation``, ``cache_read_input_tokens``).

This module harvests that transcript, deduped by ``message.id`` — Claude Code
rewrites the same message repeatedly while it streams/uses tools, each
rewrite carrying the message's own cumulative usage, so naive per-line
summing would multiply-count every message by how many times it was
redrawn.

READ-ONLY: never writes, never raises. Only the providers in
``CLAUDE_HARNESS_PROVIDERS`` (claude, deepseek-harness, glm-harness) run
through the Claude Code harness and leave a harvestable transcript; every
other provider (kimi included) has no local transcript and always resolves to
the ``unavailable`` marker here — there is no kimi-token-capture in this
module (kimi capture is a separate, out-of-scope change; see the
receipt-quality plan §Phase B).
"""

from __future__ import annotations

import glob
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Providers whose dispatches run through the Claude Code harness and therefore
# leave a local session transcript that ``harvest_session_tokens`` can read.
# ``claude`` is the native lane; ``deepseek-harness`` and ``glm-harness`` run
# through the same claude-CLI harness with a redirected ``ANTHROPIC_BASE_URL``,
# so their receipts carry a different provider string but an identical
# transcript. Kimi is deliberately absent: it has its own CLI and leaves no
# Claude transcript — its token capture runs through a separate route.
# A third harness provider only needs adding here; both consumers
# (governance_emit, link_sessions_dispatches) import this same set.
CLAUDE_HARNESS_PROVIDERS = frozenset({"claude", "deepseek-harness", "glm-harness"})

# Shape matches receipt_schema.ReceiptV2.token_usage. Zeroed counters (rather
# than an absent dict) plus an explicit ``unavailable`` flag mirrors the
# existing convention in provider_dispatch._extract_token_usage — callers can
# tell "confirmed zero" apart from "no data reported".
_UNAVAILABLE: Dict[str, Any] = {
    "input": 0,
    "output": 0,
    "cache_creation_5m": 0,
    "cache_creation_1h": 0,
    "cache_read": 0,
    "unavailable": True,
}


def _default_claude_projects_dir() -> Path:
    """Resolve ``~/.claude/projects``, overridable for tests via env var."""
    override = os.environ.get("VNX_CLAUDE_PROJECTS_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude" / "projects"


def _find_transcript(session_id: str, projects_dir: Path) -> Optional[Path]:
    """Locate the transcript file for ``session_id`` under ``projects_dir``.

    A session_id is a UUID assigned once per Claude Code session, so exactly
    one match is expected. If more than one is somehow found (e.g. a stale
    copy), the most recently modified file wins rather than raising.
    """
    pattern = str(projects_dir / "*" / f"{session_id}.jsonl")
    matches = glob.glob(pattern)
    if not matches:
        return None
    if len(matches) > 1:
        matches.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        logger.warning(
            "token_harvest: %d transcripts matched session_id=%s — using most recently modified",
            len(matches), session_id,
        )
    return Path(matches[0])


def _sum_transcript_usage(transcript_path: Path) -> Optional[Dict[str, int]]:
    """Sum the four token classes across assistant messages, deduped by
    ``message.id``. Returns None when the file carries no assistant usage
    entries at all (distinct from "found the file but usage is genuinely 0").
    """
    seen: Dict[str, Dict[str, Any]] = {}
    fallback_order = 0
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="ignore") as fh:
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
                message = entry.get("message")
                if not isinstance(message, dict) or message.get("role") != "assistant":
                    continue
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue
                msg_id = message.get("id")
                if not msg_id:
                    # No id on this entry (rare) — treat every occurrence as
                    # distinct rather than dropping it via a shared falsy key.
                    msg_id = f"__noid_{fallback_order}"
                    fallback_order += 1
                seen[msg_id] = usage
    except OSError as exc:
        logger.debug("token_harvest: failed reading transcript %s: %s", transcript_path, exc)
        return None

    if not seen:
        return None

    totals = {
        "input": 0,
        "output": 0,
        "cache_creation_5m": 0,
        "cache_creation_1h": 0,
        "cache_read": 0,
    }
    for usage in seen.values():
        totals["input"] += int(usage.get("input_tokens", 0) or 0)
        totals["output"] += int(usage.get("output_tokens", 0) or 0)
        totals["cache_read"] += int(usage.get("cache_read_input_tokens", 0) or 0)
        creation = usage.get("cache_creation")
        if isinstance(creation, dict):
            totals["cache_creation_5m"] += int(creation.get("ephemeral_5m_input_tokens", 0) or 0)
            totals["cache_creation_1h"] += int(creation.get("ephemeral_1h_input_tokens", 0) or 0)
        else:
            # Pre-split transcript entries carry only the flat total — bucket
            # it under the 5m TTL, the API's default ephemeral-cache lifetime.
            totals["cache_creation_5m"] += int(usage.get("cache_creation_input_tokens", 0) or 0)
    return totals


def harvest_session_tokens(
    session_id: Optional[str],
    project_id: Optional[str] = None,
    *,
    claude_projects_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Harvest cumulative token usage for a Claude Code session transcript.

    Returns a dict shaped for ``receipt_schema.ReceiptV2.token_usage``:
    ``{input, output, cache_creation_5m, cache_creation_1h, cache_read}``.
    Fails open to the ``unavailable`` marker (zeroed counters plus
    ``unavailable: True``) — never raises — when ``session_id`` is empty, no
    transcript is found under ``~/.claude/projects/*/<session_id>.jsonl``, or
    the transcript carries no assistant ``usage`` entries.

    ``project_id`` is accepted for interface symmetry with other
    dispatch-identity resolvers (e.g. ``dispatch_identity.resolve_dispatch_role``)
    but is not needed to locate the transcript: ``session_id`` is already a
    UUID unique across all projects.
    """
    del project_id  # not needed to locate the transcript; kept for call-site symmetry
    session_id = (session_id or "").strip()
    if not session_id:
        return dict(_UNAVAILABLE)

    projects_dir = claude_projects_dir or _default_claude_projects_dir()
    if not projects_dir.is_dir():
        return dict(_UNAVAILABLE)

    try:
        transcript_path = _find_transcript(session_id, projects_dir)
    except OSError as exc:
        logger.debug("token_harvest: transcript lookup failed for session_id=%s: %s", session_id, exc)
        return dict(_UNAVAILABLE)

    if transcript_path is None:
        return dict(_UNAVAILABLE)

    totals = _sum_transcript_usage(transcript_path)
    if totals is None:
        return dict(_UNAVAILABLE)

    return totals


__all__ = ["CLAUDE_HARNESS_PROVIDERS", "harvest_session_tokens"]
