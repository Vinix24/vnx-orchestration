#!/usr/bin/env python3
"""gate_dispatch_identity_purge.py — audited removal of harness-lane gate
results that were booked under the builder's dispatch-id (OI-1725).

Since #1837 (commit 0188b016), gate_runner routed glm_gate/kimi_gate through
the harness-lane strategy using the BUILDER's dispatch-id from the request
payload. ``_read_report`` then read the builder's own unified report as the
gate's verdict, and ``materialize_artifacts`` never stamped provider/model.
Measured on ``pr-1840-kimi_gate.json`` and ``pr-1841-kimi_gate.json``: builder
dispatch ids + ``provider: None``/``model: None``.

The guard added in this same dispatch refuses a NEW poisoned record at write
time, but the records already on disk stay poisoned. This is a hand-triggered
CORRECTION tool — it removes those records, and only those, by what they ARE,
never by PR number: a fresh instance of the same collision is removed too.

A record is poisoned when it is a harness-lane (glm_gate/kimi_gate) gate
result whose:
  - ``dispatch_id`` is a builder dispatch (the ``YYYYMMDD-...`` shape), OR
  - report on disk has no verdict block but carries a builder's identity
    (a builder dispatch-id line) — the builder's own report, read back as the
    gate's verdict.

Safety: this script VERIFIES the misstand itself before writing anything. It
refuses (no write, exit 2) when no record on disk is provably poisoned — it
never deletes on an unverified claim, and an unparseable record is left
untouched and reported, never guessed at.

Usage:
    python3 scripts/gate_dispatch_identity_purge.py \\
        --state-dir ~/.vnx-data/vnx-dev/state                     # dry run
    python3 scripts/gate_dispatch_identity_purge.py \\
        --state-dir ~/.vnx-data/vnx-dev/state --write

Dry run is the default on purpose.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
for _p in (SCRIPT_DIR / "lib", SCRIPT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from gate_recorder import GATE_PROVIDER_HARNESS_LANE, resolve_gate_provider  # noqa: E402

_LOG = logging.getLogger(__name__)

_BUILDER_DISPATCH_RE = re.compile(r"\b\d{8}-[a-z0-9][a-z0-9-]*\b")
_VERDICT_BLOCK_RE = re.compile(r'"verdict"\s*:\s*"(pass|fail|blocked)"')


class PurgeRefused(RuntimeError):
    """No record on disk is provably poisoned — see the message for why."""


def is_builder_dispatch_id(dispatch_id: Optional[str]) -> bool:
    """True when ``dispatch_id`` is a builder dispatch (the ``YYYYMMDD-...``
    shape), not a gate-eigen id and not a runner-refusal slug.

    A POSITIVE structural check on the builder form, never a list of known
    builder ids — so a NEW builder dispatch collides the same way.
    """
    if not dispatch_id:
        return False
    return bool(re.match(r"^\d{8}-", str(dispatch_id)))


def has_verdict_block(text: Optional[str]) -> bool:
    """True when the report ends with the gate's structured JSON verdict block
    (``"verdict": "pass|fail|blocked"``). A builder's own report has none."""
    return bool(re.search(_VERDICT_BLOCK_RE, text or ""))


def carries_builder_identity(text: Optional[str]) -> bool:
    """True when the text carries a builder dispatch-id token. The gate's own
    report carries its gate-eigen id (``<shortname>-gate-pr...``), which never
    matches the builder shape."""
    return bool(re.search(_BUILDER_DISPATCH_RE, text or ""))


def _poison_reason(record: Dict[str, Any], report_text: Optional[str]) -> Optional[str]:
    """Return WHY ``record`` is a poisoned harness-lane gate result, or
    ``None`` when it is not provably poisoned. Never raises: an unprovable
    record is simply not a purge target."""
    gate = record.get("gate", "")
    provider = resolve_gate_provider(gate)
    if provider is None or provider[0] != GATE_PROVIDER_HARNESS_LANE:
        return None

    dispatch_id = record.get("dispatch_id") or ""
    if dispatch_id and is_builder_dispatch_id(dispatch_id):
        return (
            f"harness-lane gate record carries the builder's dispatch-id "
            f"{dispatch_id!r} instead of a gate-eigen id (OI-1725)"
        )

    if report_text is not None and report_text.strip():
        if not has_verdict_block(report_text) and carries_builder_identity(report_text):
            return (
                "harness-lane gate report has no verdict block and carries a "
                "builder's identity — the builder's own report read back as "
                "the gate's verdict (OI-1725)"
            )
    return None


def is_poisoned_harness_lane_record(
    record: Dict[str, Any], report_text: Optional[str] = None,
) -> bool:
    """True when ``record`` is a harness-lane gate result poisoned by a
    builder's identity. See :func:`_poison_reason` for the two shapes."""
    return _poison_reason(record, report_text) is not None


