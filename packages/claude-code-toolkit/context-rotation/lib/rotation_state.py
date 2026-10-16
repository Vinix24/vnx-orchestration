"""Per-session state for the context-rotation plugin.

Everything lives under <project>/.claude/rotation/:

  .gitignore                    `*`, written before anything else in the directory
  .lock                         one lock for every marker change in the project
  state/<session>.json          the guard's per-session counters
  handover-<session>.md         the handover a session writes
  ready-<session>.json          the handover is valid and covers all work so far
  outdated-<session>.json       work happened after the handover; never injected
  claimed-<origin>-<new>.json   a session took the handover; removed last
  archive/<time>-<origin>.md    every injected handover, trimmed to archive_keep
  injections.jsonl              one line per injection
  errors.log                    the last failures of the hooks

Handovers hold a session's working state and must never be committed, hence the
.gitignore. Standard library only.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple

import transcript_work

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_DIGITS = re.compile(r"^[0-9]+$")
MARKER_VERSION = 1
MARKER_KINDS = ("ready", "outdated", "claimed")
LOCK_TIMEOUT_SECONDS = 5.0
LOCK_POLL_SECONDS = 0.02
PS_TIMEOUT_SECONDS = 2.0
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
ARCHIVE_NAME = re.compile(r"^[0-9]{8}T[0-9]{12}Z-[A-Za-z0-9_-]+\.md$")


def is_safe_session_id(session_id: Any) -> bool:
    return isinstance(session_id, str) and _SAFE_ID.match(session_id) is not None


def project_dir_for(payload: Dict[str, Any], env: Dict[str, str]) -> str:
    return env.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()


def rotation_dir(project_dir: str) -> str:
    return os.path.join(project_dir, ".claude", "rotation")


def _in_rotation(project_dir: str, name: str) -> str:
    return os.path.join(os.path.abspath(rotation_dir(project_dir)), name)


def state_path(project_dir: str, session_id: str) -> str:
    return os.path.join(rotation_dir(project_dir), "state", session_id + ".json")


def handover_path(project_dir: str, session_id: str) -> str:
    return _in_rotation(project_dir, "handover-%s.md" % session_id)


def marker_path(project_dir: str, kind: str, session_id: str) -> str:
    return _in_rotation(project_dir, "%s-%s.json" % (kind, session_id))


def claimed_path(project_dir: str, origin: str, successor: str) -> str:
    return _in_rotation(project_dir, "claimed-%s-%s.json" % (origin, successor))


def archive_dir(project_dir: str) -> str:
    return _in_rotation(project_dir, "archive")


def injections_path(project_dir: str) -> str:
    return _in_rotation(project_dir, "injections.jsonl")


def ensure_gitignore(project_dir: str) -> None:
    """Create rotation/.gitignore once. An existing file is never touched."""
    directory = rotation_dir(project_dir)
    os.makedirs(directory, exist_ok=True)
    target = os.path.join(directory, ".gitignore")
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write("*\n")


def _atomic_write(target: str, data: bytes, prefix: str) -> None:
    directory = os.path.dirname(target)
    os.makedirs(directory, exist_ok=True)
    descriptor, temp = tempfile.mkstemp(dir=directory, prefix=prefix, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.replace(temp, target)
    except BaseException:
        if os.path.exists(temp):
            os.unlink(temp)
        raise


def write_json(target: str, document: Dict[str, Any]) -> None:
    _atomic_write(target, json.dumps(document, indent=1).encode("utf-8"), ".marker-")


def load_state(project_dir: str, session_id: str) -> Dict[str, Any]:
    """The session's state, or an empty dict when absent or unreadable."""
    if not is_safe_session_id(session_id):
        return {}
    try:
        with open(state_path(project_dir, session_id), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(project_dir: str, session_id: str, state: Dict[str, Any]) -> Optional[str]:
    """Write the state atomically. Raises ValueError for an unsafe session id."""
    if not is_safe_session_id(session_id):
        raise ValueError("unsafe session id")
    ensure_gitignore(project_dir)
    target = state_path(project_dir, session_id)
    _atomic_write(target, json.dumps(state).encode("utf-8"), ".state-")
    return target


ERROR_LOG_MAX_LINES = 50


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime(TIME_FORMAT)


def parse_utc(stamp: Any) -> Optional[datetime]:
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.strptime(stamp, TIME_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def log_error(project_dir: str, line: str) -> None:
    """Append one line to rotation/errors.log, keeping the last ERROR_LOG_MAX_LINES.

    Never raises: the log is a courtesy of a hook that already failed.
    """
    try:
        ensure_gitignore(project_dir)
        target = os.path.join(rotation_dir(project_dir), "errors.log")
        try:
            with open(target, "r", encoding="utf-8") as handle:
                lines = handle.read().splitlines()
        except OSError:
            lines = []
        lines = (lines + [line.replace("\n", " ")])[-ERROR_LOG_MAX_LINES:]
        _atomic_write(target, ("\n".join(lines) + "\n").encode("utf-8"), ".errors-")
    except Exception:
        # The log is best effort; a failure here must not mask the original error.
        return


# --- the project lock ----------------------------------------------------

class LockTimeout(Exception):
    """Another hook process held the project lock for longer than the timeout."""


_HELD: Dict[str, List[int]] = {}


def _acquire(descriptor: int, path: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise LockTimeout("%s was held for more than %.0f s" % (path, timeout))
            time.sleep(LOCK_POLL_SECONDS)


@contextlib.contextmanager
def project_lock(project_dir: str, timeout: float = LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    """Serialise marker changes across the parallel hook processes of a project.

    Re-entrant within one process. Writes the .gitignore first, as the lock file is
    the first file a marker change creates.
    """
    ensure_gitignore(project_dir)
    path = _in_rotation(project_dir, ".lock")
    held = _HELD.get(path)
    if held is not None:
        held[1] += 1
        try:
            yield
        finally:
            held[1] -= 1
        return
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        _acquire(descriptor, path, timeout)
        _HELD[path] = [descriptor, 1]
        try:
            yield
        finally:
            del _HELD[path]
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


# --- process identity ----------------------------------------------------

def _proc_start(pid: int) -> Optional[str]:
    try:
        with open("/proc/%d/stat" % pid, "rb") as handle:
            stat = handle.read().decode("ascii", "replace")
        return "proc:" + stat.rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def _ps_start(pid: int) -> Optional[str]:
    env = dict(os.environ, LC_ALL="C")
    try:
        done = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, env=env, timeout=PS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = " ".join(done.stdout.split())
    return "ps:" + text if done.returncode == 0 and text else None


def process_start(pid: int) -> Optional[str]:
    """When the process started, as an opaque string; None when it cannot be told."""
    started = _proc_start(pid)
    return started if started is not None else _ps_start(pid)


def process_identity(env: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """The session's `claude` process: CLAUDE_PID plus its start time.

    The process survives /clear and the session id does not, so this is what ties
    a handover to its successor. None when CLAUDE_PID is absent or the start time
    cannot be read: then nothing is injected automatically.
    """
    raw = env.get("CLAUDE_PID") or ""
    if not _DIGITS.match(raw) or int(raw) <= 0:
        return None
    pid = int(raw)
    started = process_start(pid)
    return {"pid": pid, "start": started} if started else None


# --- markers -------------------------------------------------------------

def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_bytes(path: Any) -> Optional[bytes]:
    if not isinstance(path, str) or not path:
        return None
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def load_marker(path: str) -> Optional[Dict[str, Any]]:
    """A marker of this version with the fields injection relies on, else None."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != MARKER_VERSION:
        return None
    if not is_safe_session_id(data.get("session_id")):
        return None
    if not isinstance(data.get("sha256"), str) or not isinstance(data.get("handover"), str):
        return None
    return data


def new_marker(session_id: str, handover: str, data: bytes, env: Dict[str, str],
               transcript_path: Any, tool_use_id: Any) -> Dict[str, Any]:
    return {
        "version": MARKER_VERSION,
        "session_id": session_id,
        "handover": handover,
        "sha256": sha256_hex(data),
        "chars": len(data.decode("utf-8", "replace")),
        "process": process_identity(env),
        "tmux_pane": env.get("TMUX_PANE") or None,
        "transcript": transcript_work.position(transcript_path),
        "tool_use_id": tool_use_id if isinstance(tool_use_id, str) and tool_use_id else None,
        "written_at": utc_now(),
        "announced": False,
    }


def mark_ready(project_dir: str, marker: Dict[str, Any]) -> str:
    """Write the session's ready-marker; an outdated one of the session goes."""
    session_id = marker["session_id"]
    target = marker_path(project_dir, "ready", session_id)
    with project_lock(project_dir):
        write_json(target, marker)
        stale = marker_path(project_dir, "outdated", session_id)
        if os.path.exists(stale):
            os.unlink(stale)
    return target


def mark_outdated(project_dir: str, session_id: str, reason: str, notified: bool) -> Optional[Dict[str, Any]]:
    """Rename the ready-marker to outdated with the reason. None when there was none."""
    source = marker_path(project_dir, "ready", session_id)
    target = marker_path(project_dir, "outdated", session_id)
    with project_lock(project_dir):
        marker = load_marker(source)
        if marker is None:
            return None
        os.replace(source, target)
        marker.update(outdated_at=utc_now(), outdated_reason=reason, notified=notified)
        write_json(target, marker)
    return marker


def take_outdated_notice(project_dir: str, session_id: str) -> Optional[Dict[str, Any]]:
    """An outdated marker the model was not told about yet; it counts as told after this."""
    target = marker_path(project_dir, "outdated", session_id)
    if not os.path.exists(target):
        return None
    with project_lock(project_dir):
        marker = load_marker(target)
        if marker is None or marker.get("notified", True):
            return None
        marker["notified"] = True
        write_json(target, marker)
    return marker


def mark_announced(project_dir: str, session_id: str, sha: str) -> bool:
    """True once per ready-marker: the first time the user is told it is saved."""
    target = marker_path(project_dir, "ready", session_id)
    with project_lock(project_dir):
        marker = load_marker(target)
        if marker is None or marker["sha256"] != sha or marker.get("announced"):
            return False
        marker["announced"] = True
        write_json(target, marker)
    return True


# --- the outdated rules (loop step 6) ------------------------------------

def prompt_outdates(prompt: Any) -> bool:
    """A prompt brings new work, except the /clear the ready-marker waits for."""
    text = prompt.strip() if isinstance(prompt, str) else ""
    return text != "/clear" and not text.startswith("/clear ")


def tool_call_outdates(tool_name: Any, tool_input: Any, handover: str) -> bool:
    """Every call except Read, Glob, Grep and a write of the handover itself."""
    return not transcript_work.allowed_tool(tool_name, tool_input, handover)


def over_age(marker: Dict[str, Any], settings: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    written = parse_utc(marker.get("written_at"))
    if written is None:
        return True
    current = now or datetime.now(timezone.utc)
    return (current - written).total_seconds() > settings["handover_max_age_minutes"] * 60


def outdated_reason(marker: Dict[str, Any], settings: Dict[str, Any], data: Optional[bytes],
                    now: Optional[datetime] = None) -> Optional[str]:
    """Why this ready-marker no longer describes the session's work; None while it does."""
    if data is None:
        return "the handover file is gone"
    if sha256_hex(data) != marker["sha256"]:
        return "the handover file changed after it was checked"
    if over_age(marker, settings, now):
        return "it is older than %d minutes" % settings["handover_max_age_minutes"]
    return transcript_work.stale_reason(marker.get("transcript"), marker["handover"], marker.get("tool_use_id"))


# --- claim, archive, injection log ---------------------------------------

def plan_archive(project_dir: str, origin: str, now: Optional[datetime] = None) -> str:
    """A free archive path for a handover of `origin`; names sort by time."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%S%fZ")
    base = os.path.join(archive_dir(project_dir), "%s-%s" % (stamp, origin))
    candidate, number = base + ".md", 1
    while os.path.exists(candidate):
        candidate = "%s-%d.md" % (base, number)
        number += 1
    return candidate


def claim(project_dir: str, source: str, successor: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Take a ready or outdated marker for a successor session by atomic rename.

    The claimed marker records the successor, the time and the archive path the
    handover is copied to next. None when the marker is gone or unreadable.
    """
    with project_lock(project_dir):
        marker = load_marker(source)
        if marker is None:
            return None
        target = claimed_path(project_dir, marker["session_id"], successor)
        try:
            os.rename(source, target)
        except FileNotFoundError:
            return None
        now = datetime.now(timezone.utc)
        marker.update(
            claimed_by=successor, claimed_at=now.strftime(TIME_FORMAT),
            archive=plan_archive(project_dir, marker["session_id"], now),
        )
        write_json(target, marker)
    return target, marker


def archive_handover(target: str, data: bytes) -> str:
    _atomic_write(target, data, ".archive-")
    return target


def log_injection(project_dir: str, record: Dict[str, Any]) -> None:
    line = json.dumps(record, sort_keys=True) + "\n"
    with project_lock(project_dir):
        with open(injections_path(project_dir), "a", encoding="utf-8") as handle:
            handle.write(line)


def read_injections(project_dir: str) -> List[Dict[str, Any]]:
    try:
        with open(injections_path(project_dir), "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
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


def finish_claim(project_dir: str, claimed: str, handover: Optional[str]) -> None:
    """After the emit: remove the original handover, then the claimed marker last.

    Never raises: the injection already happened, and a failing hook would make
    Claude Code drop the output that carries it. A leftover is listed by `resume`.
    """
    for path in (handover, claimed):
        if not path:
            continue
        try:
            if os.path.exists(path):
                os.unlink(path)
        except OSError as error:
            log_error(project_dir, "%s context-rotation: cleanup of %s failed: %s" % (utc_now(), path, error))


def trim_archive(project_dir: str, keep: int) -> int:
    """Keep the newest `keep` archived handovers. Returns how many were removed."""
    directory = archive_dir(project_dir)
    if not os.path.isdir(directory):
        return 0
    with project_lock(project_dir):
        names = sorted(n for n in os.listdir(directory) if ARCHIVE_NAME.match(n))
        surplus = names[:-keep] if keep > 0 else names
        for name in surplus:
            os.unlink(os.path.join(directory, name))
    return len(surplus)


# --- listing -------------------------------------------------------------

def markers_of_kind(project_dir: str, kind: str) -> List[Tuple[str, Dict[str, Any]]]:
    """(path, marker) for every readable marker of one kind, by file name."""
    directory = rotation_dir(project_dir)
    if not os.path.isdir(directory):
        return []
    found = []
    for name in sorted(os.listdir(directory)):
        if not (name.startswith(kind + "-") and name.endswith(".json")):
            continue
        path = _in_rotation(project_dir, name)
        marker = load_marker(path)
        if marker is not None:
            found.append((path, marker))
    return found


def _resumable(kind: str, marker: Dict[str, Any]) -> bool:
    """Something is left to continue from: the handover, or for a claim its archive copy."""
    copies = [marker["handover"]] + ([marker.get("archive")] if kind == "claimed" else [])
    return any(isinstance(path, str) and os.path.exists(path) for path in copies)


def pending_handovers(project_dir: str, exclude_session: Optional[str]) -> List[Dict[str, Any]]:
    """Handovers not continued yet: unfinished claims, then ready and outdated markers.

    Newest first within each group. The excluded session's own markers are left out,
    and so is a marker whose handover the user deleted.
    """
    claims = [
        {"kind": "claimed", "marker_path": path, "marker": marker, "at": marker.get("claimed_at") or ""}
        for path, marker in markers_of_kind(project_dir, "claimed")
        if _resumable("claimed", marker)
    ]
    waiting = [
        {"kind": kind, "marker_path": path, "marker": marker, "at": marker.get("written_at") or ""}
        for kind in ("ready", "outdated")
        for path, marker in markers_of_kind(project_dir, kind)
        if marker["session_id"] != exclude_session and _resumable(kind, marker)
    ]
    return sorted(claims, key=lambda e: e["at"], reverse=True) + sorted(waiting, key=lambda e: e["at"], reverse=True)


def archived_handovers(project_dir: str) -> List[Dict[str, Any]]:
    """Archived handovers, newest first, each with its last line from injections.jsonl."""
    directory = archive_dir(project_dir)
    if not os.path.isdir(directory):
        return []
    injected = {}
    for record in read_injections(project_dir):
        injected[record.get("archive")] = record
    names = sorted((n for n in os.listdir(directory) if ARCHIVE_NAME.match(n)), reverse=True)
    paths = [os.path.join(directory, name) for name in names]
    return [{"kind": "archived", "path": path, "injection": injected.get(path)} for path in paths]
