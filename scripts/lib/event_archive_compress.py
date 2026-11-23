"""Lossless compression of the event archive (ADR-081, eventarchief-compressie C2a).

``compress_archive_round(directories, days, manual=False, ...)`` returns a
``RoundResult`` (``files``: one ``FileResult`` each, ``stopped``: ``done`` or
the budget that ran out, ``summary()``, ``has_divergent``). It takes only
``<dir>/.archive.lock``, never a live-stream lock (fixed order: live, then
archive), and only for stat, replace and unlink: compression, its
verification and every pair compare run before it, so a writer never waits for
them. ``bytes_read`` counts the bytes those reads actually took from disk.
"""

from __future__ import annotations

import fcntl
import gzip
import io
import os
import shutil
import stat
import tempfile
import time
import zlib
from collections import Counter
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple, Union

from event_store import ARCHIVE_LOCK_NAME, ARCHIVE_SUFFIX

MAX_READ_BYTES, MAX_FILE_BYTES, CHUNK = 2 * 1024**3, 1024**3, 1024**2
MAX_SECONDS, MAX_FILES = 600.0, 500

COMPRESSED = "compressed"
SKIPPED = "skipped"
PAIR_REMOVED_PLAIN = "pair_removed_plain"
PAIR_RECOMPRESSED = "pair_recompressed"
DIVERGENT = "divergent"


@dataclass
class FileResult:
    path: str
    outcome: str
    reason: Optional[str] = None  # skipped: locked|changed|too_large|no_space|mismatch
    plain_size: int = 0
    gz_path: Optional[str] = None
    gz_size: Optional[int] = None


@dataclass
class RoundResult:
    files: List[FileResult] = field(default_factory=list)
    stopped: str = "done"
    bytes_read: int = 0

    @property
    def has_divergent(self) -> bool:
        return any(f.outcome == DIVERGENT for f in self.files)

    def summary(self) -> Dict[str, object]:
        counts = dict(Counter(f.outcome for f in self.files))
        done = [f for f in self.files if f.outcome in (COMPRESSED, PAIR_RECOMPRESSED, PAIR_REMOVED_PLAIN)]
        after = sum(f.gz_size or 0 for f in done if f.outcome != PAIR_REMOVED_PLAIN)
        return {"files": len(self.files), "counts": counts, "stopped": self.stopped,
                "bytes_before": sum(f.plain_size for f in done), "bytes_after": after}


_READS: ContextVar[Optional[List[int]]] = ContextVar("event_archive_reads", default=None)


class _CountedIO(io.FileIO):
    """A raw file whose reads add to the round's counter (OI-2982)."""

    def readinto(self, buffer) -> Optional[int]:
        count = super().readinto(buffer)
        acc = _READS.get()
        if acc is not None and count:
            acc[0] += count
        return count


def _open_counted(path: Path) -> io.BufferedReader:
    return io.BufferedReader(_CountedIO(path, "r"), CHUNK)


def _snap(path: Path) -> Optional[Tuple[int, int, int]]:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns)


def _compare(gz: Path, plain: Path) -> str:
    """``equal``, ``prefix`` (the gz decompresses to a prefix of the plain) or ``different``; an unreadable gz raises."""
    with _open_counted(gz) as raw, gzip.GzipFile(fileobj=raw, mode="rb") as g, _open_counted(plain) as p:
        while True:
            chunk = g.read(CHUNK)
            other = p.read(len(chunk) or 1)
            if chunk != other[: len(chunk)] or len(other) < len(chunk):
                return "different"
            if not chunk:
                return "equal" if not other else "prefix"


def _kind(gz: Path, plain: Path) -> str:
    try:
        return _compare(gz, plain)
    except (OSError, EOFError, zlib.error):
        return "different"


