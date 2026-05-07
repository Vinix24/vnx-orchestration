#!/usr/bin/env python3
"""Phase 6 P3 — one-shot NDJSON envelope re-stamper.

Walks existing NDJSON files (t0_receipts.ndjson, dispatch_register.ndjson)
for a given project and re-streams each line with the four-tuple envelope
fields (operator_id, project_id, orchestrator_id, agent_id) filled in.

Usage:
    python3 scripts/migrate_phase3_envelope.py --project-id vnx-dev [--dry-run]
    python3 scripts/migrate_phase3_envelope.py --project-id vnx-dev --state-dir /path/to/.vnx-data/state

Locking contract (race-free with concurrent appends during P5 cutover):
- For dispatch_register.ndjson: acquires LOCK_EX on the NDJSON file itself
  (same as dispatch_register._write_event_locked).
- For t0_receipts.ndjson: acquires LOCK_EX on <dir>/append_receipt.lock
  (same as append_receipt_internals.idempotency._write_receipt_under_lock).
Lock is held through the atomic rename so concurrent writers block until
the stamped file is in place.

Idempotent: running twice yields identical output (envelope fields already
present are not overwritten).
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

_SCRIPT_DIR = Path(__file__).resolve().parent
_LIB_DIR = _SCRIPT_DIR / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))


def _resolve_identity(project_id: str) -> Dict[str, Optional[str]]:
    """Resolve the four-tuple for the given project_id."""
    result: Dict[str, Optional[str]] = {
        "operator_id": os.environ.get("VNX_OPERATOR_ID"),
        "project_id": project_id,
        "orchestrator_id": os.environ.get("VNX_ORCHESTRATOR_ID"),
        "agent_id": os.environ.get("VNX_AGENT_ID"),
    }
    try:
        from vnx_identity import try_resolve_identity
        identity = try_resolve_identity()
        if identity is not None:
            result["operator_id"] = result["operator_id"] or identity.operator_id
            result["orchestrator_id"] = result["orchestrator_id"] or identity.orchestrator_id
            result["agent_id"] = result["agent_id"] or identity.agent_id
    except Exception:
        pass
    return result


def _stamp_line(record: Dict[str, Any], envelope: Dict[str, Optional[str]]) -> Dict[str, Any]:
    """Return record with envelope fields added (existing values preserved)."""
    result = dict(record)
    for field in ("operator_id", "project_id", "orchestrator_id", "agent_id"):
        val = envelope.get(field)
        if val and not result.get(field):
            result[field] = val
    return result


def _restamp_ndjson_inplace(
    ndjson_path: Path,
    envelope: Dict[str, Optional[str]],
    *,
    lock_path: Optional[Path] = None,
    dry_run: bool = False,
) -> int:
    """Re-stamp a single NDJSON file with envelope fields. Returns stamped line count.

    Locking: acquires LOCK_EX on a directory-level sentinel (.state.lock) first,
    then on lock_path (if given) or ndjson_path itself. Both locks are held through
    the atomic rename.

    The sentinel coordinates with dispatch_register._write_event_locked so that
    writers that open the NDJSON file before the rename complete will always open
    the new inode (they block on the sentinel, which is released only after rename).
    """
    if not ndjson_path.exists():
        return 0

    sentinel = ndjson_path.parent / ".state.lock"
    effective_lock = lock_path if lock_path is not None else ndjson_path

    with sentinel.open("a+", encoding="utf-8") as _sentinel_fh:
        fcntl.flock(_sentinel_fh.fileno(), fcntl.LOCK_EX)

        with effective_lock.open("a+", encoding="utf-8") as lock_fh:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)

            # Read under lock.
            try:
                content = ndjson_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return 0

            stamped_lines: List[str] = []
            count = 0
            for raw_line in content.splitlines():
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    record = json.loads(stripped)
                except json.JSONDecodeError:
                    stamped_lines.append(stripped)
                    continue
                stamped = _stamp_line(record, envelope)
                stamped_lines.append(json.dumps(stamped, separators=(",", ":"), sort_keys=False))
                count += 1

            if dry_run:
                return count

            new_content = "\n".join(stamped_lines) + ("\n" if stamped_lines else "")

            # Atomic rename under lock — concurrent appenders block until complete.
            fd, tmp_str = tempfile.mkstemp(
                prefix=ndjson_path.name + ".restamp.tmp.",
                dir=str(ndjson_path.parent),
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as tmp_fh:
                    tmp_fh.write(new_content)
                os.replace(tmp_str, str(ndjson_path))
            except Exception:
                try:
                    os.unlink(tmp_str)
                except Exception:
                    pass
                raise

    return count


def resolve_central_data_dir(project_id: str) -> Path:
    """Importable wrapper so tests can monkeypatch at the module level."""
    from vnx_paths import resolve_central_data_dir as _resolve
    return _resolve(project_id)


def restamp_project(
    state_dir: Path,
    project_id: str,
    *,
    also_central: bool = True,
    dry_run: bool = False,
) -> Dict[str, int]:
    """Re-stamp all NDJSON files for a project. Returns {filename: line_count}."""
    envelope = _resolve_identity(project_id)
    results: Dict[str, int] = {}

    # --- dispatch_register.ndjson (self-locked) ---
    dr_path = state_dir / "dispatch_register.ndjson"
    # Lock on the file itself (same as dispatch_register._write_event_locked).
    n = _restamp_ndjson_inplace(dr_path, envelope, lock_path=None, dry_run=dry_run)
    results["dispatch_register.ndjson"] = n

    # --- t0_receipts.ndjson (locked via append_receipt.lock) ---
    receipts_path = state_dir / "t0_receipts.ndjson"
    lock_path = state_dir / "append_receipt.lock"
    n = _restamp_ndjson_inplace(receipts_path, envelope, lock_path=lock_path, dry_run=dry_run)
    results["t0_receipts.ndjson"] = n

    # --- central paths (if they differ from primary) ---
    if also_central:
        try:
            central_state = resolve_central_data_dir(project_id) / "state"
            if central_state.exists() and central_state.resolve() != state_dir.resolve():
                c_dr = central_state / "dispatch_register.ndjson"
                n = _restamp_ndjson_inplace(c_dr, envelope, lock_path=None, dry_run=dry_run)
                results["central/dispatch_register.ndjson"] = n

                c_receipts = central_state / "t0_receipts.ndjson"
                c_lock = central_state / "append_receipt.lock"
                n = _restamp_ndjson_inplace(
                    c_receipts, envelope, lock_path=c_lock, dry_run=dry_run
                )
                results["central/t0_receipts.ndjson"] = n
        except Exception:
            pass

    return results


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Phase 6 P3 one-shot NDJSON envelope re-stamper"
    )
    parser.add_argument("--project-id", required=True, help="Project ID to stamp (e.g. vnx-dev)")
    parser.add_argument(
        "--state-dir",
        default=None,
        help="Override per-project state dir (default: resolved via vnx_paths)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print counts without modifying files",
    )
    args = parser.parse_args(argv)

    if args.state_dir:
        state_dir = Path(args.state_dir).expanduser().resolve()
    else:
        try:
            from vnx_paths import resolve_paths
            state_dir = Path(resolve_paths()["VNX_STATE_DIR"])
        except Exception as exc:
            print(f"ERROR: cannot resolve state dir: {exc}", file=sys.stderr)
            return 1

    mode = "DRY-RUN" if args.dry_run else "LIVE"
    print(f"[migrate_phase3_envelope] {mode} project_id={args.project_id} state_dir={state_dir}")

    try:
        results = restamp_project(state_dir, args.project_id, dry_run=args.dry_run)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    for filename, count in sorted(results.items()):
        verb = "would re-stamp" if args.dry_run else "re-stamped"
        print(f"  {filename}: {verb} {count} lines")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
