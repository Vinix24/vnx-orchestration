#!/usr/bin/env python3
"""Session garbage collector for VNX runtime state artifacts.

Default behavior is safe:
- Dry-run only (no deletion) unless --apply is provided
- 14-day retention window
- Audit logs and critical receipt files are always preserved
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from cli_output import emit_human, emit_json, parse_human_flag

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

try:
    from vnx_paths import ensure_env
except Exception as exc:
    raise SystemExit(f"Failed to load vnx_paths: {exc}")

EXIT_OK = 0
EXIT_VALIDATION = 10
EXIT_IO = 20

DEFAULT_RETENTION_DAYS = 14

CRITICAL_RECEIPT_NAMES = {
    "t0_receipts.ndjson",
    "shadow_receipts.ndjson",
    "unified_receipts.ndjson",
    "unified_ack_receipts.ndjson",
    "receipts.ndjson",
}

PROTECTED_PATH_SEGMENTS = {
    "dispatches/pending",
    "dispatches/active",
    "dispatches/completed",
    "dispatches/accepted",
}


@dataclass
class Candidate:
    path: Path
    rel_path: str
    size_bytes: int
    age_days: float


def _is_audit_log(path: Path) -> bool:
    lower = path.name.lower()
    return "audit" in lower or path.suffix.lower() == ".jsonl"


def _is_critical_receipt(path: Path) -> bool:
    name_lower = path.name.lower()
    if name_lower in CRITICAL_RECEIPT_NAMES:
        return True
    if "receipt" in name_lower and name_lower.endswith(".ndjson"):
        return True
    return False


def _is_protected_by_path(path: Path) -> bool:
    normalized = str(path).replace("\\", "/").lower()
    return any(segment in normalized for segment in PROTECTED_PATH_SEGMENTS)


def protection_reason(path: Path) -> Optional[str]:
    if _is_protected_by_path(path):
        return "completed_dispatch_path"
    if _is_audit_log(path):
        return "audit_log"
    if _is_critical_receipt(path):
        return "critical_receipt"
    return None


def collect_candidates(state_dir: Path, cutoff_epoch: float) -> Dict[str, object]:
    candidates: List[Candidate] = []
    metrics = {
        "state_dir": str(state_dir),
        "scanned_files": 0,
        "scanned_bytes": 0,
        "eligible_files": 0,
        "eligible_bytes": 0,
        "skipped_recent": 0,
        "skipped_protected": 0,
        "skip_reasons": {},
        "errors": [],
        "candidates": candidates,
    }

    if not state_dir.exists():
        metrics["errors"].append(f"state directory does not exist: {state_dir}")
        return metrics

    if not state_dir.is_dir():
        metrics["errors"].append(f"state path is not a directory: {state_dir}")
        return metrics

    now = time.time()
    for path in state_dir.rglob("*"):
        if not path.is_file():
            continue

        try:
            stat = path.stat()
        except OSError as exc:
            metrics["errors"].append(f"stat failed for {path}: {exc}")
            continue

        metrics["scanned_files"] += 1
        metrics["scanned_bytes"] += stat.st_size

        reason = protection_reason(path)
        if reason:
            metrics["skipped_protected"] += 1
            metrics["skip_reasons"][reason] = metrics["skip_reasons"].get(reason, 0) + 1
            continue

        if stat.st_mtime >= cutoff_epoch:
            metrics["skipped_recent"] += 1
            continue

        age_days = max(0.0, (now - stat.st_mtime) / 86400.0)
        rel_path = os.path.relpath(path, start=state_dir)
        candidate = Candidate(
            path=path,
            rel_path=rel_path,
            size_bytes=stat.st_size,
            age_days=round(age_days, 2),
        )
        candidates.append(candidate)

        metrics["eligible_files"] += 1
        metrics["eligible_bytes"] += stat.st_size

    return metrics


def delete_candidates(candidates: List[Candidate]) -> Dict[str, object]:
    deleted_files = 0
    deleted_bytes = 0
    errors: List[str] = []

    for item in candidates:
        try:
            item.path.unlink(missing_ok=True)
            deleted_files += 1
            deleted_bytes += item.size_bytes
        except OSError as exc:
            errors.append(f"delete failed for {item.path}: {exc}")

    return {
        "deleted_files": deleted_files,
        "deleted_bytes": deleted_bytes,
        "delete_errors": errors,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Session cleanup + retention GC")
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_RETENTION_DAYS,
        help=f"Retention in days (default: {DEFAULT_RETENTION_DAYS})",
    )
    parser.add_argument(
        "--state-dir",
        default=None,
        help="State directory to scan (default: VNX_STATE_DIR from environment).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply deletions. Without this flag, command is dry-run.",
    )
    return parser


def run_gc(days: int, state_dir: Path, apply_changes: bool) -> Dict[str, object]:
    started = time.time()
    cutoff = started - (days * 86400)

    scan = collect_candidates(state_dir=state_dir, cutoff_epoch=cutoff)
    candidates: List[Candidate] = scan.pop("candidates")

    deletion = {"deleted_files": 0, "deleted_bytes": 0, "delete_errors": []}
    if apply_changes and candidates:
        deletion = delete_candidates(candidates)

    finished = time.time()
    metrics = {
        "gc": {
            "retention_days": days,
            "dry_run": not apply_changes,
            "state_dir": str(state_dir),
            "started_at_epoch": round(started, 3),
            "finished_at_epoch": round(finished, 3),
            "duration_seconds": round(finished - started, 3),
            "scan": scan,
            "delete": deletion,
            "candidates": [
                {
                    "path": item.rel_path,
                    "bytes": item.size_bytes,
                    "age_days": item.age_days,
                }
                for item in candidates
            ],
        }
    }
    return metrics


def render_human(metrics: Dict[str, object]) -> str:
    gc = metrics["gc"]
    scan = gc["scan"]
    delete = gc["delete"]

    lines = [
        "VNX Session GC Metrics",
        f"Mode: {'dry-run' if gc['dry_run'] else 'apply'}",
        f"Retention: {gc['retention_days']} days",
        f"State dir: {gc['state_dir']}",
        f"Duration: {gc['duration_seconds']}s",
        f"Scanned: {scan['scanned_files']} files ({scan['scanned_bytes']} bytes)",
        f"Eligible: {scan['eligible_files']} files ({scan['eligible_bytes']} bytes)",
        f"Skipped recent: {scan['skipped_recent']}",
        f"Skipped protected: {scan['skipped_protected']}",
        f"Deleted: {delete['deleted_files']} files ({delete['deleted_bytes']} bytes)",
    ]

    skip_reasons = scan.get("skip_reasons", {})
    if skip_reasons:
        lines.append("Protection reasons:")
        for key in sorted(skip_reasons.keys()):
            lines.append(f"- {key}: {skip_reasons[key]}")

    errors = list(scan.get("errors", [])) + list(delete.get("delete_errors", []))
    if errors:
        lines.append("Errors:")
        for err in errors:
            lines.append(f"- {err}")

    candidates = gc.get("candidates", [])
    if candidates:
        lines.append("Candidates:")
        for item in candidates:
            lines.append(
                f"- {item['path']} | age_days={item['age_days']} | bytes={item['bytes']}"
            )

    return "\n".join(lines)


def main() -> int:
    human, argv = parse_human_flag(sys.argv[1:])
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.days < 1:
        payload = {"error": "--days must be >= 1"}
        if human:
            emit_human(payload["error"])
        else:
            emit_json(payload)
        return EXIT_VALIDATION

    paths = ensure_env()
    state_dir = Path(args.state_dir or paths["VNX_STATE_DIR"]).expanduser()
    metrics = run_gc(days=args.days, state_dir=state_dir, apply_changes=args.apply)

    if human:
        emit_human(render_human(metrics))
    else:
        emit_json(metrics)

    gc = metrics["gc"]
    has_errors = bool(gc["scan"]["errors"] or gc["delete"]["delete_errors"])
    return EXIT_IO if has_errors else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
