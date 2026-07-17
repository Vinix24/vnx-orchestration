"""append_receipt_payload pipeline + post-append hooks."""

from __future__ import annotations

import fcntl
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .common import (
    AppendReceiptError,
    AppendResult,
    EXIT_IO_ERROR,
    EXIT_INVALID_INPUT,
    REPO_ROOT,
    SCRIPTS_DIR,
    _emit,
    facade,
)
from .idempotency import (
    _compute_idempotency_key,
    _cache_file_for,
    _lock_file_for,
    _resolve_receipts_file,
    _write_receipt_under_lock,
)
from .validation import _validate_receipt

import logging
log = logging.getLogger(__name__)


def _maybe_reroute_to_gate_stream(receipt: Dict[str, Any], receipts_file: Optional[str]) -> Optional[str]:
    """Route ghost gate receipts (dispatch_id="unknown" + gate event) to gate_events.ndjson."""
    if receipts_file is not None or not facade.should_route_to_gate_stream(receipt):
        return receipts_file
    try:
        paths = facade.ensure_env()
        state_dir = Path(paths["VNX_STATE_DIR"])
        rerouted = str(facade.gate_events_file(state_dir))
        _emit("INFO", "ghost_receipt_rerouted",
              gate=str(receipt.get("gate") or ""),
              pr_id=str(receipt.get("pr_id") or ""),
              destination=rerouted)
        return rerouted
    except Exception as exc:
        _emit("WARN", "ghost_receipt_reroute_failed", error=str(exc))
        return receipts_file


def _run_post_append_hooks(receipt: Dict[str, Any]) -> None:
    """Best-effort hooks fired after a receipt is successfully appended.

    Each hook is isolated: a failure in one does not prevent the others from
    running, and no exception is propagated to the caller. The NDJSON record
    is already durable at this point.
    """
    try:
        facade._register_quality_open_items(receipt)
    except Exception as exc:
        _emit("WARN", "oi_registration_post_hook_failed",
              dispatch_id=str(receipt.get("dispatch_id") or ""),
              error=str(exc))
    try:
        facade._update_confidence_from_receipt(receipt)
    except Exception as exc:
        _emit("WARN", "confidence_post_hook_failed", error=str(exc))
    try:
        facade._emit_dispatch_register(receipt)
    except Exception as exc:
        _emit("WARN", "dispatch_register_post_hook_failed", error=str(exc))
    try:
        facade._maybe_trigger_state_rebuild(receipt)
    except Exception as exc:
        log.warning("payload: state rebuild hook failed: %s", exc)
    try:
        facade._trigger_receipt_classifier(receipt)
    except Exception as exc:
        log.warning("payload: receipt classifier hook failed: %s", exc)


def _stamp_observability_tier(receipt: Dict[str, Any]) -> None:
    """Stamp receipt with observability_tier from the producing adapter (best-effort).

    Resolves from the receipt's own `observability_tier` field (already set by
    an adapter-aware caller), then falls back to per-provider defaults from
    observability_tier.resolve_effective_tier().

    Caller-supplied `observability_tier` values are NEVER overwritten.
    Receipts with no `provider` field and no existing `observability_tier`
    are not modified — the field remains absent rather than guessing.
    """
    if receipt.get("observability_tier") is not None:
        return
    provider = str(receipt.get("provider") or "").lower().strip()
    if not provider:
        return
    try:
        sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
        from observability_tier import resolve_effective_tier
    except ImportError:
        return
    try:
        tier = resolve_effective_tier(provider)
        receipt["observability_tier"] = tier
    except Exception as exc:
        log.warning("payload: failed to resolve observability tier for provider %r: %s", provider, exc)


def resolve_central_data_dir(project_id: str) -> Path:
    """Module-level wrapper so tests can monkeypatch ``payload_mod.resolve_central_data_dir``."""
    from vnx_paths import resolve_central_data_dir as _resolve
    return _resolve(project_id)


def _pending_mirror_queue_for(receipt_path: Path) -> Path:
    return receipt_path.parent / "pending_mirrors.ndjson"


def _resolve_central_receipts_path(receipt: Dict[str, Any], primary_path: Path) -> Optional[Path]:
    project_id = str(receipt.get("project_id") or "").strip()
    if not project_id:
        return None
    central_base = resolve_central_data_dir(project_id)
    central_state = central_base / "state"
    central_receipts = central_state / "t0_receipts.ndjson"
    if central_receipts.resolve() == primary_path.resolve():
        return None
    return central_receipts


