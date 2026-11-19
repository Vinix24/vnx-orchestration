"""project_id_refusals — the one counter for an unresolved project id (AG-1o).

Two halves:

* :func:`record_project_id_refusal` — called by a context that refused to run
  because no project id could be resolved. It never raises (a hook must not
  break on it), logs ERROR and appends one ``project_id_unresolved`` line to
  ``skip_metrics.ndjson`` in ``anchor_dir``. The anchor is the store directory
  when it is known, otherwise the data home (:func:`data_home`): the parent of
  every store asks no tenant. It reuses ``state_writer.append_locked``, the
  same writer ``dispatch_metadata_db._log_tenant_stamp_skip`` uses.
* :func:`project_id_refusal_section` — read-only; what ``vnx doctor`` shows:
  per context the count of the last 24 hours, both event types, from the
  project's and the data home's ``skip_metrics.ndjson``, plus count and amount
  of ``provider_costs.unattributed.ndjson`` (ADR-094). An unreadable line is
  counted as unreadable, never skipped silently.
"""
from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

SKIP_METRICS_NAME = "skip_metrics.ndjson"
UNATTRIBUTED_LEDGER_NAME = "provider_costs.unattributed.ndjson"
EVENT_UNRESOLVED = "project_id_unresolved"
EVENT_TENANT_SKIP = "tenant_stamp_skip"
WINDOW = timedelta(hours=24)


def data_home() -> Path:
    """The parent of every store: ``$VNX_DATA_HOME``, else ``~/.vnx-data``."""
    override = os.environ.get("VNX_DATA_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".vnx-data"


def record_project_id_refusal(
    context: str,
    anchor_dir: Optional[Path | str],
    reason: str,
    **fields: Any,
) -> None:
    """Log ERROR and count one refusal for ``context``. Never raises."""
    try:
        logger.error(
            "project_id_unresolved: context=%s reason=%s fields=%s",
            context, reason, fields,
        )
    except Exception:  # vnx-silent-except: a recorder must never raise
        return
    try:
        from state_writer import append_locked

        anchor = Path(anchor_dir) if anchor_dir else data_home()
        record = dict(fields)
        record.update({
            "event_type": EVENT_UNRESOLVED,
            "context": context,
            "reason": reason,
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        append_locked(anchor / SKIP_METRICS_NAME, record)
    except Exception as exc:  # vnx-silent-except: a recorder must never raise; the ERROR log above is the contract
        try:
            logger.error("project_id_unresolved counter append failed: %s", exc)
        except Exception:  # vnx-silent-except: a recorder must never raise
            return


def _parse_ts(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _read_records(path: Path) -> "tuple[List[dict], int, Optional[str]]":
    """(records, unreadable line count, read error). Absent file: ([], 0, None)."""
    if not path.exists():
        return [], 0, None
    records: List[dict] = []
    unreadable = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    unreadable += 1
                    continue
                if isinstance(rec, dict):
                    records.append(rec)
                else:
                    unreadable += 1
    except OSError as exc:
        return records, unreadable, str(exc)
    return records, unreadable, None


def _unique_paths(paths: Iterable[Path]) -> List[Path]:
    seen: set = set()
    out: List[Path] = []
    for p in paths:
        try:
            key = p.resolve()
        except OSError:
            key = p
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def project_id_refusal_section(
    data_root: Path | str,
    *,
    home: Optional[Path | str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Counts for ``vnx doctor``: per context, last 24 hours, plus the cost ledger.

    ``data_root`` is the project's store; ``home`` the data home (default
    :func:`data_home`). Returns ``{"contexts": {name: count}, "total": int,
    "unreadable": int, "errors": [str], "unattributed": {"ledger": bool,
    "count": int, "amount_usd": float, "unreadable": int}}``.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - WINDOW
    root = Path(data_root)
    home_dir = Path(home) if home is not None else data_home()
    contexts: Dict[str, int] = {}
    unreadable = 0
    errors: List[str] = []

    sources = _unique_paths(
        p / SKIP_METRICS_NAME for p in (root / "state", root, home_dir)
    )
    for path in sources:
        records, bad, err = _read_records(path)
        unreadable += bad
        if err:
            errors.append(f"{path}: {err}")
        for rec in records:
            event = rec.get("event_type")
            if event not in (EVENT_UNRESOLVED, EVENT_TENANT_SKIP):
                continue
            ts = _parse_ts(rec.get("ts") or rec.get("timestamp"))
            if ts is None:
                unreadable += 1  # a counted line with no usable time is unreadable, not zero
                continue
            if ts < cutoff:
                continue
            context = rec.get("context") or rec.get("table") or "unknown"
            contexts[str(context)] = contexts.get(str(context), 0) + 1

    ledger_path = home_dir / UNATTRIBUTED_LEDGER_NAME
    records, bad, err = _read_records(ledger_path)
    if err:
        errors.append(f"{ledger_path}: {err}")
    count = 0
    amount = 0.0
    for rec in records:
        ts = _parse_ts(rec.get("timestamp") or rec.get("ts"))
        if ts is None:
            bad += 1
            continue
        if ts < cutoff:
            continue
        count += 1
        cost = rec.get("cost_usd_estimate")
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost):
            if cost is not None:
                bad += 1
            continue
        amount += float(cost)

    return {
        "contexts": contexts,
        "total": sum(contexts.values()),
        "unreadable": unreadable,
        "errors": errors,
        "unattributed": {
            "ledger": ledger_path.exists(),
            "count": count,
            "amount_usd": round(amount, 6),
            "unreadable": bad,
        },
    }


def format_section(section: Dict[str, Any]) -> "tuple[str, str]":
    """(status, one-line detail) for a doctor check. WARN on any count or unreadable line."""
    un = section["unattributed"]
    parts: List[str] = []
    if section["contexts"]:
        parts.append(", ".join(f"{k}={v}" for k, v in sorted(section["contexts"].items())))
    else:
        parts.append("no refusals in 24h")
    if un["ledger"]:
        parts.append(f"unattributed cost: {un['count']} lines, ${un['amount_usd']:.4f}")
    else:
        parts.append("unattributed cost: 0 (no ledger)")
    bad = section["unreadable"] + un["unreadable"]
    if bad:
        parts.append(f"{bad} unreadable line(s)")
    parts.extend(section["errors"])
    dirty = bool(section["total"] or un["count"] or bad or section["errors"])
    return ("warn" if dirty else "pass"), "; ".join(parts)
