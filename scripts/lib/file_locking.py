#!/usr/bin/env python3
"""file_locking.py — fcntl-protected JSON read-modify-write helper.

Provides ``file_locked_rmw``: a contextmanager that holds an exclusive
``fcntl.flock`` on a JSON file while the caller mutates the parsed dict,
then atomically writes the result back. Used by components that perform
read-modify-write on shared JSON state (e.g. ``events/worker_health.json``)
where multiple workers may write concurrently.

BILLING SAFETY: No Anthropic SDK. No api.anthropic.com calls.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator

logger = logging.getLogger(__name__)


@contextmanager
def file_locked_rmw(path: Path) -> Iterator[Dict[str, Any]]:
    """Read-modify-write a JSON dict file under fcntl.LOCK_EX.

    Behaviour:
      - Creates the parent directory if missing.
      - Opens the file in r+ mode (creating an empty file if needed).
      - Holds ``fcntl.LOCK_EX`` for the lifetime of the with-block.
      - Yields the parsed dict; if the file is empty or invalid JSON,
        yields an empty dict.
      - After the block, truncates and writes the (possibly mutated) dict
        back as indented JSON, then ``flush() + os.fsync()`` for durability.
      - Releases the lock and closes the handle even if the block raises;
        on exception, the file is left untouched.

    The caller mutates the yielded dict in place. Reassignment inside the
    block has no effect on what is persisted; mutate keys instead.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    # Ensure file exists so r+ doesn't fail; create empty if needed.
    if not p.exists():
        # Open with a+ to create, then close — subsequent r+ open handles RMW.
        with p.open("a"):
            pass

    fd = p.open("r+", encoding="utf-8")
    fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
    success = False
    try:
        fd.seek(0)
        content = fd.read().strip()
        if content:
            try:
                data = json.loads(content)
                if not isinstance(data, dict):
                    logger.debug(
                        "file_locked_rmw: %s did not contain a JSON object; "
                        "resetting to {}",
                        p,
                    )
                    data = {}
            except json.JSONDecodeError:
                logger.debug(
                    "file_locked_rmw: %s contained invalid JSON; resetting to {}",
                    p,
                )
                data = {}
        else:
            data = {}

        yield data

        serialized = json.dumps(data, indent=2)
        fd.seek(0)
        fd.truncate()
        fd.write(serialized)
        fd.flush()
        try:
            os.fsync(fd.fileno())
        except OSError:
            # fsync may fail on some filesystems; correctness is preserved
            # by flush + flock semantics. Don't propagate.
            pass
        success = True
    finally:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        finally:
            fd.close()
        if not success:
            logger.debug("file_locked_rmw: aborted RMW on %s due to exception", p)
