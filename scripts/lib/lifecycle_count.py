"""ADR-045 K-9: the lifecycle count of one store, read-only.

Five defects over this project's ``v3`` lines — a start without an end past its
deadline plus the sweep's grace, an end without a start, a double end, a
projection conflict, an unknown provenance — paired on ``(project_id,
dispatch_id, attempt)`` inside the ``writer_sha`` cohort. It decides nothing:
``ledger_health``'s four checks keep their exit-code fold and this sits beside
them, and what cannot be measured is ``unmeasured`` with a reason, never zero.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from coordination_db import STORE_PRESENT, STORE_UNREADABLE, db_path_from_state_dir, get_connection, store_state
from dispatch_lifecycle import (  # its table, its grace, its parser: never a copy
    ABANDON_GRACE_SECONDS, END_INTENT_DIRNAME, END_STATE_TARGET, PROJECTION_ROUTES,
    SWEEP_HEARTBEAT_FILENAME, _as_utc, deadline_for,
)
from coordination_db import DISPATCH_TRANSITIONS, TERMINAL_DISPATCH_STATES  # never a copy
from ledger_end_line import end_line
from ndjson_io import NdjsonCorruptionError, read_ndjson_strict
from receipt_schema import DISPATCH_FINISHED_EVENT_TYPE, DISPATCH_STARTED_EVENT_TYPE
from actor_class import ACTOR_CLASSES
from vnx_paths import project_id_from_state_dir

LEDGER_FILENAME = "t0_receipts.ndjson"
STATUS_MEASURED, STATUS_UNMEASURED, COHORT_ALL = "measured", "unmeasured", "all_v3"
DEFECTS = ("start_without_end", "end_without_start", "double_end", "projection_conflict",
           "provenance_unknown")
KEY_FIELDS = ("project_id", "dispatch_id", "attempt")
GIT_TIMEOUT_SECONDS = 10.0  # a git that hangs must never hang the count
DEFAULT_REPO = Path(__file__).resolve().parent  # the checkout that writes the lines
DELIVERABLE_PREFIX = "dlv-"  # ADR-056: only these rows are deliverables
MERGE_EVENT_TYPE, MERGE_CONCLUSION = "pr_merged", "merged"  # pr_merge.py's receipt
MEMBER_DELIVERED, MEMBER_NOT_DELIVERED = "delivered", "not_delivered"
MEMBER_UNKNOWN, MEMBER_OUT = "unknown", "out_of_cohort"
COVERED, UNCOVERED, NO_DATA = "covered", "uncovered", "no_data"  # D13: per source and per project
COVERAGE_SOURCES = ("promote", "close", "decide", "merge", "override", "prompt")
PROMOTE_EVENT, CLOSE_EVENT = "deliverable_promoted", "deliverable_completed"  # planning_cli's events
DECISION_LOG_FILENAME = "t0_decision_log.jsonl"
PROMPT_LOG_FILENAME = "prompt_attribution.ndjson"  # the prompt hook's store (PR-8b); absent until then
PROJECT_UNKNOWN = "unknown"  # ADR-056: a source that cannot name its project writes this


class Unmeasured(Exception):
    """This store, or the git it needs, cannot be measured; the reason travels on."""


def _git(repo: Path, *args: str) -> int:
    """The exit code: ``0`` yes, ``1`` no, anything else is not an answer (never a guessed no)."""
    try:  # one helper, one timeout: a git that hangs must never hang the count
        probe = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                               timeout=GIT_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Unmeasured(f"git could not run in {repo}: {exc}") from exc
    return probe.returncode


@lru_cache(maxsize=None)
def _known(repo: str, sha: str) -> bool:
    return _git(Path(repo), "cat-file", "-e", f"{sha}^{{commit}}") == 0


@lru_cache(maxsize=None)
def _in_cohort(repo: str, activation: str, sha: str) -> bool:
    code = _git(Path(repo), "merge-base", "--is-ancestor", activation, sha)
    if code not in (0, 1):
        raise Unmeasured(f"git merge-base {activation} {sha} answered {code}: not an answer")
    return code == 0


def _verdict(line: dict, repo: str, activation, project: str):
    """``None`` for another project, else ``"out"``, ``"unknown"`` or ``"in"``."""
    pid = line.get("project_id")
    if isinstance(pid, str) and pid.strip() and pid.strip() != project:
        return None
    sha = line.get("writer_sha")
    if not isinstance(sha, str) or not sha.strip():
        return "unknown"  # a v3 line without provenance is not a known line
    if not _known(repo, sha.strip()):
        return "unknown"
    return "out" if activation and not _in_cohort(repo, activation, sha.strip()) else "in"


def _key(line: dict, loose: bool = False):
    """``(project_id, dispatch_id, attempt)``; ``None`` when unpairable, unless ``loose``."""
    values = [line.get(name) for name in KEY_FIELDS]
    pid = values[0].strip() if isinstance(values[0], str) and values[0].strip() else None
    did = values[1].strip() if isinstance(values[1], str) and values[1].strip() else None
    attempt = (values[2] if isinstance(values[2], int) and not isinstance(values[2], bool)
               and values[2] >= 1 else None)
    return (pid, did, attempt) if loose or (pid and did and attempt) else None


def _records(state_dir: Path, repo: str, activation, project: str):
    """In-scope v3 starts and ends by key, the unknown-provenance keys, the problems.

    Records come back grouped by dispatch id and hold only lines the cohort keeps,
    so an attempt's one end is read through ``ledger_end_line.end_line`` without
    rescanning the ledger and without an excluded line deciding it.
    """
    path = state_dir / LEDGER_FILENAME
    try:
        lines = read_ndjson_strict(path)  # a torn tail is no record; a hole is no evidence
    except NdjsonCorruptionError as exc:
        raise Unmeasured(f"{exc}; the count is not measurable") from exc
    except OSError as exc:
        raise Unmeasured(f"cannot read {path}: {exc}") from exc
    starts, ends, unknown, problems, records = {}, {}, [], [], {}
    for line in lines:
        if not isinstance(line, dict):
            continue
        version = line.get("schema_version")
        if isinstance(version, bool) or version != 3:
            records.setdefault(str(line.get("dispatch_id") or "").strip(), []).append(line)
            continue  # no backfill: a legacy line is never paired and never counted
        verdict = _verdict(line, repo, activation, project)
        if verdict == "out":
            continue  # the activation commit left it out: no attempt's end is read from here
        records.setdefault(str(line.get("dispatch_id") or "").strip(), []).append(line)
        if verdict is None:
            continue  # another project's line, whatever its event type
        if verdict == "unknown":  # every v3 line of this project carries its provenance
            key = _key(line, loose=True)  # the attempt it names, else None: never a guess
            if key not in unknown:
                unknown.append(key)
        event = str(line.get("event_type") or "").strip()
        if verdict == "out" or event not in (DISPATCH_STARTED_EVENT_TYPE,
                                             DISPATCH_FINISHED_EVENT_TYPE):
            continue  # pairing is start/end only; "out" already left the cohort
        key = _key(line)
        if key is None:
            problems.append(f"unkeyable {event}: dispatch_id={line.get('dispatch_id')!r}")
            continue
        if event == DISPATCH_STARTED_EVENT_TYPE:
            starts.setdefault(key, line)
        else:
            ends.setdefault(key, []).append(line)
    return starts, ends, unknown, problems, records


def _projection_successors(target: str) -> frozenset:
    """Every state this attempt can still reach from the projection ``target``.

    A new attempt begins at the claim (``increment_attempt_count`` runs on each
    claim path), so the walk stops at ``claimed``: it and everything behind it
    belong to a later attempt. Read from ``DISPATCH_TRANSITIONS``, never copied.
    """
    seen, queue = set(), [target]
    while queue:
        for nxt in DISPATCH_TRANSITIONS.get(queue.pop(), frozenset()):
            if nxt != "claimed" and nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return frozenset(seen)


def _defects(state_dir: Path, starts: dict, ends: dict, records: dict, evaluated: datetime,
             problems: list) -> dict:
    found = {name: [] for name in DEFECTS}  # rule 3: a list of keys per defect

    rows, db_path, pairs = {}, db_path_from_state_dir(state_dir), {k[:2] for k in [*starts, *ends]}
    store = store_state(db_path)
    if pairs and store == STORE_UNREADABLE:  # K-9c rule 4: never "no database"
        raise Unmeasured(f"{db_path} exists but is not a regular file; the count is not measurable")
    try:
        if pairs and store == STORE_PRESENT:
            with get_connection(state_dir) as conn:
                for project, dispatch in sorted(pairs):
                    row = conn.execute("SELECT * FROM dispatches WHERE dispatch_id = ? "
                                       "AND project_id = ?", (dispatch, project)).fetchone()
                    if row is not None: rows[(project, dispatch)] = dict(row)
    except Exception as exc:  # vnx-silent-except: an unreadable store is a refusal, never "no row"
        raise Unmeasured(f"cannot read {db_path}: {exc}") from exc
    for key, start in starts.items():
        if key in ends: continue
        deadline = deadline_for(state_dir, start, rows.get(key[:2]))[0]
        try:
            started = _as_utc(start.get("timestamp"))
        except ValueError as exc:
            problems.append(f"{key}: {exc}")
            continue
        if started is None:
            problems.append(f"{key}: start line has no timestamp")
        elif evaluated > started + timedelta(seconds=deadline + ABANDON_GRACE_SECONDS):
            found["start_without_end"].append(key)
    for key, lines in ends.items():
        if len(lines) > 1: found["double_end"].append(key)
        end = end_line(records.get(key[1], ()), *key) or lines[-1]  # the fabric's own reader
        if key not in starts: found["end_without_start"].append(key)
        target = END_STATE_TARGET.get(str(end.get("end_state") or "").strip())
        if target is None:
            problems.append(f"{key}: end_state {end.get('end_state')!r} has no projection")
        elif key[:2] in rows:
            row = rows[key[:2]]
            attempt, state = row.get("attempt_count"), str(row.get("state") or "").strip()
            if attempt == key[2] and not isinstance(attempt, bool) and state != target \
                    and state not in PROJECTION_ROUTES[target] \
                    and state not in _projection_successors(target):
                found["projection_conflict"].append(key)
    return found


class Member(dict):
    """One deliverable's cohort membership; a dict (JSON-safe) that also reads as attributes."""

    __slots__ = ()

    def __getattr__(self, name: str):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


