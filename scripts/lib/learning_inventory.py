"""Read-only inventory behind ``vnx learning status`` (loop D4): the failure-class
corpus of the receipt window, the three operator queues with the age of the oldest
pending entry, the three access paths, and the recorded decisions plus queue mode.
Every reader is read-only and fail-closed: a missing, ``None``, unreadable or
unknown value is reported as such, never counted as a clean zero or read as full
coverage. No database here.
"""
from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from contract_invalid_window import is_contract_invalid
from learning_failure_class import FAILURE_CLASSES, classify_failure
from prevention_rule_store import DecisionsUnreadable, QueueUnreadable, decisions, load_queue
from receipt_outcome import noise_reason
CORPUS_WINDOW_HOURS = 24
RECEIPTS_FILENAME = "t0_receipts.ndjson"
ANTIPATTERNS_PATH = Path(".claude") / "rules" / "antipatterns.md"
# (key, filename, list field, timestamp fields of one entry). ``queued_at`` is what
# the archival writers stamp, ``suggested_at`` what the edit generator stamps.
# No stamp means no age, never an age of zero.
QUEUE_SPECS: Tuple[Tuple[str, str, str, Tuple[str, ...]], ...] = (
    ("proposals", "pending_rules.json", "pending_rules", ("created_at",)),
    ("archival", "pending_archival.json", "pending_archival", ("queued_at", "created_at")),
    ("suggest-edits", "pending_edits.json", "edits", ("suggested_at", "created_at")),
)
def _parse_ts(value: Any) -> Optional[datetime]:
    """Strict ISO-8601 to aware UTC; None for absent, non-string or malformed."""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
def _queues_ts(entry: Dict[str, Any], keys: Sequence[str]) -> Optional[datetime]:
    for key in keys:
        stamp = _parse_ts(entry.get(key))
        if stamp is not None:
            return stamp
    return None
def corpus_summary(state_dir: Path, project_id: str, *, now: Optional[datetime] = None,
                   window_hours: int = CORPUS_WINDOW_HOURS) -> Dict[str, Any]:
    """Failure-class distribution of one project's receipt window. A receipt is
    noise when ``noise_reason`` names it; an unparseable timestamp is
    ``unreadable`` and never classified. ``coverage`` is None with no failure
    group: no evidence is not full coverage."""
    from learning_loop import _FAILURE_STATUSES  # late: one definition, light import
    moment = now or datetime.now(timezone.utc)
    start = moment - timedelta(hours=window_hours)
    path = Path(state_dir) / RECEIPTS_FILENAME
    summary: Dict[str, Any] = {
        "path": str(path), "window_start": start.isoformat(), "window_hours": window_hours,
        "receipts_read": 0, "noise": 0, "noise_reasons": {}, "unreadable": 0,
        "dispatches": 0, "class_counts": {name: 0 for name in sorted(FAILURE_CLASSES)},
        "coverage": None, "error": None,
    }
    if not path.exists():
        summary["error"] = "missing"
        return summary
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        summary["error"] = f"unreadable: {exc}"
        return summary
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        summary["receipts_read"] += 1
        try:
            receipt = json.loads(line)
        except ValueError:
            receipt = None
        if not isinstance(receipt, dict):
            summary["unreadable"] += 1
            continue
        reason = noise_reason(receipt, project_id)
        if reason is not None:
            summary["noise"] += 1
            summary["noise_reasons"][reason] = summary["noise_reasons"].get(reason, 0) + 1
            continue
        stamp = _parse_ts(receipt.get("timestamp"))
        if stamp is None:
            summary["unreadable"] += 1
            continue
        status = str(receipt.get("status") or "").strip().lower()
        if (stamp < start or stamp > moment) or (
                status not in _FAILURE_STATUSES and not is_contract_invalid(receipt)):
            continue
        raw_id = receipt.get("dispatch_id")
        key = str(raw_id).strip() if isinstance(raw_id, (str, int)) else ""
        groups.setdefault(key or f"receipt:{number}", []).append(receipt)
    summary["dispatches"] = len(groups)
    for group in groups.values():
        summary["class_counts"][classify_failure(group)] += 1
    if groups:
        unclassified = summary["class_counts"]["unclassified"]
        summary["coverage"] = round((len(groups) - unclassified) / len(groups), 4)
    return summary
