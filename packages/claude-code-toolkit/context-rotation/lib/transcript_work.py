"""What a session did after a point in its transcript.

A ready-marker records a transcript position: the byte offset just after the last
complete line, and the uuid of the last entry before that offset. Work after the
position makes the handover outdated (loop step 6).

Work is a tool call other than a read-only tool or a write of the handover itself,
the result of a call that is not one of those, or a prompt that reached the model
(a typed prompt, a queued prompt, a subagent hand-back, a task notification).
Assistant text and thinking, hook attachments, local command output, and system
and bookkeeping entries are not work. Standard library only.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, Iterator, Optional, Set

import context_budget
import regular_file

READ_ONLY_TOOLS = ("Read", "Glob", "Grep")
WRITE_TOOLS = ("Write", "Edit")
TEXT_BLOCKS = ("text", "thinking", "redacted_thinking")
# User entries with these openings are local command output or an interruption
# notice, not a prompt that reached the model.
NOT_A_PROMPT = ("<local-command-", "<command-name>", "[Request interrupted by user")
LOOKUP_LINES = 2000


def same_file(left: Any, right: str) -> bool:
    if not isinstance(left, str) or not left:
        return False
    return os.path.realpath(left) == os.path.realpath(right)


def is_handover_write(tool_name: Any, tool_input: Any, handover: str) -> bool:
    if tool_name not in WRITE_TOOLS or not isinstance(tool_input, dict):
        return False
    return same_file(tool_input.get("file_path"), handover)


def allowed_tool(tool_name: Any, tool_input: Any, handover: str) -> bool:
    """True for a call that changes nothing the handover describes."""
    return tool_name in READ_ONLY_TOOLS or is_handover_write(tool_name, tool_input, handover)


# --- position ------------------------------------------------------------

def _line_end(path: str) -> int:
    """Offset just after the last newline of the file (0 when it has none)."""
    with regular_file.open_regular(path) as handle:
        position = handle.seek(0, os.SEEK_END)
        while position > 0:
            step = min(context_budget.BLOCK_SIZE, position)
            position -= step
            handle.seek(position)
            index = handle.read(step).rfind(b"\n")
            if index >= 0:
                return position + index + 1
    return 0


def _parse(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        return None


def last_uuid_before(path: str, end: int) -> Optional[str]:
    """The uuid of the last entry that carries one before byte offset `end`."""
    for number, raw in enumerate(context_budget.reversed_lines(path, end)):
        if number > LOOKUP_LINES:
            break
        entry = _parse(raw) if raw.strip() else None
        if isinstance(entry, dict) and isinstance(entry.get("uuid"), str):
            return entry["uuid"]
    return None


def position(transcript_path: Any) -> Optional[Dict[str, Any]]:
    """Where the transcript stands now, or None when it cannot be read."""
    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    try:
        end = _line_end(transcript_path)
        return {"path": transcript_path, "size": end, "last_uuid": last_uuid_before(transcript_path, end)}
    except OSError:
        return None


def position_holds(pos: Dict[str, Any]) -> bool:
    """The transcript still has the recorded entry at the recorded offset."""
    path, size = pos.get("path"), pos.get("size")
    if not isinstance(path, str) or not isinstance(size, int) or size < 0:
        return False
    if os.path.getsize(path) < size:
        return False
    return last_uuid_before(path, size) == pos.get("last_uuid")


# --- what counts as work -------------------------------------------------

def entries_after(path: str, offset: int) -> Iterator[Dict[str, Any]]:
    """Complete entries after the offset; a line still being written is left out."""
    with regular_file.open_regular(path) as handle:
        handle.seek(offset)
        for raw in handle:
            if not raw.endswith(b"\n"):
                return
            entry = _parse(raw)
            if isinstance(entry, dict):
                yield entry


def _content(entry: Dict[str, Any]) -> Any:
    message = entry.get("message")
    return message.get("content") if isinstance(message, dict) else None


def _prompt_text(content: Any) -> Optional[str]:
    """The text of a prompt entry; None when the entry is not a prompt."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        texts = [b.get("text") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        text = " ".join(t for t in texts if isinstance(t, str))
    else:
        return None
    return None if text.lstrip().startswith(NOT_A_PROMPT) else text


def _assistant_work(content: Any, handover: str, allowed: Set[str]) -> Optional[str]:
    if not isinstance(content, list):
        return None
    for block in content:
        kind = block.get("type") if isinstance(block, dict) else None
        if kind in TEXT_BLOCKS:
            continue
        if kind == "tool_use" and allowed_tool(block.get("name"), block.get("input"), handover):
            allowed.add(block.get("id"))
            continue
        if kind == "tool_use":
            return "a %s call" % block.get("name")
        return "an assistant %s block" % kind
    return None


