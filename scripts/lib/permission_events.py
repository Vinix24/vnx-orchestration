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
import permission_hard_stops as hard_stops
from project_root import resolve_central_data_dir
from secret_patterns import _SEGMENT, _dq_end, mask, shell_value_end
from vnx_ids import PROJECT_ID_RE

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
          "besluit reden vorm dispatch_id hard_stop").split()
_NO_RAW = object()

_LEAD_CD = re.compile(r"\s*cd\s+")
_LEAD_ENV = re.compile(r"\s*[A-Za-z_]\w*=")
_AND = re.compile(r"\s*&&\s*")
_SPACE = re.compile(r"\s+")
_PATH_RUN = re.compile(r"[\w./~@%+,:=-]*")
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
_PATH_TOKENS = frozenset(("<scratchpad>", "<tmp>", "<vnx-data>"))
_NUMBER = re.compile(r"(?<![\w./<-])\d+(?![\w./>-])")
_HEREDOC = re.compile(r"(?<!<)<<(?!<).*", re.DOTALL)  # a here-string (<<<) is no heredoc

def _open_quote(text: str, pos: int) -> Optional[int]:
    """Index of the quote (``'``, ``"`` or ``$'``) still open at ``pos``, else None."""
    q, i = None, 0
    while i < pos:
        c = text[i]
        if q is None:
            if c == "\\":
                i += 1
            elif c in "\"'" or text.startswith("$'", i):
                q = i
        elif text[q] == '"':
            if c == "\\":
                i += 1
            elif c == '"':
                q = None
        elif c == "\\" and text[q] == "$":
            i += 1
        elif c == "'" and i > q + (text[q] == "$"):
            q = None
        i += 1
    return q

def _quote_end(text: str, q: int) -> int:
    """End of the quote opening at ``q``; an unclosed one runs to the end of the text."""
    return _dq_end(text, q) if text[q] == '"' else _SEGMENT.match(text, q).end()

def _word_tail(text: str, end: int) -> int:
    """Extend ``end`` over the rest of the shell word: escapes, adjacent quotes, path characters."""
    while end < len(text):
        if text[end] == "\\":
            end = min(end + 2, len(text))
        elif text[end] in "\"'" or text.startswith("$'", end):
            end = _quote_end(text, end)
        else:
            run = _PATH_RUN.match(text, end).end()
            if run == end:
                break
            end = run
    return end

def _sub_paths(pattern: "re.Pattern[str]", repl: str, text: str) -> str:
    """Replace each path as one whole shell word: from the opening quote it sits in (if any)
    to the end of the word; a quote that never closes masks to the end of the text."""
    out, pos = [], 0  # out: (source start, source end, text, literal) in source order
    while True:
        m = pattern.search(text, pos)
        if not m:
            return "".join(t for _, _, t, _ in out) + text[pos:]
        start, end = m.start(), m.end()
        q = _open_quote(text, start)
        # A path, or a bare "~" inside a quote, replaces the whole quoted word at once, so the
        # first pass gives the form a second pass would. A bare "~" outside a quote stays as is.
        if "/" in m.group() or q is not None:
            if q is not None:
                start, end = q, _quote_end(text, q)
            end = _word_tail(text, end)
        # A replacement never covers text already emitted: when the rewind to the opening quote
        # lands before pos, cut the output back to start so the quoted word is replaced once.
        while out and out[-1][0] >= start:
            out.pop()
        if out and out[-1][1] > start:  # only a literal piece can straddle: replacements end at a quote or word edge
            o_start, _, _, _ = out.pop()
            out.append((o_start, start, text[o_start:start], True))
        elif start > out[-1][1] if out else start > 0:
            gap = out[-1][1] if out else 0
            out.append((gap, start, text[gap:start], True))
        out.append((start, end, repl, False))
        pos = max(end, start + 1)

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
        text = _sub_paths(pattern, repl, text) if repl in _PATH_TOKENS else pattern.sub(repl, text)
    text = _sub_paths(_HOME_PATH, "~/...", text)
    text = _NUMBER.sub("<n>", text)
    text = _HEREDOC.sub("<<heredoc", text)
    return " ".join(text.split())

