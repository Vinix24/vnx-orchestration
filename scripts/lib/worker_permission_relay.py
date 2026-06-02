#!/usr/bin/env python3
"""worker_permission_relay.py — VNX worker-permission relay governance.

A DETACHED tmux worker (interactive ``claude`` on the subscription lane) has no
human at the keyboard. When Claude Code raises a permission prompt for an
off-allow-list tool/command ("Do you want to proceed?"), the worker silently
HANGS until its deadline. This module is the governance mechanism that prevents
that hang while keeping the operator as the human gate.

The model
---------
- The operator declares a short, explicit auto-accept WINDOW (``vnx permission
  window-open --minutes N``). Inside the window, ROUTINE prompts are auto-approved.
- CATASTROPHIC, irreversible operations (``rm -rf``, ``DROP TABLE``, ``mkfs`` …)
  ALWAYS escalate to the operator — even inside an open window. The window never
  grants a blank cheque for destructive ops.
- Outside a window, EVERY prompt escalates.
- Escalation = a durable record on disk that T0 surfaces in chat; the operator
  answers and the answer is relayed back into the worker's pane via send-keys.

This is intentionally infrastructure-free: no popups, no IPC daemon. The
window + the escalation records + the relay tick are plain files and tmux
``capture-pane`` / ``send-keys`` calls.

Hard send-keys rule (proven bug — see memory feedback-tmux-sendkeys-enter):
Enter is ALWAYS a SEPARATE keystroke and send-keys ALWAYS targets the explicit
session id. A combined "1\\n" misses delivery; an empty ``-t`` target lands in
the wrong pane.

BILLING SAFETY: no Anthropic SDK; only tmux subprocess calls via an injected
runner abstraction.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Window file lives directly under the state dir; escalation records under a
# dedicated subdir keyed by dispatch id.
WINDOW_FILENAME = "permission_window.json"
ESCALATIONS_DIRNAME = "permission_escalations"
RELAY_DIRNAME = "permission_relay"

# Prompt markers the Claude Code TUI prints when it needs confirmation. Matched
# case-insensitively and kept tolerant: never pinned to one Claude Code version's
# exact wording (the lane has been burned by version-specific TUI scraping before).
PROMPT_MARKERS = (
    "do you want to proceed",
    "do you want to make this edit",
    "do you want to create",
    "requires confirmation",
    "requires your permission",
)

# Box-drawing / prompt-glyph characters stripped when recovering an echoed command.
_BOX_CHARS = "│╭╮╰╯─━┃┏┓┗┛┌┐└┘╔╗╚╝║═❯>•*●○◍◆◇▶"

# Prompt-box title rows (the command/description follow the title). Matched
# case-insensitively; a row that is exactly a title — or ends in " command" /
# " file" — resets the candidate scan so the COMMAND (first content after the
# title), not the prose description, is returned.
_TITLE_ROWS = {
    "bash command", "shell command", "command", "tool use",
    "edit file", "write file", "create file", "read file", "edit", "write",
}


# ---------------------------------------------------------------------------
# Catastrophic hard-list
# ---------------------------------------------------------------------------
# Conservative by design: a false negative (a destructive op auto-approved) is
# far worse than a false positive (a needless escalation). When in doubt, match.
#
# NOTE: ``git push --force`` / ``-f`` / ``--force-with-lease`` are deliberately
# NOT catastrophic — they are recoverable (reflog/remote history) and the lane
# already runs on disposable per-dispatch worktree branches. They are allowed.
CATASTROPHIC_PATTERNS = [
    # rm with BOTH a recursive and a force flag, in any flag arrangement:
    # rm -rf, rm -fr, rm -Rf, rm -r -f, rm -f -R, rm --recursive --force.
    re.compile(
        r"\brm\b(?=.*(?:-[a-zA-Z]*[rR]|--recursive))(?=.*(?:-[a-zA-Z]*f|--force))"
    ),
    # SQL drops / truncate (DROP TABLE/DATABASE/SCHEMA, TRUNCATE …).
    re.compile(r"\bDROP\s+(TABLE|DATABASE|SCHEMA)\b", re.IGNORECASE),
    re.compile(r"\bTRUNCATE\b", re.IGNORECASE),
    # Filesystem creation / raw block-device writes.
    re.compile(r"\bmkfs(\.\w+)?\b", re.IGNORECASE),
    re.compile(r">\s*/dev/(sd|hd|nvme|disk|vd)", re.IGNORECASE),
    re.compile(r"\bdd\b.*\bof=/dev/", re.IGNORECASE),
    # Hard reset onto a remote ref discards local commits to match the remote.
    re.compile(
        r"\bgit\s+reset\s+--hard\s+\S*\b(origin|upstream|remotes?)/", re.IGNORECASE
    ),
    # Large-delete heuristics.
    re.compile(r"\bfind\b.*-delete\b", re.IGNORECASE),
    re.compile(r"\bfind\b.*-exec\s+rm\b", re.IGNORECASE),
    re.compile(r"\bgit\s+clean\b(?=.*-[a-z]*f)(?=.*-[a-z]*d)", re.IGNORECASE),
    # chmod/chown -R on a filesystem root.
    re.compile(r"\bch(mod|own)\b.*-R\b.*\s/(\s|$)", re.IGNORECASE),
]

# rm recursive targeting a filesystem/home root is catastrophic even without an
# explicit force flag (interactive rm -r / can still wipe a tree). Checked as a
# dedicated heuristic so the flag-pair regex above stays precise.
_RM_RECURSIVE_RE = re.compile(r"\brm\b(?=.*(?:-[a-zA-Z]*[rR]|--recursive))")
_RM_ROOT_TARGET_RE = re.compile(r"\brm\b.*\s(/|~|~/|\$HOME|/\*)(\s|$|/\*)")


def is_catastrophic(command: str) -> bool:
    """Return True if *command* is an irreversible op that must always escalate.

    Conservative: matches a hard-list of regexes plus a recursive-rm-on-root
    heuristic. False positives (needless escalation) are acceptable; false
    negatives (destructive auto-approve) are not.
    """
    cmd = (command or "").strip()
    if not cmd:
        return False
    for pat in CATASTROPHIC_PATTERNS:
        if pat.search(cmd):
            return True
    if _RM_RECURSIVE_RE.search(cmd) and _RM_ROOT_TARGET_RE.search(cmd):
        return True
    return False


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


def _parse_iso_epoch(value: "str | None") -> "float | None":
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except (ValueError, TypeError):
        return None


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
# Permission window
# ---------------------------------------------------------------------------
class PermissionWindow:
    """Operator-controlled auto-accept window persisted at ``permission_window.json``.

    A window is OPEN only if ``open == true`` AND ``now < expires_at``. Expiry is
    authoritative: an expired window reads as closed regardless of the ``open``
    flag. A missing file reads as closed.
    """

    def __init__(self, state_dir: "str | Path | None" = None) -> None:
        self._state_dir = _coerce_state_dir(state_dir)

    @property
    def path(self) -> Path:
        return self._state_dir / WINDOW_FILENAME

    def _read(self) -> "dict | None":
        p = self.path
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("permission_window: read failed (%s)", exc)
            return None

    def open(
        self,
        minutes: float,
        reason: "str | None" = None,
        *,
        opened_by: str = "operator",
        now: "float | None" = None,
    ) -> dict:
        """Open the window for *minutes* from now. Returns the written record."""
        if minutes <= 0:
            raise ValueError("window minutes must be > 0")
        ts = now if now is not None else time.time()
        expires = ts + minutes * 60.0
        record = {
            "open": True,
            "opened_at": _now_iso(ts),
            "expires_at": _now_iso(expires),
            "opened_by": opened_by,
            "reason": reason or "",
        }
        _atomic_write_json(self.path, record)
        return record

    def close(self, *, now: "float | None" = None) -> dict:
        """Close the window. Idempotent — closing an absent/closed window is fine."""
        existing = self._read() or {}
        record = {
            "open": False,
            "opened_at": existing.get("opened_at", ""),
            "expires_at": _now_iso(now),
            "opened_by": existing.get("opened_by", "operator"),
            "reason": existing.get("reason", ""),
        }
        _atomic_write_json(self.path, record)
        return record

    def is_open(self, *, now: "float | None" = None) -> bool:
        data = self._read()
        if not data or not data.get("open"):
            return False
        expires = _parse_iso_epoch(data.get("expires_at"))
        if expires is None:
            return False
        ts = now if now is not None else time.time()
        return ts < expires

    def status(self, *, now: "float | None" = None) -> dict:
        """Return {open, remaining_seconds, expires_at, opened_by, reason}."""
        data = self._read() or {}
        ts = now if now is not None else time.time()
        expires = _parse_iso_epoch(data.get("expires_at"))
        is_open = bool(data.get("open")) and expires is not None and ts < expires
        remaining = int(max(0, expires - ts)) if (is_open and expires is not None) else 0
        return {
            "open": is_open,
            "remaining_seconds": remaining,
            "expires_at": data.get("expires_at", ""),
            "opened_by": data.get("opened_by", ""),
            "reason": data.get("reason", ""),
        }


# ---------------------------------------------------------------------------
# Pending-command parsing
# ---------------------------------------------------------------------------
def _strip_box(line: str) -> str:
    """Strip box-drawing chars + prompt glyphs from a captured pane line."""
    return line.strip().strip(_BOX_CHARS).strip()


def parse_pending_command(pane_text: str) -> Optional[str]:
    """Extract the command/tool a permission prompt is asking about.

    Returns the command string when *pane_text* shows a permission prompt, else
    None. Two extraction strategies:
      1. The ``Bash(<cmd>)`` / ``Tool(<arg>)`` token Claude prints for tool calls.
      2. The command echoed inside the prompt box, just above the
         "Do you want to proceed?" line.
    """
    if not pane_text:
        return None
    low = pane_text.lower()
    if not any(marker in low for marker in PROMPT_MARKERS):
        return None

    # Strategy 1: explicit tool-call token, e.g. "Bash(rm -rf /tmp/x)".
    tool_matches = re.findall(r"\b(?:Bash|Shell)\(([^)\n]*)\)", pane_text)
    for cand in reversed(tool_matches):
        cand = cand.strip()
        if cand:
            return cand

    # Strategy 2: command echoed in the box above the marker line. The command
    # is the FIRST content row after the title row ("Bash command"); the prose
    # description follows it. Scan the lines just above the marker, resetting on
    # the title so a leading "● I'll …" narration line is discarded.
    lines = pane_text.splitlines()
    marker_idx = None
    for i, ln in enumerate(lines):
        if any(mk in ln.lower() for mk in PROMPT_MARKERS):
            marker_idx = i
            break
    if marker_idx is None:
        return None

    candidates: "list[str]" = []
    for ln in lines[max(0, marker_idx - 12):marker_idx]:
        cand = _strip_box(ln)
        if not cand:
            continue
        low_c = cand.lower()
        # Title row → command/description start fresh after it.
        if low_c in _TITLE_ROWS or low_c.endswith(" command") or low_c.endswith(" file"):
            candidates = []
            continue
        # Skip numbered option rows ("1. Yes", "2. No …").
        if re.match(r"^\d+\.\s", cand):
            continue
        candidates.append(cand)
    # First content row after the title is the command; the rest is description.
    return candidates[0] if candidates else None


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------
def decide(command: str, window: "PermissionWindow | bool") -> str:
    """Return "auto_approve" or "escalate".

    Escalate when the command is catastrophic OR the window is closed; otherwise
    auto-approve. *window* may be a PermissionWindow or a pre-evaluated bool.
    """
    if is_catastrophic(command):
        return "escalate"
    if isinstance(window, PermissionWindow):
        window_open = window.is_open()
    else:
        window_open = bool(window)
    return "auto_approve" if window_open else "escalate"


# ---------------------------------------------------------------------------
# Escalation records
# ---------------------------------------------------------------------------
def _escalations_dir(state_dir: Path) -> Path:
    return state_dir / ESCALATIONS_DIRNAME


def _escalation_path(state_dir: Path, dispatch_id: str) -> Path:
    return _escalations_dir(state_dir) / f"{dispatch_id}.json"


def write_escalation(
    dispatch_id: str,
    command: str,
    reason: str,
    *,
    state_dir: "str | Path | None" = None,
    now: "float | None" = None,
) -> Path:
    """Write a pending escalation record (atomic). *reason* ∈ {catastrophic, window_closed}.

    Idempotent: if a pending record for the same command already exists it is
    left untouched (so a repeated tick does not churn captured_at).
    """
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
    sd = _coerce_state_dir(state_dir)
    record = read_escalation(dispatch_id, state_dir=sd)
    if record is None:
        return None
    record["status"] = "approved" if approved else "denied"
    record["resolved_at"] = _now_iso(now)
    _atomic_write_json(_escalation_path(sd, dispatch_id), record)
    return record


# ---------------------------------------------------------------------------
# Fingerprint persistence (idempotency)
# ---------------------------------------------------------------------------
def _fingerprint_path(state_dir: Path, dispatch_id: str) -> Path:
    return state_dir / RELAY_DIRNAME / f"{dispatch_id}.last"


def _fingerprint(command: str) -> str:
    return hashlib.sha1(command.encode("utf-8")).hexdigest()


def _read_fingerprint(state_dir: Path, dispatch_id: str) -> "str | None":
    p = _fingerprint_path(state_dir, dispatch_id)
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _write_fingerprint(state_dir: Path, dispatch_id: str, fp: str) -> None:
    p = _fingerprint_path(state_dir, dispatch_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(fp, encoding="utf-8")
        os.replace(tmp, p)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Relay tick
# ---------------------------------------------------------------------------
def _capture_pane(runner, session_id: str) -> str:
    """Capture the pane for *session_id* via the injected tmux runner."""
    res = runner.run(["capture-pane", "-t", session_id, "-p"])
    if getattr(res, "returncode", 1) != 0:
        return ""
    return getattr(res, "stdout", "") or ""


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


def relay_tick(
    session_id: str,
    dispatch_id: str,
    runner,
    *,
    state_dir: "str | Path | None" = None,
    window: "PermissionWindow | None" = None,
) -> str:
    """Inspect the worker pane once and act on any pending permission prompt.

    Returns one of:
      - "idle"           — no prompt on the pane
      - "auto_approve"    — routine prompt inside an open window; sent option 1+Enter
      - "escalate"        — catastrophic or window-closed; wrote escalation record
      - "already_handled" — same prompt already actioned this run (idempotent)

    Idempotent: the last-handled prompt fingerprint is persisted, so a prompt
    still visible on the next tick (before the TUI clears it) is not re-actioned.
    """
    if not session_id:
        raise ValueError("relay_tick requires an explicit session id")
    sd = _coerce_state_dir(state_dir)
    win = window if window is not None else PermissionWindow(sd)

    pane_text = _capture_pane(runner, session_id)
    command = parse_pending_command(pane_text)
    if command is None:
        return "idle"

    fp = _fingerprint(command)
    if _read_fingerprint(sd, dispatch_id) == fp:
        return "already_handled"

    action = decide(command, win)
    if action == "auto_approve":
        _send_approval(runner, session_id)
        _write_fingerprint(sd, dispatch_id, fp)
        logger.info(
            "permission_relay: auto-approved routine prompt dispatch=%s cmd=%r",
            dispatch_id, command,
        )
        return "auto_approve"

    # escalate
    reason = "catastrophic" if is_catastrophic(command) else "window_closed"
    write_escalation(dispatch_id, command, reason, state_dir=sd)
    _write_fingerprint(sd, dispatch_id, fp)
    logger.warning(
        "permission_relay: escalated prompt dispatch=%s reason=%s cmd=%r (NO keys sent)",
        dispatch_id, reason, command,
    )
    return "escalate"


# ---------------------------------------------------------------------------
# Relay loop (used by the lane background thread)
# ---------------------------------------------------------------------------
def run_relay_loop(
    session_id: str,
    dispatch_id: str,
    runner,
    stop_event,
    *,
    state_dir: "str | Path | None" = None,
    interval: float = 3.0,
) -> None:
    """Tick ``relay_tick`` every *interval* seconds until *stop_event* is set.

    Never raises out of the loop — a relay failure must not take down the lane.
    """
    sd = _coerce_state_dir(state_dir)
    win = PermissionWindow(sd)
    while not stop_event.is_set():
        try:
            relay_tick(session_id, dispatch_id, runner, state_dir=sd, window=win)
        except Exception as exc:  # noqa: BLE001 — relay must never crash the lane
            logger.debug("permission_relay: tick failed dispatch=%s (%s)", dispatch_id, exc)
        stop_event.wait(interval)


@dataclass
class RelayHandle:
    """Handle for a running relay loop thread so the lane can stop it at teardown."""

    thread: object
    stop_event: object
