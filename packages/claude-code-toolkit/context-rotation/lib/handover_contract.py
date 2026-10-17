"""The handover contract: what a handover must hold before a fresh session gets it.

A valid handover has a `## Running` section (background work still active, or
"none"), a `## Next steps` section whose first line is a step numbered 1, and at
most `handover_max_chars` characters. Standard library only.
"""
from __future__ import annotations

import re
from typing import List, Optional

RUNNING = re.compile(r"^##[ \t]+Running[ \t]*$", re.IGNORECASE | re.MULTILINE)
NEXT_STEPS = re.compile(r"^##[ \t]+Next steps[ \t]*$", re.IGNORECASE | re.MULTILINE)
FIRST_STEP = re.compile(r"^[ \t]*1[.)][ \t]+\S")
HEADING = re.compile(r"^[ \t]*#")


def decode(data: Optional[bytes]) -> Optional[str]:
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _first_step_numbered(section: str) -> bool:
    for line in section.splitlines():
        if not line.strip():
            continue
        if HEADING.match(line):
            return False
        return FIRST_STEP.match(line) is not None
    return False


def problems(data: Optional[bytes], max_chars: int) -> List[str]:
    """Everything that keeps this handover from being used, in plain words. Empty when valid."""
    text = decode(data)
    if data is None:
        return ["the file cannot be read"]
    if text is None:
        return ["the file is not UTF-8 text"]
    found = []
    if len(text) > max_chars:
        found.append("it is %d characters long and the limit is %d" % (len(text), max_chars))
    if not RUNNING.search(text):
        found.append('the "## Running" section is missing (write "none" under it when nothing runs in the background)')
    match = NEXT_STEPS.search(text)
    if match is None:
        found.append('the "## Next steps" section is missing')
    elif not _first_step_numbered(text[match.end():]):
        found.append('the first line under "## Next steps" is not a step numbered "1."')
    return found
