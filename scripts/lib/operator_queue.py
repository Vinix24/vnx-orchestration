"""The operator queue ledger (ADR-076 decisions 1, 2, 3, 5 and 9).

``state/operator_queue.ndjson`` is the truth: append-only, one line per event, written under an exclusive
``fcntl.flock`` and fsynced before ``state/operator_queue.json`` is rebuilt. The derived state is never a
source. A point closes only through a ``closed`` line that carries ``bewijs`` (the state of its source);
there is no function that closes a point without one. A ``vraag``, ``kopieerblok`` or ``citaat`` that holds
a secret is refused with the pattern name, never masked and never stored. An unreadable ledger line makes
the whole state ``unknown`` with its line number, never a skipped line.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional

try:
    from project_root import resolve_central_data_dir
    import secret_patterns
except ImportError:  # vnx-silent-except: imported as scripts.lib.operator_queue
    from scripts.lib.project_root import resolve_central_data_dir
    from scripts.lib import secret_patterns

LEDGER_FILENAME = "operator_queue.ndjson"
STATE_FILENAME = "operator_queue.json"
LOCK_FILENAME = ".operator_queue.lock"
SUMMARY_FILENAME = "operator_inbox_summary.json"
SUMMARY_POINTS = 5
SCHEMA = "operator_queue/1"
SUMMARY_SCHEMA = "operator_inbox_summary/1"
KINDS: Dict[str, tuple] = {
    "opened": ("point_id", "bron", "ref", "revisie", "vraag", "sluit_bij", "asked_at", "opened_by"),
    "presented": ("point_id", "kanaal", "sessie"),
    "closed": ("point_id", "uitkomst", "bewijs", "beantwoord_door"),
    "unknown": ("point_id", "reden"),
    "answer": ("point_id", "citaat", "vastgelegd_door"),
    "push_reserved": ("point_ids", "sessie"),
}
OUTCOMES, CHANNELS = ("beantwoord", "vervallen"), ("kickoff", "rotatie", "status", "push", "cli", "mc")
# Patterns 1 to 9 of ADR-076 decision 9; ``long_value`` is a heuristic of the masker, not a refusal name.
REFUSED_PATTERNS = frozenset({"sk_key", "github_token", "aws_key_id", "slack_token", "bearer",
                              "secret_assignment", "authorization_header", "pem_block", "jwt"})
_LIMITS = {"vraag": 200, "kopieerblok": 10**6, "citaat": 300}  # the free-text fields and their length limit; all three are scanned for secrets
# ADR-088 decision 5: the PR-size exception is a ``grant`` on an ``answer`` line; no other field and no other reason.
GRANT_FIELDS = frozenset({"ceiling", "reason", "head_sha", "base_ref", "lines", "measured_at"})
GRANT_REASONS = frozenset({"splitting_removes_tests", "one_working_unit", "generated", "mechanical_rename"})
_HEX40 = frozenset("0123456789abcdef")


class QueueRefused(ValueError):
    """The line cannot be written; nothing was written."""


class QueueUnreadable(ValueError):
    """The ledger holds a line that is not valid; ``line`` is its 1-based number."""

    def __init__(self, message: str, line: int = 0) -> None:
        super().__init__(message)
        self.line = line


def state_dir_for(project_id: str) -> Path:
    return resolve_central_data_dir(project_id) / "state"


def point_id(project_id: str, bron: int, ref: str, revisie: int) -> str:
    key = "|".join((project_id, str(bron), ref, str(revisie)))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
def parse_ts(value: Any) -> Optional[datetime]:
    try:
        ts = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):  # vnx-silent-except: absent or malformed is no time
        return None
    return ts if ts.tzinfo is not None else None


def _ts_ok(value: Any) -> bool:
    return parse_ts(value) is not None


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _int(value: Any, low: int, high: Optional[int] = None) -> bool:  # bool is no integer here
    return isinstance(value, int) and not isinstance(value, bool) and value >= low and (high is None or value <= high)


def check_line(project_id: str, line: Any) -> Dict[str, Any]:
    """The one validation a line passes on read and before it is written; raises ValueError (reason, no value)."""
    if not isinstance(line, dict):
        raise ValueError("not an object")
    kind = line.get("kind")
    if kind not in KINDS:
        raise ValueError("unknown kind")
    if not _text(project_id) or line.get("project_id") != project_id:
        raise ValueError("project_id is not this store's")
    if not _ts_ok(line.get("ts")):
        raise ValueError("ts is not an ISO timestamp with a timezone")
    for name in KINDS[kind]:
        if name not in line:
            raise ValueError(f"{kind} misses {name}")
    _check_values(project_id, kind, line)
    if "grant" in line:
        if kind != "answer":
            raise ValueError(f"{kind}: grant is only on an answer line")
        _check_grant(line["grant"])
    return line


def _utc_ok(value: Any) -> bool:
    if not _ts_ok(value):
        return False
    return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).utcoffset() == timedelta(0)


def _check_grant(grant: Any) -> None:
    """ADR-088 decision 5: every field present and valid, or the whole line is invalid (raises ValueError)."""
    if not isinstance(grant, dict):
        raise ValueError("grant: not an object")
    if set(grant) != GRANT_FIELDS:
        raise ValueError("grant: fields are exactly " + ", ".join(sorted(GRANT_FIELDS)))
    if grant["reason"] not in GRANT_REASONS:
        raise ValueError("grant: reason is not in the closed set")
    if not _int(grant["lines"], 0):
        raise ValueError("grant: lines is not a non-negative integer")
    if not _int(grant["ceiling"], max(1, grant["lines"])):
        raise ValueError("grant: ceiling is not a positive integer of at least lines")
    sha = grant["head_sha"]
    if not (isinstance(sha, str) and len(sha) == 40 and set(sha) <= _HEX40):
        raise ValueError("grant: head_sha is not a 40-hex sha")
    if not _text(grant["base_ref"]) or len(grant["base_ref"]) > 255:
        raise ValueError("grant: base_ref is empty, not text or too long")
    if not _utc_ok(grant["measured_at"]):
        raise ValueError("grant: measured_at is not an ISO UTC timestamp")


def _check_pr_opened(line: Mapping[str, Any]) -> None:
    """ADR-088 decision 6, on write only (an older ``pr:`` line without them replays with ceiling None): a ``pr:``
    point opens with bron 2, a ceiling above 0 and a reason from the closed set; no other point carries either."""
    if not str(line["ref"]).startswith("pr:"):
        if "ceiling" in line or "reason" in line:
            raise ValueError("opened: ceiling and reason are only on a pr: point")
        return
    if line["bron"] != 2:
        raise ValueError("opened: a pr: point opens only with bron 2 (besluit)")
    if not _int(line.get("ceiling"), 1) or line.get("reason") not in GRANT_REASONS:
        raise ValueError("opened: a pr: point needs a ceiling above 0 and a reason from the closed set")


def _check_values(project_id: str, kind: str, line: Mapping[str, Any]) -> None:
    texts = [n for n in KINDS[kind] if n not in ("bron", "revisie", "point_ids", "asked_at")]  # kopieerblok may be empty
    if kind == "opened":
        texts.remove("point_id")
        if not _int(line["bron"], 1, 8) or not _int(line["revisie"], 1) or not _ts_ok(line["asked_at"]):
            raise ValueError("opened: bron, revisie or asked_at invalid")
        if line["point_id"] != point_id(project_id, line["bron"], str(line["ref"]), line["revisie"]):
            raise ValueError("opened: point_id does not match project_id, bron, ref and revisie")
    if kind == "push_reserved":
        ids = line["point_ids"]
        if not isinstance(ids, list) or not ids or not all(_text(i) for i in ids):
            raise ValueError("push_reserved: point_ids is a non-empty list of ids")
    bad = [n for n in texts if not _text(line[n])]
    if bad:
        raise ValueError(f"{kind}: {bad[0]} is empty or not text")
    if kind == "closed" and line["uitkomst"] not in OUTCOMES:
        raise ValueError("closed: uitkomst is beantwoord or vervallen")
    if kind == "presented" and line["kanaal"] not in CHANNELS:
        raise ValueError("presented: unknown kanaal")
    for name in _LIMITS:
        if name in line and (not isinstance(line[name], str) or len(line[name]) > _LIMITS[name]):
            raise ValueError(f"{name} is not text or too long")


def _secret_names(line: Mapping[str, Any]) -> List[str]:
    found: List[str] = []
    for name in _LIMITS:
        value = line.get(name)
        if isinstance(value, str):
            found += [f"{p} in {name}" for p in secret_patterns.find(value) if p in REFUSED_PATTERNS]
    return found


def _ledger_lines(state_dir: Path, project_id: str) -> List[Dict[str, Any]]:
    path = Path(state_dir) / LEDGER_FILENAME
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise QueueUnreadable(f"{path}: unreadable ({type(exc).__name__})", 0) from exc
    lines = []
    for number, raw in enumerate(text.split("\n"), 1):
        if not raw.strip():
            continue
        try:
            lines.append(check_line(project_id, json.loads(raw)))
        except ValueError as exc:
            raise QueueUnreadable(f"{path}:{number}: {exc}", number) from exc
    return lines


def _fold(lines: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    points: Dict[str, Dict[str, Any]] = {}
    for line in lines:
        kind = line["kind"]
        if kind == "opened":
            points.setdefault(line["point_id"], {
                "point_id": line["point_id"], "state": "open", "bron": line["bron"], "ref": line["ref"],
                "revisie": line["revisie"], "vraag": line["vraag"], "sluit_bij": line["sluit_bij"],
                "asked_at": line["asked_at"], "presented": None, "bewijs": None, "unknown_reden": None,
                "ceiling": line.get("ceiling") if _int(line.get("ceiling"), 1) else None,
                "reason": line.get("reason") if line.get("reason") in GRANT_REASONS else None})
            continue
        point = points.get(line.get("point_id"))  # push_reserved has no point_id: None
        if point is None:
            continue
        if kind == "presented":
            point["presented"] = {"ts": line["ts"], "kanaal": line["kanaal"], "sessie": line["sessie"]}
        elif kind == "unknown":
            point["unknown_reden"] = line["reden"]  # the point stays open; the reason travels with it
        elif kind == "closed" and point["state"] == "open":
            point.update(state=line["uitkomst"], bewijs=line["bewijs"], unknown_reden=None)
    return points


def _read_state(state_dir: Path, project_id: str) -> Dict[str, Any]:
    try:
        points = _fold(_ledger_lines(state_dir, project_id))
    except QueueUnreadable as exc:
        return {"schema": SCHEMA, "project_id": project_id, "status": "unknown", "reason": str(exc),
                "line": exc.line, "points": {}}
    return {"schema": SCHEMA, "project_id": project_id, "status": "ok", "reason": None, "line": None, "points": points}


def presented_counts(lines: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for line in lines:
        if line["kind"] == "presented":
            counts[line["point_id"]] = counts.get(line["point_id"], 0) + 1
    return counts


def order_points(points: List[Dict[str, Any]], counts: Mapping[str, int]) -> List[Dict[str, Any]]:
    """Decision 10: never presented first, then fewest presented, then oldest asked_at, ties on point_id."""
    return sorted(points, key=lambda p: (counts.get(p["point_id"], 0), parse_ts(p["asked_at"]), p["point_id"]))


@contextmanager
def _locked(state_dir: Path) -> Iterator[None]:
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    with open(Path(state_dir) / LOCK_FILENAME, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _write_state(state_dir: Path, state: Mapping[str, Any], filename: str = STATE_FILENAME) -> None:
    path = Path(state_dir) / filename
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:  # vnx-atomic-write: temp file, fsync, replace
            handle.write(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _ledger_size(state_dir: Path) -> int:
    try:
        return (Path(state_dir) / LEDGER_FILENAME).stat().st_size
    except FileNotFoundError:
        return 0


def _build_summary(state_dir: Path, project_id: str) -> Dict[str, Any]:
    size = _ledger_size(state_dir)  # taken before the read: a writer outside this module can only make it look stale
    summary: Dict[str, Any] = {"schema": SUMMARY_SCHEMA, "project_id": project_id, "generated_at": _now(),
                               "ledger_bytes": size, "status": "ok", "reason": None, "line": None,
                               "open_total": None, "more": None, "points": []}
    try:
        lines = _ledger_lines(state_dir, project_id)
    except QueueUnreadable as exc:
        return {**summary, "status": "unknown", "reason": str(exc), "line": exc.line}
    blocks = {ln["point_id"]: ln.get("kopieerblok", "") for ln in lines if ln["kind"] == "opened"}
    counts = presented_counts(lines)
    ranked = order_points([p for p in _fold(lines).values() if p["state"] == "open"], counts)
    keep = ("point_id", "bron", "ref", "vraag", "asked_at")
    return {**summary, "open_total": len(ranked), "more": max(len(ranked) - SUMMARY_POINTS, 0),
            "points": [{**{k: p[k] for k in keep}, "presented": counts.get(p["point_id"], 0),
                        "kopieerblok": blocks.get(p["point_id"], "")} for p in ranked[:SUMMARY_POINTS]]}


def _publish(state_dir: Path, project_id: str) -> Dict[str, Any]:
    """The one place every ledger write ends in (inside the lock): rewrite the state and the inbox summary."""
    state = _read_state(state_dir, project_id)
    summary = _build_summary(state_dir, project_id)
    _write_state(state_dir, state)
    _write_state(state_dir, summary, SUMMARY_FILENAME)
    return state


def read_inbox_summary(project_id: str, *, state_dir: Optional[Path] = None) -> Dict[str, Any]:
    """``{freshness, summary, reason}``. ``current``: ``ledger_bytes`` equals the ledger's size now. ``stale``: it
    differs, or the summary cannot be read (never print its count as current). ``missing``: no summary file."""
    sdir = Path(state_dir) if state_dir is not None else state_dir_for(project_id)
    path = sdir / SUMMARY_FILENAME
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"freshness": "missing", "summary": None, "reason": "no summary yet, run `vnx inbox`"}
    except (OSError, ValueError) as exc:
        return {"freshness": "stale", "summary": None, "reason": f"{path}: unreadable ({type(exc).__name__})"}
    try:
        summary = json.loads(raw)
        if (summary["schema"], summary["project_id"]) != (SUMMARY_SCHEMA, project_id) or not _int(summary["ledger_bytes"], 0):
            raise ValueError("schema, project_id or ledger_bytes does not match")
        if summary["status"] not in ("ok", "unknown") or not _ts_ok(summary["generated_at"]):
            raise ValueError("status or generated_at malformed")
        if summary["status"] == "ok" and not (_int(summary["open_total"], 0) and _int(summary["more"], 0)
                                              and isinstance(summary["points"], list)):
            raise ValueError("open_total, more or points malformed")
        now_size = _ledger_size(sdir)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        return {"freshness": "stale", "summary": None, "reason": f"{path}: unreadable ({type(exc).__name__}: {exc})"[:200]}
    if now_size != summary["ledger_bytes"]:
        return {"freshness": "stale", "summary": summary,
                "reason": f"the ledger changed after the summary ({summary['ledger_bytes']} -> {now_size} bytes), run `vnx inbox`"}
    return {"freshness": "current", "summary": summary, "reason": None}


def _append(state_dir: Path, line: Mapping[str, Any]) -> None:
    payload = (json.dumps(line, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    with open(Path(state_dir) / LEDGER_FILENAME, "ab+") as handle:  # vnx-atomic-write: append-only line, fsynced
        end = os.fstat(handle.fileno()).st_size
        if end and os.pread(handle.fileno(), 1, end - 1) != b"\n":
            payload = b"\n" + payload
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def rebuild_state(project_id: str, *, state_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Rebuild ``operator_queue.json`` from the ledger alone and return it."""
    sdir = Path(state_dir) if state_dir is not None else state_dir_for(project_id)
    with _locked(sdir):
        return _publish(sdir, project_id)


