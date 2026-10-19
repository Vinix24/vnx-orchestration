"""Per-session state for the context-rotation plugin.

Everything lives under <project>/.claude/rotation/:

  .gitignore                    `*`, written before anything else in the directory
  .lock                         one lock for every marker change and journal line in the project
  .errors.lock                  the lock of errors.log
  state/<session>.json          the guard's per-session counters and the resume list it showed
  state/<session>.enforcement.json
                                the force and hard bands of a session (enforcement_state)
  enforcement.jsonl             one line per change of an enforcement state (enforcement_state)
  handover-<session>.md         the handover a session writes
  ready-<session>.json          the handover is valid and covers all work so far
  outdated-<session>.json       work happened after the handover; never injected automatically
  claimed-<origin>-<generation>.json
                                a session took that generation of the handover; removed last
  archive/<time>-<origin>.md    every injected handover, and every file the cleanup found at a
                                handover path; trimmed to the newest archive_keep plus every
                                copy a marker on disk still names
  archive/<time>-<origin>.original
                                the original handover a claim read, moved off its path before
                                the emit; no claim, delivery or list reads it; trimmed to the
                                newest archive_keep
  injections.jsonl              one line per transition, written before the transition: ready,
                                outdated, claimed, injected, kept; and `delivered`, written
                                after the emit it records and before the claimed marker
                                goes; kept to JOURNAL_LINES_PER_ARCHIVE_COPY lines per
                                archive_keep
  errors.log                    the last failures of the hooks, and rejected markers

Handovers hold a session's working state and must never be committed, hence the
.gitignore.

A generation is one handover the plugin marked ready: new_marker gives it a random
`generation` id, which stays with it through outdated and claimed. A claim's file
name carries it, so two generations of one session never share a claim file and a
generation is claimed at most once; the `resume` list names it too. Nothing here
replaces a file by moving another one onto its name. A marker moves by a hard link
to its new name, which fails when that name exists, and the original handover
moves into the archive only under a fresh name. A file is rewritten whole only at
its own name, through a temporary file of its own (_replace_in). The cleanup never
deletes a file that was ever at a handover path: only the age-based archive trim
removes one, and never a copy a marker still names.

Every file here is an input, not a fact: any process of the user can write one,
a cloned repository can ship one, and the origin session can rewrite its handover
while a successor delivers it. So every name and path read from a marker passes
marker_problem first, every value that can reach the model's context or the
user's line passes state_fields, a path is shown as the plugin builds it and
never as read (read_marker), files are opened only when they are regular and at
most regular_file.MAX_READ_BYTES long, the original handover is removed only
while it is still the file the claim read, and an archive copy is never
overwritten. The archive copy a claim names is the one written from the bytes
the claim read, which no other process holds open: the original, which its
writer may still hold open, is kept apart under its own name and never becomes
that copy.

The directories are inputs too. `.claude/rotation`, its archive and its state
directory must each be a real directory: storage_problem is the one gate every
hook entry passes before it touches anything here, and every read, listing,
rename, unlink and new file goes through a descriptor of the verified directory
(open_storage), so a symlink there, or one swapped in later, never leads the
plugin into another directory. Standard library only.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple

import regular_file
import state_fields
import transcript_work

_DIGITS = re.compile(r"^[0-9]+$")
MAX_SESSION_ID = state_fields.MAX_SESSION_ID
MARKER_VERSION = 1
MARKER_KINDS = ("ready", "outdated", "claimed")
MARKER_FILE = re.compile(r"(ready|outdated|claimed)-[A-Za-z0-9_-]+\.json")
LOCK_TIMEOUT_SECONDS = 5.0
LOCK_POLL_SECONDS = 0.02
CREATE_ATTEMPTS = 5
PS_TIMEOUT_SECONDS = 2.0
TIME_FORMAT = state_fields.TIME_FORMAT
ARCHIVE_NAME = state_fields.ARCHIVE_NAME
# The original a claim read, once the cleanup moved it off the handover path: not an archive copy.
ORIGINAL_SUFFIX = ".original"
ORIGINAL_NAME = re.compile(r"[0-9]{8}T[0-9]{12}Z-[A-Za-z0-9_-]+\.original")
JOURNAL = "injections.jsonl"
ERRORS_LOG = "errors.log"
# A handover goes through ready, maybe outdated and ready again, claimed, injected and delivered; in
# tmux also the steps of its automatic /clear (started, entered, acknowledged, continued or aborted, told).
JOURNAL_LINES_PER_ARCHIVE_COPY = 16
is_safe_session_id = state_fields.is_safe_session_id


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


def claimed_path(project_dir: str, origin: str, generation: str) -> str:
    return _in_rotation(project_dir, "claimed-%s-%s.json" % (origin, generation))


def archive_dir(project_dir: str) -> str:
    return _in_rotation(project_dir, "archive")


def injections_path(project_dir: str) -> str:
    return _in_rotation(project_dir, JOURNAL)


# --- the storage gate and the verified directories ------------------------

# The directories below `.claude` that hold the plugin's files, each a real directory or absent.
STORAGE_DIRS = (("rotation",), ("rotation", "archive"), ("rotation", "state"))
_STORED_IN = ((), ("archive",), ("state",))
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_NEW_FILE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW


class StorageRefused(OSError):
    """`.claude/rotation` or a directory below it is a symlink or not a directory."""


def storage_problem(project_dir: str) -> Optional[str]:
    """Why the project's rotation storage must not be used; None when it may.

    The one gate every hook entry passes before it reads or changes anything under
    `.claude/rotation`: that directory, its archive and its state directory are each
    a real directory (os.lstat: not a symlink) or absent, so the plugin creates it as
    one. `.claude` itself may be a symlink: it is resolved once, and what it resolves
    to must be a directory too.
    """
    claude = os.path.realpath(os.path.join(project_dir, ".claude"))
    for parts in ((),) + STORAGE_DIRS:
        shown = os.path.join(project_dir, ".claude", *parts)
        try:
            mode = os.lstat(os.path.join(claude, *parts)).st_mode
        except FileNotFoundError:
            continue
        except OSError as error:
            return "%s cannot be checked (%s)" % (shown, error.strerror)
        if stat.S_ISLNK(mode):
            return "%s is a symlink" % shown
        if not stat.S_ISDIR(mode):
            return "%s is not a directory" % shown
    return None


def _open_below(parent: int, name: str, shown: str, create: bool) -> int:
    if create:
        with contextlib.suppress(FileExistsError):
            os.mkdir(name, 0o755, dir_fd=parent)
    try:
        return os.open(name, _DIRECTORY, dir_fd=parent)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise StorageRefused("%s is a symlink or not a directory (%s)" % (shown, error.strerror))


def open_storage(project_dir: str, *parts: str, create: bool = False) -> int:
    """A descriptor for `.claude/rotation/<parts>`, every directory below `.claude` a real one.

    `.claude` itself may be a symlink: it is resolved once. Each directory below it
    is opened with O_NOFOLLOW from its parent's descriptor, so a symlink there is
    refused (StorageRefused) and a swap after this call cannot redirect what is done
    through the descriptor. A directory that is absent is made from its parent's
    descriptor with `create`, else FileNotFoundError. The caller closes the descriptor.
    """
    claude = os.path.join(project_dir, ".claude")
    if create:
        os.makedirs(claude, exist_ok=True)
    descriptor = os.open(os.path.realpath(claude), os.O_RDONLY | os.O_DIRECTORY)
    walked = claude
    for name in ("rotation",) + parts:
        walked = os.path.join(walked, name)
        try:
            below = _open_below(descriptor, name, walked, create)
        finally:
            os.close(descriptor)
        descriptor = below
    return descriptor


@contextlib.contextmanager
def storage(project_dir: str, *parts: str, create: bool = False) -> Iterator[int]:
    """open_storage for the length of a with-block."""
    descriptor = open_storage(project_dir, *parts, create=create)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _place(project_dir: str, path: Any) -> Tuple[Tuple[str, ...], str]:
    """(directories below `.claude/rotation`, file name) of a file stored directly in one of them.

    StorageRefused for any other path, and for something that is not a path.
    """
    if isinstance(path, str) and path and "\0" not in path:
        directory, name = os.path.split(os.path.abspath(path))
        real = os.path.realpath(directory)
        for parts in _STORED_IN:
            if real == os.path.realpath(os.path.join(rotation_dir(project_dir), *parts)):
                return parts, name
    raise StorageRefused("%r is not a file in %s" % (path, rotation_dir(project_dir)))


def _name_in(project_dir: str, path: str, parts: Tuple[str, ...]) -> str:
    """The file name of `path` when it is stored directly in `.claude/rotation/<parts>`."""
    found, name = _place(project_dir, path)
    if found != parts:
        raise StorageRefused("%s is not directly in %s" % (path, os.path.join(rotation_dir(project_dir), *parts)))
    return name


def read_bytes(project_dir: str, path: Any) -> Optional[bytes]:
    """The bytes of a regular file stored under `.claude/rotation`, read through the verified directory.

    None when absent, unreadable, a symlink, a FIFO or a device, above
    regular_file.MAX_READ_BYTES, stored anywhere else, or when the storage is refused.
    """
    try:
        parts, name = _place(project_dir, path)
        with storage(project_dir, *parts) as directory:
            return regular_file.read_regular(name, directory)
    except OSError:
        return None


def read_checked(project_dir: str, path: Any) -> Optional[bytes]:
    """read_bytes for a decision a failed read must not make (the automatic /clear).

    None only when no regular file of at most regular_file.MAX_READ_BYTES is there.
    OSError when the read itself fails, and StorageRefused for a path stored anywhere
    else or storage that is refused: such a read says nothing about the file.
    """
    parts, name = _place(project_dir, path)
    try:
        with storage(project_dir, *parts) as directory:
            found = os.lstat(name, dir_fd=directory)
            if not stat.S_ISREG(found.st_mode) or found.st_size > regular_file.MAX_READ_BYTES:
                return None
            with regular_file.open_regular(name, directory) as handle:
                data = handle.read(regular_file.MAX_READ_BYTES + 1)
    except FileNotFoundError:
        return None
    return data if len(data) <= regular_file.MAX_READ_BYTES else None


def read_stamped(project_dir: str, path: Any) -> Optional[Tuple[bytes, Dict[str, int]]]:
    """read_bytes plus regular_file.stamp_of the file those bytes came from; None when read_bytes would be."""
    try:
        parts, name = _place(project_dir, path)
        with storage(project_dir, *parts) as directory:
            return regular_file.read_stamped(name, directory)
    except OSError:
        return None


def _lstat_in(directory: int, name: str) -> Optional[os.stat_result]:
    try:
        return os.lstat(name, dir_fd=directory)
    except OSError:
        return None


def _stored_stat(project_dir: str, path: Any) -> Optional[os.stat_result]:
    """lstat of a stored file through the verified directory; None when it cannot be had."""
    try:
        parts, name = _place(project_dir, path)
        with storage(project_dir, *parts) as directory:
            return os.lstat(name, dir_fd=directory)
    except OSError:
        return None


def stored_exists(project_dir: str, path: Any) -> bool:
    """Something, of any type, is at a stored path (os.path.lexists through the verified directory)."""
    return _stored_stat(project_dir, path) is not None


def stored_regular(project_dir: str, path: Any) -> bool:
    found = _stored_stat(project_dir, path)
    return found is not None and stat.S_ISREG(found.st_mode)


def stored_mtime(project_dir: str, path: Any) -> Optional[float]:
    """When a stored regular file was last written; None when it is not one."""
    found = _stored_stat(project_dir, path)
    return found.st_mtime if found is not None and stat.S_ISREG(found.st_mode) else None


def _stored_names(project_dir: str, *parts: str) -> List[str]:
    """The names in `.claude/rotation/<parts>`, listed through the verified directory; [] when there are none."""
    try:
        with storage(project_dir, *parts) as directory:
            return os.listdir(directory)
    except OSError:
        return []


def _new_temp(directory: int, prefix: str) -> Tuple[int, str]:
    """A new, empty file in the directory `directory` holds: (write descriptor, name)."""
    while True:
        name = "%s%s.tmp" % (prefix, os.urandom(8).hex())
        try:
            return os.open(name, _NEW_FILE, 0o600, dir_fd=directory), name
        except FileExistsError:
            continue


def _open_shared(directory: int, name: str, flags: int, mode: int) -> int:
    """os.open with O_CREAT of a name several processes may create at the same moment, in `directory`.

    On macOS an open that creates a file can fail with ENOENT while another process
    creates the same name, though the directory is there (measured: one of six
    parallel hook processes, in 1 of about 60 runs, on the first lock of a
    project). The name exists by then, so the open is tried again, at most
    CREATE_ATTEMPTS times.
    """
    attempt = 1
    while True:
        try:
            return os.open(name, flags, mode, dir_fd=directory)
        except FileNotFoundError:
            if attempt >= CREATE_ATTEMPTS:
                raise
            attempt += 1
            time.sleep(LOCK_POLL_SECONDS)


def _replace_in(directory: int, name: str, data: bytes, prefix: str) -> None:
    """Replace `name` in the directory `directory` holds whole: a complete temporary file renamed over it."""
    descriptor, temp = _new_temp(directory, prefix)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.replace(temp, name, src_dir_fd=directory, dst_dir_fd=directory)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp, dir_fd=directory)
        raise


def _atomic_write(project_dir: str, target: str, data: bytes, prefix: str) -> None:
    """Replace a stored file whole, in the verified directory."""
    parts, name = _place(project_dir, target)
    with storage(project_dir, *parts, create=True) as directory:
        _replace_in(directory, name, data, prefix)


def append_lines(directory: int, name: str, data: bytes, prefix: str) -> None:
    """Append whole lines to a log in the directory `directory` holds.

    One write with O_APPEND, so no line of another writer is lost or torn. Something
    other than a regular file or a directory at the name (a FIFO, a symlink) is not
    the plugin's log: it is replaced by a log that holds these lines.
    """
    try:
        regular_file.append_regular(name, data, directory)
    except OSError:
        found = _lstat_in(directory, name)
        if found is None or stat.S_ISREG(found.st_mode) or stat.S_ISDIR(found.st_mode):
            raise
        _replace_in(directory, name, data, prefix)


def keep_last_lines(directory: int, name: str, keep: int, prefix: str) -> None:
    """Cut a log down to its last `keep` lines. Call while holding the lock its appenders hold."""
    raw = regular_file.read_tail(name, directory)
    found = _lstat_in(directory, name)
    if raw is None or found is None:
        return
    lines = raw.splitlines(keepends=True)
    if len(lines) > keep or found.st_size > len(raw):
        _replace_in(directory, name, b"".join(lines[-keep:]), prefix)


def _same_file(directory: int, first: str, second: str) -> bool:
    found = _lstat_in(directory, first), _lstat_in(directory, second)
    return None not in found and os.path.samestat(*found)


def _move_in_rotation(project_dir: str, source: str, target: str) -> bool:
    """Give a marker in `.claude/rotation` another name there, never replacing anything at that name.

    A hard link to the new name, then the old name removed, in the verified
    directory: link(2) fails when the target exists, so nothing is replaced. A
    target that is already the same file is a move that stopped between those two
    steps, and this one completes it. Anything else at the target refuses the
    move: it is logged, both files stay, and the result is False. Call under the
    project lock, which every plugin process that writes a marker holds.
    FileNotFoundError when the source is gone.
    """
    old, new = _name_in(project_dir, source, ()), _name_in(project_dir, target, ())
    with storage(project_dir) as directory:
        try:
            os.link(old, new, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
        except FileExistsError:
            if not _same_file(directory, old, new):
                log_error(project_dir, "%s context-rotation: %s was not renamed to %s: something is already at "
                          "that name; nothing replaced, both left on disk" % (utc_now(), source, target))
                return False
        os.unlink(old, dir_fd=directory)
    return True


def _rename_fresh(source_dir: int, source: str, target_dir: int, target: str) -> None:
    """Rename `source` to `target`, a name that must not exist. Call under the project lock.

    For the original handover only: a link would leave its old name to remove
    while Claude Code's Write tool can put a newer file there. The standard library
    has no rename that refuses an existing target (Linux RENAME_NOREPLACE, macOS
    RENAME_EXCL), so the target is a fresh archive name from plan_archive, checked
    absent here under the project lock, which every plugin process that creates a
    name in the archive holds (archive_handover, this rename). FileExistsError when
    something is there: nothing is replaced.
    """
    if _lstat_in(target_dir, target) is not None:
        raise FileExistsError(errno.EEXIST, "already in the archive; nothing replaced", target)
    os.rename(source, target, src_dir_fd=source_dir, dst_dir_fd=target_dir)


def remove_in_rotation(project_dir: str, path: str) -> None:
    """Unlink a file directly inside `.claude/rotation`, anchored to the verified directory.

    StorageRefused when the directory is a symlink or `path` is somewhere else;
    FileNotFoundError when the file is gone.
    """
    name = _name_in(project_dir, path, ())
    with storage(project_dir) as directory:
        os.unlink(name, dir_fd=directory)


def ensure_gitignore(project_dir: str) -> None:
    """Create rotation/.gitignore once, in the verified directory. An existing file is never touched."""
    with storage(project_dir, create=True) as directory:
        try:
            descriptor = _open_shared(directory, ".gitignore", _NEW_FILE, 0o644)
        except FileExistsError:
            return
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("*\n")


def write_json(project_dir: str, target: str, document: Dict[str, Any]) -> None:
    _atomic_write(project_dir, target, json.dumps(document, indent=1).encode("utf-8"), ".marker-")


def load_state(project_dir: str, session_id: str) -> Dict[str, Any]:
    """The session's state, or an empty dict when absent or unreadable."""
    if not is_safe_session_id(session_id):
        return {}
    raw = read_bytes(project_dir, state_path(project_dir, session_id))
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
    _atomic_write(project_dir, target, json.dumps(state).encode("utf-8"), ".state-")
    return target


