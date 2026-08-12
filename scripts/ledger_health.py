#!/usr/bin/env python3
"""ledger_health.py — reconciles what the fabric DID against what it RECORDED.

VNX has integrity tooling for the receipts ledger itself (``audit_chain.py``,
``state_integrity.py``, three reconcilers) but nothing that asks the one
question none of them ask: did every dispatch the register says was fired
actually land a receipt? This is a read-only reconciliation over central
state — it never mutates ``dispatch_register.ndjson``, ``t0_receipts.ndjson``,
or ``receipt_pull_cursor.json`` — that answers three questions:

  receipt_coverage — every ``dispatch_id`` in ``dispatch_register.ndjson``
      must have a receipt carrying that SAME ``dispatch_id`` (or, for legacy
      receipts, ``cmd_id`` — the same fallback ``receipt_provenance.
      find_receipts_by_dispatch`` already applies) in ``t0_receipts.ndjson``.
      Matched on the FIELD, never a substring of the raw ledger text: a
      receipt's ``branch`` field routinely contains another dispatch's slug
      (e.g. a ``review_gate_request`` receipt for gate dispatch
      ``20260811c-gate-pr1454`` carries
      ``branch: dispatch/20260811c-a-freshinstall-hookpins``) — a substring
      search over raw lines would call that a hit for the branch's dispatch,
      when no receipt anywhere carries that ``dispatch_id``.

  pull_cursor — the age of ``receipt_pull_cursor.json`` (ADR-035 §5.3: the
      PULL that replaced the retired T0-pane push, DISPATCH_RULES §13's step
      0 of every T0 cycle) and the unread backlog behind it. The backlog is
      read via the same byte-cursor primitive ``receipt_query.py``'s
      ``pull --peek`` uses (``pull_new_receipts``) — this module never calls
      ``save_cursor``, so the cursor position on disk is provably unchanged
      by a health run. The offset itself is read by a local wrapper, not
      ``receipt_query.load_cursor`` directly: that helper collapses a
      missing cursor AND a corrupt/unparseable one to the same offset 0,
      which is correct for its own caller (the pull cadence just starts
      over) but would let a corrupt cursor file read as "legitimately at
      byte 0" here. A ledger smaller than the cursor offset (truncation/
      rotation) is its own finding, not just a reported field.

  chain_status — the existing ``ndjson_hash_chain.verify_chain`` outcome,
      with ``unchained`` reported as its own explicit class rather than
      folded into "ok". ``audit_chain.py verify`` already reports
      ``verified: true`` / ``status: "unchained"`` for a ledger with no
      ``prev_hash`` entries at all — correct in isolation (chaining is
      default-off fleet-wide), but a consumer that reads only ``verified``
      sees a green checkmark for a ledger integrity CANNOT be verified on.
      Absence of evidence is not evidence of absence.

  migration_staleness — (OI-1169) compares ``runtime_coordination.db``'s
      ``PRAGMA user_version`` against the highest migration number under
      ``schemas/migrations/`` that ``migrations.auto_apply`` can actually
      apply (i.e. it has a paired ``apply_NNNN.py`` runner — a date-named
      migration targeting a different database, such as
      ``quality_intelligence.db``, has no runner and is correctly excluded;
      see ``migrations/auto_apply.py``). Before this dispatch a store that
      never opened a T0 SessionStart (the only wiring `auto_apply` had) could
      fall behind the numbered walk `vnx migrate` actually drives and stay
      behind silently forever. A store with no ``runtime_coordination.db`` yet
      has no schema state to be stale against — that is a legitimate nothing-
      to-check case (mirrors ``pull_cursor``'s no-cursor+empty-ledger "OK"),
      not a read failure, so it reports ``STATUS_OK`` rather than
      ``SKIPPED_UNVERIFIED``.

Exit codes: 0 all healthy, 1 findings, 2 cannot measure (a required file is
missing or unreadable) — mirrors ``pre_merge_gate.py``'s ``SKIPPED_UNVERIFIED``
(#1468): an unmeasurable state is never conflated with a pass, and outranks a
finding in the overall rollup.

Out of scope (by dispatch instruction, not an oversight): does not enable
``VNX_CHAIN_RECEIPTS``, does not run the pull cadence automatically, does not
write receipts. Read-only on state; the only write this module performs is
its own atomic health beacon under ``<data_dir>/health/ledger_health.json``.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
LIB_DIR = SCRIPT_DIR / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from ndjson_hash_chain import verify_chain  # noqa: E402
from receipt_query import CURSOR_NAME, pull_new_receipts  # noqa: E402
from migrations.auto_apply import _discover_migrations as _auto_apply_discover_migrations  # noqa: E402
from migrations.auto_apply import _RUNNERS_DIR as _AUTO_APPLY_RUNNERS_DIR  # noqa: E402
from migrations.auto_apply import _DEFAULT_MIGRATIONS_DIR as _AUTO_APPLY_MIGRATIONS_DIR  # noqa: E402

REGISTER_NAME = "dispatch_register.ndjson"
LEDGER_NAME = "t0_receipts.ndjson"
RUNTIME_DB_NAME = "runtime_coordination.db"
COMPONENT_NAME = "ledger_health"

# ADR-035 §5.3 made PULL step 0 of every T0 cycle; an active project runs
# several T0 cycles a day. A cursor untouched for a full day/night cycle has
# stopped advancing, not just gone quiet between sessions — the measured
# incident this tool exists for (cursor stuck since 21 June, 16,292 unread
# receipts) was months stale, not hours; 24h catches a dead cadence early
# without paging on a single quiet evening.
DEFAULT_CURSOR_STALE_HOURS = 24.0

# Beacon refresh cadence (health_beacon.py convention — e.g. plan-gate-panel.json
# also uses 86400 for a manually/periodically-triggered component). Wiring an
# automatic run cadence is explicitly out of scope for this dispatch; the
# interval only lets any FUTURE periodic caller's beacon read as "stale" if
# nobody reruns this tool, rather than trusting an arbitrarily old snapshot
# forever (health_beacon.all_beacons semantics).
BEACON_EXPECTED_INTERVAL_SECONDS = 86400

STATUS_OK = "ok"
STATUS_FINDING = "finding"
# Mirrors pre_merge_gate.py's SKIPPED_UNVERIFIED (#1468, OI-1140): a check
# that could not read what it needed to is never a pass.
SKIPPED_UNVERIFIED = "SKIPPED_UNVERIFIED"

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_UNMEASURABLE = 2


def _chain_receipts_configured() -> bool:
    """Same truthy predicate as ``append_receipt_internals.idempotency.
    _chain_receipts_enabled`` (1/true/yes/on, case-insensitive) — duplicated
    as a two-line env read rather than importing another subsystem's
    ``_internals`` package for one predicate."""
    value = os.environ.get("VNX_CHAIN_RECEIPTS", "")
    return value.strip().lower() in ("1", "true", "yes", "on")


def _read_unique_dispatch_ids(path: Path, *, include_cmd_id: bool = False) -> Tuple[Set[str], int, int]:
    """Collect the set of distinct ``dispatch_id`` values from an NDJSON file.

    Field match only — never a substring/text search. When ``include_cmd_id``
    is set, a line missing ``dispatch_id`` but carrying ``cmd_id`` counts under
    ``cmd_id`` too (the same fallback ``receipt_provenance.
    find_receipts_by_dispatch`` applies to legacy receipts).

    Returns ``(ids, total_lines, parse_errors)``. Raises ``OSError`` if the
    file cannot be opened/read (caller maps that to ``SKIPPED_UNVERIFIED`` —
    a read failure is "cannot measure", not "zero matches").
    """
    ids: Set[str] = set()
    total = 0
    errors = 0
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            total += 1
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                errors += 1
                continue
            if not isinstance(rec, dict):
                errors += 1
                continue
            did = rec.get("dispatch_id")
            if not did and include_cmd_id:
                did = rec.get("cmd_id")
            if did:
                ids.add(str(did))
    return ids, total, errors


def check_receipt_coverage(state_dir: Path) -> Dict[str, Any]:
    """Every dispatch_id the register knows about must have a matching receipt."""
    register_path = state_dir / REGISTER_NAME
    ledger_path = state_dir / LEDGER_NAME

    if not register_path.exists():
        return {"status": SKIPPED_UNVERIFIED, "reason": f"register not found: {register_path}"}
    if not ledger_path.exists():
        return {"status": SKIPPED_UNVERIFIED, "reason": f"receipts ledger not found: {ledger_path}"}

    try:
        register_ids, register_lines, register_errors = _read_unique_dispatch_ids(register_path)
        receipt_ids, receipt_lines, receipt_errors = _read_unique_dispatch_ids(
            ledger_path, include_cmd_id=True
        )
    except OSError as exc:
        return {"status": SKIPPED_UNVERIFIED, "reason": f"could not read ledger/register: {exc}"}

    missing = sorted(register_ids - receipt_ids)
    parse_errors = register_errors + receipt_errors

    result: Dict[str, Any] = {
        "status": STATUS_FINDING if missing else STATUS_OK,
        "register_dispatch_count": len(register_ids),
        "receipted_dispatch_count": len(receipt_ids),
        "missing_receipt_count": len(missing),
        "missing_receipt_dispatch_ids": missing,
        "register_lines": register_lines,
        "register_parse_errors": register_errors,
        "receipt_lines": receipt_lines,
        "receipt_parse_errors": receipt_errors,
    }

    if parse_errors:
        # A parse error on EITHER file makes `missing` untrustworthy in both
        # directions: a malformed register line can hide a dispatch_id that
        # in fact has no receipt (coverage looks better than it is), and a
        # malformed receipt line can hide the very receipt that would clear
        # an entry off `missing` (a false "missing"). Either way this check
        # cannot certify coverage, so it is SKIPPED_UNVERIFIED — chosen over
        # STATUS_FINDING-with-a-flag because a corrupt line's blast radius is
        # unbounded (we don't know what dispatch_id it would have carried),
        # unlike a clean-parse `missing` list, which is exact. This mirrors
        # the OSError branch above: a read that didn't fully succeed is
        # "cannot measure", never "measured and fine" — status is
        # overridden even when `missing` is empty.
        result["status"] = SKIPPED_UNVERIFIED
        result["reason"] = (
            f"{register_errors} register + {receipt_errors} receipt parse error(s) — "
            "coverage cannot be certified from a partially-unreadable ledger/register"
        )

    return result


def _read_cursor_offset(cursor_path: Path) -> Tuple[Optional[int], bool]:
    """Read the byte offset from ``cursor_path`` directly, distinguishing a
    corrupt/unparseable cursor file from a legitimate offset (including 0).

    ``receipt_query.load_cursor`` intentionally collapses a missing cursor
    AND a corrupt one to the same offset 0 — correct for its own caller (the
    pull cadence just starts over from 0), but a health check consuming that
    same 0 could not tell "cursor genuinely at byte 0" from "cursor file is
    garbage, this 0 means nothing". Duplicated here as a two-line parse
    (same shape as ``load_cursor``) rather than changing that helper's
    contract for its other caller.

    Returns ``(offset, is_corrupt)``. ``offset`` is ``None`` when corrupt.
    Only called after the caller has already confirmed ``cursor_path``
    exists.
    """
    try:
        raw = cursor_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        offset = int(data.get("offset", 0))
    except (json.JSONDecodeError, ValueError, TypeError, AttributeError):
        return None, True
    return offset, False


def check_pull_cursor(
    state_dir: Path,
    *,
    stale_hours: float = DEFAULT_CURSOR_STALE_HOURS,
) -> Dict[str, Any]:
    """Age of receipt_pull_cursor.json plus the unread backlog behind it.

    Never advances the cursor: reads the offset with the local
    ``_read_cursor_offset`` (a corruption-aware wrapper, see its docstring)
    and the backlog with ``receipt_query.pull_new_receipts``, but never
    calls ``save_cursor`` — the same non-mutating contract
    ``receipt_query.py pull --peek`` gives its callers.
    """
    ledger_path = state_dir / LEDGER_NAME
    cursor_path = state_dir / CURSOR_NAME

    if not ledger_path.exists():
        return {"status": SKIPPED_UNVERIFIED, "reason": f"receipts ledger not found: {ledger_path}"}

    try:
        ledger_size = ledger_path.stat().st_size
    except OSError as exc:
        return {"status": SKIPPED_UNVERIFIED, "reason": f"could not stat receipts ledger: {exc}"}

    if not cursor_path.exists():
        if ledger_size == 0:
            return {
                "status": STATUS_OK,
                "cursor_path": str(cursor_path),
                "cursor_exists": False,
                "cursor_offset": 0,
                "cursor_age_seconds": None,
                "ledger_size_bytes": 0,
                "backlog_bytes": 0,
                "backlog_receipt_count": 0,
                "stale_threshold_hours": stale_hours,
                "reason": "no cursor yet, but the ledger is empty — nothing to pull",
            }
        return {
            "status": STATUS_FINDING,
            "cursor_path": str(cursor_path),
            "cursor_exists": False,
            "cursor_offset": 0,
            "cursor_age_seconds": None,
            "ledger_size_bytes": ledger_size,
            "backlog_bytes": ledger_size,
            "backlog_receipt_count": None,
            "stale_threshold_hours": stale_hours,
            "reason": "receipt_pull_cursor.json does not exist — the pull cadence (ADR-035 "
                      "§5.3 / DISPATCH_RULES §13) has never run against this ledger",
        }

    try:
        cursor_mtime = cursor_path.stat().st_mtime
    except OSError as exc:
        return {"status": SKIPPED_UNVERIFIED, "reason": f"could not read cursor: {exc}"}

    cursor_offset, cursor_corrupt = _read_cursor_offset(cursor_path)

    if cursor_corrupt:
        # load_cursor() would silently coerce this same file to offset 0 —
        # indistinguishable from a legitimate cursor that really is at byte
        # 0. Never trust an offset we can't parse: status is
        # SKIPPED_UNVERIFIED, not STATUS_OK on a guessed 0.
        return {
            "status": SKIPPED_UNVERIFIED,
            "cursor_path": str(cursor_path),
            "cursor_exists": True,
            "cursor_corrupt": True,
            "cursor_offset": None,
            "cursor_age_seconds": round(max(0.0, time.time() - cursor_mtime), 1),
            "ledger_size_bytes": ledger_size,
            "stale_threshold_hours": stale_hours,
            "reason": f"{cursor_path} exists but its offset could not be parsed as valid "
                      "JSON — not the same as a legitimate offset of 0, so the cursor "
                      "position cannot be trusted",
        }

    age_seconds = max(0.0, time.time() - cursor_mtime)
    truncated = ledger_size < cursor_offset

    try:
        backlog_receipts, advanceable_offset = pull_new_receipts(ledger_path, cursor_offset)
    except OSError as exc:
        return {"status": SKIPPED_UNVERIFIED, "reason": f"could not read receipts ledger: {exc}"}

    effective_offset = 0 if truncated else cursor_offset
    backlog_bytes = max(0, advanceable_offset - effective_offset)

    stale = age_seconds > (stale_hours * 3600.0)

    reasons = []
    if truncated:
        reasons.append(
            f"ledger ({ledger_size} bytes) is smaller than the cursor offset "
            f"({cursor_offset} bytes) — truncated or rotated since the last pull"
        )
    if stale:
        reasons.append(
            f"cursor is {round(age_seconds / 3600.0, 1)}h old, past the {stale_hours}h threshold"
        )

    return {
        "status": STATUS_FINDING if (stale or truncated) else STATUS_OK,
        "cursor_path": str(cursor_path),
        "cursor_exists": True,
        "cursor_corrupt": False,
        "cursor_offset": cursor_offset,
        "cursor_age_seconds": round(age_seconds, 1),
        "ledger_size_bytes": ledger_size,
        "backlog_bytes": backlog_bytes,
        "backlog_receipt_count": len(backlog_receipts),
        "ledger_truncated_since_cursor": truncated,
        "stale_threshold_hours": stale_hours,
        "reason": "; ".join(reasons) if reasons else None,
    }


def check_chain_status(state_dir: Path) -> Dict[str, Any]:
    """Wraps ndjson_hash_chain.verify_chain; ``unchained`` is its own class.

    A ledger with no ``prev_hash`` entries is the current, accepted, fleet-wide
    default (``VNX_CHAIN_RECEIPTS`` off) — that alone is not a finding, or
    every project would fail this check by default. It becomes a finding only
    when ``VNX_CHAIN_RECEIPTS`` is configured on for THIS process yet the
    ledger still carries no chain: the config and the ledger disagree.
    """
    ledger_path = state_dir / LEDGER_NAME
    if not ledger_path.exists():
        return {"status": SKIPPED_UNVERIFIED, "reason": f"receipts ledger not found: {ledger_path}"}

    try:
        ok, violations, chain_state = verify_chain(ledger_path)
    except OSError as exc:
        return {"status": SKIPPED_UNVERIFIED, "reason": f"could not read receipts ledger: {exc}"}

    configured = _chain_receipts_configured()

    if chain_state == "broken":
        return {
            "status": STATUS_FINDING,
            "chain_state": chain_state,
            "chain_receipts_configured": configured,
            "violation_count": len(violations),
            "violations_sample": violations[:5],
        }

    if chain_state == "unchained":
        return {
            "status": STATUS_FINDING if configured else STATUS_OK,
            "chain_state": chain_state,
            "chain_receipts_configured": configured,
            "reason": (
                "VNX_CHAIN_RECEIPTS is configured on but the ledger carries no prev_hash "
                "entries — chaining is not actually active on this ledger"
                if configured else
                "no entry carries prev_hash; integrity cannot be verified from this ledger "
                "alone. Not a defect while VNX_CHAIN_RECEIPTS stays fleet-default off — "
                "absence of evidence is not evidence of absence, so this is its own class, "
                "never folded into 'ok'"
            ),
        }

    # "verified" / "verified-segmented"
    return {
        "status": STATUS_OK,
        "chain_state": chain_state,
        "chain_receipts_configured": configured,
    }


def _highest_runner_backed_migration(migrations_dir: Path, runners_dir: Path) -> Optional[int]:
    """Highest NNNN under *migrations_dir* that has a paired ``apply_NNNN.py``
    runner in *runners_dir* — i.e. the highest migration ``migrations.auto_apply``
    can actually apply to ``runtime_coordination.db`` (OI-1169).

    Deliberately NOT the naive highest-numbered-filename in the directory:
    ``schemas/migrations/`` also holds date-named files (e.g.
    ``2026_05_intelligence_hygiene.sql``) that match the same ``NNNN_*.sql``
    discovery pattern with NNNN=2026 but target a different database and carry
    no runner — counting those would make every real store permanently
    "behind" a version no store can ever reach. None when no runner-backed
    migration exists at all (unmeasurable, not zero).
    """
    highest: Optional[int] = None
    for number, _sql_path in _auto_apply_discover_migrations(migrations_dir):
        if (runners_dir / f"apply_{number:04d}.py").exists():
            highest = number if highest is None else max(highest, number)
    return highest


def check_migration_staleness(
    state_dir: Path,
    *,
    migrations_dir: Optional[Path] = None,
    runners_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Compare runtime_coordination.db's PRAGMA user_version against the highest
    runner-backed migration under schemas/migrations/ (OI-1169).

    A store with no runtime_coordination.db yet has no schema state to be stale
    against — reported STATUS_OK (nothing to check), not SKIPPED_UNVERIFIED
    (mirrors check_pull_cursor's no-cursor+empty-ledger OK case). A DB that
    exists but cannot be opened/read, or a migrations directory that yields no
    runner-backed migration at all, IS unmeasurable.
    """
    db_path = state_dir / RUNTIME_DB_NAME
    mig_dir = migrations_dir or _AUTO_APPLY_MIGRATIONS_DIR
    run_dir = runners_dir or _AUTO_APPLY_RUNNERS_DIR

    if not db_path.exists():
        return {
            "status": STATUS_OK,
            "db_path": str(db_path),
            "db_exists": False,
            "reason": "no runtime_coordination.db yet — nothing to be stale against",
        }

    try:
        highest_available = _highest_runner_backed_migration(mig_dir, run_dir)
    except OSError as exc:
        return {"status": SKIPPED_UNVERIFIED, "reason": f"could not read migrations dir {mig_dir}: {exc}"}

    if highest_available is None:
        return {
            "status": SKIPPED_UNVERIFIED,
            "reason": f"no runner-backed migration found under {mig_dir} — cannot determine staleness",
        }

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            current_version = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return {"status": SKIPPED_UNVERIFIED, "reason": f"could not read {db_path}: {exc}"}

    behind = highest_available - current_version
    result: Dict[str, Any] = {
        "status": STATUS_FINDING if behind > 0 else STATUS_OK,
        "db_path": str(db_path),
        "db_exists": True,
        "current_user_version": current_version,
        "highest_available_migration": highest_available,
        "versions_behind": max(behind, 0),
    }
    if behind > 0:
        result["reason"] = (
            f"store is at user_version={current_version}, highest available "
            f"runner-backed migration is {highest_available:04d} — {behind} "
            "migration(s) behind. Run `vnx migrate` to catch it up."
        )
    return result


