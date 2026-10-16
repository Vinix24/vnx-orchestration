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
  errors.log                    the last failures of the hooks, and rejected markers

Handovers hold a session's working state and must never be committed, hence the
.gitignore.

Every file here is an input, not a fact: any process of the user can write one,
and the origin session can rewrite its handover while a successor delivers it.
So every name and path read from a marker passes marker_problem first, files are
opened only when they are regular (regular_file), a handover is removed only
while it still holds the claimed bytes, and an archive copy is never overwritten.
Standard library only.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple

import regular_file
import transcript_work

_SAFE_ID = re.compile(r"[A-Za-z0-9_-]+")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_DIGITS = re.compile(r"^[0-9]+$")
# Two ids share one file name in claimed-<origin>-<successor>.json.
MAX_SESSION_ID = 120
MARKER_VERSION = 1
MARKER_KINDS = ("ready", "outdated", "claimed")
LOCK_TIMEOUT_SECONDS = 5.0
LOCK_POLL_SECONDS = 0.02
PS_TIMEOUT_SECONDS = 2.0
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
ARCHIVE_NAME = re.compile(r"[0-9]{8}T[0-9]{12}Z-[A-Za-z0-9_-]+\.md")


def is_safe_session_id(session_id: Any) -> bool:
    """One plain file name: letters, digits, "_" and "-" only, so no separator, dot or NUL."""
    return (isinstance(session_id, str) and len(session_id) <= MAX_SESSION_ID
            and _SAFE_ID.fullmatch(session_id) is not None)


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
    raw = read_bytes(state_path(project_dir, session_id))
    try:
        data = json.loads(raw.decode("utf-8")) if raw is not None else None
    except ValueError:
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


def errors_path(project_dir: str) -> str:
    return _in_rotation(project_dir, "errors.log")


def error_lines(project_dir: str) -> List[str]:
    raw = read_bytes(errors_path(project_dir))
    return raw.decode("utf-8", "replace").splitlines() if raw is not None else []


def log_error(project_dir: str, line: str) -> None:
    """Append one line to rotation/errors.log, keeping the last ERROR_LOG_MAX_LINES.

    Never raises: the log is a courtesy of a hook that already failed.
    """
    try:
        ensure_gitignore(project_dir)
        target = errors_path(project_dir)
        lines = (error_lines(project_dir) + [line.replace("\n", " ")])[-ERROR_LOG_MAX_LINES:]
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
    """The bytes of a regular file; None when absent, unreadable, a symlink, a FIFO or a device."""
    return regular_file.read_regular(path)


# --- what a marker may say ---------------------------------------------------

def _plain_absolute(path: Any) -> bool:
    return isinstance(path, str) and os.path.isabs(path) and "\0" not in path


def _same_place(path: Any, expected: str) -> bool:
    """`path` names the file `expected` names: the same name in the same directory."""
    if not _plain_absolute(path) or os.path.basename(path) != os.path.basename(expected):
        return False
    return os.path.realpath(os.path.dirname(path)) == os.path.realpath(os.path.dirname(expected))


def archive_problem(project_dir: str, archive: Any) -> Optional[str]:
    """Why `archive` is no place for an archive copy; None when it is a direct child of archive_dir."""
    if not _plain_absolute(archive):
        return "its archive is not an absolute path"
    if ARCHIVE_NAME.fullmatch(os.path.basename(archive)) is None:
        return "its archive is not named like an archive copy"
    if os.path.islink(archive):
        return "its archive is a symlink"
    if os.path.dirname(os.path.realpath(archive)) != os.path.realpath(archive_dir(project_dir)):
        return "its archive is not in %s" % archive_dir(project_dir)
    return None


def _position_problem(position: Any) -> Optional[str]:
    if position is None:
        return None
    size = position.get("size") if isinstance(position, dict) else None
    if (not isinstance(position, dict) or not _plain_absolute(position.get("path"))
            or not isinstance(size, int) or isinstance(size, bool) or size < 0
            or not isinstance(position.get("last_uuid"), (str, type(None)))):
        return "its transcript position is not a path, a size and an entry id"
    return None


def marker_kind(path: str) -> Optional[str]:
    name = os.path.basename(path)
    return next((k for k in MARKER_KINDS if name.startswith(k + "-") and name.endswith(".json")), None)


def _marker_name(kind: str, marker: Dict[str, Any]) -> Optional[str]:
    if kind != "claimed":
        return "%s-%s.json" % (kind, marker["session_id"])
    successor = marker.get("claimed_by")
    return "claimed-%s-%s.json" % (marker["session_id"], successor) if is_safe_session_id(successor) else None


