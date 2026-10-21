#!/usr/bin/env python3
"""t0_rotation_delivery.py — did the successor T0 receive its first message? (OI-2102)

The spawner starts the successor with the first message as the positional prompt argument of
``claude`` (``/kickoff rotatie <run_id>``) and a session id it generated itself. Nothing is typed
into the input box, so the remaining question is whether the message arrived. This job answers it
from the successor's transcript, ``<config dir>/projects/*/<session uuid>.jsonl``:

  1. wait (default 120 s) for that file to exist and hold a real user record,
  2. compare that record to the first message,
  3. write ``delivery.json`` in the run dir (atomic) and book one
     ``t0_context_rotation_delivery`` receipt through ``t0_rotation_state.py event``,
  4. ``confirmed``: close the old window, but only while the new window still exists.
     ``mismatch`` / ``timeout``: leave both windows open and say so in both with
     ``tmux display-message``. Nothing is ever typed.

Measured shape (claude 2.1.267, a transcript where ``/kickoff`` was typed). A real user record is
``{"type": "user", "isSidechain": false, "origin": {"kind": "human"}, "message": {"role": "user",
"content": "<command-message>kickoff</command-message>\\n<command-name>/kickoff</command-name>"
[+ "\\n<command-args>…</command-args>"]}, "sessionId": …}``. Skipped before it: ``mode``,
``bridge-session``, ``file-history-snapshot``, ``queue-operation`` and ``system`` records, user
records with ``isMeta: true`` (the local-command caveat), ``<local-command-stdout>`` output,
hook and system-reminder text, and tool results.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from atomic_io import atomic_write_json

DELIVERY_TRIGGER = "t0_context_rotation_delivery"
DELIVERY_FILE = "delivery.json"
FOUND_CHARS = 200
STATE_PY = Path(__file__).resolve().with_name("t0_rotation_state.py")

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


def _tmux(tmux: str, *argv: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run([tmux, *argv], capture_output=True, text=True, timeout=30, check=False)


def window_exists(tmux: str, window: str) -> bool:
    listing = _tmux(tmux, "list-windows", "-a", "-F", "#{window_id}")
    return listing.returncode == 0 and window in listing.stdout.split()


def _say(tmux: str, window: str, message: str) -> None:
    _tmux(tmux, "display-message", "-t", window, message)


def _book_receipt(run_dir: Path, payload: Dict[str, Any]) -> None:
    subprocess.run([sys.executable, str(STATE_PY), "event", "--trigger", DELIVERY_TRIGGER,
                    "--file", str(run_dir / DELIVERY_FILE),
                    "--field", f"outcome={payload['outcome']}",
                    "--field", f"run_id={run_dir.name}",
                    "--field", f"session_uuid={payload['session_uuid']}",
                    "--field", f"new_window={payload['new_window']}",
                    "--field", f"old_window={payload['old_window']}",
                    "--field", f"old_window_close={payload['old_window_close']}"],
                   capture_output=True, text=True, timeout=60, check=False)


def _close_decision(outcome: str, tmux: str, new_window: str) -> str:
    if outcome != "confirmed":
        return "left_open"
    return "closing" if window_exists(tmux, new_window) else "skipped_new_window_gone"


def run_job(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    transcript, found = await_first_message(Path(args.config_dir), args.session_uuid,
                                            timeout=args.timeout, poll=args.poll)
    outcome = judge(args.expected, found)
    close = _close_decision(outcome, args.tmux, args.new_window)
    payload: Dict[str, Any] = {
        "outcome": outcome, "expected": args.expected, "found": (found or "")[:FOUND_CHARS],
        "transcript": str(transcript) if transcript else None,
        "session_uuid": args.session_uuid, "new_window": args.new_window,
        "old_window": args.old_window, "old_window_close": close,
    }
    atomic_write_json(run_dir / DELIVERY_FILE, payload)
    _book_receipt(run_dir, payload)
    if close == "closing":
        _tmux(args.tmux, "kill-window", "-t", args.old_window)
    elif outcome != "confirmed":
        notice = (f"VNX t0-rotate: first message NOT confirmed ({outcome}); both windows stay "
                  f"open, see {run_dir / DELIVERY_FILE}")
        for window in (args.new_window, args.old_window):
            _say(args.tmux, window, notice)
    return 0 if outcome == "confirmed" else 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="check the first-message delivery of a T0 rotation")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--session-uuid", required=True)
    parser.add_argument("--config-dir", required=True)
    parser.add_argument("--expected", required=True)
    parser.add_argument("--old-window", required=True)
    parser.add_argument("--new-window", required=True)
    parser.add_argument("--tmux", default="tmux")
    parser.add_argument("--timeout", type=float, default=float(os.environ.get(
        "VNX_T0_ROTATE_DELIVERY_TIMEOUT", "120")))
    parser.add_argument("--poll", type=float, default=float(os.environ.get(
        "VNX_T0_ROTATE_DELIVERY_POLL", "1")))
    return run_job(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
