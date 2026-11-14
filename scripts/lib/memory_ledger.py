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

SOURCES_FILE, SOURCES_SCHEMA = "memory_sources.json", "memory_sources/1"
LEDGER_FILE, LEDGER_SCHEMA = "memory_ledger.ndjson", "memory_ledger/1"
INDEX_NOTE = "MEMORY.md"
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
    for root in tuple(boundary.client_roots) + tuple(boundary.personal_roots):
        if any(resolved.name.startswith(enc) for enc in _encodings(root)):
            return "folder lies under a client or personal root"
    return None


def _other_project_binds(state_dir: Path, resolved: Path, project_id: str) -> bool:
    for other in sorted(Path(state_dir).parent.parent.glob("*/state/" + SOURCES_FILE)):
        data, _ = load_sources(other.parent)
        if data and data["project_id"] != project_id and str(resolved) in data["memory_dirs"]:
            return True
    return False


def bind(state_dir: Path, directory: str, project_id: str, *, boundary: Any = None,
         emit: Optional[Callable[..., Any]] = None) -> dict:
    """Add one folder to the binding (ADR-078 §4). Raises BindRefused."""
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
    if _other_project_binds(state_dir, resolved, project_id):
        raise BindRefused("folder is already bound to another project")
    old, _ = load_sources(state_dir)
    old_dirs = list(old["memory_dirs"]) if old and old["project_id"] == project_id else []
    new_dirs = old_dirs + ([str(resolved)] if str(resolved) not in old_dirs else [])
    doc = {"schema": SOURCES_SCHEMA, "project_id": project_id, "memory_dirs": new_dirs,
           "bound_at": _now()}
    target = Path(state_dir) / SOURCES_FILE
    tmp = target.with_name(target.name + ".tmp")
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)  # vnx-atomic-write: tmp + os.replace
    if emit is not None:
        emit(str(target), trigger="memory_bind old=%s new=%s" % (json.dumps(old_dirs), json.dumps(new_dirs)))
    return doc


# --- ledger lines ----------------------------------------------------------

def validate_record(rec: Dict[str, Any]) -> None:
    allowed = FIELDS.get(rec.get("event"))
    if allowed is None or rec.get("schema") != LEDGER_SCHEMA:
        raise ValueError("unknown event or schema")
    if set(rec) != set(allowed):
        raise ValueError("fields differ from the event's field set")
    for key, value in rec.items():
        if value == "" or (value == 0 and key not in ("processed",) and not isinstance(value, bool)):
            raise ValueError("empty or zero field: " + key)


def _split_tail(raw: bytes) -> Tuple[bytes, bytes]:
    cut = raw.rfind(b"\n") + 1
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


def _last_state(records: List[dict]) -> Dict[Tuple[str, str], dict]:
    state: Dict[Tuple[str, str], dict] = {}
    for rec in records:
        if rec["event"] in _STATE_EVENTS:
            state[(rec["source"], rec["note"])] = rec
    return state