def _parse_period_start(value):
    """The period start as an aware datetime; ``None`` stays ``None``, junk is ``Unmeasured``."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    try:
        stamp = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise Unmeasured(f"period start {value!r} is not an ISO timestamp") from exc
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=timezone.utc)


def _has_merge_receipt(lines, project: str) -> bool:
    """A ``pr_merged`` receipt with conclusion exactly ``merged`` for this project (positive match)."""
    for line in lines:
        pid = line.get("project_id")
        if isinstance(pid, str) and pid.strip() and pid.strip() != project:
            continue
        if line.get("event_type") == MERGE_EVENT_TYPE and line.get("conclusion") == MERGE_CONCLUSION:
            return True
    return False


def _member(row: dict, project: str, period_start, records: dict) -> Member:
    """Decide one terminal row (D9, D10); anything unreadable is ``unknown``, never delivered."""
    did, state = str(row["dispatch_id"]), str(row.get("state") or "").strip()

    def make(status: str, reason: str) -> Member:
        return Member(project_id=project, dispatch_id=did, state=state, status=status, reason=reason,
                      created_at=row.get("created_at"), pr_ref=row.get("pr_ref"))

    if period_start is not None:
        try:
            created = _parse_period_start(row.get("created_at"))
        except Unmeasured:
            return make(MEMBER_UNKNOWN, "created_at_unreadable")
        if created is None:
            return make(MEMBER_UNKNOWN, "created_at_unreadable")
        if created < period_start:
            return make(MEMBER_OUT, "created_before_period")
    if state != "completed":
        return make(MEMBER_NOT_DELIVERED, state)
    pr_ref = row.get("pr_ref")
    if isinstance(pr_ref, str) and pr_ref.strip() and not _has_merge_receipt(records.get(did, ()), project):
        return make(MEMBER_UNKNOWN, "receipt_missing")
    return make(MEMBER_DELIVERED, "completed")


def _membership(state_dir: Path, project: str, period_start, records: dict) -> list:
    """Every terminal ``dlv-`` row of this project with its membership, sorted by dispatch id."""
    db_path = db_path_from_state_dir(state_dir)
    store = store_state(db_path)
    if store == STORE_UNREADABLE:
        raise Unmeasured(f"{db_path} exists but is not a regular file; membership is not measurable")
    if store != STORE_PRESENT:
        return []
    marks = ",".join("?" for _ in TERMINAL_DISPATCH_STATES)
    try:
        with get_connection(state_dir) as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT dispatch_id, state, pr_ref, created_at FROM dispatches "
                f"WHERE project_id = ? AND dispatch_id LIKE ? AND state IN ({marks}) "
                "ORDER BY dispatch_id", (project, DELIVERABLE_PREFIX + "%",
                                         *sorted(TERMINAL_DISPATCH_STATES)))]
    except Exception as exc:  # vnx-silent-except: an unreadable store is a refusal, never "no rows"
        raise Unmeasured(f"cannot read {db_path}: {exc}") from exc
    return [_member(row, project, period_start, records) for row in rows]


def _membership_summary(members: list) -> dict:
    """Counts per status, so a reader sees the cohort without walking the list."""
    summary = dict.fromkeys((MEMBER_DELIVERED, MEMBER_NOT_DELIVERED, MEMBER_UNKNOWN, MEMBER_OUT), 0)
    for member in members:
        summary[member["status"]] += 1
    return summary


class Coverage(Member):
    """A project's coverage: ``state`` overall, ``sources`` per counted source (D13)."""

    __slots__ = ()