def event_id(project_id: str, bron: str, session_id: str, ts: str, vorm: str, hard_stop: str = "") -> str:
    """sha256(project_id|bron|sessie|ts|vorm[|hard_stop])[:16], normalised vorm; "" keeps the pre-ADR-082 id."""
    raw = "|".join((project_id, bron, session_id, ts, _safe_text(vorm)) + ((hard_stop,) if hard_stop else ()))
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
    reason_text: Optional[str] = None, dispatch_id: Optional[str] = None, raw: Any = _NO_RAW,
) -> Dict[str, str]:
    """Turn raw inputs into a safe ledger event (masked, normalised, hashed). ``hard_stop`` is classified on
    ``raw`` (default: ``command``) before any masking (ADR-082); ``raw=None`` is evidence_unavailable."""
    _check_ts(ts)
    hard_stop = hard_stops.classify(command if raw is _NO_RAW else raw)[0]
    vorm = _safe_text(command)
    return {
        "schema": SCHEMA, "ts": ts,
        "event_id": event_id(project_id, bron, session_id, ts, vorm, hard_stop),
        "project_id": project_id, "session_id": session_id, "account": account,
        "origin": origin, "bron": bron, "besluit": besluit,
        "reden": f"{_safe_text(reason_code or '', normalised=False)} "
                 f"{_safe_text(reason_text or '')[:REASON_MAX]}".strip(),  # decision 2: code + 120 chars
        "vorm": vorm,
        "dispatch_id": _safe_text(dispatch_id or "", normalised=False),
        "hard_stop": hard_stop,  # the closed value only: the raw text never enters the event
    }

def _state_dir(project_id: Any, state_dir: Optional[Path]) -> Path:
    """The single gate before any mkdir, open or lock: a fleet project_id, and a central dir under the central root."""
    if not isinstance(project_id, str) or not PROJECT_ID_RE.fullmatch(project_id):
        raise ValueError(f"project_id {project_id!r} is not a fleet project id")
    if state_dir is not None:
        return Path(state_dir)
    root, central = (Path.home() / ".vnx-data").resolve(), resolve_central_data_dir(project_id).resolve()
    if central.parent != root:
        raise ValueError(f"project_id {project_id!r} resolves to {central}, outside the central store {root}")
    return central / "state"

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
    for name, allowed in (("account", ACCOUNTS), ("origin", ORIGINS), ("bron", BRONNEN), ("besluit", BESLUITEN),
                          ("hard_stop", hard_stops.HARD_STOP_VALUES)):
        _check_closed(name, event[name], allowed)
    clean = dict(event)
    clean["vorm"] = _safe_text(event["vorm"])
    clean["reden"] = _safe_text(event["reden"])
    clean["dispatch_id"] = _safe_text(event["dispatch_id"], normalised=False)
    expected = event_id(project_id, clean["bron"], clean["session_id"], clean["ts"], clean["vorm"], clean["hard_stop"])
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
    state = _state_dir(project_id, state_dir)
    clean = [_validate(e, project_id) for e in events]
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

def _read_line(line: bytes, project_id: str) -> tuple:
    """(event, None) or (None, skip reason); a line from before ADR-082 has no ``hard_stop`` and stays so."""
    try:
        event = json.loads(line.decode("utf-8"))
    except ValueError:  # vnx-silent-except: counted by the caller as unparseable, no content kept
        return None, "unparseable"
    present = [f for f in FIELDS if f != "hard_stop" or isinstance(event, dict) and "hard_stop" in event]
    if not isinstance(event, dict) or not all(isinstance(event.get(f), str) for f in present):
        return None, "unparseable"
    if event["project_id"] != project_id:
        return None, "foreign_project"
    if event["event_id"] != event_id(project_id, event["bron"], event["session_id"], event["ts"], event["vorm"],
                                     event.get("hard_stop", "")):
        return None, "event_id_mismatch"
    return event, None

def read_events(project_id: str, state_dir: Optional[Path] = None) -> tuple:
    """(events, skipped) of one project, read only: a damaged line, a foreign ``project_id`` or an ``event_id``
    that does not match its fields is skipped and counted per reason, never returned. No ledger is ([], {})."""
    path = _state_dir(project_id, state_dir) / "permission_events.ndjson"
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return [], {}
    events: List[Dict[str, str]] = []
    skipped: Dict[str, int] = {}
    for line in data.split(b"\n"):
        if line.strip():
            event, why = _read_line(line, project_id)
            if event is None:
                skipped[why] = skipped.get(why, 0) + 1
            else:
                events.append(event)
    return events, skipped

