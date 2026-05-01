"""Parse 'Files Modified' section from completion-report markdown."""

from __future__ import annotations

import re
from pathlib import Path
from typing import List


def _extract_changed_files_from_report(report_path: Path, repo_root: Path) -> List[Path]:
    """Best-effort: parse 'Files Modified' section from report markdown.

    Supports two formats:
    1. Bullet list:  - `path/to/file.py` — description
    2. Markdown table:  | `path/to/file.py` | Type | Description |
    """
    if not report_path.exists():
        return []

    try:
        content = report_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []

    pattern = re.compile(
        r"^#{2,3}\s+Files\s+Modified(?:/Created)?\s*$", re.MULTILINE
    )
    match = pattern.search(content)
    if not match:
        return []

    section = content[match.end():]
    next_heading = re.search(r"^##+\s+", section, re.MULTILINE)
    if next_heading:
        section = section[:next_heading.start()]

    files: List[Path] = []
    for line in section.splitlines():
        line = line.strip()

        if re.match(r"^\|[\s\-:|]+\|$", line):
            continue

        backtick = re.search(r"`([^`]+\.\w+)`", line)
        if backtick:
            raw_path = backtick.group(1).strip()
        elif line.startswith("-"):
            raw_path = line.lstrip("-").strip().split(":", 1)[0].strip()
        elif line.startswith("|"):
            cells = [c.strip() for c in line.split("|") if c.strip()]
            raw_path = cells[0] if cells else ""
        else:
            continue

        if not raw_path or not re.search(r"\.\w+$", raw_path):
            continue

        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = (repo_root / candidate).resolve()
        if candidate.exists():
            files.append(candidate)

    return files
