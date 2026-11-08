"""context_manifest.py: what a Claude Code session loaded, as paths, types, lengths and hashes.

``capture`` reads the session transcript that ``token_harvest.locate_session_transcript``
finds and reports: the instruction files (``instructions`` attachments), the
skills of the initial ``skill_listing``, the auto-memory entry, the hooks that
ran (``hook_success`` attachments), the account (auth kind and a hash of the
organization id, never an address) and the config home. It never stores file
content and never raises: every failure becomes a status (``unavailable``,
``unsupported``, ``partial``, ``error``) with a reason. Only ``provider="claude"``
is supported; any other provider is ``unsupported`` without touching the file system.

Status:
- ``ok``: an initial ``instructions`` attachment and an initial ``skill_listing``
  were both found, a ``credential_org`` record identified the account, every
  entry was well formed and the scan budget held. No hook at all is a valid
  measurement; a ``hook_success`` record without event, name or command is not.
- ``partial``: the transcript was read but one of those is missing, an entry was
  malformed (counted in ``instructions.skipped``) or the budget was hit.
- ``unavailable``: no session id, or no transcript.
- ``unsupported``: ``provider`` is not ``claude`` (reason ``provider_not_supported:<provider>``).
- ``error``: extraction raised.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
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
_ATTACHMENT_BUCKETS = {"instructions": "instructions", "skill_listing": "listings",
                       "credential_org": "credentials", "hook_success": "hooks"}


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
    """Stream the transcript and keep only the attachments in ``_ATTACHMENT_BUCKETS``.

    Only lines that mention ``"attachment"`` are parsed. The scan stops at
    ``MAX_SCAN_BYTES`` or ``MAX_SCAN_SECONDS`` and reports ``budget_hit``.
    """
    scan: Dict[str, Any] = {"instructions": [], "listings": [], "credentials": [], "hooks": [],
                            "version": None, "bytes": 0, "budget_hit": False}
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
    bucket = _ATTACHMENT_BUCKETS.get(attachment.get("type"))
    if bucket is None:
        return
    if scan["version"] is None and isinstance(entry.get("version"), str):
        scan["version"] = entry["version"]
    scan[bucket].append(attachment)


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


def _clean(value: Any) -> Optional[str]:
    """``value`` when it is a non-empty string without ``@``; otherwise ``None``.

    The manifest never carries an address: a string that could hold one is dropped.
    """
    return value if isinstance(value, str) and value and "@" not in value else None


_SHELLS = frozenset({"sh", "bash", "zsh", "dash"})
_INTERPRETER = re.compile(r"^(python[0-9.]*|node|ruby|perl)$")
_INTERPRETER_INLINE = frozenset({"-c", "-e", "-m"})
_SHELL_META = frozenset(";|&$<>`")
_INLINE_FLAG = re.compile(r"^-[A-Za-z]*c[A-Za-z]*$")


def _single_path(token: str) -> Optional[str]:
    """``token`` as an absolute or ``~/`` path when it is one bare path, else ``None``.

    Whitespace, ``=`` and shell metacharacters make it an argument or a script, not a path.
    """
    if not token.startswith(("/", "~/")):
        return None
    if any(c.isspace() or c == "=" or c in _SHELL_META for c in token):
        return None
    return str(_home()) + token[1:] if token.startswith("~/") else token


def _inline_script_path(script: str) -> Optional[str]:
    """The first token of an inline ``-c`` script, only when that token alone is a single path."""
    try:
        tokens = shlex.split(script)
    except ValueError:
        return None
    return _single_path(tokens[0]) if tokens else None


def _is_assignment(token: str) -> bool:
    name, eq, _ = token.partition("=")
    return bool(eq) and name.isidentifier()


def _is_env(token: str) -> bool:
    return token == "env" or (token.startswith("/") and token.rsplit("/", 1)[-1] == "env")


def _executable_index(tokens: Sequence[str]) -> Optional[int]:
    """Index of the executable: past leading ``KEY=value`` and an ``env`` with its flags and assignments."""
    index = 0
    while index < len(tokens) and _is_assignment(tokens[index]):
        index += 1
    if index < len(tokens) and _is_env(tokens[index]):
        index += 1
        while index < len(tokens) and (tokens[index].startswith("-") or _is_assignment(tokens[index])):
            index += 1
    return index if index < len(tokens) else None


def _leading_flags(tokens: Sequence[str], start: int) -> int:
    """Index of the first token from ``start`` that is not a flag (``len(tokens)`` when none)."""
    index = start
    while index < len(tokens) and tokens[index].startswith("-"):
        index += 1
    return index


def _script_path(tokens: Sequence[str], exe: int, shell: bool) -> Optional[str]:
    """Path of the script a shell or interpreter runs; inline code (``-c``, ``-e``, ``-m``) is ``None`` or its first token."""
    end = _leading_flags(tokens, exe + 1)
    flags = tokens[exe + 1:end]
    if shell:
        for offset, flag in enumerate(flags):
            if _INLINE_FLAG.match(flag):
                follow = exe + 2 + offset
                return _inline_script_path(tokens[follow]) if follow < len(tokens) else None
    elif any(flag in _INTERPRETER_INLINE for flag in flags):
        return None
    return _single_path(tokens[end]) if end < len(tokens) else None


def _command_path(command: str) -> Optional[str]:
    """The executable or script path of a hook command, else ``None``.

    Positions are read, never scanned: leading ``KEY=value`` assignments and
    ``env`` (with its flags and assignments) are skipped, the next token is the
    executable. For a shell or an interpreter the first non-flag operand is the
    script instead; a shell ``-c`` yields the first token of its inline script,
    and an interpreter ``-c``/``-e``/``-m`` yields ``None``. The result must be
    one bare absolute or ``~/`` path. No token after that position is read, so
    an argument never becomes the path.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    index = _executable_index(tokens)
    if index is None:
        return None
    base = tokens[index].rsplit("/", 1)[-1]
    if base in _SHELLS or _INTERPRETER.match(base):
        return _script_path(tokens, index, base in _SHELLS)
    return _single_path(tokens[index])


