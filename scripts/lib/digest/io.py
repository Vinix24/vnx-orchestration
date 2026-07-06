"""digest/io.py — Digest-specific write helpers and NDJSON state reader.

Public surface:
  write_digest_output(content, output_path) -> None
  read_state_ndjson(path) -> list[dict]
"""

from __future__ import annotations

import logging
from pathlib import Path

from atomic_io import atomic_write_text
from ndjson_io import read_ndjson

logger = logging.getLogger(__name__)


def write_digest_output(content: str, output_path: Path) -> None:
    """Write digest markdown atomically. Raises OSError on write failure."""
    atomic_write_text(output_path, content)


def read_state_ndjson(path: Path) -> list[dict]:
    """Read NDJSON file line by line, tolerating a torn tail and malformed lines.

    Returns [] when the file does not exist. Delegates to the shared
    :func:`ndjson_io.read_ndjson` guard so a partial final line left by a crash
    mid-append is skipped instead of breaking the reader. OSError (other than a
    missing file) propagates.
    """
    return read_ndjson(path)