ERROR_LOG_MAX_LINES = 50


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime(TIME_FORMAT)


def parse_utc(stamp: Any) -> Optional[datetime]:
    if not state_fields.is_plugin_time(stamp):
        return None
    return datetime.strptime(stamp, TIME_FORMAT).replace(tzinfo=timezone.utc)


def errors_path(project_dir: str) -> str:
    return _in_rotation(project_dir, ERRORS_LOG)


def error_lines(project_dir: str) -> List[str]:
    try:
        with storage(project_dir) as directory:
            raw = regular_file.read_tail(ERRORS_LOG, directory)
    except OSError:
        return []
    return raw.decode("utf-8", "replace").splitlines() if raw is not None else []


# --- the locks -----------------------------------------------------------

class LockTimeout(Exception):
    """Another hook process held a lock of the project for longer than the timeout."""


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
def _locked(project_dir: str, name: str, timeout: float) -> Iterator[None]:
    """Hold the lock file `name` in `.claude/rotation`; re-entrant within one process.

    Writes the .gitignore first, as a lock file is the first file a change creates.
    The lock file is opened in the verified directory and never through a symlink.
    """
    ensure_gitignore(project_dir)
    path = _in_rotation(project_dir, name)
    held = _HELD.get(path)
    if held is not None:
        held[1] += 1
        try:
            yield
        finally:
            held[1] -= 1
        return
    with storage(project_dir) as directory:
        descriptor = _open_shared(directory, name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o644)
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