def _read_report_text(record: Dict[str, Any]) -> Optional[str]:
    report_path = record.get("report_path")
    if not report_path:
        return None
    path = Path(report_path)
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        _LOG.warning(
            "gate_dispatch_identity_purge: report unreadable at %s: %s", path, exc,
        )
        return None


def _audit_log_removal(
    state_dir: Path, entry: Path, record: Dict[str, Any], reason: str,
) -> None:
    """Append a removal entry to the governance audit trail (ADR-005).

    ``entry.unlink()`` alone erases a governance record with nothing left
    behind to show it ever existed. This reuses the repo's EXISTING
    governance audit trail (``governance_audit.ndjson``) — the same
    ``log_enforcement`` call ``gate_obligation_runner`` uses for its own
    ``provider_not_installed`` sweep and ``cleanup_orphan_gates.py`` uses for
    stale gate records, each under its own ``check_name``.

    ``governance_audit._data_dir()`` resolves ``VNX_DATA_DIR`` from the
    AMBIENT environment, so it is pinned here to this sweep's own
    ``state_dir`` — a store-scoped write must never depend on whatever
    ``VNX_DATA_DIR`` happens to be set to elsewhere in the process.

    Best-effort: an audit-write failure must never re-open the (already
    correct, already-completed) removal decision — logged loudly, never
    raised.
    """
    try:
        os.environ["VNX_DATA_DIR"] = str(Path(state_dir).parent)
        from governance_audit import log_enforcement  # noqa: PLC0415

        pr_number = record.get("pr_number")
        context: Dict[str, Any] = {
            "path": str(entry),
            "gate": record.get("gate"),
        }
        if isinstance(pr_number, int):
            context["pr_number"] = pr_number
        log_enforcement(
            check_name="gate_dispatch_identity_purge",
            level=1,
            result=True,
            context=context,
            message=(
                f"removed poisoned harness-lane gate result {entry.name} "
                f"(path={entry}, gate={record.get('gate')!r}, "
                f"dispatch_id={record.get('dispatch_id')!r}): {reason}"
            ),
            dispatch_id=record.get("dispatch_id") or None,
        )
    except Exception as exc:  # noqa: BLE001 — audit-write failure must not unwind the sweep
        _LOG.warning(
            "gate_dispatch_identity_purge: could not write governance_audit "
            "entry for removed %s: %s", entry, exc,
        )


def sweep_poisoned(state_dir: Path, *, write: bool) -> Dict[str, Any]:
    """Verify every harness-lane gate result on disk, then (if ``write``)
    remove the provably poisoned ones with an audit line each.

    Raises :class:`PurgeRefused` (no write) when nothing is provably poisoned.
    An unparseable record is never deleted — reported under ``unreadable`` and
    left exactly as it is.
    """
    results_dir = Path(state_dir) / "review_gates" / "results"
    if not results_dir.is_dir():
        raise PurgeRefused(f"results dir not found: {results_dir}")

    poisoned: List[Tuple[Path, Dict[str, Any], str]] = []
    unreadable: List[str] = []
    for entry in sorted(results_dir.glob("*.json")):
        try:
            record = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _LOG.warning(
                "gate_dispatch_identity_purge: unparseable record left "
                "untouched at %s: %s", entry, exc,
            )
            unreadable.append(entry.name)
            continue
        reason = _poison_reason(record, _read_report_text(record))
        if reason:
            poisoned.append((entry, record, reason))

    if not poisoned:
        raise PurgeRefused(
            "no provably poisoned harness-lane gate results found in "
            f"{results_dir} — nothing to purge"
        )

    outcome: Dict[str, Any] = {
        "action": "removed" if write else "would_remove",
        "state_dir": str(state_dir),
        "removed": [],
        "would_remove": [],
        "unreadable": unreadable,
    }
    for entry, record, reason in poisoned:
        if not write:
            outcome["would_remove"].append(entry.name)
            continue
        # ADR-005: the audit line lands before the state mutation, the same
        # order every other durable state-mutation in this fleet uses.
        _audit_log_removal(state_dir, entry, record, reason)
        entry.unlink()
        outcome["removed"].append(entry.name)
    return outcome


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--write", action="store_true", help="Persist the removal (default: dry run)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.state_dir is not None:
        state_dir = args.state_dir
    else:
        import vnx_paths  # noqa: PLC0415

        state_dir = Path(vnx_paths.ensure_env()["VNX_STATE_DIR"])
    if not state_dir.is_dir():
        print(f"ERROR: state dir not found: {state_dir}", file=sys.stderr)
        return 20

    try:
        outcome = sweep_poisoned(state_dir, write=args.write)
    except PurgeRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(outcome, indent=2))
    else:
        print(f"action={outcome['action']}")
        for name in outcome["removed"]:
            print(f"  removed {name}")
        for name in outcome["would_remove"]:
            print(f"  would_remove {name}")
        for name in outcome["unreadable"]:
            print(f"  unreadable (left untouched) {name}")
        if not args.write:
            print("  DRY RUN — pass --write to persist")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
