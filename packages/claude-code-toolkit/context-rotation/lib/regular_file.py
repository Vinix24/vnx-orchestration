"""Opening a file the hook did not just create: regular files only.

Markers, handovers, archive copies and the transcript a marker names are plain
files that any process of the user can replace. A FIFO would block the hook on
open and a device can be read without end, so a path is opened only when it is a
regular file and not a symlink, and the open itself never blocks. With `dir_fd`
a name is opened in the directory that descriptor holds, so a swap of a directory
above it changes nothing. A whole-file read stops at MAX_READ_BYTES: a larger file
is not one the plugin wrote, and reading it would hold the hook for as long as it
grows. Standard library only.
"""
from __future__ import annotations

import errno
import os
import stat
from typing import Any, BinaryIO, Dict, Optional, Tuple

# A handover is at most handover_max_chars (8,000 by default, at most a quarter of this in characters:
# context_budget.MAX_HANDOVER_CHARS); markers and the logs are smaller still.
MAX_READ_BYTES = 1 << 20


def is_regular(path: Any, dir_fd: Optional[int] = None) -> bool:
    """True for a regular file that is not a symlink."""
    try:
        return stat.S_ISREG(os.lstat(path, dir_fd=dir_fd).st_mode)
    except (OSError, TypeError, ValueError):
        return False


def _not_regular(path: str) -> OSError:
    return OSError(errno.EINVAL, "not a regular file", path)


def open_regular(path: str, dir_fd: Optional[int] = None) -> BinaryIO:
    """A binary file object for reading `path`; OSError when it is not a regular file.

    lstat refuses a symlink, FIFO or device before the open; O_NOFOLLOW and
    O_NONBLOCK keep a swap between the two from following a link or blocking, and
    fstat checks what was opened.
    """
    if not is_regular(path, dir_fd):
        raise _not_regular(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise _not_regular(path)
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def stamp_of(found: os.stat_result) -> Dict[str, int]:
    """What changes when a file is written: its inode, its size and its modification time."""
    return {"ino": found.st_ino, "size": found.st_size, "mtime_ns": found.st_mtime_ns}


def read_stamped(path: Any, dir_fd: Optional[int] = None,
                 limit: int = MAX_READ_BYTES) -> Optional[Tuple[bytes, Dict[str, int]]]:
    """(bytes, stamp_of the file as read) of a regular file of at most `limit` bytes; None otherwise.

    The stamp is taken from the open file after the read, so it describes the file
    those bytes came from: a later write changes it, a rename does not.
    """
    if not isinstance(path, str) or not path:
        return None
    try:
        with open_regular(path, dir_fd) as handle:
            data = handle.read(limit + 1)
            found = os.fstat(handle.fileno())
    except (OSError, ValueError):
        return None
    return (data, stamp_of(found)) if len(data) <= limit else None


def read_regular(path: Any, dir_fd: Optional[int] = None, limit: int = MAX_READ_BYTES) -> Optional[bytes]:
    """The bytes of a regular file, or None when it is absent, unreadable, not a regular file or above `limit` bytes."""
    read = read_stamped(path, dir_fd, limit)
    return read[0] if read is not None else None


def read_tail(path: Any, dir_fd: Optional[int] = None, limit: int = MAX_READ_BYTES) -> Optional[bytes]:
    """The complete lines in the last `limit` bytes of a regular file; None when it is not one.

    For an append-only log: a log that grew past the limit is read from its end,
    so it can still be trimmed. A line cut by the limit is left out.
    """
    if not isinstance(path, str) or not path:
        return None
    try:
        with open_regular(path, dir_fd) as handle:
            return tail_of(handle, limit)
    except (OSError, ValueError):
        return None


def tail_of(handle: BinaryIO, limit: int = MAX_READ_BYTES) -> bytes:
    """The complete lines in the last `limit` bytes of an open file (read_tail). OSError as the read gives it."""
    size = handle.seek(0, os.SEEK_END)
    handle.seek(max(0, size - limit))
    data = handle.read(limit)
    if size <= limit:
        return data
    cut = data.find(b"\n")
    return data[cut + 1:] if cut >= 0 else b""


def append_regular(path: str, data: bytes, dir_fd: Optional[int] = None) -> None:
    """Append to a regular file, creating it; OSError when something else is at `path`."""
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK | os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o644, dir_fd=dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise _not_regular(path)
        view = memoryview(data)
        while view:
            view = view[os.write(descriptor, view):]
    finally:
        os.close(descriptor)