def project_lock(project_dir: str, timeout: float = LOCK_TIMEOUT_SECONDS):
    """Serialise marker changes and journal lines across the parallel hook processes of a project."""
    return _locked(project_dir, ".lock", timeout)


def log_error(project_dir: str, line: str) -> bool:
    """Append one line to rotation/errors.log, keeping the last ERROR_LOG_MAX_LINES. True once written.

    Every writer appends under the lock of errors.log (not the project lock, which
    may be what failed), and the cut to the last lines happens under it too, so two
    failures at the same moment both keep their line. Never raises: the log is a
    courtesy of a hook that already failed. It is written in the verified directory,
    so storage that is refused gets no log (False).
    """
    data = (line.replace("\n", " ") + "\n").encode("utf-8")
    try:
        with _locked(project_dir, ".errors.lock", LOCK_TIMEOUT_SECONDS), storage(project_dir) as directory:
            append_lines(directory, ERRORS_LOG, data, ".errors-")
            keep_last_lines(directory, ERRORS_LOG, ERROR_LOG_MAX_LINES, ".errors-")
    except Exception:
        # The log is best effort; a failure here must not mask the original error.
        return False
    return True


def log_failure(project_dir: str, step: str, error: BaseException) -> None:
    """A failure a hook caught so it can still exit 0: one line in errors.log, else on stderr."""
    line = " ".join(("context-rotation: %s failed: %s: %s" % (step, type(error).__name__, error)).split())
    if not log_error(project_dir, "%s %s" % (utc_now(), line)):
        stderr_line(line)


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


def process_start_checked(pid: int) -> Optional[str]:
    """process_start for a decision a failed read must not make: None only when the process is gone.

    The same source and form as process_start: /proc where the system has it, else
    ps. OSError when neither can tell, so a failed read never reads as a process that
    is gone, or as one that runs.
    """
    if os.path.exists("/proc/self/stat"):
        try:
            with open("/proc/%d/stat" % pid, "rb") as handle:
                data = handle.read().decode("ascii", "replace")
        except FileNotFoundError:
            return None
        fields = data.rsplit(")", 1)[-1].split()
        if len(fields) < 20:
            raise OSError(errno.EINVAL, "no start time in /proc/%d/stat" % pid)
        return "proc:" + fields[19]
    try:
        done = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                              env=dict(os.environ, LC_ALL="C"), timeout=PS_TIMEOUT_SECONDS)
    except subprocess.SubprocessError as error:
        raise OSError(errno.ETIMEDOUT, "ps did not answer: %s" % error)
    text = " ".join(done.stdout.split())
    if done.returncode == 0 and text:
        return "ps:" + text
    if done.returncode == 1 and not text:
        return None
    raise OSError(errno.EIO, "ps exited with status %d" % done.returncode)


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


