"""ADR-078: the observed memory ledger and its operator-owned binding.

``bind`` is the only writer of ``state/memory_sources.json``. ``scan`` reads the
bound folders read-only and appends ``memory_ledger/1`` lines to
``state/memory_ledger.ndjson``. A line holds hashes, closed labels and dates,
never text from a note (ADR-072). No slug is ever derived from a path.
"""
from __future__ import annotations

import base64
import errno
import fcntl
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import atomic_io
from project_root import resolve_central_data_dir

SOURCES_FILE, SOURCES_SCHEMA = "memory_sources.json", "memory_sources/1"
REGISTRY_FILE, REGISTRY_SCHEMA = "memory_bindings.json", "memory_bindings/1"
PERSISTED = ("appended", "duplicate")  # the AppendResult statuses that mean the receipt is on disk
INDEX_NOTE = "MEMORY.md"
LEDGER_FILE, LEDGER_SCHEMA = "memory_ledger.ndjson", "memory_ledger/1"
UNKNOWN = "unknown"
_BASE = ("schema", "observed_at", "project_id", "event")
_NOTE = ("source", "note", "sha256", "prev_sha256", "frontmatter", "type", "node_type",
         "origin_session", "valid_from", "invalid_at", "superseded_by")
FIELDS = {
    "added": _BASE + _NOTE, "changed": _BASE + _NOTE, "validity_changed": _BASE + _NOTE,
    "removed": _BASE + ("source", "note", "prev_sha256"),
    "unreadable": _BASE + ("source", "note", "reason"),
    "partial": _BASE + ("source", "reason", "processed", "total"),
}
_STATE_EVENTS = ("added", "changed", "validity_changed", "removed")
_LABEL = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_NAME = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_DATEISH = re.compile(r"^[0-9T:+\-Z. ]{1,40}$")