def _results(content: Any) -> Optional[list]:
    if not isinstance(content, list) or not content:
        return None
    ids = [b.get("tool_use_id") for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
    return ids if len(ids) == len(content) else None


def find_tool_use(path: str, end: int, tool_use_id: str) -> Optional[Dict[str, Any]]:
    """The tool_use block with this id before byte offset `end`, looked up a bounded way back."""
    for number, raw in enumerate(context_budget.reversed_lines(path, end)):
        if number > LOOKUP_LINES:
            break
        entry = _parse(raw) if raw.strip() else None
        content = _content(entry) if isinstance(entry, dict) and entry.get("type") == "assistant" else None
        for block in content if isinstance(content, list) else ():
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id") == tool_use_id:
                return block
    return None


def _result_work(ids: list, handover: str, allowed: Set[str], lookup) -> Optional[str]:
    for tool_use_id in ids:
        if tool_use_id in allowed:
            continue
        block = lookup(tool_use_id) if isinstance(tool_use_id, str) else None
        if block is not None and allowed_tool(block.get("name"), block.get("input"), handover):
            allowed.add(tool_use_id)
            continue
        return "the result of %s" % ("a %s call" % block.get("name") if block else "an earlier tool call")
    return None


def _user_work(entry: Dict[str, Any], handover: str, allowed: Set[str], lookup) -> Optional[str]:
    if entry.get("isMeta") or entry.get("isCompactSummary"):
        return None
    content = _content(entry)
    ids = _results(content)
    if ids is not None:
        return _result_work(ids, handover, allowed, lookup) if lookup else None
    return None if _prompt_text(content) is None else "a prompt"


def entry_work(entry: Dict[str, Any], handover: str, allowed: Set[str], lookup=None) -> Optional[str]:
    """A few words naming the work in this entry, or None when it is not work.

    `allowed` collects the ids of allowed tool calls seen so far; a result is
    checked against it, then against `lookup(id)`. Without `lookup`, tool results
    are not judged at all (used when the call itself cannot be placed).
    """
    if entry.get("isSidechain") is True:
        return None
    kind = entry.get("type")
    if kind == "assistant":
        return _assistant_work(_content(entry), handover, allowed)
    if kind == "user":
        return _user_work(entry, handover, allowed, lookup)
    if kind == "attachment":
        attachment = entry.get("attachment")
        if isinstance(attachment, dict) and attachment.get("type") == "queued_command":
            return "a queued prompt"
    return None


def work_after(pos: Dict[str, Any], handover: str, write_id: Optional[str]) -> Optional[str]:
    """The first work after the recorded position, or None."""
    path, offset = pos["path"], pos["size"]
    allowed = {write_id} if write_id else set()

    def lookup(tool_use_id):
        return find_tool_use(path, offset, tool_use_id)

    for entry in entries_after(path, offset):
        found = entry_work(entry, handover, allowed, lookup)
        if found:
            return found
    return None


def stale_reason(pos: Any, handover: str, write_id: Optional[str]) -> Optional[str]:
    """Why the transcript says the handover no longer covers the work, or None."""
    if not isinstance(pos, dict):
        return "its transcript position is unknown"
    try:
        if not position_holds(pos):
            return "the transcript no longer matches the recorded position"
        found = work_after(pos, handover, write_id)
    except OSError:
        return "the transcript cannot be read"
    return "work after it (%s)" % found if found else None


def _epoch(stamp: Any) -> Optional[float]:
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def work_since(transcript_path: Any, since: float, handover: str) -> Optional[str]:
    """Work in entries stamped after `since` (epoch seconds), newest first.

    Used for a handover written outside Write or Edit, whose call cannot be placed
    in the transcript: tool results are not judged, only calls and prompts.
    An unreadable transcript counts as work, so no marker is written on a guess.
    """
    if not isinstance(transcript_path, str) or not transcript_path:
        return "no transcript to check"
    try:
        for raw in context_budget.reversed_lines(transcript_path):
            entry = _parse(raw) if raw.strip() else None
            stamp = _epoch(entry.get("timestamp")) if isinstance(entry, dict) else None
            if stamp is None:
                continue
            if stamp <= since:
                return None
            found = entry_work(entry, handover, set())
            if found:
                return found
    except OSError:
        return "the transcript cannot be read"
    return None