def _text(value) -> str:
    """A stripped string, or "" for anything that is not one (a malformed field names nothing)."""
    return value.strip() if isinstance(value, str) else ""


def _meta(raw) -> dict:
    """``metadata_json`` as a dict; unparseable or not an object is an empty one, never a guess."""
    try:
        meta = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return {}
    return meta if isinstance(meta, dict) else {}


def _sql_rows(state_dir: Path, table: str, where: str = "", params: tuple = ()) -> list:
    """The rows of a coordination-store table as dicts, read-only.

    An absent store or table holds no rows; an unreadable one is a refusal.
    """
    db_path = db_path_from_state_dir(state_dir)
    store = store_state(db_path)
    if store == STORE_UNREADABLE:
        raise Unmeasured(f"{db_path} exists but is not a regular file; coverage is not measurable")
    if store != STORE_PRESENT:
        return []
    try:
        with get_connection(state_dir) as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (table,)).fetchone() is None:
                return []
            return [dict(r) for r in conn.execute(f"SELECT * FROM {table} {where}", params)]
    except Exception as exc:  # vnx-silent-except: an unreadable store is a refusal, never "no rows"
        raise Unmeasured(f"cannot read {db_path}: {exc}") from exc


def _sql_entries(state_dir: Path, table: str, where: str = "", params: tuple = ()) -> list:
    """``(scope, project_id, delivery_id)`` per row of a coordination-store table, read-only.

    Scope is the column when the table has one, else the metadata; the identity reads the
    metadata first. An absent store or table holds no rows; an unreadable one is a refusal.
    """
    entries = []
    for row in _sql_rows(state_dir, table, where, params):
        meta = _meta(row.get("metadata_json"))
        entries.append((row.get("project_id") or meta.get("project_id"),
                        meta.get("project_id") or row.get("project_id"),
                        meta.get("delivery_id") or row.get("delivery_id")))
    return entries


