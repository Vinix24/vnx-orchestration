#!/usr/bin/env python3
"""Capture audit CLI: project-bound sources and the audit (capture PR-2, PR-3).

Subcommands:
  bind     check the paths and the project id, record the binding in the ledger
           (``state_mutation`` receipt in ``t0_receipts.ndjson``), then write
           ``<state-dir>/capture_sources.json`` atomically. A failed check or a
           failed ledger append writes nothing and exits 1 with the reason.
  sources  show per source the state (ok, absent, unbound, error) and counts.
           Never prints a name, title, heading or body.
  report   print the written ``capture_audit.json`` (open pairs per window) and say when the
           dismissal ledger exists but cannot be read. ``--show`` resolves names for the
           terminal only, never stored; it cannot be combined with ``--json``.
  dismiss  append one ``{pair_id, reason, at, by}`` line to ``capture_dispositions.ndjson``,
           only when ``valid_dismissal`` (the rule the reader applies) accepts it, else exit 2.
  audit    compute the window, the pairs and their disposition and write
           ``state/capture_audit.json`` (hashes, ids and counts only). With
           ``--since``/``--until`` the window is explicit; without them it
           chains on the previous audit, or is the last 24 hours on a first run.

The exit code is 0 whenever the command could report, also when a source is
not ``ok``: the state in the output (for ``audit`` the JSON file) is the
evidence, not the exit code. A window that cannot be built, a state dir that
does not exist or a previous audit file that cannot be read exits 2 and
writes nothing.
"""
from __future__ import annotations

import argparse
import fcntl
import getpass
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

import capture_sources as cs
from atomic_io import atomic_write_json
from vnx_paths import project_id_from_state_dir

LIB_FILE = SCRIPT_DIR / "lib" / "capture_audit.py"
_LIB_NAMES = ("capture_audit", "capture_audit_lib")
SOURCES_MONTHS = 2


def _load_lib() -> ModuleType:
    """The audit lib, loaded by file path. This script and the lib share the module name
    ``capture_audit``, so a plain import returns whichever comes first on sys.path, or this script
    itself when it was imported as ``capture_audit``. A lib already loaded from LIB_FILE is reused."""
    for name in _LIB_NAMES:
        loaded = sys.modules.get(name)
        if loaded is not None and Path(getattr(loaded, "__file__", None) or "").resolve() == LIB_FILE:
            return loaded
    name = _LIB_NAMES[0] if _LIB_NAMES[0] not in sys.modules else _LIB_NAMES[1]
    spec = importlib.util.spec_from_file_location(name, LIB_FILE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # registered before it runs: dataclasses looks its module up there
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[name]
        raise
    return module


ca = _load_lib()


def _recent_months(now: datetime, count: int) -> List[str]:
    months: List[str] = []
    year, month = now.year, now.month
    for _ in range(count):
        months.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return months


def _bind_problem(state_dir: Path, memory_dir: Path, bucket_dir: Path, slug: str) -> Optional[str]:
    if not state_dir.is_dir():
        return "state dir does not exist"
    if not project_id_from_state_dir(state_dir):
        return "project id cannot be derived from the state dir"
    if not memory_dir.is_dir():
        return "memory dir does not exist"
    if cs._memory_dir_problem(str(memory_dir)):
        return "memory dir is not under ~/.claude/projects/"
    if not bucket_dir.is_dir():
        return "bucket dir does not exist"
    if not slug.strip():
        return "bucket slug is empty"
    return None


def _record_bind(state_dir: Path, payload: Dict[str, object], transition: str) -> Optional[str]:
    """ADR-005: the binding decision lands in the ledger before the derived JSON. Fail closed.

    Returns None when the receipt is in the ledger, else the reason the bind must be refused."""
    receipt = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": "state_mutation",
        "receipt_kind": "state_mutation",
        "terminal": "T0",
        "source": "capture_audit",
        "file": cs.BINDING_FILENAME,
        "trigger": "capture_bind",
        "project_id": payload["project_id"],
        "bucket_slug": payload["bucket_slug"],
        "transition": transition,
    }
    try:
        from append_receipt import append_receipt_payload
        ledger = str(state_dir / "t0_receipts.ndjson")
        result = append_receipt_payload(receipt, receipts_file=ledger, skip_enrichment=True)
    # append_receipt raises SystemExit when its import bootstrap fails; like
    # planning_cli._retarget_receipt, that must refuse the bind, not escape cmd_bind.
    except (Exception, SystemExit) as exc:
        return f"ledger append failed ({exc})"
    status = getattr(result, "status", None)
    if status in ("appended", "duplicate"):
        return None
    return f"ledger append failed (append result {status!r})"


