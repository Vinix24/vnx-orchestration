"""Opening a file the hook did not just create: regular files only.

Markers, handovers, archive copies and the transcript a marker names are plain
files that any process of the user can replace. A FIFO would block the hook on
open and a device can be read without end, so a path is opened only when it is a
regular file and not a symlink, and the open itself never blocks. Standard
library only.
"""
from __future__ import annotations

import errno
import os
import stat
from typing import Any, BinaryIO, Optional


def is_regular(path: Any) -> bool:
    """True for a regular file that is not a symlink."""
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except (OSError, TypeError, ValueError):
        return False


def _not_regular(path: str) -> OSError:
    return OSError(errno.EINVAL, "not a regular file", path)


def open_regular(path: str) -> BinaryIO:
    """A binary file object for reading `path`; OSError when it is not a regular file.

    lstat refuses a symlink, FIFO or device before the open; O_NOFOLLOW and
    O_NONBLOCK keep a swap between the two from following a link or blocking, and
    fstat checks what was opened.
    """
    if not is_regular(path):
        raise _not_regular(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise _not_regular(path)
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def read_regular(path: Any) -> Optional[bytes]:
    """The bytes of a regular file, or None when it is absent, unreadable or not a regular file."""
    if not isinstance(path, str) or not path:
        return None
    try:
        with open_regular(path) as handle:
            return handle.read()
    except (OSError, ValueError):
        return None


def append_regular(path: str, data: bytes) -> None:
    """Append to a regular file, creating it; OSError when something else is at `path`."""
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK | os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o644)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise _not_regular(path)
        view = memoryview(data)
        while view:
            view = view[os.write(descriptor, view):]
    finally:
        os.close(descriptor)