def compute_health(
    data_dir: Path,
    state_dir: Path,
    *,
    cursor_stale_hours: float = DEFAULT_CURSOR_STALE_HOURS,
) -> Dict[str, Any]:
    """Run all four checks read-only. Pure function: no writes, no mutation."""
    checks = {
        "receipt_coverage": check_receipt_coverage(state_dir),
        "pull_cursor": check_pull_cursor(state_dir, stale_hours=cursor_stale_hours),
        "chain_status": check_chain_status(state_dir),
        "migration_staleness": check_migration_staleness(state_dir),
    }

    statuses = {c["status"] for c in checks.values()}
    if SKIPPED_UNVERIFIED in statuses:
        overall = SKIPPED_UNVERIFIED
    elif STATUS_FINDING in statuses:
        overall = STATUS_FINDING
    else:
        overall = STATUS_OK

    exit_code = {STATUS_OK: EXIT_OK, STATUS_FINDING: EXIT_FINDINGS, SKIPPED_UNVERIFIED: EXIT_UNMEASURABLE}[overall]

    return {
        "component": COMPONENT_NAME,
        "data_dir": str(data_dir),
        "state_dir": str(state_dir),
        "overall_status": overall,
        "exit_code": exit_code,
        "cursor_stale_hours_threshold": cursor_stale_hours,
        "checks": checks,
    }


