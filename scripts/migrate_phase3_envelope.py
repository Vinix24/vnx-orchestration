#!/usr/bin/env python3
"""Phase 6 P3 — one-shot NDJSON envelope re-stamper.

Walks existing NDJSON files (t0_receipts.ndjson, dispatch_register.ndjson)
for a given project and re-streams each line with the four-tuple envelope
fields (operator_id, project_id, orchestrator_id, agent_id) filled in.

Usage:
    python3 scripts/migrate_phase3_envelope.py --project-id vnx-dev [--dry-run]
    python3 scripts/migrate_phase3_envelope.py --project-id vnx-dev --state-dir /path/to/.vnx-data/state

Locking contract (race-free with concurrent appends during P5 cutover):
- All rewrite paths delegate to scripts.lib.state_writer.rewrite_locked().
- The shared state_writer sentinel registry coordinates concurrent appenders,
  rewriters, and migrators across dispatch_register.ndjson and t0_receipts.ndjson.

Idempotent: running twice yields identical output (envelope fields already
present are not overwritten).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_SCRIPT_DIR = Path(__file__).resolve().parent
_LIB_DIR = _SCRIPT_DIR / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import state_writer


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


def _restamp_content(
    content: bytes,
    envelope: Dict[str, Optional[str]],
    *,
    hold_lock_delay: float = 0.0,
) -> tuple[bytes, int]:
    """Re-stamp raw NDJSON bytes and return rewritten content plus stamped count."""
    if hold_lock_delay > 0:
        time.sleep(hold_lock_delay)

    text = content.decode("utf-8", errors="replace")
    stamped_lines: List[str] = []
    count = 0

    for raw_line in text.splitlines():
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

    new_content = "\n".join(stamped_lines) + ("\n" if stamped_lines else "")
    return new_content.encode("utf-8"), count


def _restamp_ndjson_inplace(
    ndjson_path: Path,
    envelope: Dict[str, Optional[str]],
    *,
    dry_run: bool = False,
    hold_lock_delay: float = 0.0,
) -> int:
    """Re-stamp a single NDJSON file with envelope fields. Returns stamped line count.

    Locking is delegated to scripts.lib.state_writer.rewrite_locked(), which
    acquires the registered sentinel and the data-file lock before reading the
    file and holds both through any atomic replace.
    """
    if not ndjson_path.exists():
        return 0

    if dry_run:
        try:
            content = ndjson_path.read_bytes()
        except OSError:
            return 0
        _, count = _restamp_content(content, envelope)
        return count

    stamped_count = 0

    def _rewrite(current_content: bytes) -> bytes:
        nonlocal stamped_count
        rewritten, stamped_count = _restamp_content(
            current_content,
            envelope,
            hold_lock_delay=hold_lock_delay,
        )
        return rewritten

    state_writer.rewrite_locked(ndjson_path, _rewrite)
    return stamped_count


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

    # --- t0_receipts.ndjson (state_writer registry maps to append_receipt.lock) ---
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