class BindRefused(Exception):
    """``vnx memory bind`` refuses; the message is the reason."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def projects_root() -> Path:
    return (Path.home() / ".claude" / "projects").resolve()


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def source_id(directory: str) -> str:
    return hashlib.sha256(str(Path(directory).resolve()).encode("utf-8")).hexdigest()[:16]


# --- binding ---------------------------------------------------------------

def load_sources(state_dir: Path) -> Tuple[Optional[dict], str]:
    """``(binding, "ok")`` or ``(None, "unbound: <reason>")``. Fail-closed."""
    path = Path(state_dir) / SOURCES_FILE
    if not path.is_file():
        return None, "unbound: binding file missing"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "unbound: binding file unreadable or corrupt"
    dirs = data.get("memory_dirs") if isinstance(data, dict) else None
    if (data.get("schema") != SOURCES_SCHEMA if isinstance(data, dict) else True) \
            or not isinstance(data.get("project_id"), str) or not data["project_id"].strip() \
            or not isinstance(dirs, list) or not dirs \
            or not all(isinstance(d, str) and d for d in dirs):
        return None, "unbound: binding invalid"
    return data, "ok"


def _encodings(root: Path) -> Tuple[str, str]:
    text = str(root)
    return text.replace("/", "-"), re.sub(r"[/._]", "-", text)


def _boundary_refusal(resolved: Path, project_id: str, boundary: Any) -> Optional[str]:
    if boundary is None or not getattr(boundary, "configured", False):
        return None
    if project_id in boundary.client_project_ids:
        return None
    try:
        relative = resolved.relative_to(projects_root())
    except ValueError:
        return "folder is outside ~/.claude/projects/"
    if not relative.parts:
        return "folder is the projects root"
    project_dir = relative.parts[0]  # the encoded project the folder belongs to, at any depth
    for root in tuple(boundary.client_roots) + tuple(boundary.personal_roots):
        for enc in _encodings(root):
            if project_dir == enc or project_dir.startswith(enc + "-"):
                return "folder lies under a client or personal root"
    return None


def registry_path() -> Path:
    return resolve_central_data_dir(REGISTRY_FILE)  # ~/.vnx-data/memory_bindings.json


def _load_registry() -> dict:
    """The folder -> project_id map every bind records, read fail-closed."""
    path = registry_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise BindRefused("binding registry unreadable or corrupt")
    bindings = data.get("bindings") if isinstance(data, dict) else None
    if not isinstance(bindings, dict) or data.get("schema") != REGISTRY_SCHEMA:
        raise BindRefused("binding registry invalid")
    return bindings


def _other_project_binds(state_dir: Path, resolved: Path, project_id: str, registry: dict) -> bool:
    if registry.get(str(resolved), project_id) != project_id:
        return True
    stores = set(registry_path().parent.glob("*/state/" + SOURCES_FILE))  # every central store
    stores.add(Path(state_dir) / SOURCES_FILE)  # the caller's own store, repo-local or central
    for other in sorted(stores):
        data, _ = load_sources(other.parent)
        if data and data["project_id"] != project_id and str(resolved) in data["memory_dirs"]:
            return True
    return False


def bind(state_dir: Path, directory: str, project_id: str, *, boundary: Any = None,
         emit: Optional[Callable[..., Any]] = None) -> dict:
    """Add one folder to the binding (ADR-078 §4). Raises BindRefused.

    The state_mutation receipt is persisted first (ADR-005), then the registry
    entry, then the binding, each atomically, under one registry lock. A crash
    between the two writes leaves a registry claim without a binding: other
    projects see the claim and refuse, ``load_sources`` reports ``unbound``
    (OI-2877), and a repeated ``bind`` of the same project completes it.
    """
    if emit is None:
        raise BindRefused("no state_mutation emitter; a binding needs a receipt (ADR-005)")
    try:
        resolved = Path(directory).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        raise BindRefused("path cannot be resolved")
    if not resolved.is_dir() or resolved == projects_root() or not _under(resolved, projects_root()):
        raise BindRefused("not an existing folder under ~/.claude/projects/")
    if not any(p.is_file() and p.name != INDEX_NOTE for p in resolved.glob("*.md")):
        raise BindRefused("folder holds no note besides MEMORY.md")
    reason = _boundary_refusal(resolved, project_id, boundary)
    if reason:
        raise BindRefused(reason)
    target, registry_file = Path(state_dir) / SOURCES_FILE, registry_path()
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    registry_file.parent.mkdir(parents=True, exist_ok=True)
    with atomic_io.slot_lock(registry_file):
        registry = _load_registry()
        if _other_project_binds(state_dir, resolved, project_id, registry):
            raise BindRefused("folder is already bound to another project")
        with atomic_io.slot_lock(target):
            old, _ = load_sources(state_dir)
            old_dirs = list(old["memory_dirs"]) if old and old["project_id"] == project_id else []
            new_dirs = old_dirs + ([str(resolved)] if str(resolved) not in old_dirs else [])
            doc = {"schema": SOURCES_SCHEMA, "project_id": project_id, "memory_dirs": new_dirs,
                   "bound_at": _now()}
            try:
                result = emit(str(target), trigger="memory_bind old=%s new=%s"
                              % (json.dumps(old_dirs), json.dumps(new_dirs)))
            except Exception as exc:  # any emitter failure is a missing receipt: refuse, write nothing
                raise BindRefused("state_mutation receipt not persisted; binding unchanged") from exc
            if getattr(result, "status", None) not in PERSISTED:
                raise BindRefused("state_mutation receipt not persisted; binding unchanged")
            registry[str(resolved)] = project_id
            atomic_io.atomic_write_json(registry_file, {"schema": REGISTRY_SCHEMA, "bindings": registry})
            atomic_io.atomic_write_json(target, doc)
    return doc


# --- ledger lines ----------------------------------------------------------

def validate_record(rec: Dict[str, Any]) -> None:
    """Raise ValueError unless ``rec`` is exactly one ``memory_ledger/1`` event."""
    allowed = FIELDS.get(rec.get("event"))
    if allowed is None or rec.get("schema") != LEDGER_SCHEMA:
        raise ValueError("unknown event or schema")
    if set(rec) != set(allowed):
        raise ValueError("fields differ from the event's field set")
    for key, value in rec.items():
        if value == "" or (value == 0 and key != "processed" and not isinstance(value, bool)):
            raise ValueError("empty or zero field: " + key)


def _split_tail(raw: bytes) -> Tuple[bytes, bytes]:
    cut = raw.rfind(b"\n") + 1  # the "\n" is the commit mark (ADR-077 §4)
    return raw[:cut], raw[cut:]


def read_ledger(path: Path) -> Tuple[List[dict], int, List[int]]:
    """Committed records, count of ``afgescheurd`` tail, line numbers of ``ongeldig``."""
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return [], 0, []
    body, tail = _split_tail(raw)
    records, bad = [], []
    for number, line in enumerate(body.split(b"\n")[:-1], 1):
        try:
            rec = json.loads(line)
            validate_record(rec)
            records.append(rec)
        except (ValueError, TypeError, AttributeError):
            bad.append(number)
    return records, 1 if tail else 0, bad


def _append_fsync(path: Path, data: bytes) -> None:
    """Append every byte of ``data`` and fsync, or restore the size before the call and raise."""
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        size_before = os.fstat(fd).st_size
        try:
            view = memoryview(data)
            while view:  # a short write is legal: loop on the count
                written = os.write(fd, view)  # vnx-atomic-write: O_APPEND, all bytes or roll back, then fsync
                if written <= 0:
                    raise OSError("zero-byte write to " + str(path))
                view = view[written:]
            os.fsync(fd)
        except BaseException:
            os.ftruncate(fd, size_before)  # no half line survives (ADR-077 §4)
            os.fsync(fd)
            raise
    finally:
        os.close(fd)


def _set_apart_tail(state_dir: Path, ledger: Path, raw: bytes) -> None:
    body, tail = _split_tail(raw)
    if not tail:
        return
    entry = {"offset": len(body), "sha256": hashlib.sha256(tail).hexdigest(),
             "bytes_b64": base64.b64encode(tail).decode("ascii")}
    _append_fsync(Path(state_dir) / (LEDGER_FILE + ".afgescheurd"), (json.dumps(entry) + "\n").encode())
    os.truncate(ledger, len(body))  # only after the archive line is complete and fsynced
    _append_fsync(ledger, b"")  # fsync the cut ledger before the first new line


class _Unreadable(Exception):
    pass


def _read_once(path: Path) -> Tuple[bytes, os.stat_result, os.stat_result]:
    before = os.stat(path)
    with open(path, "rb") as handle:
        data = handle.read()
    return data, before, os.stat(path)


def _read_stable(path: Path) -> bytes:
    for _ in range(2):  # one read, one retry (ADR-078 §7)
        data, a, b = _read_once(path)
        if (a.st_size, a.st_mtime_ns) == (b.st_size, b.st_mtime_ns):
            return data
    raise _Unreadable("changed_during_read")


def _label(value: Any, pattern: "re.Pattern[str]") -> str:
    return value if isinstance(value, str) and pattern.match(value) else UNKNOWN


def _validity(value: Any) -> Any:
    if value is None:
        return None  # field not set: a known "no bound", not an unknown source
    text = value.isoformat() if hasattr(value, "isoformat") else str(value)
    return text if _DATEISH.match(text) else UNKNOWN


def _pick(meta: dict, name: str) -> Any:
    inner = meta.get("metadata")
    if isinstance(inner, dict) and name in inner:
        return inner[name]
    return meta.get(name)


def _frontmatter(data: bytes) -> Tuple[str, Optional[dict]]:
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return "unparseable", None
    if not lines or lines[0].strip() != "---":
        return "absent", None
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return "unparseable", None
    import yaml
    try:
        meta = yaml.safe_load("\n".join(lines[1:end]))
    except (yaml.YAMLError, ValueError, TypeError):  # e.g. valid_from: 2026-13-01 fails date conversion
        return "unparseable", None
    return ("ok", meta) if isinstance(meta, dict) else ("unparseable", None)


def parse_note(data: bytes, note: str) -> Dict[str, Any]:
    """Fields of a ledger line that come from the bytes (ADR-078 §5). No text kept."""
    status, meta = _frontmatter(data)
    out = {"sha256": hashlib.sha256(data).hexdigest(), "frontmatter": status,
           "type": "index" if note == INDEX_NOTE else UNKNOWN, "node_type": UNKNOWN,
           "origin_session": UNKNOWN, "valid_from": UNKNOWN, "invalid_at": UNKNOWN,
           "superseded_by": UNKNOWN}
    if meta is None:
        return out
    if note != INDEX_NOTE:
        out["type"] = _label(_pick(meta, "type"), _LABEL)
    out["node_type"] = _label(_pick(meta, "node_type"), _LABEL)
    out["origin_session"] = _label(_pick(meta, "originSessionId"), _NAME)
    out["valid_from"] = _validity(_pick(meta, "valid_from"))
    out["invalid_at"] = _validity(_pick(meta, "invalid_at"))
    sup = _pick(meta, "superseded_by")
    out["superseded_by"] = None if sup is None else _label(sup, _NAME)
    return out


def _os_reason(exc: OSError) -> str:
    return "os_error:" + errno.errorcode.get(exc.errno or 0, "UNKNOWN")


def _line(event: str, project_id: str, now: str, **fields: Any) -> dict:
    rec = {"schema": LEDGER_SCHEMA, "observed_at": now, "project_id": project_id, "event": event}
    rec.update(fields)
    return rec


def _observe(base: Path, name: str, src: str, last: Optional[dict], line: Callable) -> Optional[dict]:
    """The one line a note gets this scan, or None when it is unchanged."""
    path = base / name
    try:
        if not _under(path.resolve(), base.resolve()):
            raise _Unreadable("outside_dir")
        fields = parse_note(_read_stable(path), name)
    except _Unreadable as exc:
        return line("unreadable", source=src, note=name, reason=str(exc))
    except OSError as exc:
        return line("unreadable", source=src, note=name, reason=_os_reason(exc))
    if last is None or last["event"] == "removed":
        return line("added", source=src, note=name, prev_sha256="none", **fields)
    if last["sha256"] == fields["sha256"]:
        return None
    same = all(last[k] == fields[k] for k in ("valid_from", "invalid_at", "superseded_by"))
    return line("changed" if same else "validity_changed", source=src, note=name,
                prev_sha256=last["sha256"], **fields)


def _scan_dir(directory: str, project_id: str, state: dict, out: List[dict],
              expired: Callable[[], bool]) -> None:
    base, src, now = Path(directory), source_id(directory), _now()

    def line(event: str, **fields: Any) -> dict:
        return _line(event, project_id, now, **fields)
    try:
        names = sorted((n for n in os.listdir(base) if n.endswith(".md")), key=os.fsencode)
    except OSError as exc:  # folder unreadable: say so, and never infer a "removed" from it
        out.append(line("unreadable", source=src, note=UNKNOWN, reason="dir_" + _os_reason(exc)))
        return
    for processed, name in enumerate(names):
        if expired():
            out.append(line("partial", source=src, reason="budget", processed=processed,
                            total=len(names)))
            return
        rec = _observe(base, name, src, state.get((src, name)), line)
        if rec is not None:
            out.append(rec)
    seen = set(names)
    for (s, name), last in sorted(state.items()):
        if s == src and last["event"] != "removed" and name not in seen:
            out.append(line("removed", source=src, note=name, prev_sha256=last["sha256"]))


def scan(state_dir: Path, *, budget: Optional[float] = None,
         clock: Callable[[], float] = time.monotonic) -> Dict[str, Any]:
    """One scan (ADR-078 §7): ``{"status": "ok" | "partial" | "unbound", "reason": str,
    "written": int, "unreadable": int}``. ``unbound`` writes nothing; no change writes 0 lines."""
    binding, why = load_sources(state_dir)
    if binding is None:
        return {"status": "unbound", "reason": why, "written": 0, "unreadable": 0}
    started = clock()
    expired = (lambda: False) if budget is None else (lambda: clock() - started >= budget)
    ledger = Path(state_dir) / LEDGER_FILE
    lock = os.open(str(ledger) + ".lock", os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if ledger.exists():
            _set_apart_tail(Path(state_dir), ledger, ledger.read_bytes())
        state = {(r["source"], r["note"]): r for r in read_ledger(ledger)[0]
                 if r["event"] in _STATE_EVENTS}  # the last state of each note: later lines win
        lines: List[dict] = []
        for directory in binding["memory_dirs"]:
            _scan_dir(directory, binding["project_id"], state, lines, expired)
        for rec in lines:
            validate_record(rec)  # the writer refuses an invalid line before writing any
        for rec in lines:
            _append_fsync(ledger, (json.dumps(rec, sort_keys=True) + "\n").encode("utf-8"))
    finally:
        os.close(lock)
    partial = any(r["event"] == "partial" for r in lines)
    return {"status": "partial" if partial else "ok", "reason": "ok" if not partial else "budget",
            "written": len(lines), "unreadable": sum(r["event"] == "unreadable" for r in lines)}
