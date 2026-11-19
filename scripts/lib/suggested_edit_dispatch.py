"""Suggested edits go through a dispatch (ochtendronde D3c, ADR-095 decisions 10 and 13).

``accept`` computes the exact diff on a temp copy and stages one dispatch; ``reconcile`` marks an edit
applied only on a ``pr_merged`` receipt of that dispatch. Counting for the baseline lives in
``count_dispatches_in_window`` so D3d can call it for the after-window.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from suggested_edit_targets import guard_target

_UTC = timezone.utc
REMEASURE_DAYS = 14
LEDGER_NAME = "t0_receipts.ndjson"


class LedgerUnreadable(ValueError):
    """The receipt ledger cannot be read as evidence; ``str(exc)`` names why."""


def compute_diff(edit: dict, root: Path, handler: Callable[[dict], bool], set_root: Callable[[Path], None],
                 get_root: Callable[[], Path]) -> Tuple[Optional[str], Optional[str]]:
    """``(diff, None)`` or ``(None, reason)``. Runs ``handler`` on a copy of the target, never the real file."""
    target, reason = guard_target(edit.get("target"), root)
    if target is None:
        return None, f"target refused ({reason}): {edit.get('target')!r}"
    before = target.read_text(encoding="utf-8")
    saved = get_root()
    with tempfile.TemporaryDirectory(prefix="suggest-diff-") as tmp:
        copy = Path(tmp) / edit["target"]
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(target, copy)
        set_root(Path(tmp))
        try:
            changed = handler(edit)
        except Exception as exc:  # the reason goes back to the caller, the edit stays pending
            return None, f"handler raised {type(exc).__name__}: {exc}"
        finally:
            set_root(saved)
        if not changed:
            return None, "handler made no change"
        after = copy.read_text(encoding="utf-8")
    if after == before:
        return None, "handler made no change"
    rel = edit["target"]
    diff = "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True), f"a/{rel}", f"b/{rel}"))
    return diff, None


def dispatch_id_for(edit_id: object, diff: str) -> str:
    return f"edit-{edit_id}-{hashlib.sha256(diff.encode('utf-8')).hexdigest()[:8]}"


def instruction_for(edit: dict, diff: str) -> str:
    return (
        f"# Apply suggested edit #{edit.get('id')} to {edit['target']}\n\n"
        "Apply exactly this diff, change nothing else, open a PR.\n\n"
        f"```diff\n{diff}```\n"
    )


def stage_edit(edit: dict, diff: str, *, project_id: str, data_dir: Path, stage: Optional[Callable] = None) -> str:
    """Stage one bundle for the edit and return its dispatch id; an already staged bundle is reused."""
    dispatch_id = dispatch_id_for(edit.get("id"), diff)
    if (Path(data_dir) / "dispatches" / "pending" / dispatch_id / "dispatch-spec.json").is_file():
        return dispatch_id
    if stage is None:
        from dispatch_bridge import stage_spec_bundle as stage
    stage(
        instruction_text=instruction_for(edit, diff), dispatch_id=dispatch_id, role="backend-developer",
        target_slot="T1", project_id=project_id, provider="claude", model="sonnet",
        task_class="04_documentation", dispatch_paths=({"path": edit["target"], "access": "read_write"},),
        data_dir=Path(data_dir),
    )
    return dispatch_id


def iter_ledger(path: Path) -> Iterator[dict]:
    """Every JSON object of the ledger. Missing file, unreadable file or a corrupt complete line raises."""
    try:
        raw_lines = Path(path).read_bytes().split(b"\n")
    except OSError as exc:
        raise LedgerUnreadable(f"{Path(path).name} cannot be read: {exc}") from exc
    # the last element is "" after a final newline, else a writer mid-append: skipped, not evidence
    for n, raw in enumerate(raw_lines[:-1], 1):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise LedgerUnreadable(f"{Path(path).name} line {n} is not JSON: {exc}") from exc
        if not isinstance(obj, dict):
            raise LedgerUnreadable(f"{Path(path).name} line {n} is not an object")
        yield obj


def merge_receipts(ledger: Path, project_id: str) -> Dict[str, dict]:
    """``dispatch_id -> pr_merged receipt`` for this project; exact match on event, conclusion and project."""
    out: Dict[str, dict] = {}
    for rec in iter_ledger(ledger):
        if rec.get("event_type") != "pr_merged" or rec.get("conclusion") != "merged":
            continue
        if rec.get("project_id") != project_id:
            continue
        did = rec.get("dispatch_id")
        if isinstance(did, str) and did:
            out[did] = rec
    return out


def applied_ref(rec: dict) -> Optional[str]:
    pr, sha = rec.get("pr_number"), rec.get("merge_sha")
    if isinstance(pr, bool) or not isinstance(pr, int) or not isinstance(sha, str) or len(sha) < 12:
        return None
    return f"#{pr}@{sha[:12]}"


def _ids(raw: object) -> Optional[List[str]]:
    try:
        ids = json.loads(raw) if isinstance(raw, str) else None
    except json.JSONDecodeError:
        return None
    if not isinstance(ids, list) or not all(isinstance(i, str) and i for i in ids):
        return None
    return ids


def edit_source_text(edit: dict) -> str:
    """The rule text the edit adds: what the antipattern row's description carries."""
    return re.sub(r"^\s*-\s*", "", (edit.get("content") or "").strip())