def marker_problem(project_dir: str, path: str, marker: Any) -> Optional[str]:
    """Why the marker read from `path` must not be used; None when it may.

    The one rule for every reader of marker fields: a marker of this version whose
    session_id is a plain file name, whose sha256 (and for a claim claimed_sha256)
    is a digest, whose own file name is the one that id gives for its kind, whose
    handover is that session's handover path, whose archive (always for a claim)
    is a direct child of archive_dir, and whose transcript position and tool-use
    id have their shapes.
    """
    if not isinstance(marker, dict) or marker.get("version") != MARKER_VERSION:
        return "it is not a version %d marker" % MARKER_VERSION
    if not is_safe_session_id(marker.get("session_id")):
        return "its session_id is not a plain file name"
    kind = marker_kind(path)
    for field in ("sha256", "claimed_sha256") if kind == "claimed" else ("sha256",):
        if not isinstance(marker.get(field), str) or _SHA256.fullmatch(marker[field]) is None:
            return "its %s is not a sha256 digest" % field
    if kind is None or os.path.basename(path) != _marker_name(kind, marker):
        return "its file name does not match its session_id"
    expected = handover_path(project_dir, marker["session_id"])
    if not _same_place(marker.get("handover"), expected):
        return "its handover is not %s" % expected
    if kind == "claimed" or "archive" in marker:
        problem = archive_problem(project_dir, marker.get("archive"))
        if problem:
            return problem
    if not isinstance(marker.get("tool_use_id"), (str, type(None))):
        return "its tool_use_id is not a string"
    return _position_problem(marker.get("transcript"))