def write_health_surface(data_dir: Path, result: Dict[str, Any]) -> Path:
    """Atomically persist ``result`` as the ledger_health beacon.

    Reuses ``health_beacon.HealthBeacon`` — the same fcntl-locked
    tmp-write-then-``os.replace`` primitive every other component beacon
    under ``<data_dir>/health/`` already uses (t0_state_builder.json,
    plan-gate-panel.json, ...), so a reader gets the same all-or-nothing
    guarantee for this beacon it gets for any other.
    """
    from health_beacon import HealthBeacon

    beacon = HealthBeacon(
        data_dir, COMPONENT_NAME, expected_interval_seconds=BEACON_EXPECTED_INTERVAL_SECONDS
    )
    status = "ok" if result["overall_status"] == STATUS_OK else "fail"
    beacon.heartbeat_strict(status=status, details=result)
    return beacon.path


def read_health_surface(data_dir: Path) -> Optional[Dict[str, Any]]:
    """Read a previously-written ledger_health beacon. None if absent/unreadable.

    Consumed by ``vnx doctor`` (delegation seam — see ``vnx_cli/commands/
    doctor.py``'s ``_check_ledger_health``) so the doctor check never
    duplicates this module's threshold logic.
    """
    path = Path(data_dir) / "health" / f"{COMPONENT_NAME}.json"
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _resolve_default_dirs() -> Tuple[Path, Path]:
    """Ambient resolution via the canonical resolver — never a hardcoded
    ``.vnx-data/`` path (project CLAUDE.md)."""
    lib_dir = str(SCRIPT_DIR / "lib")
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    from vnx_paths import resolve_paths

    paths = resolve_paths()
    return Path(paths["VNX_DATA_DIR"]), Path(paths["VNX_STATE_DIR"])