def _read_queue_file(path: Path, field: str) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """The list under ``field``, or None plus the reason it cannot be read."""
    if not path.exists():
        return [], None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"unreadable: {exc}"
    if not isinstance(data, dict) or not isinstance(data.get(field), list):
        return None, f"not an object with a {field!r} list"
    entries = data[field]
    if not all(isinstance(entry, dict) for entry in entries):
        return None, f"{field!r} holds an entry that is not an object"
    return entries, None
def _queue_summary(path: Path, entries: Optional[Sequence[Dict[str, Any]]], error: Optional[str],
                   keys: Sequence[str], moment: datetime) -> Dict[str, Any]:
    result: Dict[str, Any] = {"path": str(path), "pending": None, "oldest": None,
                              "age_days": None, "without_timestamp": 0, "error": error}
    if entries is None:
        return result
    pending = [entry for entry in entries if entry.get("status") == "pending"]
    stamps = [_queues_ts(entry, keys) for entry in pending]
    result["without_timestamp"] = sum(1 for stamp in stamps if stamp is None)
    stamps = [stamp for stamp in stamps if stamp is not None]
    result["pending"] = len(pending)
    if stamps:
        result["oldest"] = min(stamps).isoformat()
        result["age_days"] = (moment - min(stamps)).days
    return result
