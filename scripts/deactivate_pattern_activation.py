#!/usr/bin/env python3
"""Switch one pattern activation off: ``--activation-id <id> --reason <tekst>`` (loop D9).

The deactivation is a decision first: the store appends a ``deactivated`` record with its own
``approval_id`` to ``state/prevention_rule_decisions.ndjson`` and the queue rule becomes
``deactivated``. Then ``pattern_activation`` gets ``active = 0`` with the time and the reason.
It stays off: ``record_activation`` on the same approval hits the same ``activation_id`` and
writes nothing, so only a new ``vnx learning decide-rule --decision approved`` switches the
pattern on again.

Exit 0 switched off or already off; 1 no such activation in this project, a legacy rule, or a
record that refuses; 2 usage (an empty reason included).
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

import pattern_activation as pa
import prevention_rule_store as store
from project_scope import TenantUnresolved, resolve_stamp_project_id

LEGACY_HINT = ("is a legacy rule without an activation: retire it through the archival path "
               "(vnx learning approve/dismiss, valid_until)")


def _args(argv):
    parser = argparse.ArgumentParser(description="Deactivate one learning-loop pattern activation.")
    parser.add_argument("--activation-id", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--by", default="operator", help="who decides (default: operator)")
    parser.add_argument("--state-dir", type=Path, help="project state directory (default: VNX_STATE_DIR)")
    args = parser.parse_args(argv)
    for name in ("activation_id", "reason", "by"):
        if not getattr(args, name).strip():
            parser.error(f"--{name.replace('_', '-')} must not be empty")
    return args


def _is_legacy(conn: sqlite3.Connection, project_id: str, ref: str) -> bool:
    """True when ``ref`` is the id of a ``prevention_rules`` row of this project that is no loop rule."""
    if not ref.strip().isdigit():
        return False
    row = conn.execute("SELECT source FROM prevention_rules WHERE project_id = ? AND id = ?",
                       (project_id, int(ref))).fetchone()
    return row is not None and row[0] != pa.LOOP_SOURCE


def _deactivate(conn: sqlite3.Connection, state_dir: Path, project_id: str, args) -> int:
    activation = pa.activation_for(conn, project_id, args.activation_id)
    if activation is None:
        what = LEGACY_HINT if _is_legacy(conn, project_id, args.activation_id) else \
            f"is no activation in project {project_id!r}"
        print(f"error: {args.activation_id} {what}", file=sys.stderr)
        return 1
    if activation["active"] != 1:
        print(f"unchanged: {args.activation_id} is off since {activation['deactivated_at']} "
              f"({activation['deactivation_reason']})")
        return 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        record = store.record_deactivation(state_dir, activation, reason=args.reason.strip(),
                                           decided_by=args.by.strip(), now=now)
        pa.deactivate(conn, state_dir, args.activation_id, project_id=project_id)
    except (store.DecisionRefused, store.DecisionsUnreadable, store.QueueUnreadable, ValueError) as exc:
        conn.rollback()
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    conn.commit()
    print(f"deactivated: {args.activation_id} approval_id={record['approval_id']} reason={record['reason']}")
    return 0


def main(argv=None) -> int:
    args = _args(argv)
    if args.state_dir is None:
        from vnx_paths import ensure_env  # imported only without an explicit state dir
        args.state_dir = Path(ensure_env()["VNX_STATE_DIR"])
    state_dir = args.state_dir.expanduser().resolve()
    db = state_dir / "quality_intelligence.db"
    try:
        project_id = resolve_stamp_project_id(db_path=str(db))
        with closing(sqlite3.connect(f"file:{db}?mode=rw", uri=True)) as conn:
            return _deactivate(conn, state_dir, project_id, args)
    except (TenantUnresolved, sqlite3.Error) as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