def _format_human(result: Dict[str, Any]) -> str:
    lines = [f"ledger_health: {result['overall_status']} (exit {result['exit_code']})"]
    for name, check in result["checks"].items():
        status = check["status"]
        reason = check.get("reason") or ""
        lines.append(f"  {name}: {status}" + (f" — {reason}" if reason else ""))
        if name == "receipt_coverage" and check.get("missing_receipt_count"):
            for did in check["missing_receipt_dispatch_ids"]:
                lines.append(f"    - {did}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile dispatch_register.ndjson against t0_receipts.ndjson "
                     "(coverage, pull-cursor freshness, chain status, migration staleness). "
                     "Read-only on state.",
    )
    parser.add_argument("--data-dir", default=None, help="override VNX_DATA_DIR (default: ambient resolution)")
    parser.add_argument("--state-dir", default=None, help="override VNX_STATE_DIR (default: ambient resolution)")
    parser.add_argument(
        "--cursor-stale-hours", type=float, default=DEFAULT_CURSOR_STALE_HOURS,
        help=f"pull-cursor age threshold in hours (default: {DEFAULT_CURSOR_STALE_HOURS})",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument(
        "--no-write", action="store_true",
        help="do not write the health/ledger_health.json beacon (measure + print only)",
    )
    args = parser.parse_args(argv)

    default_data_dir, default_state_dir = (None, None)
    if args.data_dir is None or args.state_dir is None:
        default_data_dir, default_state_dir = _resolve_default_dirs()

    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir
    state_dir = Path(args.state_dir) if args.state_dir else default_state_dir

    result = compute_health(data_dir, state_dir, cursor_stale_hours=args.cursor_stale_hours)

    if not args.no_write:
        write_health_surface(data_dir, result)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(_format_human(result))

    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
