"""context_manifest.py: what a Claude Code session loaded, as paths, types, lengths and hashes.

``capture`` reads the session transcript that ``token_harvest.locate_session_transcript``
finds and reports three things: the instruction files (``instructions``
attachments), the skills of the initial ``skill_listing`` and the auto-memory
entry. It never stores file content and never raises: every failure becomes a
status (``unavailable``, ``partial``, ``error``) with a reason.

Status:
- ``ok``: an initial ``instructions`` attachment and an initial ``skill_listing``
  were both found, every entry was well formed and the scan budget held.
- ``partial``: the transcript was read but one of those is missing, an entry was
  malformed (counted in ``instructions.skipped``) or the budget was hit.
- ``unavailable``: no session id, or no transcript.
- ``error``: extraction raised.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from token_harvest import locate_session_transcript

logger = logging.getLogger(__name__)

SCHEMA = 1
MAX_SCAN_BYTES = 64 * 1024 * 1024
MAX_SCAN_SECONDS = 2.0
_SYNCED_PREFIX = "anthropic-skills:"
_WANTED_ATTACHMENTS = (b'"attachment"',)


def _home() -> Path:
    return Path.home()


def _display_path(path: str) -> str:
    """``~/...`` for a path under the home dir; anything else unchanged."""
    home = str(_home())
    if path == home or path.startswith(home + "/"):
        return "~" + path[len(home):]
    return path


def _in_config_home(path: str) -> bool:
    """True when ``path`` lies under ``~/.claude`` or any ``~/.claude-*``."""
    home = str(_home())
    if not path.startswith(home + "/"):
        return False
    top = path[len(home) + 1:].split("/", 1)[0]
    return top == ".claude" or top.startswith(".claude-")


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:16]


def _config_homes() -> List[Path]:
    """Existing ``~/.claude`` and ``~/.claude-*`` directories."""
    try:
        return sorted(p for p in _home().glob(".claude*") if p.is_dir() and (p.name == ".claude" or p.name.startswith(".claude-")))
    except OSError as exc:
        logger.warning("context_manifest: config homes unreadable: %s", exc)
        return []


def _scan_transcript(path: Path) -> Dict[str, Any]:
    """Stream the transcript and keep only ``instructions`` and ``skill_listing`` attachments.

    Only lines that mention ``"attachment"`` are parsed. The scan stops at
    ``MAX_SCAN_BYTES`` or ``MAX_SCAN_SECONDS`` and reports ``budget_hit``.
    """
    scan: Dict[str, Any] = {"instructions": [], "listings": [], "version": None,
                            "bytes": 0, "budget_hit": False}
    deadline = time.monotonic() + MAX_SCAN_SECONDS
    with open(path, "rb") as fh:
        for raw in fh:
            scan["bytes"] += len(raw)
            if scan["bytes"] > MAX_SCAN_BYTES or time.monotonic() > deadline:
                scan["budget_hit"] = True
                break
            if not any(marker in raw for marker in _WANTED_ATTACHMENTS):
                continue
            _take_attachment(raw, scan)
    return scan


def _take_attachment(raw: bytes, scan: Dict[str, Any]) -> None:
    try:
        entry = json.loads(raw)
    except ValueError:
        return
    attachment = entry.get("attachment") if isinstance(entry, dict) else None
    if not isinstance(attachment, dict):
        return
    kind = attachment.get("type")
    if kind not in ("instructions", "skill_listing"):
        return
    if scan["version"] is None and isinstance(entry.get("version"), str):
        scan["version"] = entry["version"]
    scan["instructions" if kind == "instructions" else "listings"].append(attachment)


def extract_instructions(attachments: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Union of the files over all ``instructions`` attachments, keyed by ``(type, path)``.

    The first attachment is ``initial``, later ones ``later``. An entry without a
    string path, a string type and string content is not recorded: it is counted
    in ``skipped`` so the caller does not read the manifest as complete.
    """
    files: Dict[tuple, Dict[str, Any]] = {}
    skipped = 0
    for index, attachment in enumerate(attachments):
        entries = attachment.get("files")
        for entry in entries if isinstance(entries, list) else []:
            record = _instruction_record(entry, "initial" if index == 0 else "later")
            if record is None:
                skipped += 1
            else:
                files.setdefault((record["type"], record["path"]), record)
    ordered = list(files.values())
    by_type: Dict[str, int] = {}
    for record in ordered:
        by_type[record["type"]] = by_type.get(record["type"], 0) + 1
    return {"files": ordered, "total_chars": sum(r["chars"] for r in ordered),
            "by_type": by_type, "attachments": len(attachments), "skipped": skipped}