# --- what a marker may say ---------------------------------------------------

def _plain_absolute(path: Any) -> bool:
    return isinstance(path, str) and os.path.isabs(path) and "\0" not in path


def _same_place(path: Any, expected: str) -> bool:
    """`path` names the file `expected` names: the same name in the same directory, in plain form."""
    if not state_fields.is_plain_path(path) or os.path.basename(path) != os.path.basename(expected):
        return False
    return os.path.realpath(os.path.dirname(path)) == os.path.realpath(os.path.dirname(expected))


def archive_problem(project_dir: str, archive: Any) -> Optional[str]:
    """Why `archive` is no place for an archive copy; None when it is a direct child of archive_dir."""
    if not state_fields.is_plain_path(archive):
        return "its archive is not a plain absolute path"
    if ARCHIVE_NAME.fullmatch(os.path.basename(archive)) is None:
        return "its archive is not named like an archive copy"
    found = _stored_stat(project_dir, archive)
    if found is not None and stat.S_ISLNK(found.st_mode):
        return "its archive is a symlink"
    if os.path.dirname(os.path.realpath(archive)) != os.path.realpath(archive_dir(project_dir)):
        return "its archive is not in %s" % archive_dir(project_dir)
    return None


def _position_problem(position: Any, session_id: str) -> Optional[str]:
    """Why a transcript position is not one of the session's own transcript; None when it is (or absent).

    Claude Code keeps a session's transcript as `<session id>.jsonl`, so a position
    naming any other file belongs to another session or to no session at all.
    """
    if position is None:
        return None
    path = position.get("path") if isinstance(position, dict) else None
    size = position.get("size") if isinstance(position, dict) else None
    if (not _plain_absolute(path) or os.path.basename(path) != session_id + ".jsonl"
            or not state_fields.is_count(size)
            or not isinstance(position.get("last_uuid"), (str, type(None)))):
        return ("its transcript position is not the session's own transcript (%s.jsonl), a size and an entry id"
                % session_id)
    return None


def marker_kind(path: str) -> Optional[str]:
    name = os.path.basename(path)
    return next((k for k in MARKER_KINDS if name.startswith(k + "-") and name.endswith(".json")), None)


def _marker_name(kind: str, marker: Dict[str, Any]) -> str:
    """The file name a marker of this kind has: per session, and per generation for a claim."""
    if kind != "claimed":
        return "%s-%s.json" % (kind, marker["session_id"])
    return "claimed-%s-%s.json" % (marker["session_id"], marker["generation"])


def marker_problem(project_dir: str, path: str, marker: Any) -> Optional[str]:
    """Why the marker read from `path` must not be used; None when it may.

    The one rule for every reader of marker fields: a marker of this version whose
    session_id is a plain file name, whose generation is one the plugin gives,
    whose sha256 (and for a claim claimed_sha256) is a digest, whose claimed_by
    (for a claim) is a plain file name, whose own file name is the one its
    session_id (and for a claim its generation) gives for its kind, whose handover
    is that session's handover path, whose archive (always for a claim) is a direct
    child of archive_dir, whose transcript position is one of the session's own
    transcript, and whose times, outdated reason and claimed file are values the
    plugin writes (state_fields.marker_value_problem).
    """
    if not isinstance(marker, dict) or marker.get("version") != MARKER_VERSION:
        return "it is not a version %d marker" % MARKER_VERSION
    if not is_safe_session_id(marker.get("session_id")):
        return "its session_id is not a plain file name"
    if not state_fields.is_generation(marker.get("generation")):
        return "its generation is not one the plugin gives"
    kind = marker_kind(path)
    for field in ("sha256", "claimed_sha256") if kind == "claimed" else ("sha256",):
        if not state_fields.is_sha256(marker.get(field)):
            return "its %s is not a sha256 digest" % field
    if kind == "claimed" and not is_safe_session_id(marker.get("claimed_by")):
        return "its claimed_by is not a plain file name"
    if kind is None or os.path.basename(path) != _marker_name(kind, marker):
        return "its file name does not match its session_id and generation"
    expected = handover_path(project_dir, marker["session_id"])
    if not _same_place(marker.get("handover"), expected):
        return "its handover is not %s" % expected
    if kind == "claimed" or "archive" in marker:
        problem = archive_problem(project_dir, marker.get("archive"))
        if problem:
            return problem
    if not isinstance(marker.get("tool_use_id"), (str, type(None))):
        return "its tool_use_id is not a string"
    return (_position_problem(marker.get("transcript"), marker["session_id"])
            or state_fields.marker_value_problem(kind, marker))


def _marker_json(project_dir: str, path: str) -> Tuple[Any, Optional[str]]:
    """(parsed JSON, None), (None, why) for an unusable file, (None, None) when absent. No validation."""
    raw = read_bytes(project_dir, path)
    if raw is None:
        unusable = "it is not a regular file of at most %d bytes" % regular_file.MAX_READ_BYTES
        return None, (unusable if stored_exists(project_dir, path) else None)
    try:
        return json.loads(raw.decode("utf-8")), None
    except ValueError:
        return None, "it is not JSON"