def _append_receipt_line_locked(receipts_path: Path, receipt: Dict[str, Any]) -> None:
    receipts_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = receipts_path.parent / "append_receipt.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        with receipts_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(receipt, separators=(",", ":"), sort_keys=False) + "\n")


def _mirror_receipt_to_central_or_raise(receipt: Dict[str, Any], primary_path: Path) -> bool:
    """Phase 6 P4: route the locked central append through scripts.lib.dual_writer.

    Path resolution stays inside this module (so test monkey-patches on
    ``payload.resolve_central_data_dir`` keep redirecting the mirror).
    The locked append itself is delegated to ``dual_writer.append_record_locked``
    so every dual-write call site (receipts + register events) shares one
    fcntl/atomicity implementation.

    Returns True on a successful central write, False on cutover skip or
    missing project_id, and raises ``OSError`` on I/O failure so the
    pending-mirror queue can retain the record for a later flush.
    """
    central_receipts = _resolve_central_receipts_path(receipt, primary_path)
    if central_receipts is None:
        return False
    try:
        sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
        from dual_writer import append_record_locked
    except Exception:
        _append_receipt_line_locked(central_receipts, receipt)
        return True
    append_record_locked(
        central_receipts, receipt, lock_filename="append_receipt.lock"
    )
    return True


def _mirror_receipt_to_central(receipt: Dict[str, Any], primary_path: Path) -> None:
    """Best-effort mirror of a receipt to the central path. Never raises.

    Phase 6 P3 dual-write: writes to ``~/.vnx-data/<project_id>/state/t0_receipts.ndjson``
    using the same ``append_receipt.lock`` locking convention as the primary writer.

    P5 cutover guard: skips when central_receipts resolves to the same file as
    primary_path so that at Phase 5 cutover there is no double-write.
    """
    project_id = str(receipt.get("project_id") or "").strip()
    if not project_id:
        return
    try:
        _mirror_receipt_to_central_or_raise(receipt, primary_path)
    except Exception as exc:
        log.warning("payload: central mirror failed for project %r: %s", project_id, exc)


def _load_pending_mirrors(queue_path: Path) -> List[Dict[str, Any]]:
    if not queue_path.exists():
        return []
    pending: List[Dict[str, Any]] = []
    try:
        with queue_path.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                receipt = entry.get("receipt")
                key = str(entry.get("idempotency_key") or "").strip()
                if isinstance(receipt, dict) and key:
                    pending.append({"idempotency_key": key, "receipt": receipt})
    except OSError as exc:
        raise AppendReceiptError(
            "pending_mirror_read_failed",
            EXIT_IO_ERROR,
            f"Failed to read pending mirror queue: {exc}",
        ) from exc
    return pending


def _write_pending_mirrors(queue_path: Path, pending: List[Dict[str, Any]]) -> None:
    tmp_path = queue_path.with_name(f"{queue_path.name}.{os.getpid()}.tmp")
    try:
        if not pending:
            if queue_path.exists():
                queue_path.unlink()
            return
        with tmp_path.open("w", encoding="utf-8") as fh:
            for entry in pending:
                fh.write(json.dumps(entry, separators=(",", ":"), sort_keys=False))
                fh.write("\n")
        os.replace(tmp_path, queue_path)
    except OSError as exc:
        raise AppendReceiptError(
            "pending_mirror_write_failed",
            EXIT_IO_ERROR,
            f"Failed to write pending mirror queue: {exc}",
        ) from exc
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