def _window_line(line: bytes, project_id: str, ledger: str) -> tuple:
    """((ts, line), None) or (None, skip reason) for one line of ``read_window``."""
    if ledger == "permission_events":
        obj, why = _read_line(line, project_id)
    else:
        try:
            obj = json.loads(line.decode("utf-8"))
        except ValueError:  # vnx-silent-except: counted by the caller as unparseable, no content kept
            obj = None
        why = None if isinstance(obj, dict) and obj.get("project_id") == project_id else (
            "foreign_project" if isinstance(obj, dict) and isinstance(obj.get("project_id"), str) else "unparseable")
    if why:
        return None, why
    try:
        when = datetime.fromisoformat(str(obj["ts"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):  # vnx-silent-except: counted by the caller as bad_ts, never placed in a window
        return None, "bad_ts"
    return ((when.astimezone(timezone.utc), obj), None) if when.tzinfo is not None else (None, "bad_ts")

def read_window(project_id: str, ledger: str, start: datetime, end: datetime, state_dir: Optional[Path] = None) -> tuple:
    """``(lines, skipped, held_from)`` of one ledger for ``start <= ts < end``, read only, over the live file and the
    archive month files of ``prune`` (ADR-075 decision 6, ADR-086 decision 4) that can hold the window: in time order,
    a line both in the archive and live (a retried prune round) once. A torn tail, an unparseable line, a foreign
    ``project_id`` or a ``ts`` without a timezone is skipped and counted per reason, never raised. ``held_from`` is
    the oldest time the store still holds, ``None`` when it holds no line: a window that starts before it is not held."""
    import permission_events_prune as prune  # late: prune imports this module
    state = _state_dir(project_id, state_dir)
    with _locked(state):  # a prune between the archive reads and the live read would move lines past the reader
        archive, start, end = state / prune.ARCHIVE_DIR, start.astimezone(timezone.utc), end.astimezone(timezone.utc)
        months = sorted(archive.glob(f"{prune.LEDGERS[ledger].split('.')[0]}-[0-9][0-9][0-9][0-9]-[0-9][0-9].ndjson"))
        first = f"{start.year:04d}-{start.month:02d}"
        older = [p for p in months if p.stem[-7:] < first]
        paths = [p for p in months if first <= p.stem[-7:] <= f"{end.year:04d}-{end.month:02d}"] + [state / prune.LEDGERS[ledger]]
        seen: set = set()
        found: List[tuple] = []
        skipped: Dict[str, int] = {}
        oldest: Optional[datetime] = None
        for path in (older[-1:] if older else []) + paths:  # the newest older month only proves the window start is held
            *done, tail = (path.read_bytes() if path.exists() else b"").split(b"\n")
            if tail.strip():
                skipped["torn_tail"] = skipped.get("torn_tail", 0) + 1
            for line in (ln for ln in done if ln.strip()):
                item, why = _window_line(line, project_id, ledger)
                if item is None:
                    skipped[why] = skipped.get(why, 0) + 1
                    continue
                oldest = min(oldest, item[0]) if oldest else item[0]
                if path not in older and start <= item[0] < end and line not in seen:
                    seen.add(line)
                    found.append(item)
    return [obj for _when, obj in sorted(found, key=lambda p: p[0])], skipped, oldest

def write_event(event: Dict[str, Any], project_id: str, state_dir: Optional[Path] = None) -> bool:
    """Append one event; False when its event_id was already in the ledger."""
    return write_events([event], project_id, state_dir) == 1

def write_coverage(
    project_id: str, source: str, *, state: str, files_read: int, window: str,
    count: Optional[int] = None, files_unreadable: int = 0,
    unreadable_reason: Optional[str] = None, state_dir: Optional[Path] = None,
    skipped: Optional[Dict[str, int]] = None,
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
    if skipped:  # lines left out, per fixed reason code (no content)
        entry["skipped"] = {k: int(v) for k, v in sorted(skipped.items()) if re.fullmatch(r"[a-z0-9_]+", k)}
    sdir = _state_dir(project_id, state_dir)
    path = sdir / "permission_coverage.json"
    with _locked(sdir):
        sources, unparseable = _load_coverage(path)
        sources[source] = entry
        _store_coverage(path, project_id, sources, unparseable)
    return entry
