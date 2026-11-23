"""plan_table.py — the deliverable table of one plan (ADR-096, B8/B9).

Pure: no store access, no writes. ``find_plan`` finds the one plan of a track,
``parse`` reads the first table with a ``#`` label column and an order column,
``map_label`` turns a row label into the one deliverable id of the track.
Unreadable parts are named with their line number; nothing is guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

ORDER_HEADERS = frozenset({"hangt af van", "depends on", "na", "after"})
PATHS_HEADERS = frozenset({"paden", "bestanden", "paths"})
_LABEL_RE = re.compile(r"^[A-Z][A-Za-z0-9]*(-[A-Za-z0-9]+)*$")
_DLV_RE = re.compile(r"dlv-[0-9a-f]{12}")
_SEP_RE = re.compile(r"^:?-{3,}:?$")
_NONE_WORDS = frozenset({"geen", "none", "-"})

@dataclass
class Row:
    label: str
    dlv_id: Optional[str] = None
    depends: List[Tuple[str, str]] = field(default_factory=list)
    paths: Optional[List[str]] = None
    line: int = 0
    order_error: Optional[str] = None
    paths_error: Optional[str] = None

@dataclass
class Table:
    path: Path
    rows: List[Row] = field(default_factory=list)
    found: bool = False
    header_line: Optional[int] = None
    order_header: Optional[str] = None

def is_dlv_id(token: str) -> bool:
    return bool(_DLV_RE.fullmatch(token or ""))

def title_matches_label(label: str, title: str) -> bool:
    return bool(label and isinstance(title, str)
                and (title.startswith(f"{label}:") or title.startswith(f"{label} ")))

def map_label(label: str, deliverables: Sequence[Tuple[str, str]]) -> Tuple[Optional[str], str]:
    if not label:
        return None, "no deliverable titled "
    matches = [dlv for dlv, title in deliverables if title_matches_label(label, title)]
    if len(matches) == 1:
        return matches[0], ""
    return (None, f"{len(matches)} deliverables titled {label}") if matches else (
        None, f"no deliverable titled {label}")

def find_plan(track_id: str, repo_root: Path) -> Tuple[Optional[Path], str]:
    """The one plan of *track_id*, else a note naming none or several."""
    root = Path(repo_root)
    root = root / "claudedocs" if (root / "claudedocs").is_dir() else root
    needle = f"Track `{track_id}`"
    matches: List[Path] = []
    for path in sorted(root.rglob("plan*.md")):
        relative = path.relative_to(root)
        if "archive" in relative.parts or relative.name.startswith("plan-gate"):
            continue
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                head = list(islice(handle, 12))
        except OSError:  # vnx-silent-except: an unreadable plan cannot name the track; remaining candidates still decide
            continue
        if any(needle in line for line in head):
            matches.append(path)
    if len(matches) > 1:
        return None, "several plans: " + ", ".join(str(path) for path in matches)
    return (matches[0], "") if matches else (None, "no plan")

def _strip(text: str) -> str:
    return (text or "").replace("`", "").strip()

def _cells(line: str) -> List[str]:
    text = line.strip()
    text = text[1:] if text.startswith("|") else text
    text = text[:-1] if text.endswith("|") else text
    return [cell.strip() for cell in text.split("|")]

def _is_row(line: str) -> bool:
    return line.strip().startswith("|")

def _is_separator(line: str) -> bool:
    return _is_row(line) and all(_SEP_RE.fullmatch(cell) for cell in _cells(line))

def _parse_order(cell: str, line: int) -> Tuple[List[Tuple[str, str]], Optional[str]]:
    raw = _strip(cell)
    lowered = raw.lower()
    if not raw or lowered in _NONE_WORDS or lowered.startswith(("geen (", "none (")):
        return [], None
    depends: List[Tuple[str, str]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        kind = "soft" if part.endswith(" (soft)") else "hard"
        part = part[: -len(" (soft)")].strip() if kind == "soft" else part
        if is_dlv_id(part) or _LABEL_RE.fullmatch(part):
            depends.append((part, kind))
        else:
            return [], f"unreadable order part {part!r} at line {line}"
    return depends, None

def _parse_paths(cell: str, line: int) -> Tuple[Optional[List[str]], Optional[str]]:
    raw = _strip(cell)
    if not raw or raw.lower() in _NONE_WORDS:
        return None, None
    paths: List[str] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        candidate = Path(part)
        if " " in part or candidate.is_absolute() or ".." in candidate.parts:
            return None, f"unreadable path {part!r} at line {line}"
        paths.append(part)
    return (paths or None), None

def parse(path: Path) -> Table:
    """The first deliverable table in *path*; ``found=False`` when there is none."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines[:-1]):
        if not (_is_row(line) and _is_separator(lines[index + 1])):
            continue
        headers = _cells(line)
        normalised = [_strip(cell).lower() for cell in headers]
        orders = [k for k, cell in enumerate(normalised) if cell in ORDER_HEADERS]
        if "#" not in normalised or not orders:
            continue
        label_index, order_index = normalised.index("#"), orders[0]
        id_index = normalised.index("id") if "id" in normalised else None
        paths_index = next((k for k, cell in enumerate(normalised) if cell in PATHS_HEADERS), None)
        rows: List[Row] = []
        row_index = index + 2
        while row_index < len(lines) and _is_row(lines[row_index]) and not _is_separator(lines[row_index]):
            cells = _cells(lines[row_index])

            def at(position: Optional[int]) -> str:
                return cells[position] if position is not None and position < len(cells) else ""

            number = row_index + 1
            id_match = _DLV_RE.search(at(id_index)) if id_index is not None else None
            depends, order_error = _parse_order(at(order_index), number)
            paths, paths_error = _parse_paths(at(paths_index), number) if paths_index is not None else (None, None)
            rows.append(Row(label=_strip(at(label_index)), dlv_id=id_match.group(0) if id_match else None,
                            depends=depends, paths=paths, line=number, order_error=order_error,
                            paths_error=paths_error))
            row_index += 1
        return Table(Path(path), rows, True, index + 1, headers[order_index])
    return Table(Path(path), found=False)