def _drain_pending_mirrors_and_mirror_current(
    receipt: Dict[str, Any],
    primary_path: Path,
    idempotency_key: str,
) -> int:
    queue_path = _pending_mirror_queue_for(primary_path)
    lock_path = _lock_file_for(primary_path)
    remaining: List[Dict[str, Any]] = []
    seen_keys: set[str] = set()
    last_error: Optional[Exception] = None

    def _keep_pending(entry: Dict[str, Any]) -> None:
        key = str(entry.get("idempotency_key") or "").strip()
        if not key or key in seen_keys:
            return
        pending_receipt = entry.get("receipt")
        if not isinstance(pending_receipt, dict):
            return
        seen_keys.add(key)
        remaining.append({"idempotency_key": key, "receipt": pending_receipt})

    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)

        for entry in _load_pending_mirrors(queue_path):
            try:
                _mirror_receipt_to_central_or_raise(entry["receipt"], primary_path)
            except Exception as exc:
                last_error = exc
                _keep_pending(entry)

        try:
            _mirror_receipt_to_central_or_raise(receipt, primary_path)
        except Exception as exc:
            last_error = exc
            _keep_pending({"idempotency_key": idempotency_key, "receipt": receipt})

        _write_pending_mirrors(queue_path, remaining)

    if remaining:
        fields: Dict[str, Any] = {
            "pending_count": len(remaining),
            "receipts_file": str(primary_path),
        }
        if last_error is not None:
            fields["error"] = str(last_error)
        _emit("WARN", "central_receipt_mirror_pending", **fields)

    return len(remaining)


def _stamp_identity(receipt: Dict[str, Any], *, identity_cwd: Optional[Path] = None) -> None:
    """Backfill the four-tuple identity fields on a receipt in place.

    Phase 6 P2: every NDJSON line should be attributable to
    {operator, project, orchestrator, agent}. Resolution is best-effort —
    when ``vnx_identity.try_resolve_identity()`` returns None (no env,
    no ``.vnx-project-id``, no registry hit), the receipt is written
    without identity fields rather than blocking the durability path.
    Caller-supplied values are never overwritten. Fields with no value
    are NOT serialized (we do not stamp ``"operator_id": null``).
    """
    try:
        sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
        from vnx_identity import try_resolve_identity
    except Exception:
        return

    identity = try_resolve_identity(cwd=identity_cwd)
    if identity is None:
        return

    if not receipt.get("operator_id"):
        receipt["operator_id"] = identity.operator_id
    if not receipt.get("project_id"):
        receipt["project_id"] = identity.project_id
    if not receipt.get("orchestrator_id") and identity.orchestrator_id:
        receipt["orchestrator_id"] = identity.orchestrator_id
    if not receipt.get("agent_id") and identity.agent_id:
        receipt["agent_id"] = identity.agent_id


def _stamp_ingested_at(receipt: Dict[str, Any]) -> None:
    """Stamp the authoritative ingest time — set by this append layer, NEVER
    merged from report-body content (report_to_receipt_converter.py copies its
    ``timestamp``/``recorded_at`` fields straight out of the worker's own
    report frontmatter, which a broken/adversarial worker can forge to an
    arbitrary old date). Staleness windowing for ``contract_invalid`` receipts
    (``report_contract_scope.contract_invalid_effective_timestamp``) keys off
    this field so a forged old body timestamp can no longer vanish a fresh
    failure from the fleet's counters. Caller-supplied values are never
    overwritten (idempotent re-append keeps the original ingest time).
    """
    if receipt.get("ingested_at"):
        return
    from datetime import datetime, timezone
    receipt["ingested_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append_receipt_payload(
    receipt: Dict[str, Any],
    *,
    receipts_file: Optional[str] = None,
    cache_window_seconds: int = 300,
    skip_enrichment: bool = False,
) -> AppendResult:
    if not isinstance(receipt, dict):
        raise AppendReceiptError("invalid_receipt_type", EXIT_INVALID_INPUT, "Receipt payload must be a JSON object")

    receipt_path = _resolve_receipts_file(receipts_file).expanduser().resolve()

    _stamp_identity(receipt, identity_cwd=receipt_path.parent)
    _stamp_observability_tier(receipt)

    if not skip_enrichment:
        receipt = facade._enrich_completion_receipt(receipt)

    receipt.setdefault("open_items_created", facade._count_quality_violations(receipt))

    receipts_file = _maybe_reroute_to_gate_stream(receipt, receipts_file)
    # Keep the resolved receipt path aligned with any gate-stream reroute.
    receipt_path = _resolve_receipts_file(receipts_file).expanduser().resolve()

    event_name = _validate_receipt(receipt)
    idempotency_key = _compute_idempotency_key(receipt, event_name)

    # Stamped AFTER the idempotency key so a fresh ingest time never perturbs
    # dedup (IDEMPOTENCY_FIELDS does not include ingested_at, but the rare
    # content-hash fallback branch would otherwise mint a new hash every call).
    _stamp_ingested_at(receipt)

    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path = _cache_file_for(receipt_path)

    result = _write_receipt_under_lock(
        receipt,
        receipt_path,
        cache_path,
        idempotency_key,
        cache_window_seconds,
    )

    if result.status == "appended":
        # Phase 6 P3 dual-write: drain persisted mirror debt before attempting
        # the current central write so transient mirror failures are repaired.
        # Best-effort only — the durable local write already succeeded; a mirror
        # failure (including pending-queue I/O) must never fail the append.
        try:
            _drain_pending_mirrors_and_mirror_current(receipt, receipt_path, idempotency_key)
        except Exception as exc:  # noqa: BLE001
            log.warning("payload: central mirror drain failed (best-effort, ignoring): %s", exc)
        if not skip_enrichment:
            _run_post_append_hooks(receipt)

    return result


