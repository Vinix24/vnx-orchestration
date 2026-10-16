#!/usr/bin/env python3
"""atomic_swap — exchange two directory entries in one step.

``vnx guard install`` replaces a settings file that Claude Code writes too. ``os.replace`` drops
whatever sits at the target at that instant and keeps no copy, so a write that lands just before
it is lost without a trace. An exchange moves that file to the source name instead: afterwards
the source path holds exactly what was at the target, and the caller can compare it with what it
planned from. The call goes through ``ctypes``:

- macOS: ``renamex_np(from, to, RENAME_SWAP)`` from libSystem (macOS 10.12 and later). Measured
  on 2026-10-16 on APFS, macOS 27.0.1: the two inodes trade places, and a missing side fails
  with ENOENT and changes nothing.
- Linux: ``renameat2(AT_FDCWD, from, AT_FDCWD, to, RENAME_EXCHANGE)`` (glibc 2.28, kernel 3.15).

Any other platform, a C library without the call, or a filesystem that refuses the flag raises
``ExchangeUnsupported``. There is no fallback to a plain replace: the caller refuses instead.
"""
from __future__ import annotations

import ctypes
import errno
import os
import sys
from typing import Callable, Optional, Tuple, Union

RENAME_SWAP = 0x00000002  # macOS <stdio.h>
RENAME_EXCHANGE = 1 << 1  # Linux <linux/fs.h>
AT_FDCWD = -100  # Linux <fcntl.h>
# What a platform or filesystem without the exchange answers.
_UNSUPPORTED_ERRNOS = frozenset({errno.ENOTSUP, errno.EOPNOTSUPP, errno.EINVAL, errno.ENOSYS})

PathLike = Union[str, os.PathLike]
_Call = Callable[[bytes, bytes], int]
_bound: Optional[Tuple[Optional[_Call], str]] = None


class ExchangeUnsupported(Exception):
    """This platform or filesystem has no atomic exchange of two directory entries."""


def _function(name: str):
    try:
        return getattr(ctypes.CDLL(None, use_errno=True), name, None)
    except OSError:
        return None


def _bind() -> Tuple[Optional[_Call], str]:
    """(the call, "") or (None, why there is none)."""
    if sys.platform == "darwin":
        fn = _function("renamex_np")
        if fn is None:
            return None, "the C library has no renamex_np (macOS 10.12 or later has it)"
        fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        fn.restype = ctypes.c_int
        return (lambda a, b: fn(a, b, RENAME_SWAP)), ""
    if sys.platform.startswith("linux"):
        fn = _function("renameat2")
        if fn is None:
            return None, "the C library has no renameat2 (glibc 2.28 or later has it)"
        fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        fn.restype = ctypes.c_int
        return (lambda a, b: fn(AT_FDCWD, a, AT_FDCWD, b, RENAME_EXCHANGE)), ""
    return None, f"no atomic exchange call is known for platform {sys.platform!r}"


def _binding() -> Tuple[Optional[_Call], str]:
    global _bound
    if _bound is None:
        _bound = _bind()
    return _bound


def unsupported_reason() -> Optional[str]:
    """Why this platform has no atomic exchange; None when it has one. A filesystem can still
    refuse it, which ``exchange`` reports."""
    call, reason = _binding()
    return None if call is not None else reason


def exchange(first: PathLike, second: PathLike) -> None:
    """Swap the directory entries ``first`` and ``second`` atomically. Both must exist on the
    same filesystem. Raises ExchangeUnsupported when the platform or the filesystem has no
    exchange, and OSError with the errno otherwise (FileNotFoundError for a missing side)."""
    call, reason = _binding()
    if call is None:
        raise ExchangeUnsupported(f"no atomic exchange here: {reason}")
    if call(os.fsencode(first), os.fsencode(second)) == 0:
        return
    err = ctypes.get_errno()
    if err in _UNSUPPORTED_ERRNOS:
        raise ExchangeUnsupported(
            f"the filesystem holding {second} refuses an atomic exchange "
            f"({errno.errorcode.get(err, err)}: {os.strerror(err)})")
    raise OSError(err, os.strerror(err), os.fspath(first), None, os.fspath(second))
