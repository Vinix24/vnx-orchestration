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

The /goal follow-up reads the same transcript (``turn_state``, and the CLI at the bottom that the
spawner's ``--goal-followup`` mode calls). Measured on claude 2.1.308 transcripts of T0 sessions
(the SEOcrawler rotations of 2026-10-22 and earlier /goal follow-ups on 2.1.284 to 2.1.295):

  - a turn is over at ``{"type": "system", "subtype": "turn_duration"}``, written once per turn
    after the Stop hooks let it end. ``assistant`` records with ``stop_reason: end_turn`` and
    ``system`` / ``stop_hook_summary`` are no end: a blocking Stop hook (a /goal that is not met)
    writes both and the turn goes on without a ``turn_duration``. A turn the operator interrupts
    can end on the user record ``[Request interrupted by user]`` without one;
  - a turn starts with a user record the model answers: a prompt, a task notification, a slash
    command such as ``/goal``. Tool results, ``isMeta`` records and ``<local-command-...>``
    output start none (a local command like ``/voortgang`` is a ``system`` record on 2.1.308);
  - ``/goal`` that ran is the user record ``<command-name>/goal</command-name>
    <command-message>goal</command-message> <command-args>...``;
  - a line submitted while a turn runs is no user record: claude queues it
    (``{"type": "queue-operation", "operation": "enqueue", "content": ...}``) and later hands it
    to the running turn as an attachment, as plain text. That is what became of the /goal in both
    rotations of 2026-10-22.
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_NOISE_PREFIXES = ("<local-command-", "<system-reminder>", "<user-prompt-submit-hook>",
                   "<hook", "<task-notification>", "Caveat:")
_COMMAND_NAME_RE = re.compile(r"<command-name>\s*(/[^<\s]+)\s*</command-name>")
_COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)
_COMMAND_FORM = ("<command-name>", "<command-message>")
TURN_END_SUBTYPE = "turn_duration"
INTERRUPTED_PREFIX = "[Request interrupted by user"
LOCAL_COMMAND_PREFIX = "<local-command-"
GOAL_COMMAND = "/goal"
# A queued line is recognised by its start; the spawner checks the screen with the same 40.
QUEUED_PREFIX_CHARS = 40


def _text_of(content: Any) -> str:
    """The text of a user message: a string, or the text blocks of a list (tool results carry
    none and so give the empty string)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text") or "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _user_text(record: Dict[str, Any]) -> Optional[str]:
    """The stripped text of a main-thread user record that is not meta ('' for a tool result);
    None for every other record."""
    if record.get("type") != "user" or record.get("isMeta") or record.get("isSidechain"):
        return None
    message = record.get("message")
    return _text_of(message.get("content") if isinstance(message, dict) else None).strip()


def real_user_text(record: Dict[str, Any]) -> Optional[str]:
    """The text of ``record`` when it is something a person (or a start argument) sent; None for
    meta records, other record types, hook and local-command output, and tool results."""
    text = _user_text(record)
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


def _records(path: Path) -> List[Dict[str, Any]]:
    """The records of a transcript in file order; [] when it cannot be read. The file may still
    be growing, so an unparseable line (a half-written last one) is skipped, not an error."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def first_user_message(path: Path) -> Optional[str]:
    """The first real user record of a transcript, normalised; None while there is none."""
    for record in _records(path):
        text = real_user_text(record)
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


# ── the turns of the successor, for the /goal follow-up ─────────────────────

def ends_turn(record: Dict[str, Any]) -> bool:
    """``system`` / ``turn_duration``, or the interrupt record a turn can end on without one."""
    if record.get("isSidechain"):
        return False
    if record.get("type") == "system":
        return record.get("subtype") == TURN_END_SUBTYPE
    return (_user_text(record) or "").startswith(INTERRUPTED_PREFIX)


def starts_turn(record: Dict[str, Any]) -> bool:
    """A user record the model answers: a prompt, a task notification, a slash command."""
    text = _user_text(record)
    return bool(text) and not text.startswith((LOCAL_COMMAND_PREFIX, INTERRUPTED_PREFIX))


def is_goal_command(record: Dict[str, Any]) -> bool:
    """The user record of a /goal that ran. A prompt that only mentions /goal, or carries it as
    pasted text, is none."""
    text = real_user_text(record)
    if text is None or not text.startswith(_COMMAND_FORM):
        return False
    name = _COMMAND_NAME_RE.search(text)
    return bool(name) and name.group(1) == GOAL_COMMAND


def is_queued(record: Dict[str, Any], line: str) -> bool:
    """``line`` was submitted while a turn ran and claude queued it instead of running it."""
    prefix = line.strip()[:QUEUED_PREFIX_CHARS]
    return (bool(prefix) and record.get("type") == "queue-operation"
            and record.get("operation") == "enqueue" and prefix in str(record.get("content") or ""))


@dataclass(frozen=True)
class TurnState:
    """What the transcript says after the kickoff entry, the first real user record."""

    kickoff: bool      # the kickoff entry is there
    turns_ended: int   # turn ends after it
    idle: bool         # a turn ended after it and no turn started since the last end
    goal: bool         # a /goal ran after it
    queued: bool       # the given line was queued after it


def turn_state(path: Path, line: str = "") -> TurnState:
    records = _records(path)
    kickoff = next((i for i, r in enumerate(records) if real_user_text(r) is not None), None)
    if kickoff is None:
        return TurnState(kickoff=False, turns_ended=0, idle=False, goal=False, queued=False)
    ended, running, goal, queued = 0, True, False, False
    for record in records[kickoff + 1:]:
        if ends_turn(record):
            ended, running = ended + 1, False
        elif starts_turn(record):
            running = True
        goal = goal or is_goal_command(record)
        queued = queued or (bool(line) and is_queued(record, line))
    return TurnState(kickoff=True, turns_ended=ended, idle=ended > 0 and not running,
                     goal=goal, queued=queued)


def main(argv: Optional[List[str]] = None) -> int:
    """For the spawner's --goal-followup mode: one question, answered by the exit code
    (0 yes, 1 no, 2 usage). ``idle``: the kickoff turn and every turn after it ended. ``goal``:
    a /goal ran after the kickoff entry. ``queued``: the line in --line-file was queued."""
    parser = argparse.ArgumentParser(description="read the successor's transcript")
    parser.add_argument("question", choices=("idle", "goal", "queued"))
    parser.add_argument("--transcript", required=True)
    parser.add_argument("--line-file", default="", help="queued: the file with the typed line")
    args = parser.parse_args(argv)
    line = ""
    if args.question == "queued":
        if not args.line_file:
            parser.error("queued needs --line-file")
        try:
            line = Path(args.line_file).read_text(encoding="utf-8")
        except OSError as exc:
            parser.error(f"cannot read --line-file: {exc}")
    state = turn_state(Path(args.transcript), line)
    return 0 if getattr(state, args.question) else 1


if __name__ == "__main__":
    sys.exit(main())