def queues_summary(state_dir: Path, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """The three queues. A missing file is an empty queue; a malformed one is an
    ``error`` with ``pending`` None, never a zero."""
    state_dir = Path(state_dir)
    moment = now or datetime.now(timezone.utc)
    result: Dict[str, Any] = {}
    specs = QUEUE_SPECS[0]
    try:
        result["proposals"] = _queue_summary(
            state_dir / specs[1], load_queue(state_dir), None, specs[3], moment)
    except QueueUnreadable as exc:
        result["proposals"] = _queue_summary(state_dir / specs[1], None, str(exc), specs[3], moment)
    for key, filename, field, keys in QUEUE_SPECS[1:]:
        path = state_dir / filename
        entries, error = _read_queue_file(path, field)
        result[key] = _queue_summary(path, entries, error, keys, moment)
    return result
def access_summary(state_dir: Path, project_root: Path) -> Dict[str, Any]:
    """The three queue paths with the command that leaves each, and the file an
    accepted suggest-edit would create."""
    state_dir = Path(state_dir)
    antipatterns = Path(project_root) / ANTIPATTERNS_PATH
    return {
        "paths": [
            {"queue": "proposals", "path": str(state_dir / "pending_rules.json"),
             "command": "vnx learning decide-rule"},
            {"queue": "archival", "path": str(state_dir / "pending_archival.json"),
             "command": "vnx learning approve <id> --approval-id <id> --reason <text>"},
            {"queue": "suggest-edits", "path": str(state_dir / "pending_edits.json"),
             "command": "vnx suggest accept <ids>"},
        ],
        "antipatterns_path": str(antipatterns),
        "antipatterns_exists": antipatterns.exists(),
    }
def decisions_summary(state_dir: Path, *, queue_mode: bool) -> Dict[str, Any]:
    """Recorded decisions per kind. An unreadable record file is an error, not an
    empty history."""
    try:
        records = decisions(Path(state_dir))
    except DecisionsUnreadable as exc:
        return {"counts": None, "total": None, "queue_mode": queue_mode, "error": str(exc)}
    counts: Dict[str, int] = {}
    for record in records:
        kind = str(record.get("decision") or "")
        counts[kind] = counts.get(kind, 0) + 1
    return {"counts": counts, "total": len(records), "queue_mode": queue_mode, "error": None}
def _queue_mode() -> bool:
    from learning_loop import queue_enabled  # late: this module stays import-light
    return queue_enabled()
def _print_corpus(summary: Dict[str, Any], out: Callable[[str], None]) -> None:
    if summary["error"] == "missing":
        out(f"corpus: no receipt stream at {summary['path']} (coverage unmeasured)")
        return
    if summary["error"]:
        out(f"corpus: UNREADABLE {summary['path']} ({summary['error']})")
        return
    noise = ", ".join(f"{name}={count}" for name, count in sorted(summary["noise_reasons"].items()))
    noise_note = f", noise {summary['noise']}" + (f" ({noise})" if noise else "")
    out(f"corpus: {summary['dispatches']} failure dispatches in the last "
        f"{summary['window_hours']}h ({summary['receipts_read']} receipts read"
        f"{noise_note}, {summary['unreadable']} unreadable)")
    classes = " ".join(f"{name}={count}" for name, count in sorted(summary["class_counts"].items()) if count)
    out(f"  classes: {classes or 'none'}")
    if summary["coverage"] is None:
        out("  coverage: unmeasured (no failure dispatch in the window)")
    else:
        unclassified = summary["class_counts"]["unclassified"]
        out(f"  coverage: {summary['coverage'] * 100:.2f}% classified "
            f"({unclassified} of {summary['dispatches']} unclassified)")
def _print_queues(summary: Dict[str, Any], out: Callable[[str], None]) -> None:
    out("queues:")
    for key, _, _, _ in QUEUE_SPECS:
        entry = summary[key]
        if entry["error"]:
            out(f"  {key}: UNREADABLE {entry['path']} ({entry['error']})")
        elif not Path(entry["path"]).exists():
            out(f"  {key}: 0 pending (file missing)")
        elif entry["pending"] == 0:
            out(f"  {key}: 0 pending, no pending entry so no age")
        else:
            age = (f"oldest {entry['age_days']}d ({entry['oldest']})"
                   if entry["age_days"] is not None else "oldest age unknown")
            if entry["without_timestamp"]:
                age += f" (+{entry['without_timestamp']} without timestamp)"
            out(f"  {key}: {entry['pending']} pending, {age}")
def _print_access(summary: Dict[str, Any], out: Callable[[str], None]) -> None:
    out("access:")
    for entry in summary["paths"]:
        out(f"  {entry['queue']:<13} {entry['path']} -> {entry['command']}")
    note = (f"{summary['antipatterns_path']} exists" if summary["antipatterns_exists"] else
            f"{summary['antipatterns_path']} does not exist; `vnx suggest accept` would create it")
    out(f"  note: {note}")
def _print_decisions(summary: Dict[str, Any], out: Callable[[str], None]) -> None:
    mode = "on" if summary["queue_mode"] else "off"
    if summary["error"]:
        out(f"decisions: UNREADABLE ({summary['error']}); queue mode: {mode}")
    elif summary["total"] == 0:
        out(f"decisions: none recorded; queue mode: {mode}")
    else:
        counts = " ".join(f"{kind}={count}" for kind, count in sorted(summary["counts"].items()))
        out(f"decisions: {counts} ({summary['total']} records); queue mode: {mode}")
def print_status_blocks(state_dir: Path, project_root: Path, *, project_id: str,
                        now: Optional[datetime] = None, out: Callable[[str], None] = print) -> None:
    """Print the four ``vnx learning status`` blocks of loop D4."""
    moment = now or datetime.now(timezone.utc)
    _print_corpus(corpus_summary(state_dir, project_id, now=moment), out)
    _print_queues(queues_summary(state_dir, now=moment), out)
    _print_access(access_summary(state_dir, project_root), out)
    _print_decisions(decisions_summary(state_dir, queue_mode=_queue_mode()), out)