def _compress_verified(plain: Path) -> Optional[Path]:
    """Compress *plain* to an fsynced scratch beside it and verify it byte for byte; None (scratch gone) on a difference."""
    fd, name = tempfile.mkstemp(dir=plain.parent, prefix=f".{plain.name}.", suffix=".tmp")
    scratch, ok = Path(name), False
    try:
        with os.fdopen(fd, "wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz_f:
                with _open_counted(plain) as src:
                    while chunk := src.read(CHUNK):
                        gz_f.write(chunk)
            raw.flush()
            os.fsync(raw.fileno())
        ok = _kind(scratch, plain) == "equal"
        return scratch if ok else None
    finally:
        if not ok:
            scratch.unlink(missing_ok=True)


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _settle(plain: Path, free_space: Callable[[Path], int]) -> FileResult:
    """One plain archive file by the protocol of ADR-081 (steps 1 to 3, pair rules)."""
    gz = plain.with_name(plain.name + ".gz")
    snap = _snap(plain)
    if snap is None:
        return FileResult(str(plain), SKIPPED, "changed")
    size = snap[1]
    res = FileResult(str(plain), SKIPPED, plain_size=size)
    gz_snap = _snap(gz)
    kind = _kind(gz, plain) if gz_snap else None
    if kind == "different":
        return _divergent(res, gz)
    scratch: Optional[Path] = None
    if kind != "equal":
        if free_space(plain.parent) < 2 * size:
            res.reason = "no_space"
            return res
        scratch = _compress_verified(plain)
        if scratch is None:
            res.reason = "mismatch"
            return res
    try:
        gz_seen = _snap(gz)
        if gz_seen != gz_snap:
            # A gz appeared or changed meanwhile: the pair rules, compared BEFORE the
            # lock (OI-2983); under it only the snapshots of this compare are re-checked.
            kind = _kind(gz, plain) if gz_seen else None
            if (gz_seen is None and gz_snap is not None) or (kind in (None, "prefix") and scratch is None):
                res.reason = "changed"
                return res
            gz_snap = gz_seen
        with open(plain.parent / ARCHIVE_LOCK_NAME, "ab") as lock_f:
            try:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                res.reason = "locked"
                return res
            try:
                return _replace_under_lock(res, plain, gz, snap, gz_snap, kind, scratch)
            finally:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
    finally:
        if scratch is not None:
            scratch.unlink(missing_ok=True)


def _divergent(res: FileResult, gz: Path) -> FileResult:
    snap = _snap(gz)
    res.outcome, res.gz_path, res.gz_size = DIVERGENT, str(gz), snap and snap[1]
    return res


def _replace_under_lock(res, plain, gz, snap, gz_snap, kind, scratch) -> FileResult:
    """Only stat, replace and unlink: a file that moved since its compare is ``changed``."""
    if _snap(plain) != snap or _snap(gz) != gz_snap:
        res.reason = "changed"
        return res
    if kind == "different":
        return _divergent(res, gz)
    if kind == "equal":
        plain.unlink()
        _fsync_dir(plain.parent)
        res.outcome = PAIR_REMOVED_PLAIN
        res.gz_path, res.gz_size = str(gz), _snap(gz)[1]
        return res
    st = os.stat(plain)
    os.chmod(scratch, stat.S_IMODE(st.st_mode))
    os.utime(scratch, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.replace(scratch, gz)
    _fsync_dir(plain.parent)
    plain.unlink()
    res.outcome = COMPRESSED if kind is None else PAIR_RECOMPRESSED
    res.gz_path, res.gz_size = str(gz), os.stat(gz).st_size
    return res


def symlinked_root(store_root: Path, archive: Path) -> Optional[Path]:
    """The first symlink on the way from *archive* up to *store_root* (the archive itself first).

    Also returns *archive* when it is not lexically under *store_root* or when it
    resolves outside the resolved store root: a root that cannot be shown to lie
    inside the store is refused, never walked.
    """
    store_root, archive = Path(store_root), Path(archive)
    try:
        relative = archive.relative_to(store_root)
    except ValueError:
        return archive
    current = archive
    for _ in range(len(relative.parts)):
        if current.is_symlink():
            return current
        current = current.parent
    if not archive.resolve().is_relative_to(store_root.resolve()):
        return archive
    return None


def _contained(directory: Path, pattern: str) -> List[Path]:
    """Files matching *pattern* that are not symlinks and resolve inside *directory*."""
    root = directory.resolve()
    return [p for p in directory.glob(pattern) if not p.is_symlink() and p.resolve().is_relative_to(root)]


def candidates(directories: Iterable[Path], days: float, now: Callable[[], float] = time.time) -> List[Path]:
    """Plain archive files with an mtime more than *days* old, oldest first."""
    cutoff, found = now() - days * 86400, []
    for path in (p for d in directories for p in _contained(Path(d), "*" + ARCHIVE_SUFFIX)):
        snap = _snap(path)
        if snap is not None and path.is_file() and snap[2] / 1e9 < cutoff:
            found.append((snap[2], path))
    return [p for _, p in sorted(found)]


def compress_archive_round(
    directories: Union[str, Path, Iterable[Union[str, Path]]],
    days: float,
    *,
    manual: bool = False,
    max_bytes: int = MAX_READ_BYTES,
    max_seconds: float = MAX_SECONDS,
    max_files: int = MAX_FILES,
    max_file_bytes: int = MAX_FILE_BYTES,
    now: Callable[[], float] = time.time,
    clock: Callable[[], float] = time.monotonic,
    free_space: Callable[[Path], int] = lambda d: shutil.disk_usage(d).free,
) -> RoundResult:
    """One round over the plain archives of one or several directories.

    An automatic round stops at the first budget that runs out (checked before
    each file) and skips a file over *max_file_bytes* as ``too_large``; a manual
    round takes it. ``free_space`` under twice the size is ``no_space``. An
    ``OSError`` (a failed read or write) propagates: it is never a success.
    """
    if isinstance(directories, (str, Path)):
        directories = [directories]
    result, reads = RoundResult(), [0]
    start, handled = clock(), 0
    token = _READS.set(reads)
    try:
        for plain in candidates([Path(d) for d in directories], days, now):
            result.bytes_read = reads[0]
            if handled >= max_files:
                result.stopped = "max_files"
            elif result.bytes_read >= max_bytes:
                result.stopped = "max_bytes"
            elif clock() - start >= max_seconds:
                result.stopped = "max_seconds"
            if result.stopped != "done":
                break
            size = (_snap(plain) or (0, 0, 0))[1]
            if not manual and size > max_file_bytes:
                result.files.append(FileResult(str(plain), SKIPPED, "too_large", plain_size=size))
                continue
            outcome = _settle(plain, free_space)
            result.files.append(outcome)
            if outcome.reason != "no_space":
                handled += 1
    finally:
        _READS.reset(token)
        result.bytes_read = reads[0]
    return result