def _hook_record(attachment: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    event, name, command = _clean(attachment.get("hookEvent")), _clean(attachment.get("hookName")), attachment.get("command")
    if event is None or name is None or not isinstance(command, str) or not command:
        return None
    path = _clean(_command_path(command))
    return {"event": event, "name": name,
            "command_path": _display_path(path) if path else None,
            "in_config_home": _in_config_home(path) if path else False,
            "command_sha256": _digest(command)}


def extract_hooks(attachments: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Hooks that ran, grouped by ``(event, name, command hash)`` with a ``count`` each.

    The command text itself is not kept, only the path of its script. A record
    without event, name or command is counted in ``skipped``, not recorded.
    """
    groups: Dict[tuple, Dict[str, Any]] = {}
    skipped = 0
    for attachment in attachments:
        record = _hook_record(attachment)
        if record is None:
            skipped += 1
            continue
        entry = groups.setdefault((record["event"], record["name"], record["command_sha256"]), {**record, "count": 0})
        entry["count"] += 1
    return {"hooks": list(groups.values()), "total": sum(h["count"] for h in groups.values()), "skipped": skipped}


def extract_account(attachments: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Auth kind and a sha256 of the organization id, from the first ``credential_org`` record.

    A ``credential_org`` record is the OAuth credential's organization, so it
    gives ``auth: "oauth"``. Without one the kind is ``"unknown"``: an api-key
    session is not guessed. The record's other fields are never read.
    """
    for attachment in attachments:
        org = attachment.get("organizationUuid")
        if isinstance(org, str) and org:
            return {"auth": "oauth", "org_sha256": hashlib.sha256(org.encode("utf-8", errors="replace")).hexdigest()}
    return {"auth": "unknown", "org_sha256": None}


def _transcript_config_home(path: Path) -> Optional[str]:
    """``<home>/.claude[-x]`` when the transcript lies at ``<that>/projects/<enc>/<id>.jsonl``."""
    projects = path.parent.parent
    candidate = str(projects.parent)
    return candidate if projects.name == "projects" and _in_config_home(candidate + "/x") else None


def extract_config_dir(path: Path) -> Dict[str, Any]:
    """The config home the session ran under, as a display path, and whether it is the default.

    The transcript's own location is the evidence; without it ``CLAUDE_CONFIG_DIR``
    of this process, then ``~/.claude``, applies (``source`` says which).
    """
    default = str(_home() / ".claude")
    found = _transcript_config_home(path)
    source = "transcript" if found else "env"
    chosen = found or os.environ.get("CLAUDE_CONFIG_DIR") or default
    shown = _clean(_display_path(chosen))
    return {"path": shown, "default": chosen == default, "source": source}


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
    hooks = extract_hooks(scan["hooks"])
    account = extract_account(scan["credentials"])
    complete = (
        bool(scan["instructions"]) and skills["initial_count"] is not None
        and not instructions["skipped"] and not hooks["skipped"]
        and account["auth"] != "unknown" and not scan["budget_hit"]
    )
    return _result(
        "ok" if complete else "partial",
        cli_version=scan["version"],
        transcript={"path": _display_path(str(path)), "bytes": scan["bytes"], "budget_hit": scan["budget_hit"]},
        instructions=instructions,
        auto_memory=extract_auto_memory(scan["instructions"]),
        skills=skills,
        hooks=hooks,
        account=account,
        config_dir=extract_config_dir(path),
    )


def capture(
    session_id: Optional[str],
    *,
    provider: str = "claude",
    claude_projects_dir: Optional[Path] = None,
    repo_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """The context manifest of one session: paths, types, lengths and hashes, never content.

    Never raises. ``repo_root`` classes a skill as ``repo`` when
    ``<repo_root>/.claude/skills/<name>/SKILL.md`` exists; without it the
    consumer project root is resolved, failing open to no repo. A ``provider``
    other than ``claude`` is ``unsupported`` before anything is read.
    """
    name = str(provider).strip().lower()
    if name != "claude":
        return _result("unsupported", f"provider_not_supported:{name}")
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


__all__ = ["capture", "extract_account", "extract_auto_memory", "extract_config_dir", "extract_hooks",
           "extract_instructions", "extract_skills"]
