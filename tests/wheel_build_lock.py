"""One exclusive file lock around every wheel build of the repo root (OI-2875).

setuptools writes its intermediate tree to ``<repo_root>/build/bdist.*/wheel``
whatever ``--outdir`` says, so two builds on two xdist workers delete each
other's folder. Every test that builds the repo root takes this lock around the
build subprocess only.
"""
from __future__ import annotations

import fcntl
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOCK_PATH = REPO_ROOT / "build" / ".wheel-build.lock"
DEFAULT_TIMEOUT_S = 600.0
_POLL_S = 0.1


@contextmanager
def wheel_build_lock(
    lock_path: Optional[Path] = None, timeout: float = DEFAULT_TIMEOUT_S
) -> Iterator[Path]:
    """Hold an exclusive flock on ``lock_path``; fail the test after ``timeout`` s."""
    path = Path(lock_path) if lock_path is not None else DEFAULT_LOCK_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as handle:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    pytest.fail(
                        f"wheel build lock {path} not acquired within {timeout:g}s: "
                        "another wheel build holds it"
                    )
                time.sleep(_POLL_S)
        try:
            yield path
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