def _ndjson_lines(path: Path, event_type=None) -> list:
    """The object lines of an NDJSON store (optionally of one event type); an absent file has none."""
    if not path.exists():
        return []
    try:
        lines = read_ndjson_strict(path)
    except (NdjsonCorruptionError, OSError) as exc:
        raise Unmeasured(f"cannot read {path}: {exc}; coverage is not measurable") from exc
    return [ln for ln in lines if isinstance(ln, dict)
            and (event_type is None or ln.get("event_type") == event_type)]


def _ndjson_entries(path: Path, event_type=None) -> list:
    """``(scope, project_id, delivery_id)`` per line of an NDJSON store; an absent file has none."""
    return [(ln.get("project_id"), ln.get("project_id"), ln.get("delivery_id"))
            for ln in _ndjson_lines(path, event_type)]


def _row_class(value) -> str:
    """A recorded actor class, or ``unknown`` for a missing, malformed or foreign value."""
    text = _text(value)
    return text if text in ACTOR_CLASSES else PROJECT_UNKNOWN


def _sql_source_rows(state_dir: Path, table: str, where: str = "", params: tuple = ()) -> list:
    """``(project_id, delivery_id, actor_class)`` per row; the class is read from the metadata."""
    out = []
    for row in _sql_rows(state_dir, table, where, params):
        meta = _meta(row.get("metadata_json"))
        out.append((_text(meta.get("project_id") or row.get("project_id")),
                    _text(meta.get("delivery_id") or row.get("delivery_id")),
                    _row_class(meta.get("actor_class"))))
    return out