def read_marker(project_dir: str, path: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """(marker, None) when usable, (None, why) when it must not be used, (None, None) when absent."""
    raw = read_bytes(path)
    if raw is None:
        return None, ("it is not a regular file" if os.path.lexists(path) else None)
    try:
        marker = json.loads(raw.decode("utf-8"))
    except ValueError:
        return None, "it is not JSON"
    problem = marker_problem(project_dir, path, marker)
    return (None, problem) if problem else (marker, None)


def _stderr(line: str) -> None:
    try:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    except Exception:
        # stderr may be closed; errors.log still gets the line.
        return


def report_rejected(project_dir: str, path: str, why: str) -> None:
    """One stderr line and one errors.log line for a rejected marker, while that line is in the log.

    The key names the file and the sha256 of its bytes, so a marker that changes
    is reported again. The marker itself is left on disk for the user.
    """
    key = "rejected %s (sha256 %s)" % (os.path.basename(path), sha256_hex(read_bytes(path) or b"")[:12])
    with project_lock(project_dir):
        if any(key in line for line in error_lines(project_dir)):
            return
        line = "context-rotation: %s: %s; not used, left on disk" % (key, why)
        _stderr(line)
        log_error(project_dir, "%s %s" % (utc_now(), line))


def load_marker(project_dir: str, path: str) -> Optional[Dict[str, Any]]:
    """The marker at `path` when it passes marker_problem, else None (and a rejected one is reported)."""
    marker, why = read_marker(project_dir, path)
    if why:
        report_rejected(project_dir, path, why)
    return marker


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
    """Write the session's ready-marker; a valid outdated one of the session goes.

    The ready path is this session's own: a rejected marker there is reported,
    then replaced. A rejected outdated marker is reported and left.
    """
    session_id = marker["session_id"]
    target = marker_path(project_dir, "ready", session_id)
    with project_lock(project_dir):
        load_marker(project_dir, target)
        write_json(target, marker)
        stale = marker_path(project_dir, "outdated", session_id)
        if load_marker(project_dir, stale) is not None:
            os.unlink(stale)
    return target


def mark_outdated(project_dir: str, session_id: str, reason: str, notified: bool) -> Optional[Dict[str, Any]]:
    """Rename the ready-marker to outdated with the reason. None when there was none."""
    source = marker_path(project_dir, "ready", session_id)
    target = marker_path(project_dir, "outdated", session_id)
    with project_lock(project_dir):
        marker = load_marker(project_dir, source)
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
        marker = load_marker(project_dir, target)
        if marker is None or marker.get("notified", True):
            return None
        marker["notified"] = True
        write_json(target, marker)
    return marker


def mark_announced(project_dir: str, session_id: str, sha: str) -> bool:
    """True once per ready-marker: the first time the user is told it is saved."""
    target = marker_path(project_dir, "ready", session_id)
    with project_lock(project_dir):
        marker = load_marker(project_dir, target)
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
    while os.path.lexists(candidate):
        candidate = "%s-%d.md" % (base, number)
        number += 1
    return candidate


def claim(project_dir: str, source: str, successor: str,
          as_checked: bool = False) -> Optional[Tuple[str, Dict[str, Any], bytes]]:
    """Take a ready or outdated marker for a successor session: (claimed path, marker, bytes).

    Under the project lock the handover is read, and the marker is rewritten in
    place with the successor, the time, the archive path the handover is copied to
    next, and `claimed_sha256`, the sha256 of exactly the bytes handed over (the
    claimed generation), then renamed to its claimed name. A claimed marker is
    therefore never seen without those fields, and `sha256` keeps meaning the bytes
    the marker was checked against. With `as_checked`, the handover must still hold
    those bytes. None when the marker is gone or rejected, or the handover is gone
    or (with `as_checked`) changed.
    """
    with project_lock(project_dir):
        marker = load_marker(project_dir, source)
        data = read_bytes(marker["handover"]) if marker is not None else None
        if data is None or (as_checked and sha256_hex(data) != marker["sha256"]):
            return None
        now = datetime.now(timezone.utc)
        marker.update(
            claimed_by=successor, claimed_at=now.strftime(TIME_FORMAT),
            archive=plan_archive(project_dir, marker["session_id"], now), claimed_sha256=sha256_hex(data),
        )
        target = claimed_path(project_dir, marker["session_id"], successor)
        write_json(source, marker)
        try:
            os.rename(source, target)
        except FileNotFoundError:
            return None
    return target, marker, data


def archive_handover(target: str, data: bytes) -> str:
    """Copy a handover to its planned archive path. An existing archive copy is never overwritten.

    When the target already holds exactly these bytes (a delivery repeated after a
    crash) it is kept as it is; anything else at the target raises FileExistsError.
    The copy appears whole or not at all: a complete temporary file is hard-linked
    to the target, which fails when the target exists.
    """
    if read_bytes(target) == data:
        return target
    directory = os.path.dirname(target)
    os.makedirs(directory, exist_ok=True)
    descriptor, temp = tempfile.mkstemp(dir=directory, prefix=".archive-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.link(temp, target)
    finally:
        os.unlink(temp)
    return target


def claimed_bytes(marker: Dict[str, Any]) -> Tuple[Optional[bytes], Optional[str]]:
    """The bytes a claim stands for, or (None, why not), for recovery after a crash.

    The archive copy first, when it exists and matches `claimed_sha256`. The
    original only when there is no archive copy yet and it still matches: an
    original that changed is a newer handover, not the claimed one.
    """
    archive, handover = marker["archive"], marker["handover"]
    if os.path.lexists(archive):
        data = read_bytes(archive)
        if data is not None and sha256_hex(data) == marker["claimed_sha256"]:
            return data, None
        return None, "its archive copy %s does not hold the claimed handover" % archive
    data = read_bytes(handover)
    if data is None:
        return None, "it has no archive copy and its handover %s is gone" % handover
    if sha256_hex(data) != marker["claimed_sha256"]:
        return None, "it has no archive copy and %s now holds a newer handover" % handover
    return data, None


def log_injection(project_dir: str, record: Dict[str, Any]) -> None:
    line = json.dumps(record, sort_keys=True) + "\n"
    with project_lock(project_dir):
        regular_file.append_regular(injections_path(project_dir), line.encode("utf-8"))


def read_injections(project_dir: str) -> List[Dict[str, Any]]:
    raw = read_bytes(injections_path(project_dir))
    records = []
    for line in (raw or b"").decode("utf-8", "replace").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _unlink_logged(project_dir: str, path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    except OSError as error:
        log_error(project_dir, "%s context-rotation: cleanup of %s failed: %s" % (utc_now(), path, error))


def finish_claim(project_dir: str, claimed: str, marker: Dict[str, Any]) -> None:
    """After the emit: remove the original handover if it still holds the claimed bytes, then the claimed marker.

    An original with other bytes is a newer handover, written while this one was
    delivered: it stays, and so does its ready marker. The compare and the unlink
    run under the project lock, as one step against the other hook processes.
    Never raises: the injection already happened, and a failing hook would make
    Claude Code drop the output that carries it. A leftover is listed by `resume`.
    """
    try:
        with project_lock(project_dir):
            data = read_bytes(marker["handover"])
            if data is not None and sha256_hex(data) == marker["claimed_sha256"]:
                _unlink_logged(project_dir, marker["handover"])
            _unlink_logged(project_dir, claimed)
    except LockTimeout as error:
        log_error(project_dir, "%s context-rotation: cleanup of %s failed: %s" % (utc_now(), claimed, error))


def _archive_names(directory: str) -> List[str]:
    """Archive copies by name, oldest first: regular files named like one, nothing else."""
    return sorted(
        name for name in os.listdir(directory)
        if ARCHIVE_NAME.fullmatch(name) and regular_file.is_regular(os.path.join(directory, name))
    )


def trim_archive(project_dir: str, keep: int) -> int:
    """Keep the newest `keep` archived handovers. Returns how many were removed."""
    directory = archive_dir(project_dir)
    if not os.path.isdir(directory):
        return 0
    with project_lock(project_dir):
        names = _archive_names(directory)
        surplus = names[:-keep] if keep > 0 else names
        for name in surplus:
            os.unlink(os.path.join(directory, name))
    return len(surplus)


# --- listing -------------------------------------------------------------

GONE = "gone"


def scan_markers(project_dir: str, kind: str) -> Tuple[List[Tuple[str, Dict[str, Any]]], List[Tuple[str, str]]]:
    """([(path, marker)] that pass marker_problem, [(path, why)] that do not), by file name.

    A rejected marker is reported once and left on disk.
    """
    directory = rotation_dir(project_dir)
    if not os.path.isdir(directory):
        return [], []
    found, rejected = [], []
    for name in sorted(os.listdir(directory)):
        if not (name.startswith(kind + "-") and name.endswith(".json")):
            continue
        path = _in_rotation(project_dir, name)
        marker, why = read_marker(project_dir, path)
        if why:
            report_rejected(project_dir, path, why)
            rejected.append((path, why))
        elif marker is not None:
            found.append((path, marker))
    return found, rejected


def markers_of_kind(project_dir: str, kind: str) -> List[Tuple[str, Dict[str, Any]]]:
    """(path, marker) for every marker of one kind that passes marker_problem, by file name."""
    return scan_markers(project_dir, kind)[0]


def why_not_resumable(kind: str, marker: Dict[str, Any]) -> Optional[str]:
    """None when there is something to continue from; GONE when the user deleted it; else why not."""
    if kind == "claimed":
        if not (os.path.lexists(marker["archive"]) or os.path.lexists(marker["handover"])):
            return GONE
        return claimed_bytes(marker)[1]
    if not os.path.lexists(marker["handover"]):
        return GONE
    if not regular_file.is_regular(marker["handover"]):
        return "its handover %s is not a regular file" % marker["handover"]
    return None


def survey(project_dir: str, exclude_session: Optional[str]) -> Tuple[List[Dict[str, Any]], List[Tuple[str, str]]]:
    """(handovers not continued yet, [(marker path, why it cannot be resumed)]).

    Unfinished claims come first, then ready and outdated markers, newest first
    within each group. The excluded session's own ready and outdated markers are
    left out, and so is a marker whose files the user deleted.
    """
    claims, waiting, blocked = [], [], []
    for kind in ("claimed", "ready", "outdated"):
        found, rejected = scan_markers(project_dir, kind)
        blocked += rejected
        for path, marker in found:
            if kind != "claimed" and marker["session_id"] == exclude_session:
                continue
            why = why_not_resumable(kind, marker)
            if why is None:
                at = marker.get("claimed_at" if kind == "claimed" else "written_at")
                (claims if kind == "claimed" else waiting).append(
                    {"kind": kind, "marker_path": path, "marker": marker, "at": at if isinstance(at, str) else ""})
            elif why != GONE:
                blocked.append((path, why))
    newest = sorted(claims, key=lambda e: e["at"], reverse=True) + sorted(waiting, key=lambda e: e["at"], reverse=True)
    return newest, sorted(blocked)


def pending_handovers(project_dir: str, exclude_session: Optional[str]) -> List[Dict[str, Any]]:
    """The handovers that can be resumed, as survey lists them."""
    return survey(project_dir, exclude_session)[0]


def archived_handovers(project_dir: str) -> List[Dict[str, Any]]:
    """Archived handovers, newest first, each with its last line from injections.jsonl."""
    directory = archive_dir(project_dir)
    if not os.path.isdir(directory):
        return []
    injected = {}
    for record in read_injections(project_dir):
        if isinstance(record.get("archive"), str):
            injected[record["archive"]] = record
    paths = [os.path.join(directory, name) for name in reversed(_archive_names(directory))]
    return [{"kind": "archived", "path": path, "injection": injected.get(path)} for path in paths]
