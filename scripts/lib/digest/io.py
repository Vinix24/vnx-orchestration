"""digest/io.py — Digest-specific write helpers and NDJSON state reader.

Public surface:
  write_digest_output(content, output_path) -> None
  read_state_ndjson(path) -> list[dict]
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from atomic_io import atomic_write_text

logger = logging.getLogger(__name__)


def write_digest_output(content: str, output_path: Path) -> None:
    """Write digest markdown atomically. Raises OSError on write failure."""
    atomic_write_text(output_path, content)


def read_state_ndjson(path: Path) -> list[dict]:
    """Read NDJSON file line by line, skipping malformed lines.

    Returns [] when the file does not exist (FileNotFoundError).
    Skips and debug-logs each line that fails json.JSONDecodeError.
    No other exceptions caught — OSError (e.g. permission denied) propagates.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []

    records: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            logger.debug("digest.io: skipping malformed NDJSON line in %s", path)
    return records