def _ndjson_source_rows(path: Path, event_type=None) -> list:
    """``(project_id, delivery_id, actor_class)`` per line; the class is the top-level field."""
    return [(_text(ln.get("project_id")), _text(ln.get("delivery_id")), _row_class(ln.get("actor_class")))
            for ln in _ndjson_lines(path, event_type)]


def source_rows(state_dir) -> dict:
    """Per counted source the rows as ``(project_id, delivery_id, actor_class)``, read-only.

    The same stores as ``_coverage_entries``. A row without a readable class reads as
    ``unknown``; the project is the row's own identity, never inferred. A store that cannot
    be read raises ``Unmeasured``.
    """
    state_path, events = Path(state_dir), "WHERE event_type = ?"
    return {
        "promote": _sql_source_rows(state_path, "coordination_events", events, (PROMOTE_EVENT,)),
        "close": _sql_source_rows(state_path, "coordination_events", events, (CLOSE_EVENT,)),
        "decide": _ndjson_source_rows(state_path / DECISION_LOG_FILENAME),
        "merge": _ndjson_source_rows(state_path / LEDGER_FILENAME, MERGE_EVENT_TYPE),
        "override": _sql_source_rows(state_path, "governance_overrides"),
        "prompt": _ndjson_source_rows(state_path / PROMPT_LOG_FILENAME),
    }


def terminal_moments(state_dir, project: str) -> dict:
    """``{dispatch_id: occurred_at}`` of each ``dlv-`` dispatch's transition into its current terminal state.

    The writer is ``runtime_state_machine.transition_dispatch``: it appends a
    ``coordination_events`` row with ``entity_type='dispatch'``, ``event_type='dispatch_<state>'``
    and ``to_state='<state>'``, stamped ``occurred_at``. Only that event of this project, matching
    the dispatch's current terminal state, counts (``dispatches.updated_at`` moves on later writes).
    A dispatch without such an event, or with an unparseable stamp, is absent: the reader never guesses.
    """
    state_path = Path(state_dir)
    states = {_text(r.get("dispatch_id")): _text(r.get("state")) for r in _sql_rows(
        state_path, "dispatches", "WHERE project_id = ? AND dispatch_id LIKE ?",
        (project, DELIVERABLE_PREFIX + "%"))}
    moments = {}
    for event in _sql_rows(state_path, "coordination_events",
                           "WHERE entity_type = ? AND project_id = ?", ("dispatch", project)):
        did, to_state = _text(event.get("entity_id")), _text(event.get("to_state"))
        if (to_state in TERMINAL_DISPATCH_STATES and states.get(did) == to_state
                and event.get("event_type") == f"dispatch_{to_state}"):
            try:
                _parse_period_start(event.get("occurred_at"))
            except Unmeasured:
                continue
            moments[did] = _text(event.get("occurred_at"))
    return moments


def _classify(entries: list, project: str) -> dict:
    """One source: another project's rows never count; no rows is ``no_data``, never covered."""
    kept = [(_text(scope), _text(pid), _text(did)) for scope, pid, did in entries
            if _text(scope) in ("", PROJECT_UNKNOWN, project)]
    # a row whose own project column says "unknown" is uncovered whatever its metadata claims
    with_identity = sum(1 for scope, pid, did in kept
                        if scope != PROJECT_UNKNOWN and pid == project and did)
    state = NO_DATA if not kept else COVERED if with_identity else UNCOVERED
    return {"state": state, "rows": len(kept), "with_identity": with_identity}


