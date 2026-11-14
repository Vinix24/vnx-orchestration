"""Permission-event ledger (ADR-075 decisions 1-4, 8): schema, normalisation,
masking, append-only writer and coverage, under the project's central
``state/`` dir. Nothing raw reaches disk; an unmaskable field is ``<unmaskable>``."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import atomic_io
from project_root import resolve_central_data_dir
from secret_patterns import mask, shell_value_end

SCHEMA = "permission_event/1"
COVERAGE_SCHEMA = "permission_coverage/1"
UNMASKABLE = "<unmaskable>"
REASON_MAX = 120
ACCOUNTS = frozenset("vnx sm unknown".split())
ORIGINS = frozenset("t0 worker operator unknown".split())
BRONNEN = frozenset("B1 B2 B3 B4a B4b".split())
BESLUITEN = frozenset(
    "refused_config refused_user refused_classifier refused_hook "
    "approved_once approved_permanent asked executed".split())
COVERAGE_STATES = frozenset("ok absent unbound error unknown".split())
FIELDS = ("schema ts event_id project_id session_id account origin bron "
          "besluit reden vorm dispatch_id").split()

_LEAD_CD = re.compile(r"\s*cd\s+")
_LEAD_ENV = re.compile(r"\s*[A-Za-z_]\w*=")
_AND = re.compile(r"\s*&&\s*")
_SPACE = re.compile(r"\s+")
_PATH = r"[\w./~@%+,:=-]*"  # path characters only; a value and its quotes are read by shell_value_end
_HOME_PATH = re.compile(r"(?<![\w.<])(?:/(?:Users|home)/[\w.-]+|~)(?:/" + _PATH + ")?")
_RULES = (  # (pattern, replacement): rule 2, ids, then rule 3, paths, then rule 4
    (re.compile(r"\bdlv-[0-9a-f]{6,}(?:-[a-z0-9]+)*"), "<dlv>"),
    (re.compile(r"\b\d{8}-\d{6}-[\w-]+"), "<dispatch>"),
    (re.compile(r"#\d+\b|\b(pr|pulls?)([ /])\d+\b", re.IGNORECASE), "<pr>"),
    (re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b"), "<sha>"),
    (re.compile(r"(?<![\w/.])" + _PATH + r"/scratchpad(?:/" + _PATH + ")?"), "<scratchpad>"),
    (re.compile(r"(?<![\w/.<])(?:/private)?/tmp(?:/" + _PATH + ")?"), "<tmp>"),
    (re.compile(r"(?<![\w.<])(?:/(?:Users|home)/[\w.-]+|~)/\.vnx-data(?:/" + _PATH + ")?"), "<vnx-data>"),
)
_NUMBER = re.compile(r"(?<![\w./<-])\d+(?![\w./>-])")
_HEREDOC = re.compile(r"(?<!<)<<(?!<).*", re.DOTALL)  # a here-string (<<<) is no heredoc


def _strip_lead(text: str) -> str:
    """Drop leading ``cd <path> &&`` and ``NAME=value`` words (values read by ``shell_value_end``)."""
    while True:
        cd = _LEAD_CD.match(text)
        if cd:
            end = shell_value_end(text, cd.end())
            nxt = _AND.match(text, end) if end else None
            if nxt:
                text = text[nxt.end():]
                continue
        env = _LEAD_ENV.match(text)
        if env:
            end = shell_value_end(text, env.end()) or env.end()
            gap = _SPACE.match(text, end)
            if gap:
                text = text[gap.end():]
                continue
        return text


def normalise(command: str) -> str:
    """ADR-075 decision 4: mask, then the five rules in order. Idempotent."""
    text = _strip_lead(mask(command))
    for pattern, repl in _RULES:
        text = pattern.sub(repl, text)
    text = _HOME_PATH.sub("~/...", text)
    text = _NUMBER.sub("<n>", text)
    text = _HEREDOC.sub("<<heredoc", text)
    return " ".join(text.split())

def event_id(project_id: str, bron: str, session_id: str, ts: str, vorm: str) -> str:
    """sha256(project_id|bron|sessie|ts|vorm)[:16] over the normalised vorm."""
    raw = "|".join((project_id, bron, session_id, ts, _safe_text(vorm)))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]

def _safe_text(value: Any, *, normalised: bool = True) -> str:
    """Mask (and normalise) a text field; fail-closed to ``<unmaskable>``."""
    try:
        if not isinstance(value, str):
            raise TypeError("text field is not a str")
        return normalise(value) if normalised else mask(value)
    except Exception:  # vnx-silent-except: fail-closed, the raw value never reaches disk
        return UNMASKABLE

def _check_closed(name: str, value: Any, allowed: frozenset) -> None:
    if value not in allowed:
        raise ValueError(f"{name} {value!r} is not one of {sorted(allowed)}")

def _check_ts(ts: Any) -> None:
    try:
        datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (AttributeError, ValueError, TypeError) as exc:
        raise ValueError(f"ts {ts!r} is not an ISO timestamp") from exc

def build_event(
    *, project_id: str, session_id: str, ts: str, account: str, origin: str, bron: str,
    besluit: str, command: str, reason_code: Optional[str] = None,
    reason_text: Optional[str] = None, dispatch_id: Optional[str] = None,
) -> Dict[str, str]:
    """Turn raw inputs into a safe ledger event (masked, normalised, hashed)."""
    _check_ts(ts)
    vorm = _safe_text(command)
    return {
        "schema": SCHEMA, "ts": ts,
        "event_id": event_id(project_id, bron, session_id, ts, vorm),
        "project_id": project_id, "session_id": session_id, "account": account,
        "origin": origin, "bron": bron, "besluit": besluit,
        "reden": f"{_safe_text(reason_code or '', normalised=False)} "
                 f"{_safe_text(reason_text or '')[:REASON_MAX]}".strip(),  # decision 2: code + 120 chars
        "vorm": vorm,
        "dispatch_id": _safe_text(dispatch_id or "", normalised=False),
    }

def _state_dir(project_id: str, state_dir: Optional[Path]) -> Path:
    return Path(state_dir) if state_dir is not None else resolve_central_data_dir(project_id) / "state"

@contextmanager
def _locked(state: Path) -> Iterator[None]:
    state.mkdir(parents=True, exist_ok=True)
    fd = os.open(state / "permission_events.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)

def _validate(event: Dict[str, Any], project_id: str) -> Dict[str, str]:
    if not project_id or not event.get("project_id") or event["project_id"] != project_id:
        raise ValueError("event project_id is missing or is not the store's project_id")
    if set(event) != set(FIELDS) or event["schema"] != SCHEMA or not isinstance(event["session_id"], str):
        raise ValueError(f"event must carry exactly the {SCHEMA} fields, session_id a str")
    _check_ts(event["ts"])
    for name, allowed in (("account", ACCOUNTS), ("origin", ORIGINS), ("bron", BRONNEN), ("besluit", BESLUITEN)):
        _check_closed(name, event[name], allowed)
    clean = dict(event)
    clean["vorm"] = _safe_text(event["vorm"])
    clean["reden"] = _safe_text(event["reden"])
    clean["dispatch_id"] = _safe_text(event["dispatch_id"], normalised=False)
    expected = event_id(project_id, clean["bron"], clean["session_id"], clean["ts"], clean["vorm"])
    if clean["event_id"] != expected:
        raise ValueError("event_id does not match the normalised event")
    return clean

def _scan(path: Path) -> tuple:
    """(event_ids, unparseable line count); a damaged line is kept, never returned."""
    ids: set = set()
    bad = 0
    for line in (path.read_bytes() if path.exists() else b"").split(b"\n"):
        if not line.strip():
            continue
        try:
            eid = json.loads(line.decode("utf-8")).get("event_id")
        except (ValueError, AttributeError):  # vnx-silent-except: counted below as unparseable
            eid = None
        if isinstance(eid, str) and eid:
            ids.add(eid)
        else:
            bad += 1
    return ids, bad


def _append_all(path: Path, payload: bytes) -> None:
    """Append ``payload`` completely (caller holds the lock): a torn tail gets a newline first, a short
    write loops on, a write of 0 or an error raises and restores the old length."""
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        old = os.fstat(fd).st_size
        try:
            if old and os.pread(fd, 1, old - 1) != b"\n":
                payload = b"\n" + payload
            view = memoryview(payload)
            while view:
                n = os.write(fd, view)
                if n <= 0:
                    raise OSError("permission ledger write returned 0 bytes")
                view = view[n:]
            os.fsync(fd)
        except BaseException:
            os.ftruncate(fd, old)
            raise
    finally:
        os.close(fd)


def _load_coverage(path: Path) -> tuple:
    """Existing projection; a missing file starts empty, any other failure is logged and raised."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc["schema"] != COVERAGE_SCHEMA or not isinstance(doc["sources"], dict):
            raise ValueError("unexpected schema or sources")
        return dict(doc["sources"]), int(doc.get("unparseable_lines", 0))
    except FileNotFoundError:
        return {}, 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"permission_events: cannot use {path.name} ({type(exc).__name__}), not overwriting it", file=sys.stderr)
        raise