def cmd_bind(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).expanduser().resolve()
    memory_dir = Path(args.memory_dir).expanduser().resolve()
    bucket_dir = Path(args.bucket_dir).expanduser().resolve()
    problem = _bind_problem(state_dir, memory_dir, bucket_dir, args.bucket_slug)
    if problem:
        print(f"bind refused: {problem}", file=sys.stderr)
        return 1
    payload = {
        "schema": cs.BINDING_SCHEMA,
        "project_id": project_id_from_state_dir(state_dir),
        "memory_dir": str(memory_dir),
        "bucket_dir": str(bucket_dir),
        "bucket_slug": args.bucket_slug.strip(),
        "bound_at": datetime.now(timezone.utc).isoformat(),
    }
    target = state_dir / cs.BINDING_FILENAME
    refusal = _record_bind(state_dir, payload, "replaced" if target.exists() else "created")
    if refusal:
        print(f"bind refused: {refusal}", file=sys.stderr)
        return 1
    atomic_write_json(target, payload)
    print(f"bound project {payload['project_id']}: {target}")
    return 0


def _bin(status: str, count: int, **extra: int) -> Dict[str, object]:
    row: Dict[str, object] = {"state": cs.state_of(status), "count": count}
    row.update(extra)
    return row


def collect_sources(state_dir: Path, now: datetime) -> Dict[str, object]:
    """Per-bin state and counts only. Reasons are fixed texts, never data."""
    binding, status = cs.load_binding(state_dir)
    if binding is None:
        unbound = _bin(status, 0)
        return {"binding": unbound, "open_items": unbound, "memory": unbound, "bucket": unbound}
    items, oi_status, oi_foreign = cs.read_open_items(state_dir)
    notes, mem_status = cs.read_memory_notes(binding)
    entries, bucket_status, foreign = cs.read_bucket_entries(binding, _recent_months(now, SOURCES_MONTHS))
    return {
        "binding": _bin(status, 1),
        "open_items": _bin(oi_status, len(items), foreign_project_skipped=oi_foreign),
        "memory": _bin(mem_status, len(notes)),
        "bucket": _bin(bucket_status, len(entries), foreign_slug_skipped=foreign),
    }


def cmd_sources(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).expanduser().resolve()
    report = {"project_id": project_id_from_state_dir(state_dir) or None}
    report["sources"] = collect_sources(state_dir, datetime.now(timezone.utc))
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    for name, row in report["sources"].items():
        extras = " ".join(f"{k}={v}" for k, v in row.items() if k != "state")
        print(f"{name}: {row['state']} {extras}")
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).expanduser().resolve()
    try:
        explicit = args.since is not None or args.until is not None
        chain = None if explicit else ca.previous_chain_until(state_dir)
        window = ca.parse_window(args.since, args.until, chain_until=chain, now=datetime.now(timezone.utc))
        audit = ca.build_audit(state_dir, window)
    except ValueError as exc:
        print(f"capture_audit audit: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(audit, indent=2, sort_keys=True))
        return 0
    bins = " ".join(f"{name}={row['state']}" for name, row in audit["bins"].items())
    print(f"window={audit['window']['source']} anchors_total={audit['anchors_total']} "
          f"open_pairs={audit['open_pairs']} backlog_open_pairs={audit['backlog_open_pairs']} {bins}")
    return 0


def _ledger_state(state_dir: Path) -> str:
    """``ok``, ``absent`` or ``unreadable``. The read model counts an unreadable ledger as nothing
    dismissed, so ``report`` has to say it: the open pairs are then an over-count."""
    path = state_dir / ca.LEDGER_FILENAME
    if not os.path.lexists(path):
        return "absent"
    try:
        path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):  # vnx-silent-except: the state is the answer, reported as unreadable
        return "unreadable"
    return "ok"