def _parse_ts(raw: object) -> Optional[datetime]:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        ts = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=_UTC)


def _first_receipt_times(ledger: Path, project_id: str, wanted: set) -> Dict[str, datetime]:
    """``dispatch_id -> earliest receipt time`` for the wanted ids of this project.

    Reads the ``timestamp`` field of every receipt with that exact ``dispatch_id`` and ``project_id``; a receipt
    whose timestamp is missing or unparsable is not evidence and is skipped. Raises ``LedgerUnreadable``.
    """
    first: Dict[str, datetime] = {}
    for rec in iter_ledger(ledger):
        did = rec.get("dispatch_id")
        if rec.get("project_id") != project_id or not isinstance(did, str) or did not in wanted:
            continue
        ts = _parse_ts(rec.get("timestamp"))
        if ts is not None and (did not in first or ts < first[did]):
            first[did] = ts
    return first


def count_dispatches_in_window(db_path: Path, project_id: str, source_text: str,
                               start: datetime, end: datetime, ledger: Optional[Path] = None) -> dict:
    """Distinct dispatches that showed the pattern in ``[start, end)``: ``{status, count, reason, source}``.

    A dispatch of a matching row's ``source_dispatch_ids`` counts only when the receipt ledger ``ledger`` holds a
    receipt for that exact ``dispatch_id`` and ``project_id`` whose earliest ``timestamp`` lies in the window. The
    row's ``last_seen`` only prefilters (a row last seen before ``start`` holds no in-window occurrence); it is never
    the evidence. Unknown (never 0) when the db or table cannot be read, no row matches, a ``last_seen`` or the ids
    cannot be parsed, the ledger is missing or unreadable, or an id has no timestamped receipt (the reason names how
    many).
    """
    base = {"source": source_text[:200], "window_start": start.isoformat().replace("+00:00", "Z"),
            "window_end": end.isoformat().replace("+00:00", "Z")}

    def unknown(reason: str) -> dict:
        return {**base, "status": "unknown", "count": None, "reason": reason}

    if not source_text:
        return unknown("edit has no source text")
    if not Path(db_path).is_file():
        return unknown(f"{Path(db_path).name} not found")
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT source_dispatch_ids, last_seen FROM antipatterns WHERE project_id = ? "
                "AND (description = ? OR title = ?)", (project_id, source_text, source_text)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return unknown(f"antipatterns unreadable: {exc}")
    if not rows:
        return unknown("no antipattern row matches the edit")
    candidates: set = set()
    for raw_ids, last_seen in rows:
        ts = _parse_ts(str(last_seen) if last_seen is not None else None)
        if ts is None:
            return unknown(f"last_seen unparsable: {last_seen!r}")
        if ts < start:
            continue
        ids = _ids(raw_ids)
        if ids is None:
            return unknown("source_dispatch_ids unreadable")
        candidates.update(ids)
    if not candidates:
        return {**base, "status": "ok", "count": 0, "reason": None}
    if ledger is None:
        return unknown("no receipt ledger given to date the dispatches")
    try:
        first = _first_receipt_times(Path(ledger), project_id, candidates)
    except LedgerUnreadable as exc:
        return unknown(f"receipt ledger unreadable: {exc}")
    missing = len(candidates - set(first))
    if missing:
        return unknown(f"{missing} of {len(candidates)} source dispatch ids have no timestamped receipt in the ledger")
    return {**base, "status": "ok", "count": sum(1 for t in first.values() if start <= t < end), "reason": None}


def baseline_for(edit: dict, project_id: str, db_path: Path, applied_at: datetime,
                 ledger: Optional[Path] = None) -> dict:
    start = applied_at - timedelta(days=REMEASURE_DAYS)
    return {"window_days": REMEASURE_DAYS,
            **count_dispatches_in_window(db_path, project_id, edit_source_text(edit), start, applied_at, ledger)}
