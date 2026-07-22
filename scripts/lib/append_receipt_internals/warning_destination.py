"""Warning-destination engine — ADR-035 §6.1/§6.2/§6.4.

Generalizes ``quality.py``'s single-warning-class dedup/promote logic
(``_register_quality_open_items``, ``_count_quality_violations_against_store``)
to an arbitrary ``warnings[]`` entry ``{code, severity, message}``. In one
pass, ``assign_destination`` computes both ``destination`` and
``requires_tracking`` for an entry — the same discipline §6.2 already
applies to ``open_items_created`` (one computation, one place).

PR-2 of the ADR-035 §9 decomposition: a library, not wired into either
write path yet (that is PR-4). Nothing here is called by
``append_receipt_payload`` or ``emit_dispatch_receipt``.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from .common import _emit, _get_open_items_manager

_PACKAGE_DIR = Path(__file__).resolve().parent
_SCRIPTS_LIB = _PACKAGE_DIR.parent
if str(_SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_LIB))

# ADR-035 §6.1: the four legal `destination` values.
LEGAL_DESTINATIONS = frozenset({"oi", "oi_pending", "counted", "dropped"})

# ADR-035 §6.1 (r3 HIGH-3): mirrors open_items_manager.py::SeverityLevel —
# the same closed vocabulary _SEVERITY_MAP (quality.py) already normalizes
# raw advisory severities into before calling add_item_programmatic.
LEGAL_SEVERITIES = frozenset({"blocker", "warn", "info"})

# ADR-035 §6.1 point 4: the closed allow-list of `destination: "dropped"`
# reasons — a free-form excuse is never legal, only one of these four.
DROP_REASON_ALLOWLIST = frozenset(
    {
        "retired_check",
        "duplicate_of_blocker",
        "superseded_by_code",
        "operator_acknowledged_noise",
    }
)

# ADR-035 §6.1 rule 1: default rolling-window recurrence threshold for
# promoting a recurring `severity: "warn"` code to `destination: "oi"`.
# Configurable per call via `assign_destination(..., threshold=...)`.
DEFAULT_RECURRENCE_THRESHOLD = 3

_COUNTER_FILENAME = "warning_recurrence_counts.json"

# ADR-035 §9 PR-4 fix-r1: internal marker stamped into `reason` by
# `classify_destination` for an entry whose side effect (OI-store promotion
# or counter increment) is DEFERRED, not yet performed. `commit_destination`
# looks for this exact sentinel to know which entries still need their
# write-side primitive called — never a real drop_reason (constrained to
# `DROP_REASON_ALLOWLIST`) or a real OI-store failure message, so it can
# never collide with a legitimate persisted value. Always overwritten by
# `commit_destination` before an entry reaches disk.
_PENDING_COMMIT_REASON = "__pending_append_commit__"


class WarningDestinationError(ValueError):
    """Raised when the engine is asked to compute an impossible destination.

    Distinct from AppendReceiptError (validation.py): this is a programming-
    error guard inside the engine itself (e.g. malformed input, contradictory
    caller-supplied drop_reason) — the append-time validator (§6.1's reject
    list) is the actual enforcement point for a receipt already on disk.
    """


def dedup_key_for(entry: Dict[str, Any]) -> str:
    """The dedup key an oi-bound warning is promoted under (ADR-035 §6.3).

    The pre-cutover quality-advisory dedup key (``qa:{check_id}:{file}:
    {symbol}``) is preserved verbatim — as the v2 warning's own ``code``
    value — so an already-tracked open item's dedup_key keeps matching a
    v2 warning describing the same underlying check; the cutover creates
    no duplicate or orphaned open items. Generalized to any warning class,
    the dedup key IS the entry's ``code``: the generalized ``{code,
    severity, message}`` shape carries no separate file/symbol fields to
    fold in, so genericizing "per warning-code" means the code itself.
    """
    return str(entry.get("code") or "")


def compute_requires_tracking(
    severity: str,
    recurrence_count: int,
    *,
    threshold: int = DEFAULT_RECURRENCE_THRESHOLD,
) -> bool:
    """ADR-035 §6.1 rule 1 / matrix: True exactly when `severity == "blocker"`,
    or `severity == "warn"` at/above the recurrence threshold. False
    otherwise (warn below threshold, or info at any recurrence)."""
    if severity == "blocker":
        return True
    if severity == "warn" and recurrence_count >= threshold:
        return True
    return False


def derive_open_items_created(warnings_list: Optional[Any]) -> int:
    """ADR-035 §6.2: `open_items_created` becomes derived — the count of
    `warnings[]` entries on THIS receipt that resolved to
    `destination: "oi"`. `oi_pending` entries are explicitly NOT counted:
    no open item exists yet for them (that count only moves once §6.4's
    reconcile step succeeds, which updates the open-items store directly,
    never this immutable receipt field)."""
    if not warnings_list:
        return 0
    return sum(
        1
        for entry in warnings_list
        if isinstance(entry, dict) and entry.get("destination") == "oi"
    )


def _default_counter_path() -> Path:
    from project_root import resolve_state_dir

    return resolve_state_dir(__file__) / _COUNTER_FILENAME


def _counter_lock_path(counter_path: Path) -> Path:
    return counter_path.with_name(counter_path.name + ".lock")


def _load_counter_data(counter_path: Path) -> Dict[str, int]:
    if not counter_path.exists():
        return {}
    try:
        with counter_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    result: Dict[str, int] = {}
    for key, value in data.items():
        try:
            result[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return result


def _atomic_write_counter_data(counter_path: Path, data: Dict[str, int]) -> None:
    counter_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = counter_path.with_name(f"{counter_path.name}.{os.getpid()}.tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"), sort_keys=True)
    os.replace(tmp_path, counter_path)


def _peek_counter(counter_path: Path, key: str) -> int:
    """Read-only: current persisted occurrence count for `key` (0 if unseen)."""
    return _load_counter_data(counter_path).get(key, 0)


def _increment_counter(counter_path: Path, key: str) -> int:
    """Atomically increment and persist the occurrence count for `key`.

    This is Part C's "counted"-warning counter primitive — the rolling
    per-code tally `receipt_query.py digest` (PR-7) will read. Locked
    read-modify-write + atomic tmp-then-rename write, consistent with the
    Codex Defense Checklist's rule for any rewrite of a persistent state
    file consumed by concurrent callers.
    """
    lock_path = _counter_lock_path(counter_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            data = _load_counter_data(counter_path)
            new_value = data.get(key, 0) + 1
            data[key] = new_value
            _atomic_write_counter_data(counter_path, data)
            return new_value
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def _counter_key(code: str, scope: str) -> str:
    return f"{code}:{scope}" if scope else code


def classify_destination(
    entry: Dict[str, Any],
    *,
    scope: str = "",
    threshold: int = DEFAULT_RECURRENCE_THRESHOLD,
    recurrence_count: Optional[int] = None,
    counter_path: Optional[Path] = None,
    drop_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Side-effect-free half of `assign_destination` (ADR-035 §9 PR-4 fix-r1).

    Computes `{destination, oi_id, reason, requires_tracking}` for one
    `warnings[]` entry WITHOUT touching the open-items store or the
    persistent recurrence counter — the only I/O is a read-only peek at the
    counter (never an increment), needed to decide `requires_tracking` for a
    `severity: "warn"` entry.

    An entry that `requires_tracking` gets the placeholder
    `destination: "oi_pending"` / `oi_id: None` — the real
    `add_item_programmatic` call (and its oi/oi_pending resolution) is
    deferred to `commit_destination`. An entry that resolves to `"counted"`
    similarly gets a placeholder `reason` (`_PENDING_COMMIT_REASON`) marking
    that its counter increment is still pending. `"dropped"` needs no
    placeholder: it is a pure, final decision from the caller-supplied
    `drop_reason` alone.

    See `assign_destination` for the full parameter contract this mirrors.
    Never mutates `entry`; always returns a new dict.
    """
    code = str(entry.get("code") or "").strip()
    if not code:
        raise WarningDestinationError("warning entry missing required 'code'")

    severity = entry.get("severity")
    if severity not in LEGAL_SEVERITIES:
        raise WarningDestinationError(
            f"warning entry has unrecognized severity: {severity!r} "
            f"(expected one of {sorted(LEGAL_SEVERITIES)})"
        )

    if severity == "warn":
        if recurrence_count is not None:
            effective_recurrence = recurrence_count
        else:
            path = counter_path or _default_counter_path()
            effective_recurrence = _peek_counter(path, _counter_key(code, scope)) + 1
        requires_tracking = compute_requires_tracking(
            severity, effective_recurrence, threshold=threshold
        )
    elif severity == "blocker":
        requires_tracking = True
    else:  # info
        requires_tracking = False

    result: Dict[str, Any] = dict(entry)
    result["requires_tracking"] = requires_tracking

    if requires_tracking:
        if drop_reason is not None:
            raise WarningDestinationError(
                "cannot set drop_reason on a warning that requires_tracking"
            )
        result["destination"] = "oi_pending"
        result["oi_id"] = None
        result["reason"] = _PENDING_COMMIT_REASON
        return result

    if drop_reason is not None:
        if drop_reason not in DROP_REASON_ALLOWLIST:
            raise WarningDestinationError(
                f"drop_reason {drop_reason!r} is not in the allow-list "
                f"{sorted(DROP_REASON_ALLOWLIST)}"
            )
        result["destination"] = "dropped"
        result["oi_id"] = None
        result["reason"] = drop_reason
        return result

    result["destination"] = "counted"
    result["oi_id"] = None
    result["reason"] = _PENDING_COMMIT_REASON
    return result