def _set_apart_tail(state_dir: Path, ledger: Path, raw: bytes) -> bytes:
    body, tail = _split_tail(raw)
    if not tail:
        return raw
    entry = {"offset": len(body), "sha256": hashlib.sha256(tail).hexdigest(),
             "bytes_b64": base64.b64encode(tail).decode("ascii")}
    side = Path(state_dir) / (LEDGER_FILE + ".afgescheurd")
    fd = os.open(side, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, (json.dumps(entry) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.truncate(ledger, len(body))
    fd = os.open(ledger, os.O_WRONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return body


# --- reading a note --------------------------------------------------------

def _read_once(path: Path) -> Tuple[bytes, os.stat_result, os.stat_result]:
    before = os.stat(path)
    with open(path, "rb") as handle:
        data = handle.read()
    return data, before, os.stat(path)


def _read_stable(path: Path) -> bytes:
    for _ in range(2):
        data, a, b = _read_once(path)
        if (a.st_size, a.st_mtime_ns) == (b.st_size, b.st_mtime_ns):
            return data
    raise _Unreadable("changed_during_read")


class _Unreadable(Exception):
    pass


def _label(value: Any, pattern: "re.Pattern[str]") -> str:
    return value if isinstance(value, str) and pattern.match(value) else UNKNOWN


def _validity(value: Any) -> Any:
    if value is None:
        return None
    text = value.isoformat() if hasattr(value, "isoformat") else str(value)
    return text if _DATEISH.match(text) else UNKNOWN


def _pick(meta: dict, name: str) -> Any:
    inner = meta.get("metadata")
    if isinstance(inner, dict) and name in inner:
        return inner[name]
    return meta.get(name)


def parse_note(data: bytes, note: str) -> Dict[str, Any]:
    """Fields of a ledger line that come from the bytes (ADR-078 §5). No text kept."""
    out = {"sha256": hashlib.sha256(data).hexdigest(), "frontmatter": "absent", "type": UNKNOWN,
           "node_type": UNKNOWN, "origin_session": UNKNOWN, "valid_from": UNKNOWN,
           "invalid_at": UNKNOWN, "superseded_by": UNKNOWN}
    if note == INDEX_NOTE:
        out["type"] = "index"
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        lines = None
    if lines is None or not lines or lines[0].strip() != "---":
        if lines is not None:
            return out
        out["frontmatter"] = "unparseable"
        return out
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    meta: Any = None
    if end is not None:
        import yaml
        try:
            meta = yaml.safe_load("\n".join(lines[1:end]))
        except yaml.YAMLError:
            meta = None
    if not isinstance(meta, dict):
        out["frontmatter"] = "unparseable"
        return out
    out["frontmatter"] = "ok"
    if note != INDEX_NOTE:
        out["type"] = _label(_pick(meta, "type"), _LABEL)
    out["node_type"] = _label(_pick(meta, "node_type"), _LABEL)
    out["origin_session"] = _label(_pick(meta, "originSessionId"), _NAME)
    out["valid_from"] = _validity(_pick(meta, "valid_from"))
    out["invalid_at"] = _validity(_pick(meta, "invalid_at"))
    sup = _pick(meta, "superseded_by")
    out["superseded_by"] = None if sup is None else _label(sup, _NAME)
    return out


def _list_notes(directory: Path) -> List[str]:
    return sorted((n for n in os.listdir(directory) if n.endswith(".md")), key=os.fsencode)


# --- scan ------------------------------------------------------------------

def _scan_dir(directory: str, project_id: str, state: dict, out: List[dict], now: str,
              expired: Callable[[], bool]) -> str:
    base, src = Path(directory), source_id(directory)
    names = _list_notes(base)
    seen = set(names)
    base_real = base.resolve()
    processed = 0
    for name in names:
        if expired():
            out.append(_line("partial", project_id, now, source=src, reason="budget",
                             processed=processed, total=len(names)))
            break
        processed += 1
        path = base / name
        try:
            if not _under(path.resolve(), base_real):
                raise _Unreadable("outside_dir")
            fields = parse_note(_read_stable(path), name)
        except _Unreadable as exc:
            out.append(_line("unreadable", project_id, now, source=src, note=name, reason=str(exc)))
            continue
        except OSError as exc:
            out.append(_line("unreadable", project_id, now, source=src, note=name,
                             reason="os_error:" + errno.errorcode.get(exc.errno or 0, "UNKNOWN")))
            continue
        last = state.get((src, name))
        if last is None or last["event"] == "removed":
            event, prev = "added", "none"
        elif last["sha256"] == fields["sha256"]:
            continue
        else:
            prev = last["sha256"]
            same = all(last[k] == fields[k] for k in ("valid_from", "invalid_at", "superseded_by"))
            event = "changed" if same else "validity_changed"
        out.append(_line(event, project_id, now, source=src, note=name, prev_sha256=prev, **{
            k: v for k, v in fields.items()}))
    for (s, name), last in sorted(state.items()):
        if s == src and last["event"] != "removed" and name not in seen:
            out.append(_line("removed", project_id, now, source=src, note=name,
                             prev_sha256=last["sha256"]))
    return src


def _line(event: str, project_id: str, now: str, **fields: Any) -> dict:
    rec = {"schema": LEDGER_SCHEMA, "observed_at": now, "project_id": project_id, "event": event}
    rec.update(fields)
    return rec


def scan(state_dir: Path, *, budget: Optional[float] = None,
         clock: Callable[[], float] = time.monotonic) -> Dict[str, Any]:
    """One scan. ``{"status": "unbound", ...}`` writes nothing at all."""
    binding, why = load_sources(state_dir)
    if binding is None:
        return {"status": "unbound", "reason": why, "written": 0}
    started = clock()
    expired = (lambda: False) if budget is None else (lambda: clock() - started >= budget)
    ledger = Path(state_dir) / LEDGER_FILE
    lock = os.open(str(ledger) + ".lock", os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        raw = ledger.read_bytes() if ledger.exists() else b""
        raw = _set_apart_tail(Path(state_dir), ledger, raw) if raw else raw
        records, _, _ = read_ledger(ledger)
        state, lines = _last_state(records), []
        for directory in binding["memory_dirs"]:
            _scan_dir(directory, binding["project_id"], state, lines, _now(), expired)
        for rec in lines:
            validate_record(rec)
        _append(ledger, lines)
    finally:
        os.close(lock)
    partial = any(r["event"] == "partial" for r in lines)
    return {"status": "partial" if partial else "ok", "written": len(lines)}


def _append(ledger: Path, lines: List[dict]) -> None:
    if not lines:
        return
    fd = os.open(ledger, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        for rec in lines:
            os.write(fd, (json.dumps(rec, sort_keys=True) + "\n").encode("utf-8"))
            os.fsync(fd)
    finally:
        os.close(fd)
