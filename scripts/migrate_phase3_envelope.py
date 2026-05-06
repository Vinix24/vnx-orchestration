#!/usr/bin/env python3
"""Phase 6 P3: one-shot NDJSON envelope re-stamper.

Walks existing NDJSON files in a project state directory and re-streams each
line with the four-tuple identity envelope added where missing:
  project_id, operator_id, orchestrator_id, agent_id

Idempotent: records that already carry a field are NOT overwritten.
Dry-run mode prints what would change without writing.

Usage::

    # dry run
    python3 scripts/migrate_phase3_envelope.py --dry-run --project-id vnx-dev

    # live run
    python3 scripts/migrate_phase3_envelope.py --project-id vnx-dev

    # with explicit state dir
    python3 scripts/migrate_phase3_envelope.py --project-id vnx-dev \\
        --state-dir /path/to/.vnx-data/state
"""
from __future__ import annotations

import argparse
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

ENVELOPE_FIELDS = ("project_id", "operator_id", "orchestrator_id", "agent_id")


def _resolve_state_dir(state_dir_arg: Optional[str]) -> Path:
    if state_dir_arg:
        return Path(state_dir_arg).expanduser().resolve()
    try:
        from vnx_paths import resolve_paths
        return Path(resolve_paths()["VNX_STATE_DIR"])
    except Exception:
        return Path(os.environ.get("VNX_STATE_DIR") or ".vnx-data/state").expanduser()


def _build_envelope(project_id: str) -> Dict[str, str]:
    """Resolve the four-tuple envelope for a project.

    Tries environment variables then vnx_identity; falls back gracefully.
    Returns only the fields that have a non-None value.
    """
    raw: Dict[str, Optional[str]] = {
        "project_id": project_id,
        "operator_id": os.environ.get("VNX_OPERATOR_ID") or None,
        "orchestrator_id": os.environ.get("VNX_ORCHESTRATOR_ID") or None,
        "agent_id": os.environ.get("VNX_AGENT_ID") or None,
    }
    try:
        from vnx_identity import try_resolve_identity
        identity = try_resolve_identity()
        if identity:
            raw["operator_id"] = raw["operator_id"] or identity.operator_id
            raw["orchestrator_id"] = raw["orchestrator_id"] or identity.orchestrator_id
            raw["agent_id"] = raw["agent_id"] or identity.agent_id
    except Exception:
        pass
    return {k: v for k, v in raw.items() if v is not None}


def _needs_stamp(record: Dict[str, Any]) -> bool:
    """Return True when at least one envelope field is absent."""
    return any(not record.get(f) for f in ENVELOPE_FIELDS)


def _stamp_line(record: Dict[str, Any], envelope: Dict[str, str]) -> Dict[str, Any]:
    """Return a copy of record with envelope fields added where absent."""
    out = dict(record)
    for field, value in envelope.items():
        if not out.get(field):
            out[field] = value
    return out


def _restamp_file(
    path: Path,
    envelope: Dict[str, str],
    *,
    dry_run: bool,
) -> int:
    """Re-stamp one NDJSON file in-place. Returns number of records stamped."""
    try:
        raw = path.read_bytes()
    except OSError:
        return 0

    lines = raw.splitlines(keepends=True)
    stamped_count = 0
    out_lines: List[bytes] = []

    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped:
            out_lines.append(raw_line)
            continue
        try:
            record = json.loads(stripped.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            out_lines.append(raw_line)
            continue

        if _needs_stamp(record):
            record = _stamp_line(record, envelope)
            stamped_count += 1

        out_lines.append(
            (json.dumps(record, separators=(",", ":"), sort_keys=False) + "\n").encode("utf-8")
        )

    if not dry_run and stamped_count > 0:
        fd, tmp_str = tempfile.mkstemp(prefix=path.name + ".tmp.", dir=str(path.parent))
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.writelines(out_lines)
            os.replace(tmp_str, str(path))
        except Exception:
            try:
                os.unlink(tmp_str)
            except Exception:
                pass

    return stamped_count


def migrate(
    project_id: str,
    *,
    state_dir: Optional[str] = None,
    dry_run: bool = False,
    verbose: bool = False,
) -> Dict[str, int]:
    """Run the migration. Returns {filename: lines_stamped}."""
    resolved_state = _resolve_state_dir(state_dir)
    envelope = _build_envelope(project_id)

    if not resolved_state.is_dir():
        print(
            f"[migrate_phase3_envelope] state-dir not found: {resolved_state}",
            file=sys.stderr,
        )
        return {}

    ndjson_files = sorted(resolved_state.glob("*.ndjson"))
    summary: Dict[str, int] = {}

    for fpath in ndjson_files:
        count = _restamp_file(fpath, envelope, dry_run=dry_run)
        summary[fpath.name] = count
        if verbose or (dry_run and count > 0):
            action = "would re-stamp" if dry_run else "re-stamped"
            print(f"  {fpath.name}: {action} {count} lines")

    total = sum(summary.values())
    if dry_run:
        print(f"[dry-run] would re-stamp {total} lines across {len(ndjson_files)} file(s)")
    else:
        print(
            f"[migrate_phase3_envelope] re-stamped {total} lines across {len(ndjson_files)} file(s)"
        )

    return summary


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 6 P3 NDJSON envelope re-stamper")
    parser.add_argument("--project-id", required=True, help="VNX project_id (e.g. vnx-dev)")
    parser.add_argument("--state-dir", default=None, help="Override state directory path")
    parser.add_argument(
        "--dry-run", action="store_true", default=False, help="Print without writing"
    )
    parser.add_argument(
        "--verbose", action="store_true", default=False, help="Print per-file details"
    )
    args = parser.parse_args(argv)

    try:
        migrate(
            args.project_id,
            state_dir=args.state_dir,
            dry_run=args.dry_run,
            verbose=args.verbose,
        )
        return 0
    except Exception as exc:
        print(f"[migrate_phase3_envelope] ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