def _coverage_entries(state_dir: Path) -> dict:
    """Each counted source read from the store ADR-056 names for it, through its resolver."""
    events = "WHERE event_type = ?"
    return {
        "promote": _sql_entries(state_dir, "coordination_events", events, (PROMOTE_EVENT,)),
        "close": _sql_entries(state_dir, "coordination_events", events, (CLOSE_EVENT,)),
        "decide": _ndjson_entries(state_dir / DECISION_LOG_FILENAME),
        "merge": _ndjson_entries(state_dir / LEDGER_FILENAME, MERGE_EVENT_TYPE),
        "override": _sql_entries(state_dir, "governance_overrides"),
        "prompt": _ndjson_entries(state_dir / PROMPT_LOG_FILENAME),
    }


def measure_coverage(state_dir, *, project_id=None) -> Coverage:
    """Per counted source: does its store hold rows of this project with ``(project_id, delivery_id)``?

    Read-only. Overall ``state`` is ``no_data`` when no source has a row, ``covered`` when every
    source is covered, else ``uncovered`` (D13). A store that cannot be read raises ``Unmeasured``.
    """
    state_path = Path(state_dir)
    project = _text(project_id) or project_id_from_state_dir(state_path) or _text(
        os.environ.get("VNX_PROJECT_ID"))
    if not project:
        raise Unmeasured(f"no project id for {state_path}: coverage is read per project")
    entries = _coverage_entries(state_path)
    sources = {name: _classify(entries[name], project) for name in COVERAGE_SOURCES}
    states = {src["state"] for src in sources.values()}
    overall = NO_DATA if states == {NO_DATA} else COVERED if states == {COVERED} else UNCOVERED
    return Coverage(project_id=project, state=overall, sources=sources)


def _sweep_age(state_dir: Path, evaluated: datetime):
    """The heartbeat K-6 writes: ``(age_seconds, None)``, or ``(None, reason)`` when absent."""
    path = state_dir / END_INTENT_DIRNAME / SWEEP_HEARTBEAT_FILENAME
    try:
        written = _as_utc(json.loads(path.read_text(encoding="utf-8")).get("finished_at"))
    except FileNotFoundError:
        return None, f"no sweep heartbeat at {path}"
    except (OSError, ValueError, AttributeError) as exc:
        return None, f"unreadable sweep heartbeat {path}: {exc}"
    return (round((evaluated - written).total_seconds(), 3), None) if written else \
        (None, f"sweep heartbeat {path} carries no finished_at")


def _unmeasured(reason: str, problems=None) -> dict:
    """Fail-closed: every count is ``None`` — there is no zero here to mistake for clean."""
    return {"status": STATUS_UNMEASURED, "reason": reason, "cohort": None,
            "counts": dict.fromkeys(DEFECTS), "findings": {name: [] for name in DEFECTS},
            "sweep_heartbeat_age_seconds": None, "sweep_heartbeat_reason": reason,
            "membership": None, "membership_summary": None, "period_start": None,
            "problems": list(problems) if problems else [reason]}


REASON_CAP = 160


def _capped_reason(reason: str, problems) -> str:
    """The reason, cut at ``REASON_CAP`` with an ellipsis and how many problems it hides."""
    if len(reason) <= REASON_CAP:
        return reason
    cut = reason[:REASON_CAP - 1] + "\u2026"
    if not isinstance(problems, list) or "; ".join(map(str, problems)) != reason:
        return cut
    starts, at = [], 0
    for problem in problems:
        starts.append(at)
        at += len(str(problem)) + 2
    hidden = sum(1 for start in starts if start >= REASON_CAP - 1)
    return f"{cut} (+{hidden} more)" if hidden else cut


