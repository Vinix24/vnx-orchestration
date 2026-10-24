#!/usr/bin/env python3
"""t0_rotation_delivery_job.py — the server-side job of a T0 rotation (OI-2102, OI-2218).

``scripts/t0_rotate_spawn.sh`` writes a script file that ``tmux run-shell -b`` runs on the tmux
server, after the spawner printed its last stdout line. The server outlives the old window, which
this job may close. One job, in this order:

  1. wait (default 120 s) for the successor's transcript to hold a real user record and judge it
     against the first message (``scripts/lib/t0_rotation_delivery.py``);
  2. book the ``t0_context_rotation_delivery`` receipt through ``t0_rotation_state.py event`` and
     require that the ledger took it, BEFORE ``delivery.json`` exists and before any window is
     touched (ADR-005). A refused or hung append leaves both windows open and says so in both;
  3. write ``delivery.json`` (atomic);
  4. ``confirmed``: hand over the /goal follow-up when ``goal.txt`` exists (the spawner's
     ``--goal-followup`` mode, waited for, given the transcript of step 1: it reads the end of the
     kickoff turn and the arrival of /goal there), and only then close the old window, while the
     new window still exists.
     ``mismatch`` / ``timeout``: type nothing, close nothing, name the outcome in both windows.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

from atomic_io import atomic_write_json
from t0_rotation_delivery import await_first_message, judge

DELIVERY_TRIGGER = "t0_context_rotation_delivery"
DELIVERY_FILE = "delivery.json"
FOUND_CHARS = 200
STATE_PY = Path(__file__).resolve().parent / "lib" / "t0_rotation_state.py"
RECEIPT_TIMEOUT = 60
EXIT_RECEIPT_FAILED = 3


def _tmux(tmux: str, *argv: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run([tmux, *argv], capture_output=True, text=True, timeout=30, check=False)


def window_exists(tmux: str, window: str) -> bool:
    listing = _tmux(tmux, "list-windows", "-a", "-F", "#{window_id}")
    return listing.returncode == 0 and window in listing.stdout.split()


def _say(tmux: str, windows: List[str], message: str) -> None:
    for window in windows:
        _tmux(tmux, "display-message", "-t", window, message)


def _book_receipt(run_dir: Path, payload: Dict[str, Any]) -> Optional[str]:
    """Book the delivery receipt. None when the ledger took it, else why not. ``event`` exits
    non-zero when its append failed, so the exit code is the acknowledgement."""
    argv = [sys.executable, str(STATE_PY), "event", "--trigger", DELIVERY_TRIGGER,
            "--file", str(run_dir / DELIVERY_FILE),
            "--field", f"outcome={payload['outcome']}",
            "--field", f"run_id={run_dir.name}",
            "--field", f"session_uuid={payload['session_uuid']}",
            "--field", f"new_window={payload['new_window']}",
            "--field", f"old_window={payload['old_window']}",
            "--field", f"old_window_close={payload['old_window_close']}"]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=RECEIPT_TIMEOUT,
                              check=False)
    except subprocess.TimeoutExpired:
        return f"the receipt append did not finish in {RECEIPT_TIMEOUT}s"
    if done.returncode != 0:
        detail = (done.stderr or "").strip().splitlines()
        return f"the ledger refused the receipt (exit {done.returncode}" + (
            f": {detail[-1]})" if detail else ")")
    return None


def _close_decision(outcome: str, tmux: str, new_window: str) -> str:
    if outcome != "confirmed":
        return "left_open"
    return "closing" if window_exists(tmux, new_window) else "skipped_new_window_gone"


def _goal_followup(args: argparse.Namespace, transcript: Path) -> int:
    """The /goal follow-up of the spawner, run to its end, on the transcript the delivery check
    read. Its own outcome is booked and spoken by the follow-up itself; the old window closes
    after it either way."""
    done = subprocess.run(
        ["bash", args.followup_script, "--goal-followup", "--target", args.new_window,
         "--goal-file", args.goal_file, "--event-file", args.goal_file,
         "--transcript", str(transcript)],
        check=False)
    return done.returncode


def run_job(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    both = [args.new_window, args.old_window]
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
    problem = _book_receipt(run_dir, payload)
    if problem:
        sys.stderr.write(f"t0_rotation_delivery_job: {problem}; delivery.json not written, "
                         "nothing closed, no /goal follow-up\n")
        _say(args.tmux, both, f"VNX t0-rotate: delivery receipt NOT booked ({problem}); both "
                              "windows stay open, no /goal follow-up was sent")
        return EXIT_RECEIPT_FAILED
    atomic_write_json(run_dir / DELIVERY_FILE, payload)
    if outcome != "confirmed":
        _say(args.tmux, both, f"VNX t0-rotate: first message NOT confirmed ({outcome}); both "
                              f"windows stay open, nothing was typed, see {run_dir / DELIVERY_FILE}")
        return 1
    if transcript and args.goal_file and Path(args.goal_file).is_file():
        _goal_followup(args, transcript)
    if close == "closing" and window_exists(args.tmux, args.new_window):
        _tmux(args.tmux, "kill-window", "-t", args.old_window)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="the server-side job of a T0 rotation")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--session-uuid", required=True)
    parser.add_argument("--config-dir", required=True)
    parser.add_argument("--expected", required=True)
    parser.add_argument("--old-window", required=True)
    parser.add_argument("--new-window", required=True)
    parser.add_argument("--tmux", default="tmux")
    parser.add_argument("--goal-file", default="", help="the /goal follow-up line; empty: no goal")
    parser.add_argument("--followup-script", default="",
                        help="the spawner, run in --goal-followup mode")
    parser.add_argument("--timeout", type=float, default=float(os.environ.get(
        "VNX_T0_ROTATE_DELIVERY_TIMEOUT", "120")))
    parser.add_argument("--poll", type=float, default=float(os.environ.get(
        "VNX_T0_ROTATE_DELIVERY_POLL", "1")))
    args = parser.parse_args(argv)
    if args.goal_file and not args.followup_script:
        parser.error("--goal-file needs --followup-script")
    return run_job(args)


if __name__ == "__main__":
    raise SystemExit(main())
