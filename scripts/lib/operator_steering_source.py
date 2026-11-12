"""Operator-steering source: binding and per-file containment (ADR-072, OB-1a).

Nothing is read before ``state/steering_sources.json`` binds the project; every file is
re-checked on the path of its open file descriptor. Nothing read is logged or written.
"""
from __future__ import annotations

import fcntl
import getpass
import hashlib
import json
import os
import re
import stat
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, TextIO, Tuple

from atomic_io import atomic_write_json
from vnx_paths import project_id_from_state_dir

SCHEMA = "steering_sources/1"
BINDING_FILE = "steering_sources.json"
LEDGER_FILE = "steering_sources.ndjson"
LOCK_FILE = "steering_sources.lock"
DEFAULT_PROJECTS_ROOT = Path.home() / ".claude" / "projects"
FORBIDDEN_SLUG_PARTS = ("clients", "business", "salesminds")
SYSTEM_TAGS = (("<task-notification", "task_notification"), ("<system-reminder", "system_reminder"),
               ("<command-", "system_tag"), ("<local-command-", "system_tag"), ("<bash-", "system_tag"))
# Typed by scripts/t0_rotate_spawn.sh (FIRST_MESSAGE) and t0_rotation_state.goal_followup_text.
AUTOMATIC_PREFIXES = ("/kickoff rotatie ", "Je bent de verse T0")
GOAL_MARKER = ", hervat na context-rotatie"
PASTED = re.compile(r"<pasted_content\b[^>]*>.*?(?:</pasted_content[^>]*>|\Z)", re.DOTALL)


class BindingError(ValueError):
    """bind refused: invalid input (exit 2)."""


class Refused(Exception):
    """A file failed containment; ``args[0]`` is the count key."""


@dataclass
class SourceResult:
    state: str  # ok | absent | unbound | error
    reason: str = ""
    refused: Dict[str, int] = field(default_factory=dict)
    refused_files: List[str] = field(default_factory=list)
    prompts: List["Prompt"] = field(default_factory=list)
    excluded: Dict[str, int] = field(default_factory=dict)


@dataclass
class Prompt:
    prompt_id: str
    text: str
    source_file: str
    line: int


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _slug_error(slug: object, root: Path) -> str:
    """Why ``slug`` may not be a bound transcript dir, or "" when it may."""
    if not isinstance(slug, str) or not slug or "/" in slug or slug in (".", ".."):
        return f"slug ongeldig: {slug!r}"
    if any(p in slug.lower() for p in FORBIDDEN_SLUG_PARTS):
        return f"klant- of businessmap geweigerd: {slug!r}"
    if (root / slug).resolve() != root / slug:
        return f"slug ligt niet direct onder {root}: {slug!r}"
    return ""


def _memory_ok(memory: Path, root: Path) -> bool:
    return _under(memory, root) and not any(p in str(memory).lower() for p in FORBIDDEN_SLUG_PARTS)


def bind(state_dir: Path, projects_root: Path, slugs: List[str], memory_dir: Path) -> dict:
    project_id = project_id_from_state_dir(state_dir)
    if not project_id:
        raise BindingError("project_id niet af te leiden uit de state-map")
    root = projects_root.resolve()
    memory = Path(memory_dir).resolve()
    errors = [e for e in (_slug_error(s, root) for s in slugs) if e]
    if errors or not slugs or not _memory_ok(memory, root):
        raise BindingError(errors[0] if errors else "geen slug of memory-map buiten de projectenmap")
    record = {"schema": SCHEMA, "project_id": project_id, "transcript_dirs": list(slugs),
              "memory_dir": str(memory), "bound_at": datetime.now(timezone.utc).isoformat()}
    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / LOCK_FILE, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            previous = _last_ledger(state_dir)
            entry = {**record, "previous_transcript_dirs": (previous or {}).get("transcript_dirs"),
                     "previous_memory_dir": (previous or {}).get("memory_dir"), "actor": getpass.getuser()}
            _append_ledger(state_dir, entry)
            atomic_write_json(state_dir / BINDING_FILE, record)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return record


def _last_ledger(state_dir: Path) -> Optional[dict]:
    """Last ledger line, or None when there is none or it is not a valid object."""
    try:
        lines = (state_dir / LEDGER_FILE).read_text(encoding="utf-8").splitlines()
        last = json.loads(lines[-1])
    except (OSError, ValueError, IndexError):
        return None
    return last if isinstance(last, dict) else None