def _store_coverage(path: Path, project_id: str, sources: Dict[str, Any], unparseable: int) -> None:
    doc = {"schema": COVERAGE_SCHEMA, "project_id": project_id, "sources": sources, "unparseable_lines": unparseable}
    atomic_io.atomic_write_text(path, json.dumps(doc, sort_keys=True))  # read-modify-write under _locked


def write_events(
    events: Iterable[Dict[str, Any]], project_id: str, state_dir: Optional[Path] = None,
) -> int:
    """Append new events under the lock; returns lines written. A bad event writes nothing, a known
    event_id is skipped, unparseable ledger lines are counted in the coverage record (no content)."""
    clean = [_validate(e, project_id) for e in events]
    state = _state_dir(project_id, state_dir)
    path = state / "permission_events.ndjson"
    cov_path = state / "permission_coverage.json"
    with _locked(state):
        seen, bad = _scan(path)
        sources, recorded = _load_coverage(cov_path)
        if bad != recorded:
            if bad:
                print(f"permission_events: {bad} unparseable line(s) in the ledger", file=sys.stderr)
            _store_coverage(cov_path, project_id, sources, bad)
        lines: List[str] = []
        for event in clean:
            if event["event_id"] not in seen:
                seen.add(event["event_id"])
                lines.append(json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n")
        if lines:
            _append_all(path, "".join(lines).encode("utf-8"))
    return len(lines)

def write_event(event: Dict[str, Any], project_id: str, state_dir: Optional[Path] = None) -> bool:
    """Append one event; False when its event_id was already in the ledger."""
    return write_events([event], project_id, state_dir) == 1

def write_coverage(
    project_id: str, source: str, *, state: str, files_read: int, window: str,
    count: Optional[int] = None, files_unreadable: int = 0,
    unreadable_reason: Optional[str] = None, state_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Replace the coverage line of one source (ADR-075 decision 8), atomically."""
    _check_closed("source", source, BRONNEN)
    _check_closed("state", state, COVERAGE_STATES)
    if not window or not isinstance(window, str) or min(files_read, files_unreadable) < 0:
        raise ValueError("window is required and file counts cannot be negative")
    if count == 0 and (state != "ok" or files_read < 1):
        raise ValueError("a count of 0 is only valid with state ok and at least one file read")
    if files_unreadable and (state == "ok" or not unreadable_reason):
        raise ValueError("unreadable files need a reason and cannot be state ok")
    entry = {
        "state": state, "files_read": files_read, "count": count, "window": window, "files_unreadable": files_unreadable,
        "unreadable_reason": _safe_text(unreadable_reason or "", normalised=False),
        "updated": datetime.now(timezone.utc).isoformat(),
    }
    sdir = _state_dir(project_id, state_dir)
    path = sdir / "permission_coverage.json"
    with _locked(sdir):
        sources, unparseable = _load_coverage(path)
        sources[source] = entry
        _store_coverage(path, project_id, sources, unparseable)
    return entry
