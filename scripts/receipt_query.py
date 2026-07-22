#!/usr/bin/env python3
"""receipt_query.py — the receipt-v2 pull interface (ADR-035 §5).

This PR (ADR-035 §9 PR-6) ships two subcommands. ``by-pr``/``since``/``by-track``/
``digest`` land in PR-7; wiring into the T0 cycle and retiring the tmux-pane push
land in PR-8 — neither is touched here.

  pull         — the tick primitive. Absorbs receipt_pull.py's cursor algorithm
                 (parked branch feat/receipt-mailbox-delivery, commit 54089155),
                 reimplemented against current main rather than resurrecting the
                 branch: byte cursor in receipt_pull_cursor.json, read-then-advance,
                 advances only past complete (newline-terminated) lines so a
                 concurrent append's partial trailing line is never consumed early,
                 resets to 0 on a truncated/rotated ledger, --seed-now sets the
                 cursor to EOF (skip the backlog without deleting it), --peek reads
                 without advancing.
  by-dispatch  — thin wrapper over receipt_provenance.find_receipts_by_dispatch.
                 No reimplementation.

Both subcommands tolerate a mixed v1/v2 ledger: a line missing ``schema_version``
is a v1 line, read like any other JSON object — never a reason to crash.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR / "lib") not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from receipt_provenance import find_receipts_by_dispatch  # noqa: E402

LEDGER_NAME = "t0_receipts.ndjson"
CURSOR_NAME = "receipt_pull_cursor.json"


def _ledger_path(state_dir: Path) -> Path:
    return state_dir / LEDGER_NAME


def _default_cursor_path(state_dir: Path) -> Path:
    return state_dir / CURSOR_NAME


def load_cursor(cursor_path: Path) -> int:
    """Read the byte offset from ``cursor_path``. Missing or corrupt -> 0."""
    if not cursor_path.exists():
        return 0
    try:
        return int(json.loads(cursor_path.read_text(encoding="utf-8")).get("offset", 0))
    except (json.JSONDecodeError, ValueError, OSError, TypeError):
        return 0


def save_cursor(cursor_path: Path, offset: int) -> None:
    """Atomically persist ``offset`` to ``cursor_path`` (tmp write + os.replace)."""
    tmp = cursor_path.with_suffix(cursor_path.suffix + ".tmp")
    tmp.write_text(json.dumps({"offset": int(offset)}), encoding="utf-8")
    os.replace(tmp, cursor_path)


def pull_new_receipts(
    ledger_path: Path,
    cursor_offset: int = 0,
) -> Tuple[List[Dict[str, Any]], int]:
    """Read receipts appended after ``cursor_offset``. Returns ``(receipts, new_offset)``.

    Read-then-advance: ``new_offset`` only ever moves past COMPLETE
    (newline-terminated) lines, so a concurrent append's partial trailing line is
    left untouched for the next pull. A truncated/rotated ledger (smaller than the
    cursor) resets the cursor to 0. A malformed complete line is skipped, but the
    cursor still advances past it — it will never parse on a later pull either.
    Mixed v1/v2 lines are both plain JSON objects; no schema_version branching is
    needed to read them.
    """
    receipts: List[Dict[str, Any]] = []
    new_offset = cursor_offset
    if not ledger_path.exists():
        return receipts, new_offset
    if ledger_path.stat().st_size < cursor_offset:
        new_offset = 0
        cursor_offset = 0
    with open(ledger_path, "rb") as f:
        f.seek(cursor_offset)
        while True:
            raw = f.readline()
            if not raw:
                break
            if not raw.endswith(b"\n"):
                break  # incomplete trailing line (mid-append) — do not advance past it
            new_offset = f.tell()
            try:
                receipts.append(json.loads(raw.decode("utf-8")))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue  # skip a malformed complete line; cursor already advanced past it
    return receipts, new_offset


def _format_receipt(r: Dict[str, Any]) -> str:
    term = r.get("terminal_id", "?")
    did = r.get("dispatch_id", "?")
    status = r.get("status", "?")
    schema_version = r.get("schema_version", 1)
    pr = r.get("pr_id") or "-"
    return f"  {term} {did} [{status}] schema_version={schema_version} pr={pr}"


def _cmd_pull(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    ledger = _ledger_path(state_dir)
    cursor_path = Path(args.cursor_file) if args.cursor_file else _default_cursor_path(state_dir)

    if args.seed_now:
        eof = ledger.stat().st_size if ledger.exists() else 0
        save_cursor(cursor_path, eof)
        if args.json:
            print(json.dumps({"seeded": True, "cursor": eof}))
        else:
            print(
                f"cursor seeded to EOF ({eof} bytes) — "
                "backlog skipped (still in the ledger, auditable)."
            )
        return 0

    cursor = load_cursor(cursor_path)
    receipts, new_offset = pull_new_receipts(ledger, cursor)

    if args.json:
        print(json.dumps(
            {"count": len(receipts), "cursor": new_offset, "receipts": receipts},
            indent=2,
        ))
    else:
        print(f"{len(receipts)} new receipt(s) since cursor {cursor}:")
        for r in receipts:
            print(_format_receipt(r))

    if not args.peek and new_offset != cursor:
        save_cursor(cursor_path, new_offset)
    return 0


def _cmd_by_dispatch(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    ledger = _ledger_path(state_dir)
    receipts = find_receipts_by_dispatch(ledger, args.dispatch_id)

    if args.json:
        print(json.dumps(
            {"dispatch_id": args.dispatch_id, "count": len(receipts), "receipts": receipts},
            indent=2,
        ))
    else:
        print(f"{len(receipts)} receipt(s) for dispatch {args.dispatch_id}:")
        for r in receipts:
            print(_format_receipt(r))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Receipt v2 pull-model query interface (ADR-035 §5)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_pull = sub.add_parser(
        "pull", help="tick primitive — read receipts since the cursor, advance it",
    )
    p_pull.add_argument("--state-dir", required=True)
    p_pull.add_argument(
        "--cursor-file", default=None,
        help="override the cursor file path (default: <state-dir>/receipt_pull_cursor.json)",
    )
    p_pull.add_argument(
        "--seed-now", action="store_true",
        help="set the cursor to EOF (skip the backlog; it stays on disk, auditable)",
    )
    p_pull.add_argument(
        "--peek", action="store_true",
        help="read new receipts without advancing the cursor",
    )
    p_pull.add_argument("--json", action="store_true")
    p_pull.set_defaults(func=_cmd_pull)

    p_by_dispatch = sub.add_parser(
        "by-dispatch",
        help="all receipts for a dispatch_id (wraps receipt_provenance.find_receipts_by_dispatch)",
    )
    p_by_dispatch.add_argument("dispatch_id")
    p_by_dispatch.add_argument("--state-dir", required=True)
    p_by_dispatch.add_argument("--json", action="store_true")
    p_by_dispatch.set_defaults(func=_cmd_by_dispatch)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