def _load_audit(state_dir: Path) -> dict:
    try:
        audit = json.loads((state_dir / ca.AUDIT_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{ca.AUDIT_FILENAME} cannot be read (run audit first): {type(exc).__name__}") from exc
    if not isinstance(audit, dict) or audit.get("schema") != ca.AUDIT_SCHEMA or not isinstance(audit.get("pairs"), list):
        raise ValueError(f"{ca.AUDIT_FILENAME} has an unknown schema")
    return audit


def _names(state_dir: Path) -> Dict[str, str]:
    """Locator -> name, read from the bound sources again. Terminal use only; never stored."""
    binding, _ = cs.load_binding(state_dir)
    if binding is None:
        return {}
    items, _, _ = cs.read_open_items(state_dir)
    notes, _ = cs.read_memory_notes(binding)
    entries, _, _ = cs.read_bucket_entries(binding, _recent_months(datetime.now(timezone.utc), SOURCES_MONTHS))
    names = {r["locator"]: str(r.get("id")) + " " + str(r.get("title_tokens_input")) for r in items}
    names.update({cs._locator(binding["project_id"], "memory", n["key"]): f"[[{n['key']}]]" for n in notes})
    names.update({r["locator"]: str(r.get("title_tokens_input")) for r in entries})
    return names


def cmd_report(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).expanduser().resolve()
    if args.show and args.json:
        print("capture_audit report: --show prints names and cannot be combined with --json", file=sys.stderr)
        return 2
    try:
        audit = _load_audit(state_dir)
    except ValueError as exc:
        print(f"capture_audit report: {exc}", file=sys.stderr)
        return 2
    ledger = _ledger_state(state_dir)
    if args.json:
        print(json.dumps({**audit, "dismissal_ledger": {"path": str(state_dir / ca.LEDGER_FILENAME), "state": ledger}},
                         indent=2, sort_keys=True))
        return 0
    bins = " ".join(f"{name}={row.get('state')}" for name, row in (audit.get("bins") or {}).items())
    print(f"window={(audit.get('window') or {}).get('source')} anchors_total={audit.get('anchors_total')} "
          f"open_pairs={audit.get('open_pairs')} backlog_open_pairs={audit.get('backlog_open_pairs')} {bins}")
    if ledger == "unreadable":
        print(f"dismissal ledger {state_dir / ca.LEDGER_FILENAME}: unreadable, nothing counts as dismissed (open pairs over-counted)")
    names = _names(state_dir) if args.show else {}
    for pair in audit["pairs"]:
        if isinstance(pair, dict) and pair.get("disposition") == "open":
            shown = " | ".join(names.get(loc, str(loc)) for loc in pair.get("locators") or []) if args.show else ""
            print(f"open {pair.get('pair_id')} {pair.get('kind')} anchor={pair.get('anchor_bin')} "
                  f"in_window={pair.get('in_window')} {shown}".rstrip())
    return 0


def _append_line(path: Path, line: str) -> None:
    """Append one line under an exclusive flock; a last line without newline is closed first."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        size = os.fstat(fd).st_size
        lead = b""
        if size:
            os.lseek(fd, size - 1, os.SEEK_SET)
            lead = b"" if os.read(fd, 1) == b"\n" else b"\n"
        os.write(fd, lead + line.encode("utf-8") + b"\n")
        os.fsync(fd)
    finally:
        os.close(fd)


def cmd_dismiss(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).expanduser().resolve()
    row = {"pair_id": args.pair, "reason": args.reason, "at": datetime.now(timezone.utc).isoformat(),
           "by": args.by or getpass.getuser()}
    if not ca.valid_dismissal(row):
        print("dismiss refused: pair id (16 hex), reason and by must be non-empty and valid; nothing written",
              file=sys.stderr)
        return 2
    if not state_dir.is_dir():
        print("dismiss refused: the state dir does not exist; nothing written", file=sys.stderr)
        return 2
    try:
        _append_line(state_dir / ca.LEDGER_FILENAME, json.dumps(row, sort_keys=True))
    except OSError as exc:
        print(f"dismiss failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"dismissed {args.pair}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Capture audit: project-bound sources and the audit")
    sub = parser.add_subparsers(dest="command", required=True)
    bind = sub.add_parser("bind", help="record the binding in the ledger, then write the binding file")
    bind.add_argument("--state-dir", required=True)
    bind.add_argument("--memory-dir", required=True)
    bind.add_argument("--bucket-dir", required=True)
    bind.add_argument("--bucket-slug", required=True)
    bind.set_defaults(func=cmd_bind)
    sources = sub.add_parser("sources", help="show the state and counts per source")
    sources.add_argument("--state-dir", required=True)
    sources.add_argument("--json", action="store_true")
    sources.set_defaults(func=cmd_sources)
    audit = sub.add_parser("audit", help="write state/capture_audit.json for one window")
    audit.add_argument("--state-dir", required=True)
    audit.add_argument("--since", help="ISO-8601 start that names its zone (with --until)")
    audit.add_argument("--until", help="ISO-8601 end that names its zone (with --since)")
    audit.add_argument("--json", action="store_true")
    audit.set_defaults(func=cmd_audit)
    report = sub.add_parser("report", help="print the open pairs of the written audit")
    report.add_argument("--state-dir", required=True)
    report.add_argument("--json", action="store_true")
    report.add_argument("--show", action="store_true", help="resolve names for the terminal (not stored)")
    report.set_defaults(func=cmd_report)
    dismiss = sub.add_parser("dismiss", help="record that a pair is no duplicate, with a reason")
    dismiss.add_argument("--state-dir", required=True)
    dismiss.add_argument("--pair", required=True)
    dismiss.add_argument("--reason", required=True)
    dismiss.add_argument("--by", help="who decides (default: the login name)")
    dismiss.set_defaults(func=cmd_dismiss)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