def read_state(project_id: str, *, state_dir: Optional[Path] = None) -> Dict[str, Any]:
    """The state replayed from the ledger (not from the json file); ``status`` is ``unknown`` on a bad line."""
    sdir = Path(state_dir) if state_dir is not None else state_dir_for(project_id)
    return _read_state(sdir, project_id)


def read_lines(project_id: str, *, state_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Every ledger line in file order. Raises QueueUnreadable on a bad line (never a shorter list)."""
    return _ledger_lines(Path(state_dir) if state_dir is not None else state_dir_for(project_id), project_id)


def answers(project_id: str, *, state_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """The ``answer`` lines of the ledger in file order. Raises QueueUnreadable on a bad ledger line."""
    sdir = Path(state_dir) if state_dir is not None else state_dir_for(project_id)
    return [line for line in _ledger_lines(sdir, project_id) if line["kind"] == "answer"]


def latest_grant(project_id: str, pr: int, *, state_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """ADR-088 decision 5: the grant of the ``pr:<pr>`` point with the highest revision that carries one, as
    ``{grant, point_id, revision, quote, by, at}`` (the answer's citation, writer and time); None when no answer
    of such a point carries a grant (no ledger is no grant).
    A bad ledger line raises QueueUnreadable, never None: "cannot read" is not "no grant" (the door refuses on it).
    Only an answer line with a citation counts; a ``check_line`` without one already makes the ledger unreadable."""
    if not _int(pr, 1):
        raise ValueError("pr is not a positive integer")
    sdir = Path(state_dir) if state_dir is not None else state_dir_for(project_id)
    lines = _ledger_lines(sdir, project_id)
    ref = f"pr:{pr}"
    revisions = {ln["point_id"]: ln["revisie"] for ln in lines if ln["kind"] == "opened" and ln["ref"] == ref}
    best: Optional[Dict[str, Any]] = None
    for line in lines:
        pid = line.get("point_id")
        if line["kind"] != "answer" or "grant" not in line or pid not in revisions or not _text(line["citaat"]):
            continue
        if best is None or revisions[pid] > best["revision"]:  # same revision: the first answer stands
            best = {"grant": dict(line["grant"]), "point_id": pid, "revision": revisions[pid],
                    "quote": line["citaat"], "by": line["vastgelegd_door"], "at": line["ts"]}
    return best


def append_line(project_id: str, kind: str, fields: Mapping[str, Any], *, now: Optional[str] = None,
                state_dir: Optional[Path] = None, guard: Optional[Callable[..., Optional[str]]] = None,
                ) -> Optional[Dict[str, Any]]:
    """Validate and append one line, then rebuild the state. Returns the line, or None for an ``opened``
    whose point_id exists. ``guard(points, lines)`` runs under the lock, right before the append: a reason it
    returns is a refusal. Raises QueueRefused (nothing written) or QueueUnreadable (ledger broken)."""
    line = {**fields, "kind": kind, "project_id": project_id, "ts": now or _now()}
    try:
        check_line(project_id, line)
        if kind == "opened":
            _check_pr_opened(line)
    except ValueError as exc:
        raise QueueRefused(str(exc)) from exc
    hits = _secret_names(line)
    if hits:
        raise QueueRefused("refused, secret pattern: " + ", ".join(hits))
    sdir = Path(state_dir) if state_dir is not None else state_dir_for(project_id)
    with _locked(sdir):
        lines = _ledger_lines(sdir, project_id)
        points = _fold(lines)
        ids = line["point_ids"] if kind == "push_reserved" else [line["point_id"]]
        known = [i in points for i in ids]
        if kind == "opened" and known[0]:
            return None
        if kind != "opened" and not all(known):
            raise QueueRefused(f"{kind}: point_id is not in the ledger")
        if kind == "closed" and points[line["point_id"]]["state"] != "open":
            raise QueueRefused("closed: the point is already closed")
        why = guard(points, lines) if guard is not None else None
        if why:
            raise QueueRefused(why)
        _append(sdir, line)
        _publish(sdir, project_id)
    return line


def open_point(project_id: str, *, bron: int, ref: str, revisie: int = 1, vraag: str, sluit_bij: str,
               opened_by: str, kopieerblok: str = "", asked_at: Optional[str] = None, now: Optional[str] = None,
               state_dir: Optional[Path] = None, extra: Optional[Mapping[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Open a point; ``ref`` is taken as given. None when it exists. ``extra`` adds fields ``check_line`` validates
    (``ceiling`` and ``reason`` of a ``pr:`` point). Other kinds: ``append_line``."""
    stamp = now or _now()
    return append_line(project_id, "opened", {
        **(extra or {}), "point_id": point_id(project_id, bron, ref, revisie), "bron": bron, "ref": ref,
        "revisie": revisie, "vraag": vraag, "sluit_bij": sluit_bij, "kopieerblok": kopieerblok,
        "asked_at": asked_at or stamp, "opened_by": opened_by}, now=stamp, state_dir=state_dir)


def close_point(project_id: str, pid: str, *, uitkomst: str, bewijs: str, beantwoord_door: str = "onbekend",
                now: Optional[str] = None, state_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Close on source state: ``bewijs`` is required and refused when empty."""
    return append_line(project_id, "closed", {"point_id": pid, "uitkomst": uitkomst, "bewijs": bewijs,
                                              "beantwoord_door": beantwoord_door}, now=now, state_dir=state_dir)


def reserve(project_id: str, choose: Callable[[Dict[str, Any]], List[str]], *, sessie: str, now: Optional[str] = None,
            state_dir: Optional[Path] = None) -> List[str]:
    """Decide and reserve inside one lock (ADR-076 decision 10). ``choose`` gets the state plus ``lines`` (the ledger)
    and returns the point ids to reserve; one ``push_reserved`` line is appended and fsynced before they are returned.
    An empty choice writes nothing. A bad ledger raises QueueUnreadable and writes nothing."""
    sdir = Path(state_dir) if state_dir is not None else state_dir_for(project_id)
    with _locked(sdir):
        lines = _ledger_lines(sdir, project_id)
        ids = list(choose({"points": _fold(lines), "lines": lines}))
        if not ids:
            return []
        line = {"kind": "push_reserved", "project_id": project_id, "ts": now or _now(), "point_ids": ids, "sessie": sessie}
        try:
            check_line(project_id, line)
        except ValueError as exc:
            raise QueueRefused(str(exc)) from exc
        _append(sdir, line)
        _publish(sdir, project_id)
    return ids


RETENTION_DAYS = 180


def _expired(lines: List[Dict[str, Any]], cutoff: datetime) -> set:
    """Point ids whose effective ``closed`` line is older than the cutoff. Open points (also ``unknown``) never are."""
    gone: set = set()
    for pid, point in _fold(lines).items():
        if point["state"] == "open":
            continue
        closed = next(ln for ln in lines if ln["kind"] == "closed" and ln["point_id"] == pid)  # first closed line took effect
        if datetime.fromisoformat(closed["ts"].strip().replace("Z", "+00:00")) < cutoff:
            gone.add(pid)
    return gone


def compact(project_id: str, *, now: Optional[datetime] = None, dry_run: bool = False,
            state_dir: Optional[Path] = None) -> Dict[str, Any]:
    """ADR-076 decision 12: drop every line of a point closed more than RETENTION_DAYS ago, and a ``push_reserved``
    once all its ids are gone. Under the writer's lock: temp file beside the ledger, fsync, atomic rename, rebuild.
    An unreadable ledger is not touched: ``status`` ``unknown`` with the line number."""
    sdir = Path(state_dir) if state_dir is not None else state_dir_for(project_id)
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=RETENTION_DAYS)
    with _locked(sdir):
        try:
            lines = _ledger_lines(sdir, project_id)
        except QueueUnreadable as exc:
            return {"project_id": project_id, "status": "unknown", "reason": str(exc), "line": exc.line,
                    "points": 0, "lines": 0}
        gone = _expired(lines, cutoff)
        alive = set(_fold(lines)) - gone  # a push_reserved goes once none of its ids survives (earlier compacts removed some)
        keep = [ln for ln in lines if not (ln.get("point_id") in gone
                                           or (ln["kind"] == "push_reserved" and not set(ln["point_ids"]) & alive))]
        result = {"project_id": project_id, "status": "ok", "reason": None, "line": None, "points": len(gone),
                  "lines": len(lines) - len(keep)}
        if dry_run or not result["lines"]:
            return result
        path = sdir / LEDGER_FILENAME
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:  # vnx-atomic-write: temp file, fsync, replace
                handle.write("".join(json.dumps(ln, sort_keys=True, ensure_ascii=False) + "\n" for ln in keep))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        _publish(sdir, project_id)
    return result