def commit_destination(
    classified: Dict[str, Any],
    *,
    dispatch_id: str = "",
    report_path: str = "",
    pr_id: str = "",
    scope: str = "",
    recurrence_count: Optional[int] = None,
    counter_path: Optional[Path] = None,
    open_items_manager_module: Optional[Any] = None,
) -> Dict[str, Any]:
    """Side-effect commit half of `assign_destination` (ADR-035 §9 PR-4
    fix-r1). Takes a `classify_destination`-produced entry and performs the
    deferred write:

      - `destination: "oi_pending"` with the pending-commit placeholder
        reason and `requires_tracking: True`: calls `add_item_programmatic`,
        resolving to `"oi"` + a real `oi_id` on success, or staying
        `"oi_pending"` with the real failure reason on error — exactly the
        outcome `assign_destination` used to compute inline (ADR-035 §6.4).
      - `destination: "counted"` with the pending-commit placeholder reason:
        increments the persisted recurrence counter, then resolves `reason`
        back to `None`.
      - anything else (a pre-resolved `"dropped"`, or an already-committed
        entry): passed through unchanged — never double-committed.

    Callers MUST only invoke this once the shared append primitive has
    confirmed the receipt carrying `classified` will actually be written
    (not deduped, not rejected by the validator) — committing a side effect
    for a receipt that never lands on disk is exactly the bug this split
    fixes. Never mutates `classified`; always returns a new dict.
    """
    destination = classified.get("destination")
    result: Dict[str, Any] = dict(classified)

    if destination == "oi_pending" and classified.get("requires_tracking"):
        code = str(classified.get("code") or "")
        severity = classified.get("severity")
        message = classified.get("message", "") or ""
        oim = open_items_manager_module or _get_open_items_manager()
        dedup_key = dedup_key_for(classified)
        try:
            item_id, _created = oim.add_item_programmatic(
                title=message or code,
                severity=severity,
                dispatch_id=dispatch_id,
                report_path=report_path,
                pr_id=pr_id,
                details=message,
                dedup_key=dedup_key,
                source="warning_destination",
            )
            result["destination"] = "oi"
            result["oi_id"] = item_id
            result["reason"] = None
        except Exception as exc:
            # ADR-035 §6.4: the OI store itself failed (raised, or is
            # unreachable/locked) — never swallow-and-log-only (the
            # precursor's quality.py:146-147 bug this generalization
            # closes). Stamp the specific failure so it is durably
            # attributable and recoverable via reconcile-oi-pending.
            _emit(
                "WARN",
                "warning_oi_promotion_failed",
                warning_code=code,
                dispatch_id=dispatch_id,
                error=str(exc),
            )
            result["destination"] = "oi_pending"
            result["oi_id"] = None
            result["reason"] = str(exc)
        return result

    if destination == "counted" and classified.get("reason") == _PENDING_COMMIT_REASON:
        code = str(classified.get("code") or "")
        if recurrence_count is None:
            path = counter_path or _default_counter_path()
            _increment_counter(path, _counter_key(code, scope))
        result["reason"] = None
        return result

    return result