def _append_ledger(state_dir: Path, entry: dict) -> None:
    """Under the bind lock: a last line without its newline never joins the new one."""
    payload = (json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    with open(state_dir / LEDGER_FILE, "ab+") as handle:  # vnx-atomic-write: append-only ledger, fsynced
        end = os.fstat(handle.fileno()).st_size
        if end and os.pread(handle.fileno(), 1, end - 1) != b"\n":
            payload = b"\n" + payload
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def load_binding(state_dir: Path, projects_root: Path) -> Tuple[Optional[dict], str]:
    """Return (binding, "") or (None, reason). Anything not positively valid is unbound."""
    try:
        record = json.loads((state_dir / BINDING_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "geen geldig bindingsbestand"
    if not isinstance(record, dict) or record.get("schema") != SCHEMA:
        return None, "onbekend schema"
    project_id = project_id_from_state_dir(state_dir)
    if not project_id or record.get("project_id") != project_id:
        return None, "project_id komt niet overeen"
    last = _last_ledger(state_dir)
    if last is None or any(last.get(k) != record.get(k) for k in ("project_id", "transcript_dirs", "memory_dir")):
        return None, "bindingsbestand komt niet overeen met het bindingsledger"
    slugs, memory, root = record.get("transcript_dirs"), record.get("memory_dir"), projects_root.resolve()
    if not isinstance(slugs, list) or not slugs:
        return None, "transcript_dirs ongeldig"
    errors = [e for e in (_slug_error(s, root) for s in slugs) if e]
    if errors:
        return None, errors[0]
    if not isinstance(memory, str) or not _memory_ok(Path(memory).resolve(), root):
        return None, "memory_dir ongeldig"
    return record, ""


def _fd_path(fd: int) -> str:
    """Real path of an open descriptor; raises Refused when it cannot be established."""
    try:
        if hasattr(fcntl, "F_GETPATH"):
            return os.fsdecode(fcntl.fcntl(fd, fcntl.F_GETPATH, b"\0" * 1024).split(b"\0", 1)[0])
        return os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        raise Refused("fd_path_unverifiable")


def open_contained(path: Path, root: Path):
    """Open ``path`` for reading only if it resolves, before and after opening, under ``root``.

    ``root`` must be the canonical path ``read_sources`` validated. It is never resolved again here:
    a directory swapped for a symlink after that check must fail both comparisons, not become the boundary.
    """
    if not _under(Path(os.path.realpath(path)), root):
        raise Refused("outside_bound_dir")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        raise Refused("open_failed")
    try:
        if not _under(Path(os.path.realpath(_fd_path(fd))), root):
            raise Refused("outside_bound_dir")
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Refused("not_regular_file")
        return os.fdopen(fd, "r", encoding="utf-8", errors="replace")
    except BaseException:
        os.close(fd)
        raise


def read_sources(state_dir: Path, projects_root: Path, consume: Callable[[TextIO, str], None]) -> SourceResult:
    """Hand every contained transcript file to ``consume(handle, filename)``; nothing without a binding.

    A containment refusal makes the whole source `error` (ADR-072 besluit 2).
    """
    binding, why = load_binding(state_dir, projects_root)
    if binding is None:
        return SourceResult("unbound", why)
    root = projects_root.resolve()
    refused: Counter = Counter()
    refused_files: List[str] = []
    present = 0
    for slug in binding["transcript_dirs"]:
        directory = root / slug
        if not directory.is_dir():
            continue
        present += 1
        if directory.resolve() != directory:
            refused["outside_bound_dir"] += 1
            refused_files.append(slug)
            continue
        for path in sorted(directory.glob("*.jsonl")):
            try:
                with open_contained(path, directory) as handle:
                    consume(handle, path.name)
            except Refused as exc:
                refused[exc.args[0]] += 1
                refused_files.append(path.name)
    if refused:
        return SourceResult("error", "containment geweigerd", dict(refused), refused_files)
    if not present:
        return SourceResult("absent", "transcript-map ontbreekt")
    return SourceResult("ok")


def _text_of(message: object) -> Optional[str]:
    """Prompt text, or None when the message carries a tool_result or no usable text."""
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") == "tool_result":
            return None
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


def _automatic(text: str) -> bool:
    return text.startswith(AUTOMATIC_PREFIXES) or (text.startswith("/goal ") and GOAL_MARKER in text)


def classify_line(obj: object) -> Tuple[Optional[str], str]:
    """(None, own words) for an operator prompt, else (exclusion reason, "")."""
    if not isinstance(obj, dict):
        return "malformed", ""
    if obj.get("type") != "user":
        return "not_user", ""
    if obj.get("isMeta") is True:
        return "is_meta", ""
    if obj.get("isCompactSummary") is True:
        return "compact_summary", ""
    message = obj.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return "tool_result", ""
    text = _text_of(message)
    if text is None or not text.strip():
        return "no_text", ""
    own = PASTED.sub("", text).strip()
    if not own:
        return "pasted_content", ""
    for tag, reason in SYSTEM_TAGS:
        if own.startswith(tag):
            return reason, ""
    return ("automatic", "") if _automatic(own) else (None, own)


def _prompt_id(session: str, uuid: str) -> str:
    return hashlib.sha256(f"{session}|{uuid}".encode("utf-8")).hexdigest()[:16]


def read_prompts(state_dir: Path, projects_root: Path) -> SourceResult:
    """Operator prompts of the bound transcripts; prompts are handed out only when the source is `ok`."""
    prompts: List[Prompt] = []
    excluded: Counter = Counter()

    def consume(handle: TextIO, name: str) -> None:
        for number, raw in enumerate(handle, 1):
            try:
                obj = json.loads(raw)
            except ValueError:
                obj = None
            reason, text = classify_line(obj)
            uuid = obj.get("uuid") if isinstance(obj, dict) else None
            if reason is None and not (isinstance(uuid, str) and uuid):
                reason = "no_uuid"  # identity absent: never a prompt
            if reason:
                excluded[reason] += 1
                continue
            session = obj.get("sessionId") or Path(name).stem
            prompts.append(Prompt(_prompt_id(str(session), uuid), text, name, number))

    result = read_sources(state_dir, projects_root, consume)
    if result.state == "ok":
        result.prompts, result.excluded = prompts, dict(excluded)
    return result
