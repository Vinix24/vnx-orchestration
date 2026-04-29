#!/usr/bin/env python3
"""Receipt-driven reconciliation of dispatcher's active/ directory.

Replaces the mtime-only "stuck file" heuristic in dispatcher_v8_minimal.sh.
That heuristic moved any *.md older than 60 minutes from active/ to
completed/, which silently misclassified legitimate long-running tasks
(file mtime is set at delivery time and never refreshed) as completed and
hid live work from T0 state.

Reconciliation rules
--------------------
- Dispatch has a matching receipt in receipts/processed/  → move to completed/
- No receipt + age >= stale_hours                         → orphan (logged, file stays)
- Otherwise                                               → skipped (file stays)

Receipts are scanned by reading every JSON file under
``receipts/processed/`` once and indexing by ``dispatch_id`` (matches the
existing janitor in ``check_active_drain.py``).

CLI
---
    python3 active_dispatch_janitor.py \
        --active-dir <dir> --completed-dir <dir> \
        --receipts-processed-dir <dir> [--stale-hours N] [--json]

Exit codes
----------
0  reconciliation finished (possibly with orphans)
1  one or more move operations failed
2  bad CLI args
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


@dataclass(frozen=True)
class ReconcileResult:
    dispatch_id: str
    action: str   # "completed" | "orphan" | "skipped" | "error"
    reason: str


def build_receipt_index(receipts_processed_dir: Path) -> frozenset[str]:
    """Return the set of dispatch_ids found in receipts/processed/*.json."""
    if not receipts_processed_dir.is_dir():
        return frozenset()
    ids: set[str] = set()
    for path in receipts_processed_dir.iterdir():
        if path.suffix != ".json" or not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        did = data.get("dispatch_id", "")
        if isinstance(did, str) and did and did != "unknown":
            ids.add(did)
    return frozenset(ids)


def _dispatch_id_from_filename(name: str) -> str:
    if name.endswith(".md"):
        return name[: -len(".md")]
    return name


def reconcile_active(
    active_dir: Path,
    completed_dir: Path,
    receipts_processed_dir: Path,
    stale_hours: float = 24.0,
    now_ts: Optional[float] = None,
) -> list[ReconcileResult]:
    """Reconcile active/*.md files against the receipt index.

    Never moves a file out of active/ purely on age — only receipt evidence
    promotes a file to completed/. Orphans (no receipt, age >= stale_hours)
    are surfaced via the result list so callers can log them, but the file
    stays in active/ until a human or higher-level janitor decides.
    """
    if not active_dir.is_dir():
        return []
    receipt_ids = build_receipt_index(receipts_processed_dir)
    now = time.time() if now_ts is None else now_ts
    stale_seconds = max(0.0, stale_hours) * 3600.0
    results: list[ReconcileResult] = []
    for path in sorted(active_dir.iterdir()):
        if not path.is_file() or path.suffix != ".md":
            continue
        did = _dispatch_id_from_filename(path.name)
        if did in receipt_ids:
            try:
                completed_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(completed_dir / path.name))
                results.append(ReconcileResult(did, "completed", "receipt found"))
            except (OSError, shutil.Error) as exc:
                results.append(ReconcileResult(did, "error", f"move failed: {exc}"))
            continue
        try:
            age = now - path.stat().st_mtime
        except OSError as exc:
            results.append(ReconcileResult(did, "error", f"stat failed: {exc}"))
            continue
        if stale_seconds and age >= stale_seconds:
            results.append(
                ReconcileResult(did, "orphan", f"no receipt, age {age / 3600.0:.1f}h >= {stale_hours:.1f}h")
            )
        else:
            results.append(
                ReconcileResult(did, "skipped", f"no receipt, age {age / 3600.0:.2f}h < {stale_hours:.1f}h")
            )
    return results


def _format_human(results: Iterable[ReconcileResult]) -> str:
    return "\n".join(f"{r.action.upper():10s} {r.dispatch_id}  ({r.reason})" for r in results)


def _format_json(results: Iterable[ReconcileResult]) -> str:
    return json.dumps(
        [{"dispatch_id": r.dispatch_id, "action": r.action, "reason": r.reason} for r in results],
        separators=(",", ":"),
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--active-dir", required=True)
    parser.add_argument("--completed-dir", required=True)
    parser.add_argument("--receipts-processed-dir", required=True)
    parser.add_argument(
        "--stale-hours",
        type=float,
        default=24.0,
        help="Age (hours) above which a receiptless dispatch is reported as orphan (default: 24).",
    )
    parser.add_argument("--json", action="store_true", help="Emit results as JSON to stdout.")
    args = parser.parse_args(argv)

    results = reconcile_active(
        Path(args.active_dir),
        Path(args.completed_dir),
        Path(args.receipts_processed_dir),
        stale_hours=args.stale_hours,
    )
    if args.json:
        print(_format_json(results))
    else:
        if results:
            print(_format_human(results))
    return 1 if any(r.action == "error" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
