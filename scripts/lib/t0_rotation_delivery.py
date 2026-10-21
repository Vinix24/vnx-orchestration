"""t0_rotation_delivery.py — did the successor T0 receive its first message? (OI-2102)

The pure half of the delivery check: reading the successor's transcript and judging its first
message. No window is touched here. The job that acts on the verdict (receipt, ``delivery.json``,
the /goal follow-up, closing the old window) is ``scripts/t0_rotation_delivery_job.py``, next to
the spawner, because no ``scripts/lib`` module may drive tmux directly.

The spawner starts the successor with the first message as the positional prompt argument of
``claude`` (``/kickoff rotatie <run_id>``) and a session id it generated itself. The question is
whether the message arrived. The answer comes from the successor's transcript,
``<config dir>/projects/*/<session uuid>.jsonl``: wait for the file to hold a real user record,
compare that record to the first message.

A start argument can land in the transcript in two forms, and ``normalise`` folds both to the line
that was passed:

  - the expanded slash-command form, measured on claude 2.1.304 transcripts: ``{"type": "user",
    "isSidechain": false, "origin": {"kind": "human"}, "message": {"role": "user", "content":
    "<command-message>kickoff</command-message>\\n<command-name>/kickoff</command-name>\\n
    <command-args>rotatie <run_id></command-args>"}}``;
  - the plain text ``/kickoff rotatie <run_id>``.

Which one claude writes for a positional start argument is confirmed by the first live rotation
(docs/operations/T0_CONTEXT_ROTATION.md). Skipped before the first real user record: ``mode``,
``bridge-session``, ``file-history-snapshot``, ``queue-operation`` and ``system`` records, user
records with ``isMeta: true`` (the local-command caveat), ``<local-command-stdout>`` output, hook
and system-reminder text, and tool results.
"""
from __future__ import annotations

import glob
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

_NOISE_PREFIXES = ("<local-command-", "<system-reminder>", "<user-prompt-submit-hook>",
                   "<hook", "<task-notification>", "Caveat:")
_COMMAND_NAME_RE = re.compile(r"<command-name>\s*(/[^<\s]+)\s*</command-name>")
_COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)


def _text_of(content: Any) -> str:
    """The text of a user message: a string, or the text blocks of a list (tool results carry
    none and so give the empty string)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text") or "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def real_user_text(record: Dict[str, Any]) -> Optional[str]:
    """The text of ``record`` when it is something a person (or a start argument) sent; None for
    meta records, other record types, hook and local-command output, and tool results."""
    if record.get("type") != "user" or record.get("isMeta") or record.get("isSidechain"):
        return None
    message = record.get("message")
    text = _text_of(message.get("content") if isinstance(message, dict) else None).strip()
    if not text or text.startswith(_NOISE_PREFIXES):
        return None
    return text


def normalise(text: str) -> str:
    """A slash command is stored as ``<command-name>/x</command-name>`` plus optional
    ``<command-args>``; rebuild the line that was typed or passed as the start argument."""
    name = _COMMAND_NAME_RE.search(text)
    if not name:
        return text.strip()
    args = _COMMAND_ARGS_RE.search(text)
    return f"{name.group(1)} {args.group(1).strip()}".strip() if args else name.group(1)


def first_user_message(path: Path) -> Optional[str]:
    """The first real user record of a transcript, normalised; None while there is none (the file
    may still be growing, so an unparseable last line is not an error)."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        text = real_user_text(record) if isinstance(record, dict) else None
        if text is not None:
            return normalise(text)
    return None


def find_transcript(config_dir: Path, session_uuid: str) -> Optional[Path]:
    hits = sorted(glob.glob(str(config_dir / "projects" / "*" / f"{session_uuid}.jsonl")))
    return Path(hits[0]) if hits else None


def await_first_message(config_dir: Path, session_uuid: str, *, timeout: float,
                        poll: float) -> Tuple[Optional[Path], Optional[str]]:
    """Poll until the transcript holds a real user record, or ``timeout`` seconds pass. Returns
    (transcript or None, first message or None). One check always runs, also with timeout 0."""
    deadline = time.monotonic() + timeout
    transcript: Optional[Path] = None
    while True:
        transcript = find_transcript(config_dir, session_uuid) or transcript
        found = first_user_message(transcript) if transcript else None
        if found is not None or time.monotonic() >= deadline:
            return transcript, found
        time.sleep(max(poll, 0.05))


def judge(expected: str, found: Optional[str]) -> str:
    if found is None:
        return "timeout"
    return "confirmed" if found == normalise(expected) else "mismatch"