def assign_destination(
    entry: Dict[str, Any],
    *,
    dispatch_id: str = "",
    report_path: str = "",
    pr_id: str = "",
    scope: str = "",
    threshold: int = DEFAULT_RECURRENCE_THRESHOLD,
    recurrence_count: Optional[int] = None,
    counter_path: Optional[Path] = None,
    drop_reason: Optional[str] = None,
    open_items_manager_module: Optional[Any] = None,
) -> Dict[str, Any]:
    """Compute `{destination, oi_id, reason, requires_tracking}` for one
    `warnings[]` entry `{code, severity, message}`, in a single pass
    (ADR-035 §6.1/§6.4).

    ADR-035 §9 PR-4 fix-r1: composed from `classify_destination` (pure) +
    `commit_destination` (the write-side primitive) run back-to-back — same
    externally-observable result as before the split, for callers (this
    function, and `receipt_finalize.finalize_receipt_v2_fields`) that want
    the old all-in-one behavior with no deferred-commit step. The write
    paths (`append_receipt_internals.payload`, `governance_emit`) call the
    two halves separately instead, so the commit only fires once the append
    primitive confirms the receipt will actually be written.

    Args:
        entry: the warning entry, at minimum `{code, severity}`.
        dispatch_id/report_path/pr_id: forwarded to `add_item_programmatic`
            when promotion fires.
        scope: dispatch-adjacent scope (skill/terminal) the recurrence
            threshold is keyed on alongside `code` — §6.1 rule 1.
        threshold: recurrence threshold for `severity: "warn"` promotion.
            Default 3, per §6.1.
        recurrence_count: when given, used directly as "this occurrence's
            recurrence count" — bypasses the on-disk rolling-window store
            entirely, making the engine unit-testable deterministically
            without a write-path. When omitted (the default production
            path), the engine reads/increments a persistent per-(code,
            scope) counter at `counter_path`.
        counter_path: where the rolling-window/counted-counter is
            persisted. Defaults to `<state_dir>/warning_recurrence_counts.json`
            when omitted and `recurrence_count` was not injected.
        drop_reason: when given (and the entry does not require tracking),
            resolves to `destination: "dropped"` with this reason — must be
            drawn from `DROP_REASON_ALLOWLIST`. Raises `WarningDestinationError`
            if the entry requires tracking (a blocker/promoted-warn can
            never be silently dropped) or the reason is not allow-listed.
        open_items_manager_module: injectable `open_items_manager` module
            (tests use this to control/observe the real OI store, or to
            simulate an unreachable/locked store for the oi_pending path).

    Returns:
        A new dict: a copy of `entry` plus `destination`, `oi_id`, `reason`,
        `requires_tracking`. The input `entry` is never mutated.
    """
    classified = classify_destination(
        entry,
        scope=scope,
        threshold=threshold,
        recurrence_count=recurrence_count,
        counter_path=counter_path,
        drop_reason=drop_reason,
    )
    return commit_destination(
        classified,
        dispatch_id=dispatch_id,
        report_path=report_path,
        pr_id=pr_id,
        scope=scope,
        recurrence_count=recurrence_count,
        counter_path=counter_path,
        open_items_manager_module=open_items_manager_module,
    )