def read_marker(project_dir: str, path: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """(marker, None) when usable, (None, why) when it must not be used, (None, None) when absent.

    A usable marker carries the paths the plugin builds itself: its handover and
    archive name the same files as the ones read (marker_problem checked that), in
    the form handover_path and archive_dir give, so no path text from disk is shown.
    """
    marker, why = _marker_json(project_dir, path)
    if marker is None:
        return None, why
    return _usable_marker(project_dir, path, marker)


def _usable_marker(project_dir: str, path: str, marker: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """(marker with the plugin's own paths, None) when it passes marker_problem, else (None, why)."""
    problem = marker_problem(project_dir, path, marker)
    if problem:
        return None, problem
    marker["handover"] = handover_path(project_dir, marker["session_id"])
    if "archive" in marker:
        marker["archive"] = os.path.join(archive_dir(project_dir), os.path.basename(marker["archive"]))
    return marker, None


def read_marker_checked(project_dir: str, path: str) -> Optional[Dict[str, Any]]:
    """read_marker for a decision a failed read must not make: OSError when the read fails.

    None when no usable marker is there (absent, not JSON, or refused by
    marker_problem); a rejected one is left for the hooks to report.
    """
    raw = read_checked(project_dir, path)
    if raw is None:
        return None
    try:
        marker = json.loads(raw.decode("utf-8"))
    except ValueError:
        return None
    return _usable_marker(project_dir, path, marker)[0]


def journal_checked(project_dir: str) -> bytes:
    """The complete lines at the end of injections.jsonl; b"" while there is none.

    For a decision a failed read must not make: OSError when it cannot be read, also
    when something other than a regular file is at its name.
    """
    try:
        with storage(project_dir) as directory:
            os.lstat(JOURNAL, dir_fd=directory)  # FileNotFoundError while there is none
            with regular_file.open_regular(JOURNAL, directory) as handle:
                return regular_file.tail_of(handle)
    except FileNotFoundError:
        return b""


def stderr_line(line: str) -> None:
    """One line on stderr; nothing when stderr is closed."""
    try:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    except Exception:
        # stderr may be closed; errors.log still gets the line.
        return


def shown_marker_path(path: str) -> str:
    """A marker path as it may be shown: a name the plugin does not write is not shown at all."""
    if MARKER_FILE.fullmatch(os.path.basename(path)) is None:
        return "a file whose name the plugin does not write, in %s" % os.path.dirname(path)
    return path


def report_rejected(project_dir: str, path: str, why: str) -> None:
    """One stderr line and one errors.log line for a rejected marker, while that line is in the log.

    The key names the file and the sha256 of its bytes, so a marker that changes
    is reported again. A file name the plugin does not write is escaped to one
    plain line. The marker itself is left on disk for the user.
    """
    name = os.path.basename(path)
    shown = name if MARKER_FILE.fullmatch(name) else ascii(name)
    key = "rejected %s (sha256 %s)" % (shown, sha256_hex(read_bytes(project_dir, path) or b"")[:12])
    with project_lock(project_dir):
        if any(key in line for line in error_lines(project_dir)):
            return
        line = "context-rotation: %s: %s; not used, left on disk" % (key, why)
        stderr_line(line)
        log_error(project_dir, "%s %s" % (utc_now(), line))


def load_marker(project_dir: str, path: str) -> Optional[Dict[str, Any]]:
    """The marker at `path` when it passes marker_problem, else None (and a rejected one is reported)."""
    marker, why = read_marker(project_dir, path)
    if why:
        report_rejected(project_dir, path, why)
    return marker


def new_generation() -> str:
    """The id of a new handover generation: random, so no other marker of the project carries it."""
    return os.urandom(state_fields.GENERATION_HEX // 2).hex()


def new_marker(session_id: str, handover: str, data: bytes, env: Dict[str, str],
               position: Optional[Dict[str, Any]], tool_use_id: Any) -> Dict[str, Any]:
    """A ready-marker of a new generation; `position` is the transcript position taken before `data` was read."""
    return {
        "version": MARKER_VERSION,
        "session_id": session_id,
        "generation": new_generation(),
        "handover": handover,
        "sha256": sha256_hex(data),
        "chars": len(data.decode("utf-8", "replace")),
        "process": process_identity(env),
        "tmux_pane": env.get("TMUX_PANE") or None,
        "transcript": position,
        "tool_use_id": tool_use_id if isinstance(tool_use_id, str) and tool_use_id else None,
        "written_at": utc_now(),
        "announced": False,
    }


# --- the journal: injections.jsonl ------------------------------------------

def append_journal(project_dir: str, record: Dict[str, Any]) -> None:
    """One line in injections.jsonl, appended under the project lock in the verified directory.

    The line holds only values the plugin writes: one that state_fields.record_problem
    would refuse to read back raises ValueError and is not written.
    """
    problem = state_fields.record_problem(record)
    if problem:
        raise ValueError("journal line refused: %s" % problem)
    line = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    with project_lock(project_dir), storage(project_dir) as directory:
        append_lines(directory, JOURNAL, line, ".journal-")


def journal(project_dir: str, event: str, **fields: Any) -> None:
    """The line of one transition, stamped now. Written before the transition it records."""
    append_journal(project_dir, dict(fields, event=event, at=utc_now()))


def log_injection(project_dir: str, record: Dict[str, Any]) -> None:
    """The line of an injection (event "injected"), written before the handover is emitted."""
    append_journal(project_dir, record)


def log_delivery(project_dir: str, via: str, origin: str, generation: str, successor: str) -> bool:
    """The line that says the emit of a claimed handover returned (event "delivered"). True once written.

    Written after the emit, so it is the one evidence that the handover went out:
    "injected" comes before the emit, which can still fail (BrokenPipeError). It is
    also the line of the claim's last transition: the claimed marker is removed
    only after it (handover_injection.deliver). The injection already happened,
    and a failing hook would make Claude Code drop the output that carries it, so
    this never raises: a failure to write it (OSError, LockTimeout, ValueError)
    goes to log_failure and gives False. The claim then stays, listed by `resume`,
    and whatever waits for this line acts as if the delivery did not finish.
    """
    try:
        journal(project_dir, "delivered", via=via, origin_session_id=origin, generation=generation,
                session_id=successor)
    except (OSError, LockTimeout, ValueError) as error:
        log_failure(project_dir, "log the delivery of generation %s" % generation, error)
        return False
    return True


def read_injections(project_dir: str) -> List[Dict[str, Any]]:
    """The lines of injections.jsonl that state_fields.record_problem accepts, oldest first.

    The journal is history: the markers on disk are the truth of what is waiting,
    so a line whose transition never happened (a crash in between) changes nothing.
    """
    try:
        with storage(project_dir) as directory:
            raw = regular_file.read_tail(JOURNAL, directory)
    except OSError:
        return []
    records = []
    for line in (raw or b"").decode("utf-8", "replace").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if state_fields.record_problem(record) is None:
            records.append(record)
    return records


def injected_on_clear(project_dir: str, session_id: str) -> bool:
    """True when this session was continued from a handover by the SessionStart path.

    That is the one injection the loop guard covers: an "injected" line in
    injections.jsonl with `via: "clear"` naming this session as its successor. A
    fresh `startup`, a `/resume` pick and every other session return False.
    """
    for record in read_injections(project_dir):
        if record["event"] == "injected" and record["via"] == "clear" and record["session_id"] == session_id:
            return True
    return False


def trim_journal(project_dir: str, archive_keep: int) -> None:
    """Keep the newest JOURNAL_LINES_PER_ARCHIVE_COPY × archive_keep lines of injections.jsonl.

    Under the project lock every appender holds, in the verified directory. Runs in
    the SessionStart hook and never raises: a failure goes to log_failure.
    """
    if not stored_regular(project_dir, injections_path(project_dir)):
        return
    try:
        with project_lock(project_dir), storage(project_dir) as directory:
            keep_last_lines(directory, JOURNAL, JOURNAL_LINES_PER_ARCHIVE_COPY * archive_keep, ".journal-")
    except (OSError, LockTimeout) as error:
        log_failure(project_dir, "journal trim", error)


# --- the transitions of a marker ------------------------------------------------

def _same_stored_file(project_dir: str, first: str, second: str) -> bool:
    """Two names directly in `.claude/rotation` are one file (a move that stopped between its link and its unlink)."""
    with storage(project_dir) as directory:
        return _same_file(directory, _name_in(project_dir, first, ()), _name_in(project_dir, second, ()))


def _drop_superseded(project_dir: str, session_id: str) -> None:
    """Remove the session's valid outdated marker while its ready-marker is another file. Call under the project lock.

    The session's handover file holds one generation, and its ready-marker
    describes it: mark_ready removes the outdated marker right after it writes the
    ready one, and this completes that step when a crash came in between. An
    outdated marker that is the same file as the ready one is a move to outdated
    that stopped halfway, which the next move completes. A rejected outdated marker
    is reported and left.
    """
    ready, stale = marker_path(project_dir, "ready", session_id), marker_path(project_dir, "outdated", session_id)
    if not stored_exists(project_dir, stale):
        return
    if load_marker(project_dir, stale) is not None and not _same_stored_file(project_dir, ready, stale):
        remove_in_rotation(project_dir, stale)


def mark_ready(project_dir: str, marker: Dict[str, Any]) -> str:
    """Write the session's ready-marker, after its line; a valid outdated one of the session goes.

    The ready path is this session's own, rewritten whole through its own
    temporary file: a rejected marker there is reported, then replaced. A rejected
    outdated marker is reported and left. The session's handover file holds one
    generation, so this line also records the end of the generation it replaces,
    ready or outdated: the marker that goes (here, or in _drop_superseded after a
    crash) has no line of its own.
    """
    session_id = marker["session_id"]
    target = marker_path(project_dir, "ready", session_id)
    with project_lock(project_dir):
        load_marker(project_dir, target)
        journal(project_dir, "ready", origin_session_id=session_id, generation=marker["generation"],
                sha256=marker["sha256"])
        write_json(project_dir, target, marker)
        _drop_superseded(project_dir, session_id)
    return target


def mark_outdated(project_dir: str, session_id: str, reason: str, notified: bool) -> Optional[Dict[str, Any]]:
    """Move the ready-marker to outdated with the reason, after its line. None when there was none, or refused.

    `reason` is one of state_fields.OUTDATED_REASONS (ValueError otherwise), so the
    reason a reader shows is always one the plugin wrote. The move never replaces a
    file at the outdated name (_move_in_rotation): a rejected marker there refuses
    it, logged, and the ready-marker stays, so it is still checked at every clear.
    The move comes before the fields: a crash in between leaves an outdated marker
    without a reason, which is never injected automatically and is listed as
    REASON_NOT_RECORDED.
    """
    if not state_fields.is_reason(reason):
        raise ValueError("%r is not an outdated reason the plugin writes" % (reason,))
    source = marker_path(project_dir, "ready", session_id)
    target = marker_path(project_dir, "outdated", session_id)
    with project_lock(project_dir):
        marker = load_marker(project_dir, source)
        if marker is None:
            return None
        _drop_superseded(project_dir, session_id)
        journal(project_dir, "outdated", origin_session_id=session_id, generation=marker["generation"],
                sha256=marker["sha256"], reason=reason)
        if not _move_in_rotation(project_dir, source, target):
            return None
        marker.update(outdated_at=utc_now(), outdated_reason=reason, notified=notified)
        write_json(project_dir, target, marker)
    return marker


def take_outdated_notice(project_dir: str, session_id: str) -> Optional[Dict[str, Any]]:
    """An outdated marker the model was not told about yet; it counts as told after this."""
    target = marker_path(project_dir, "outdated", session_id)
    if not stored_exists(project_dir, target):
        return None
    with project_lock(project_dir):
        marker = load_marker(project_dir, target)
        if marker is None or marker.get("notified", True):
            return None
        marker["notified"] = True
        write_json(project_dir, target, marker)
    return marker


def mark_announced(project_dir: str, session_id: str, sha: str) -> bool:
    """True once per ready-marker: the first time the user is told it is saved."""
    target = marker_path(project_dir, "ready", session_id)
    with project_lock(project_dir):
        marker = load_marker(project_dir, target)
        if marker is None or marker["sha256"] != sha or marker.get("announced"):
            return False
        marker["announced"] = True
        write_json(project_dir, target, marker)
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
    """Why this ready-marker no longer describes the session's work (one of OUTDATED_REASONS); None while it does."""
    if data is None:
        return state_fields.FILE_GONE
    if sha256_hex(data) != marker["sha256"]:
        return state_fields.FILE_CHANGED
    if over_age(marker, settings, now):
        return state_fields.OVER_AGE
    return transcript_work.stale_reason(marker.get("transcript"), marker["handover"], marker.get("tool_use_id"))


# --- claim, archive, injection log ---------------------------------------

def plan_archive(project_dir: str, origin: str, now: Optional[datetime] = None, suffix: str = ".md") -> str:
    """A free archive path for a handover of `origin`; names sort by time.

    `suffix` is ".md" for an archive copy, ORIGINAL_SUFFIX for an original the cleanup moves.
    """
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%S%fZ")
    taken = set(_stored_names(project_dir, "archive"))
    base = "%s-%s" % (stamp, origin)
    candidate, number = base + suffix, 1
    while candidate in taken:
        candidate = "%s-%d%s" % (base, number, suffix)
        number += 1
    return os.path.join(archive_dir(project_dir), candidate)


def _settle_claimed(project_dir: str, source: str, target: str) -> None:
    """The generation of the marker at `source` has its claim name taken: remove the rest of a claim, or refuse."""
    if load_marker(project_dir, target) is not None:
        remove_in_rotation(project_dir, source)
        return
    log_error(project_dir, "%s context-rotation: %s was not claimed: %s holds no valid claim; nothing replaced, "
              "both left on disk" % (utc_now(), source, target))


class GenerationChanged(Exception):
    """The marker holds another generation than the one its caller found: nothing was claimed."""


def _check_generation(marker: Optional[Dict[str, Any]], expected: Optional[str]) -> None:
    if marker is not None and expected is not None and marker["generation"] != expected:
        raise GenerationChanged(
            "the marker of session %s holds generation %s now, not generation %s that was picked; "
            "nothing was claimed" % (marker["session_id"], marker["generation"], expected))


def claim(project_dir: str, source: str, successor: str, as_checked: bool = False,
          generation: Optional[str] = None) -> Optional[Tuple[str, Dict[str, Any], bytes]]:
    """Take a ready or outdated marker for a successor session: (claimed path, marker, bytes).

    Under the project lock the handover is read, and the marker is rewritten in
    place with the successor, the time, the archive path the handover is copied to
    next, `claimed_sha256` (the sha256 of exactly the bytes handed over) and
    `claimed_file` (the inode, size and modification time of the file they came
    from), then moved to the claim name of its generation; the claim's line in
    injections.jsonl comes first. A claimed marker is therefore never seen without
    those fields, and `sha256` keeps meaning the bytes the marker was checked
    against. With `as_checked`, the handover must still hold those bytes.

    A generation is claimed at most once: its claim name exists only once, and the
    move never replaces it (_move_in_rotation). When a valid claim of this
    generation is there already, the marker at `source` is what is left of it (a
    claim that stopped between its link and its unlink) and is removed; anything
    else at that name refuses the claim, logged. None then, and when the marker is
    gone or rejected, or the handover is gone or (with `as_checked`) changed.

    A ready or outdated marker is named per session, so the origin can publish a
    new generation at `source` between a caller's lookup and this call. With
    `generation`, the generation that caller found, the marker read under the lock
    must still hold it: another one raises GenerationChanged, naming both, before
    anything is written.
    """
    with project_lock(project_dir):
        marker = load_marker(project_dir, source)
        _check_generation(marker, generation)
        read = read_stamped(project_dir, marker["handover"]) if marker is not None else None
        if read is None or (as_checked and sha256_hex(read[0]) != marker["sha256"]):
            return None
        target = claimed_path(project_dir, marker["session_id"], marker["generation"])
        if stored_exists(project_dir, target):
            _settle_claimed(project_dir, source, target)
            return None
        data, stamp = read
        now = datetime.now(timezone.utc)
        marker.update(
            claimed_by=successor, claimed_at=now.strftime(TIME_FORMAT),
            archive=plan_archive(project_dir, marker["session_id"], now), claimed_sha256=sha256_hex(data),
            claimed_file=stamp,
        )
        journal(project_dir, "claimed", origin_session_id=marker["session_id"], generation=marker["generation"],
                session_id=successor, sha256=marker["claimed_sha256"], archive=marker["archive"])
        write_json(project_dir, source, marker)
        try:
            if not _move_in_rotation(project_dir, source, target):
                return None
        except FileNotFoundError:
            return None
    return target, marker, data


def archive_handover(project_dir: str, target: str, data: bytes) -> str:
    """Copy a handover to its planned archive path. An existing archive copy is never overwritten.

    When the target already holds exactly these bytes (a delivery repeated after a
    crash) it is kept as it is; anything else at the target raises FileExistsError.
    The copy appears whole or not at all: a complete temporary file is hard-linked
    to the target, which fails when the target exists. Under the project lock, as
    every plugin process that creates a name in the archive is (_rename_fresh), and
    in the verified archive directory, so a symlinked archive gets no copy
    (StorageRefused).
    """
    name = _name_in(project_dir, target, ("archive",))
    with project_lock(project_dir), storage(project_dir, "archive", create=True) as directory:
        if regular_file.read_regular(name, directory) == data:
            return target
        descriptor, temp = _new_temp(directory, ".archive-")
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
            os.link(temp, name, src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            os.unlink(temp, dir_fd=directory)
    return target


def claimed_bytes(project_dir: str, marker: Dict[str, Any]) -> Tuple[Optional[bytes], Optional[str]]:
    """The bytes a claim stands for, or (None, why not), for recovery after a crash.

    The archive copy first, when it exists and matches `claimed_sha256`. The
    original only when there is no archive copy yet and it still matches: an
    original that changed is a newer handover, not the claimed one.
    """
    archive, handover = marker["archive"], marker["handover"]
    if stored_exists(project_dir, archive):
        data = read_bytes(project_dir, archive)
        if data is not None and sha256_hex(data) == marker["claimed_sha256"]:
            return data, None
        return None, "its archive copy %s does not hold the claimed handover" % archive
    data = read_bytes(project_dir, handover)
    if data is None:
        return None, "it has no archive copy and its handover %s is gone" % handover
    if sha256_hex(data) != marker["claimed_sha256"]:
        return None, "it has no archive copy and %s now holds a newer handover" % handover
    return data, None


def _unlink_logged(project_dir: str, path: str) -> None:
    try:
        remove_in_rotation(project_dir, path)
    except FileNotFoundError:
        return
    except OSError as error:
        log_failure(project_dir, "cleanup of %s" % path, error)


# --- moving the original out of its writer's way ---------------------------------

def _still_claimed(directory: int, name: str, marker: Dict[str, Any]) -> Tuple[bool, Optional[bytes]]:
    """(the file set aside is the one the claim read, its bytes): same stamp before and after the read, same sha256."""
    before = _lstat_in(directory, name)
    data = regular_file.read_regular(name, directory)
    after = _lstat_in(directory, name)
    same = (before is not None and after is not None and data is not None
            and regular_file.stamp_of(before) == regular_file.stamp_of(after) == marker["claimed_file"]
            and sha256_hex(data) == marker["claimed_sha256"])
    return same, data


def _list_kept(project_dir: str, marker: Dict[str, Any], data: Optional[bytes]) -> None:
    """A handover file no marker of its session describes gets an outdated marker of a new generation.

    `resume` lists it through that marker. A ready-marker of the session describes
    the file, and so does a valid outdated one with its sha256; a rejected outdated
    marker is reported and left.
    """
    session_id = marker["session_id"]
    if data is None or stored_exists(project_dir, marker_path(project_dir, "ready", session_id)):
        return
    stale = marker_path(project_dir, "outdated", session_id)
    if stored_exists(project_dir, stale):
        found = load_marker(project_dir, stale)
        if found is None or found["sha256"] == sha256_hex(data):
            return
    now = utc_now()
    kept = {field: marker.get(field) for field in ("version", "session_id", "handover", "process", "tmux_pane")}
    kept.update(generation=new_generation(), sha256=sha256_hex(data), chars=len(data.decode("utf-8", "replace")),
                transcript=None, tool_use_id=None, written_at=now, announced=False, outdated_at=now,
                outdated_reason=state_fields.FILE_CHANGED, notified=True)
    journal(project_dir, "outdated", origin_session_id=session_id, generation=kept["generation"],
            sha256=kept["sha256"], reason=state_fields.FILE_CHANGED)
    write_json(project_dir, stale, kept)


def _keep_in_archive(project_dir: str, marker: Dict[str, Any], aside: str, data: Optional[bytes]) -> None:
    """The moved file is another generation than the claimed one: it keeps its archive name, with its line."""
    archived = os.path.join(archive_dir(project_dir), aside)
    if data is None:
        log_error(project_dir, "%s context-rotation: kept %s, a handover written during its cleanup" % (
            utc_now(), archived))
        return
    journal(project_dir, "kept", origin_session_id=marker["session_id"], sha256=sha256_hex(data), archive=archived)


def _retire_original(project_dir: str, marker: Dict[str, Any]) -> None:
    name = _name_in(project_dir, marker["handover"], ())
    found = _stored_stat(project_dir, marker["handover"])
    if found is None or not stat.S_ISREG(found.st_mode):
        return
    if regular_file.stamp_of(found) != marker["claimed_file"]:
        _list_kept(project_dir, marker, read_bytes(project_dir, marker["handover"]))
        return
    original = os.path.basename(plan_archive(project_dir, marker["session_id"], suffix=ORIGINAL_SUFFIX))
    with storage(project_dir) as rotation, storage(project_dir, "archive", create=True) as archive:
        _rename_fresh(rotation, name, archive, original)
        same, data = _still_claimed(archive, original, marker)
        if same:
            return
        aside = os.path.basename(plan_archive(project_dir, marker["session_id"]))
        _rename_fresh(archive, original, archive, aside)
        try:
            os.link(aside, name, src_dir_fd=archive, dst_dir_fd=rotation, follow_symlinks=False)
            put_back = True
        except FileExistsError:
            put_back = False
    _keep_in_archive(project_dir, marker, aside, data)
    if put_back:
        _list_kept(project_dir, marker, data)


def retire_original(project_dir: str, marker: Dict[str, Any]) -> None:
    """Before the emit, move the original handover off its path. The claim keeps its archive copy.

    Claude Code's Write tool does not take the project lock, so the original can
    be written at any moment, also through a descriptor opened before this runs.
    So the cleanup never deletes a file that was ever at the handover path, and
    never makes it the claim's archive copy: that copy is the one archive_handover
    wrote from the claimed bytes, which no writer holds open, so the context, the
    injection's line and claimed_bytes always read the claimed bytes there. A file
    at the handover path that is not the one the claim read (inode, size,
    modification time) is left alone, listed through its own marker or an outdated
    one (_list_kept). The file the claim read is renamed into the archive under a
    fresh name of its own, `<time>-<origin>.original` (_rename_fresh), so a write
    to the handover path from then on makes a new file there. When it still holds
    the claimed bytes it stays under that name, kept apart: no claim, delivery or
    list reads it, and a write through a descriptor opened earlier lands in it.
    Otherwise it is a newer generation: it is renamed to an archive copy name and
    goes back to the handover path by a hard link, listed like a file left alone,
    or stays only in the archive when a newer file took the path meanwhile; either
    way it keeps that archive name, with a `kept` line. Both are removed only by
    the age-based trim.

    Never raises: a failure to lock, move or read (OSError, LockTimeout) goes to
    log_failure, and the claim still names its archive copy.
    """
    try:
        with project_lock(project_dir):
            _retire_original(project_dir, marker)
    except (OSError, LockTimeout) as error:
        log_failure(project_dir, "cleanup of %s" % marker["handover"], error)


def finish_claim(project_dir: str, claimed: str) -> None:
    """After the emit and its `delivered` line (log_delivery): remove the claimed marker. Never raises.

    The injection already happened, and a failing hook would make Claude Code drop
    the output that carries it: a failure to lock or unlink (OSError, LockTimeout)
    goes to log_failure, and the claim stays, listed by `resume`.
    """
    try:
        with project_lock(project_dir):
            _unlink_logged(project_dir, claimed)
    except (OSError, LockTimeout) as error:
        log_failure(project_dir, "cleanup of %s" % claimed, error)


# --- trimming the archive ------------------------------------------------------

def _archive_names(directory: int, pattern: "re.Pattern[str]" = ARCHIVE_NAME) -> List[str]:
    """Archive copies (or with ORIGINAL_NAME the moved originals) by name, oldest first.

    The regular files named like `pattern` in the directory `directory` holds.
    """
    def regular(name: str) -> bool:
        try:
            return stat.S_ISREG(os.lstat(name, dir_fd=directory).st_mode)
        except OSError:
            return False

    return sorted(name for name in os.listdir(directory) if pattern.fullmatch(name) and regular(name))


def _names_archive_copy(field: Any, archive: str) -> bool:
    """`field` is a plain absolute path whose real directory is `archive`; anything else names nothing."""
    if not _plain_absolute(field):
        return False
    try:
        return os.path.dirname(os.path.realpath(field)) == archive
    except (OSError, ValueError):
        return False


def _named_archive_copies(project_dir: str) -> set:
    """Base names of the archive copies any marker file on disk names, valid or not.

    A marker that fails marker_problem stays on disk for the user, so the copy it
    names stays too. A file that cannot be read or parsed, or a field that is not a
    string path directly in archive_dir, names nothing; no marker can stop the trim.
    """
    archive = os.path.realpath(archive_dir(project_dir))
    named = set()
    for name in _stored_names(project_dir):
        if marker_kind(name) is None:
            continue
        marker, _ = _marker_json(project_dir, _in_rotation(project_dir, name))
        field = marker.get("archive") if isinstance(marker, dict) else None
        if _names_archive_copy(field, archive):
            named.add(os.path.basename(field))
    return named


def _trim(project_dir: str, keep: int) -> int:
    with storage(project_dir, "archive") as directory, project_lock(project_dir):
        named = _named_archive_copies(project_dir)
        removed = 0
        for names in (_archive_names(directory), _archive_names(directory, ORIGINAL_NAME)):
            for name in [n for n in (names[:-keep] if keep > 0 else names) if n not in named]:
                try:
                    os.unlink(name, dir_fd=directory)
                except FileNotFoundError:
                    continue
                removed += 1
    return removed


def trim_archive(project_dir: str, keep: int) -> int:
    """Keep the newest `keep` archived handovers and every copy a marker still names.

    A claimed marker's copy is the recovery record after a crash, so it outlives
    the newest-`keep` window for as long as the marker is on disk. The originals
    the cleanup moved off a handover path (ORIGINAL_NAME) are counted apart: the
    newest `keep` of them stay, and they never push an archive copy out of the
    window. Returns how many
    were removed; without an archive nothing is created. Runs in the SessionStart
    hook and never raises: a failure to lock, list or unlink (OSError, LockTimeout)
    goes to log_failure, and the next start trims again.
    """
    try:
        return _trim(project_dir, keep)
    except FileNotFoundError:
        return 0
    except (OSError, LockTimeout) as error:
        log_failure(project_dir, "archive trim", error)
        return 0


# --- listing -------------------------------------------------------------

GONE = "gone"


def scan_markers(project_dir: str, kind: str) -> Tuple[List[Tuple[str, Dict[str, Any]]], List[Tuple[str, str]]]:
    """([(path, marker)] that pass marker_problem, [(path, why)] that do not), by file name.

    A file of that kind whose name the plugin does not write (MARKER_FILE) is
    rejected without being read. A rejected marker is reported once and left on disk.
    """
    found, rejected = [], []
    for name in sorted(_stored_names(project_dir)):
        if not (name.startswith(kind + "-") and name.endswith(".json")):
            continue
        path = _in_rotation(project_dir, name)
        if MARKER_FILE.fullmatch(name) is None:
            marker, why = None, "its file name is not one the plugin writes"
        else:
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


def why_not_resumable(project_dir: str, kind: str, marker: Dict[str, Any]) -> Optional[str]:
    """None when there is something to continue from; GONE when the user deleted it; else why not."""
    if kind == "claimed":
        if not (stored_exists(project_dir, marker["archive"]) or stored_exists(project_dir, marker["handover"])):
            return GONE
        return claimed_bytes(project_dir, marker)[1]
    found = _stored_stat(project_dir, marker["handover"])
    if found is None:
        return GONE
    if not stat.S_ISREG(found.st_mode):
        return "its handover %s is not a regular file" % marker["handover"]
    if found.st_size > regular_file.MAX_READ_BYTES:
        return "its handover %s is larger than %d bytes" % (marker["handover"], regular_file.MAX_READ_BYTES)
    return None


def survey(project_dir: str, exclude_session: Optional[str]) -> Tuple[List[Dict[str, Any]], List[Tuple[str, str]]]:
    """(handovers not continued yet, [(marker path, why it cannot be resumed)]).

    Three groups in this order, each newest first: unfinished claims (by claimed_at),
    ready markers, then outdated markers (both by written_at). The excluded
    session's own ready and outdated markers are left out, and so is a marker whose
    files the user deleted.
    """
    groups: Dict[str, List[Dict[str, Any]]] = {kind: [] for kind in MARKER_KINDS}
    blocked = []
    for kind in ("claimed", "ready", "outdated"):
        found, rejected = scan_markers(project_dir, kind)
        blocked += rejected
        for path, marker in found:
            if kind != "claimed" and marker["session_id"] == exclude_session:
                continue
            why = why_not_resumable(project_dir, kind, marker)
            if why is None:
                at = marker["claimed_at" if kind == "claimed" else "written_at"]
                groups[kind].append({"kind": kind, "marker_path": path, "marker": marker, "at": at})
            elif why != GONE:
                blocked.append((path, why))
    newest = []
    for kind in ("claimed", "ready", "outdated"):
        newest += sorted(groups[kind], key=lambda e: e["at"], reverse=True)
    return newest, sorted(blocked)


def pending_handovers(project_dir: str, exclude_session: Optional[str]) -> List[Dict[str, Any]]:
    """The handovers that can be resumed, as survey lists them."""
    return survey(project_dir, exclude_session)[0]


def archived_handovers(project_dir: str) -> List[Dict[str, Any]]:
    """Archived handovers, newest first, each with its last "injected" line from injections.jsonl."""
    try:
        with storage(project_dir, "archive") as directory:
            names = _archive_names(directory)
    except OSError:
        return []
    injected = {}
    for record in read_injections(project_dir):
        if record["event"] == "injected":
            injected[record["archive"]] = record
    paths = [os.path.join(archive_dir(project_dir), name) for name in reversed(names)]
    return [{"kind": "archived", "path": path, "injection": injected.get(path)} for path in paths]
