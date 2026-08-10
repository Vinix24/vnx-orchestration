#!/usr/bin/env python3
"""dispatch_cleanup.py — Governed cleanup for stale dispatch bundles (OI-1072).

Scans dispatches/pending/ for directory bundles (dispatch-spec.json +
instruction.md) that were staged but never cleaned up after the door processed
them.  Each bundle is classified by age and whether a matching receipt exists
in the ledger.  The default is a dry-run report — nothing is moved or deleted
without an explicit ``--apply`` flag.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


# ── classification ─────────────────────────────────────────────────────────

@dataclass
class BundleEntry:
    """Outcome for a single pending dispatch bundle."""

    dispatch_id: str
    bundle_dir: str
    age_days: float
    has_receipt: bool
    has_instruction: bool
    has_spec: bool
    project_id: str = ""
    role: str = ""
    gate: str = ""
    target_slot: str = ""
    classification: str = ""  # "receipt-found", "stale-no-receipt", "recent-no-receipt", "empty", "error"
    action: str = ""  # "move-to-completed", "move-to-abandoned", "skip", "error"
    error: str = ""


@dataclass
class CleanupReport:
    """Aggregate outcome of a cleanup run."""

    entries: List[BundleEntry] = field(default_factory=list)
    dry_run: bool = True
    timestamp: str = ""

    @property
    def counts(self) -> Dict[str, int]:
        c: Dict[str, int] = {}
        for e in self.entries:
            c[e.classification] = c.get(e.classification, 0) + 1
        return c

    @property
    def action_counts(self) -> Dict[str, int]:
        c: Dict[str, int] = {}
        for e in self.entries:
            c[e.action] = c.get(e.action, 0) + 1
        return c


# ── path resolution ────────────────────────────────────────────────────────

def _resolve_data_dir() -> Path:
    """Resolve VNX_DATA_DIR via canonical resolver with env fallbacks."""
    env = os.environ.get("VNX_DATA_DIR", "")
    if env:
        return Path(env)
    try:
        from vnx_paths import resolve_paths
        return Path(resolve_paths()["VNX_DATA_DIR"])
    except Exception:
        pass
    try:
        result = __import__("subprocess").run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return Path(result.stdout.strip()) / ".vnx-data"
    except Exception:
        pass
    return Path.cwd() / ".vnx-data"


def _resolve_state_dir() -> Path:
    """Resolve VNX_STATE_DIR."""
    env = os.environ.get("VNX_STATE_DIR", "")
    if env:
        return Path(env)
    return _resolve_data_dir() / "state"


# ── receipt index ──────────────────────────────────────────────────────────

def _build_receipt_index(state_dir: Path) -> Dict[str, bool]:
    """Build a set of dispatch_ids that have at least one receipt in t0_receipts.ndjson.

    Returns a dict mapping dispatch_id → True for O(1) lookup.
    """
    receipt_file = state_dir / "t0_receipts.ndjson"
    index: Dict[str, bool] = {}
    if not receipt_file.exists():
        return index

    try:
        with open(receipt_file, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    did = rec.get("dispatch_id", "")
                    if did:
                        index[did] = True
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass

    return index


# ── bundle scanning ────────────────────────────────────────────────────────

def _read_spec(spec_file: Path) -> Dict[str, Any]:
    """Read dispatch-spec.json; return empty dict on any error."""
    try:
        return json.loads(spec_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def scan_pending(data_dir: Path, state_dir: Path) -> List[BundleEntry]:
    """Scan dispatches/pending/ for stale directory bundles.

    A bundle is a subdirectory containing dispatch-spec.json and/or instruction.md.
    Returns a list of BundleEntry objects, one per bundle found.
    """
    pending_dir = data_dir / "dispatches" / "pending"
    if not pending_dir.is_dir():
        return []

    receipt_index = _build_receipt_index(state_dir)
    now = datetime.now(timezone.utc)
    entries: List[BundleEntry] = []

    try:
        children = sorted(pending_dir.iterdir())
    except OSError:
        return []

    for child in children:
        if not child.is_dir():
            continue

        dispatch_id = child.name
        spec_file = child / "dispatch-spec.json"
        instr_file = child / "instruction.md"

        has_spec = spec_file.is_file()
        has_instruction = instr_file.is_file()

        # Not a staged bundle — skip
        if not has_spec and not has_instruction:
            continue

        # Read spec metadata
        spec: Dict[str, Any] = {}
        if has_spec:
            spec = _read_spec(spec_file)

        # Compute age from directory mtime
        try:
            mtime = child.stat().st_mtime
            age_days = (now.timestamp() - mtime) / 86400.0
        except OSError:
            age_days = 0.0

        has_receipt = receipt_index.get(dispatch_id, False)
        project_id = str(spec.get("project_id", ""))
        role = str(spec.get("role", ""))
        gate = str(spec.get("gate", ""))
        target_slot = str(spec.get("target_slot", ""))

        # Classify
        error = ""
        if not has_spec and not has_instruction:
            classification = "empty"
            action = "error"
            error = "no spec or instruction"
        elif has_receipt:
            classification = "receipt-found"
            action = "move-to-completed"
        elif age_days >= 7:
            classification = "stale-no-receipt"
            action = "move-to-abandoned"
        else:
            classification = "recent-no-receipt"
            action = "skip"

        entries.append(BundleEntry(
            dispatch_id=dispatch_id,
            bundle_dir=str(child),
            age_days=round(age_days, 1),
            has_receipt=has_receipt,
            has_instruction=has_instruction,
            has_spec=has_spec,
            project_id=project_id,
            role=role,
            gate=gate,
            target_slot=target_slot,
            classification=classification,
            action=action,
            error=error,
        ))

    return entries


# ── actions ────────────────────────────────────────────────────────────────

def _move_bundle(entry: BundleEntry, dest_dir: Path, dry_run: bool = True) -> bool:
    """Move a bundle directory to dest_dir.

    Returns True on success (or dry-run simulation), False on error.
    """
    src = Path(entry.bundle_dir)
    if not src.exists():
        entry.error = "source directory missing"
        return False

    if dry_run:
        return True

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name

    try:
        import shutil
        shutil.move(str(src), str(dest))
        entry.bundle_dir = str(dest)
        return True
    except OSError as exc:
        entry.error = f"move failed: {exc}"
        return False


def execute_cleanup(
    entries: List[BundleEntry],
    data_dir: Path,
    dry_run: bool = True,
) -> CleanupReport:
    """Execute the classified actions for each bundle entry.

    In dry_run mode, reports what would happen without moving anything.
    In apply mode, moves bundles to completed/ or abandoned/.
    """
    report = CleanupReport(
        entries=entries,
        dry_run=dry_run,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )

    dispatch_dir = data_dir / "dispatches"
    completed_dir = dispatch_dir / "completed"
    abandoned_dir = dispatch_dir / "abandoned"

    for entry in entries:
        if entry.action == "move-to-completed":
            _move_bundle(entry, completed_dir, dry_run=dry_run)
        elif entry.action == "move-to-abandoned":
            _move_bundle(entry, abandoned_dir, dry_run=dry_run)
        # "skip" and "error" are no-ops

    return report


# ── reporting ──────────────────────────────────────────────────────────────

def format_report(report: CleanupReport) -> str:
    """Format a human-readable report."""
    total = len(report.entries)
    lines: List[str] = []

    header = "DRY-RUN" if report.dry_run else "APPLIED"
    lines.append(f"=== Dispatch Cleanup Report ({header}) ===")
    lines.append(f"Timestamp: {report.timestamp}")
    lines.append(f"Pending directory: {_resolve_data_dir() / 'dispatches' / 'pending'}")
    lines.append(f"Total bundles scanned: {total}")
    lines.append("")

    lines.append("## Classification Distribution")
    for cls, count in sorted(report.counts.items()):
        lines.append(f"  {cls}: {count}")
    lines.append("")

    lines.append("## Planned Actions")
    for action, count in sorted(report.action_counts.items()):
        lines.append(f"  {action}: {count}")
    lines.append("")

    # Per-bundle detail for actionable items
    actionable = [e for e in report.entries if e.action not in ("skip",)]
    if actionable:
        lines.append("## Actionable Bundles")
        for e in actionable:
            age = f"{e.age_days:.0f}d" if e.age_days >= 1 else f"{e.age_days * 24:.0f}h"
            lines.append(
                f"  [{e.action}] {e.dispatch_id}  age={age}  "
                f"role={e.role or '?'}  gate={e.gate or '?'}"
            )
            if e.error:
                lines.append(f"           error: {e.error}")
        lines.append("")

    # Skipped bundles summary
    skipped = [e for e in report.entries if e.action == "skip"]
    if skipped:
        age_vals = [e.age_days for e in skipped if e.age_days > 0]
        youngest = min(age_vals) if age_vals else 0
        oldest = max(age_vals) if age_vals else 0
        lines.append(
            f"## Skipped ({len(skipped)} bundles, age {youngest:.0f}d–{oldest:.0f}d)"
        )
        lines.append(
            "  These bundles have no matching receipt but are less than 7 days old."
        )
        lines.append("  They will be eligible for cleanup once they age past the threshold.")
        lines.append("")

    if report.dry_run:
        lines.append(
            "DRY-RUN: nothing was changed. Re-run with --apply to execute cleanup."
        )
    else:
        moved = sum(
            1 for e in report.entries
            if e.action in ("move-to-completed", "move-to-abandoned") and not e.error
        )
        lines.append(f"APPLIED: {moved} bundles moved. Report the results to T0.")

    return "\n".join(lines)


def format_json(report: CleanupReport) -> str:
    """Format the report as JSON."""
    payload = {
        "timestamp": report.timestamp,
        "dry_run": report.dry_run,
        "total": len(report.entries),
        "counts": report.counts,
        "action_counts": report.action_counts,
        "entries": [
            {
                "dispatch_id": e.dispatch_id,
                "age_days": e.age_days,
                "has_receipt": e.has_receipt,
                "classification": e.classification,
                "action": e.action,
                "role": e.role,
                "gate": e.gate,
                "target_slot": e.target_slot,
                "project_id": e.project_id,
                "error": e.error,
            }
            for e in report.entries
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True)


# ── main ───────────────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dispatch_cleanup",
        description="Governed cleanup for stale dispatch bundles (OI-1072).",
    )
    parser.add_argument(
        "--data-dir", type=Path, default=None,
        help="VNX_DATA_DIR override",
    )
    parser.add_argument(
        "--state-dir", type=Path, default=None,
        help="VNX_STATE_DIR override",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Execute cleanup (default: dry-run only)",
    )
    parser.add_argument(
        "--json", action="store_true", dest="json_output",
        help="Machine-readable JSON output",
    )
    parser.add_argument(
        "--stale-days", type=int, default=7,
        help="Age threshold in days for stale-no-receipt classification (default: 7)",
    )
    args = parser.parse_args(argv)

    data_dir = args.data_dir if args.data_dir else _resolve_data_dir()
    state_dir = args.state_dir if args.state_dir else _resolve_state_dir()
    dry_run = not args.apply

    entries = scan_pending(data_dir, state_dir)

    # Override classification using the stale-days threshold
    stale_seconds = args.stale_days * 86400
    now = datetime.now(timezone.utc)
    for entry in entries:
        if entry.classification == "recent-no-receipt" and entry.age_days >= args.stale_days:
            entry.classification = "stale-no-receipt"
            entry.action = "move-to-abandoned"
        elif entry.classification == "stale-no-receipt" and entry.age_days < args.stale_days:
            entry.classification = "recent-no-receipt"
            entry.action = "skip"

    report = execute_cleanup(entries, data_dir, dry_run=dry_run)

    if args.json_output:
        print(format_json(report))
    else:
        print(format_report(report))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