def _update_confidence_from_receipt(receipt: Dict[str, Any]) -> None:
    """Wire dispatch outcome into pattern confidence scores (best-effort)."""
    try:
        SUCCESS_STATUSES = {"success", "completed", "complete", "ok", "", "done"}
        # Keep in sync with check_active_drain.FAILURE_STATUSES,
        # weekly_digest._FAILURE_STATUSES, and receipt_classifier._FAILURE_STATUSES
        # (gate-F2). contract_invalid = report-body-contract failure → semantically
        # a failure; confidence scoring should treat it as such.
        # "" is a payload-own addition (empty status string counts as success here).
        # "timeout" is intentionally excluded: task_timeout events reach the else-return
        # branch in _update_confidence_from_receipt and never arrive at this
        # FAILURE_STATUSES status-match (pre-existing behaviour, kept deliberately).
        FAILURE_STATUSES = {"failed", "failure", "error", "blocked", "contract_invalid"}

        event_type = str(receipt.get("event_type") or receipt.get("event") or "").lower()
        status = str(receipt.get("status", "")).lower()

        if event_type in ("task_complete", "task_completed"):
            if status in FAILURE_STATUSES:
                outcome = "failure"
            elif status in SUCCESS_STATUSES:
                outcome = "success"
            else:
                return
        elif event_type == "task_failed":
            outcome = "failure"
        else:
            return

        dispatch_id = str(receipt.get("dispatch_id") or "")
        terminal = str(receipt.get("terminal") or "")
        if not dispatch_id:
            return

        state_dir = facade.resolve_state_dir(__file__)

        db_path = state_dir / "quality_intelligence.db"
        if not db_path.exists():
            return

        sys.path.insert(0, str(SCRIPTS_DIR / "lib"))
        from intelligence_persist import update_confidence_from_outcome
        update_confidence_from_outcome(db_path, dispatch_id, terminal, outcome)
    except Exception as exc:
        _emit("WARN", "confidence_update_failed", error=str(exc))


def _trigger_receipt_classifier(receipt: Dict[str, Any]) -> None:
    """Best-effort fire of the adaptive receipt classifier (ARC-3).

    Disabled by default; opt-in via VNX_RECEIPT_CLASSIFIER_ENABLED=1. Never
    raises — the receipt writer must remain on its happy path even if the
    classifier import or subprocess spawn fails.
    """
    if os.environ.get("VNX_RECEIPT_CLASSIFIER_ENABLED", "0") != "1":
        return
    try:
        sys.path.insert(0, str(SCRIPTS_DIR / "lib"))
        from receipt_classifier import trigger_receipt_classifier_async
        action = trigger_receipt_classifier_async(receipt)
        if action:
            _emit("INFO", "receipt_classifier_action", action=action)
    except Exception as exc:
        _emit("WARN", "receipt_classifier_trigger_failed", error=str(exc))


def _maybe_trigger_state_rebuild(receipt: Dict[str, Any]) -> None:
    """Trigger state rebuild via shared throttled helper. Best-effort."""
    event_type = str(receipt.get("event_type") or receipt.get("event") or "").lower()

    TRIGGER_EVENTS = {
        "task_complete", "task_completed", "completion", "complete",
        "task_failed", "task_timeout",
        "dispatch_promoted", "dispatch_started",
    }
    if event_type not in TRIGGER_EVENTS:
        return

    try:
        sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
        from state_rebuild_trigger import maybe_trigger_state_rebuild
    except ImportError:
        return
    try:
        maybe_trigger_state_rebuild(event_type=event_type)
    except Exception as exc:
        log.warning("payload: state rebuild trigger failed for event %r: %s", event_type, exc)
