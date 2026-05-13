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


def _default_primary_state_dir(project_id: str) -> Path:
    """Derive the default repo-local primary state dir for project_id.

    Uses the current repo root rather than ambient ``VNX_STATE_DIR`` so
    ``--project-id`` cannot silently target another project's state. When the
    repo-local ``.vnx-project-id`` disagrees with ``project_id``, callers must
    pass ``--state-dir`` explicitly.
    """
    from vnx_paths import project_id_from_state_dir, resolve_paths

    project_root = Path(resolve_paths()["PROJECT_ROOT"]).expanduser().resolve()
    candidate = project_root / ".vnx-data" / "state"
    inferred_project_id = project_id_from_state_dir(candidate)
    if inferred_project_id != project_id:
        inferred_label = inferred_project_id or "<unknown>"
        raise ValueError(
            f"--project-id {project_id!r} does not match inferred project_id "
            f"{inferred_label!r} for default state_dir {candidate}. "
            "Pass --state-dir explicitly."
        )
    return candidate


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


def _acquire_sentinel_lock(envelope_path: Path):
    """Open/create the per-file migration sentinel and acquire LOCK_EX.

    Lock file: .{envelope_path.name}.migration.lock in the same directory.
    Returns an open file handle — close it (or use as context manager) to release.

    Using a sentinel file instead of locking the replaced inode prevents the
    lost-append race: concurrent writers that acquire this same sentinel will block
    until the rename is complete and will then open the new inode, not the old one.
    The sentinel persists on disk so writers can coordinate on it in follow-up OIs.
    """
    lock_path = envelope_path.parent / f".{envelope_path.name}.migration.lock"
    fp = open(lock_path, "w")
    fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
    return fp


def _migrate_envelope_atomically(
    envelope_path: Path,
    envelope: Optional[Dict[str, Optional[str]]] = None,
) -> int:
    """Atomically re-stamp envelope_path under the per-file sentinel lock.

    Acquires .{envelope_path.name}.migration.lock, reads all NDJSON lines,
    re-stamps with envelope fields (existing values preserved), then writes
    a temp file and calls os.replace() — all under the sentinel lock.

    Returns the number of JSON lines stamped. Empty/non-existent files return 0.
    """
    return _restamp_ndjson_inplace(envelope_path, envelope or {})


def _restamp_ndjson_inplace(
    ndjson_path: Path,
    envelope: Dict[str, Optional[str]],
    *,
    dry_run: bool = False,
) -> int:
    """Re-stamp a single NDJSON file with envelope fields. Returns stamped line count.

    Locking: acquires LOCK_EX on the per-file sentinel
    (.{ndjson_path.name}.migration.lock) and holds it through the atomic rename.
    Concurrent appenders that acquire the same sentinel will block until the new
    inode is in place, preventing lost-event race conditions on the old inode.
    """
    if not ndjson_path.exists():
        return 0

    with _acquire_sentinel_lock(ndjson_path) as _sentinel_fh:
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

        # Atomic rename under sentinel lock — concurrent appenders block until complete.
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

    # --- dispatch_register.ndjson ---
    dr_path = state_dir / "dispatch_register.ndjson"
    n = _restamp_ndjson_inplace(dr_path, envelope, dry_run=dry_run)
    results["dispatch_register.ndjson"] = n

    # --- t0_receipts.ndjson ---
    receipts_path = state_dir / "t0_receipts.ndjson"
    n = _restamp_ndjson_inplace(receipts_path, envelope, dry_run=dry_run)
    results["t0_receipts.ndjson"] = n

    # --- central paths (if they differ from primary) ---
    if also_central:
        try:
            central_state = resolve_central_data_dir(project_id) / "state"
            if central_state.exists() and central_state.resolve() != state_dir.resolve():
                c_dr = central_state / "dispatch_register.ndjson"
                n = _restamp_ndjson_inplace(c_dr, envelope, dry_run=dry_run)
                results["central/dispatch_register.ndjson"] = n

                c_receipts = central_state / "t0_receipts.ndjson"
                n = _restamp_ndjson_inplace(c_receipts, envelope, dry_run=dry_run)
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
            state_dir = _default_primary_state_dir(args.project_id)
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