def _instruction_record(entry: Any, seen: str) -> Optional[Dict[str, Any]]:
    if not isinstance(entry, dict):
        return None
    path, kind, content = entry.get("path"), entry.get("type"), entry.get("content")
    if not (isinstance(path, str) and path and isinstance(kind, str) and kind and isinstance(content, str)):
        return None
    return {"path": _display_path(path), "type": kind, "chars": len(content),
            "sha256": _digest(content), "in_config_home": _in_config_home(path), "seen": seen}


def extract_auto_memory(attachments: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """The ``AutoMem`` entry of the instructions: ``loaded``, ``path``, ``chars``."""
    for attachment in attachments:
        entries = attachment.get("files")
        for entry in entries if isinstance(entries, list) else []:
            record = _instruction_record(entry, "initial")
            if record is not None and record["type"] == "AutoMem":
                return {"loaded": True, "path": record["path"], "chars": record["chars"]}
    return {"loaded": False, "path": None, "chars": None}


def _skill_origin(name: str, repo_root: Optional[Path], homes: Sequence[Path]) -> str:
    if name.startswith(_SYNCED_PREFIX):
        return "synced"
    if ":" in name:
        return "namespaced"
    if repo_root is not None and (repo_root / ".claude" / "skills" / name / "SKILL.md").is_file():
        return "repo"
    if any((home / "skills" / name / "SKILL.md").is_file() for home in homes):
        return "user"
    return "other"


def _listing_names(listing: Dict[str, Any]) -> List[str]:
    names = listing.get("names")
    return [n for n in names if isinstance(n, str) and n] if isinstance(names, list) else []


def extract_skills(
    listings: Sequence[Dict[str, Any]], repo_root: Optional[Path], homes: Sequence[Path],
) -> Dict[str, Any]:
    """Skills of the first ``isInitial: true`` listing, split by origin; ``later`` is the sorted union of the rest."""
    initial = next((l for l in listings if l.get("isInitial") is True), None)
    later = sorted({n for l in listings if l is not initial for n in _listing_names(l)})
    groups: Dict[str, List[str]] = {k: [] for k in ("repo", "user", "synced", "namespaced", "other")}
    if initial is None:
        return {"initial_count": None, "initial_chars": None, "initial": groups, "later": later}
    for name in _listing_names(initial):
        groups[_skill_origin(name, repo_root, homes)].append(name)
    count, content = initial.get("skillCount"), initial.get("content")
    return {
        "initial_count": count if isinstance(count, int) and not isinstance(count, bool) else None,
        "initial_chars": len(content) if isinstance(content, str) else None,
        "initial": groups, "later": later,
    }


def _resolve_repo_root() -> Optional[Path]:
    try:
        from dispatch_worktree_isolation import resolve_consumer_project_root
        return resolve_consumer_project_root()
    except Exception as exc:  # fail open: without a repo root no skill is classed as `repo`
        logger.warning("context_manifest: repo root unresolved (%s): %s", type(exc).__name__, exc)
        return None


def _result(status: str, reason: Optional[str] = None, **sections: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"schema": SCHEMA, "status": status}
    if reason is not None:
        out["reason"] = reason
    out.update(sections)
    return out


def _build(path: Path, repo_root: Optional[Path]) -> Dict[str, Any]:
    scan = _scan_transcript(path)
    instructions = extract_instructions(scan["instructions"])
    skills = extract_skills(scan["listings"], repo_root, _config_homes())
    complete = (
        bool(scan["instructions"]) and skills["initial_count"] is not None
        and not instructions["skipped"] and not scan["budget_hit"]
    )
    return _result(
        "ok" if complete else "partial",
        cli_version=scan["version"],
        transcript={"path": _display_path(str(path)), "bytes": scan["bytes"], "budget_hit": scan["budget_hit"]},
        instructions=instructions,
        auto_memory=extract_auto_memory(scan["instructions"]),
        skills=skills,
    )


def capture(
    session_id: Optional[str],
    *,
    claude_projects_dir: Optional[Path] = None,
    repo_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """The context manifest of one session: paths, types, lengths and hashes, never content.

    Never raises. ``repo_root`` classes a skill as ``repo`` when
    ``<repo_root>/.claude/skills/<name>/SKILL.md`` exists; without it the
    consumer project root is resolved, failing open to no repo.
    """
    sid = (session_id or "").strip()
    if not sid:
        return _result("unavailable", "no_session_id")
    try:
        path = locate_session_transcript(sid, claude_projects_dir=claude_projects_dir)
        if path is None:
            return _result("unavailable", "transcript_not_found")
        return _build(path, repo_root if repo_root is not None else _resolve_repo_root())
    except Exception as exc:  # capture is a measurement and must not break its caller
        logger.warning("context_manifest: extract failed for session %s: %s: %s", sid, type(exc).__name__, exc)
        return _result("error", f"extract_failed:{type(exc).__name__}")


__all__ = ["capture", "extract_auto_memory", "extract_instructions", "extract_skills"]