def _count_age(count: dict, now) -> str:
    """``count 14h old`` from ``counted_at`` / ``evaluated_at``; anything unusable is unknown."""
    try:
        stamped = _as_utc(count.get("counted_at") or count.get("evaluated_at"))
        minutes = int(((_as_utc(now) or datetime.now(timezone.utc)) - stamped).total_seconds() // 60)
    except (ValueError, TypeError, AttributeError):  # fail-closed: an unreadable stamp is no age
        return "count age unknown"
    if minutes < 0:  # a stamp in the future is not a measurement of age
        return "count age unknown"
    return f"count {minutes}m old" if minutes < 60 else f"count {minutes // 60}h old"


def briefing_line(count: dict, now=None) -> str:
    """The one SessionStart line: the cohort, the five numbers, the heartbeat's age and the count's."""
    if count.get("status") != STATUS_MEASURED:
        reason = _capped_reason(str(count.get("reason") or "not measured"), count.get("problems"))
        return f"lifecycle: unmeasured ({reason}); sweep unknown"
    counts, age = count.get("counts") or {}, count.get("sweep_heartbeat_age_seconds")
    sweep = (f"{int(age)}s" if isinstance(age, (int, float)) and not isinstance(age, bool)
             else f"unknown ({count.get('sweep_heartbeat_reason') or 'no reason'})")
    cohort = count.get("cohort")  # an absent or blank cohort is named unknown, never guessed
    label = ("unknown" if not isinstance(cohort, str) or not cohort.strip()
             else cohort if cohort == COHORT_ALL else f"since {cohort.strip()[:8]}")
    return (f"lifecycle [cohort {label}]: {counts.get('start_without_end')} start-without-end, "
            f"{counts.get('end_without_start')} end-without-start, "
            f"{counts.get('double_end')} double, {counts.get('projection_conflict')} conflict, "
            f"{counts.get('provenance_unknown')} provenance-unknown; sweep {sweep}; "
            f"{_count_age(count, now)}")


def compute_lifecycle_count(state_dir, *, project_id=None, activation_commit=None, repo=None,
                            now=None, period_start=None) -> dict:
    """Count the five defects over this project's ``v3`` lines. Read-only.

    ``activation_commit`` narrows the cohort (without it every ``v3`` line is in
    and ``cohort`` says ``all_v3``), ``repo`` is the checkout the provenance is
    read against, ``now`` the evaluation instant (absent: the real clock).
    ``period_start`` bounds the deliverable cohort (D9): a ``dlv-`` row created
    before it is ``out_of_cohort``; absent, no row is excluded by age.
    """
    state_path = Path(state_dir)
    evaluated = _as_utc(now) or datetime.now(timezone.utc)
    project = (project_id or project_id_from_state_dir(state_path)
               or os.environ.get("VNX_PROJECT_ID", "")).strip()
    if not project:
        return _unmeasured(f"no project id for {state_path}: the count is read per project")
    activation = (activation_commit or "").strip() or None
    repo = str(Path(repo) if repo is not None else DEFAULT_REPO)
    try:
        if _git(Path(repo), "rev-parse", "--git-dir"):
            return _unmeasured(f"{repo} is not a git repository: provenance cannot be measured")
        if activation and _git(Path(repo), "rev-parse", "--verify", "--quiet",
                               f"{activation}^{{commit}}"):
            return _unmeasured(f"activation commit {activation} does not resolve")
        starts, ends, unknown, problems, records = _records(
            state_path, repo, activation, project)
        found = _defects(state_path, starts, ends, records, evaluated, problems)
        start = _parse_period_start(period_start)
        members = _membership(state_path, project, start, records)
        if problems:  # a problem is not a measurement: no zero may stand for it
            return _unmeasured("; ".join(problems), problems)
    except Unmeasured as exc:
        return _unmeasured(str(exc))
    found["provenance_unknown"] = unknown
    counts = {name: len(found[name]) for name in DEFECTS}
    age, age_reason = _sweep_age(state_path, evaluated)
    return {"status": STATUS_MEASURED, "cohort": activation or COHORT_ALL,
            "evaluated_at": evaluated.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "counts": dict(counts, total=sum(counts.values())), "findings": found,
            "sweep_heartbeat_age_seconds": age, "sweep_heartbeat_reason": age_reason,
            "membership": members, "membership_summary": _membership_summary(members),
            "period_start": start.strftime("%Y-%m-%dT%H:%M:%SZ") if start else None,
            "problems": problems}
