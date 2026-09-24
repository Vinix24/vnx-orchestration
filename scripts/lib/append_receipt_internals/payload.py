"""append_receipt_payload pipeline + post-append hooks."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .common import (
    AppendReceiptError,
    AppendResult,
    EXIT_IO_ERROR,
    EXIT_INVALID_INPUT,
    EXIT_UNEXPECTED_ERROR,
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
    _resolve_state_dir_env_first,
    _write_receipt_under_lock,
)
from .receipt_finalize import classify_receipt_v2_warnings, commit_receipt_v2_fields
from .validation import _validate_receipt

# Sibling scripts/lib module (scripts/lib is on sys.path whenever this package
# is imported, since append_receipt_internals lives there). Single source of
# truth for the monotonic receipt-schema stamp — see ledger_schema_version.py.
from ledger_schema_version import CURRENT_SCHEMA_VERSION

import logging
log = logging.getLogger(__name__)


def _maybe_reroute_to_gate_stream(receipt: Dict[str, Any], receipts_file: Optional[str]) -> Optional[str]:
    """Route ghost gate receipts (dispatch_id="unknown" + gate event) to gate_events.ndjson."""
    if receipts_file is not None or not facade.should_route_to_gate_stream(receipt):
        return receipts_file
    try:
        state_dir = _resolve_state_dir_env_first()
        rerouted = str(facade.gate_events_file(state_dir))
        _emit("INFO", "ghost_receipt_rerouted",
              gate=str(receipt.get("gate") or ""),
              pr_id=str(receipt.get("pr_id") or ""),
              destination=rerouted)
        return rerouted
    except Exception as exc:
        _emit("WARN", "ghost_receipt_reroute_failed", error=str(exc))
        return receipts_file


def _run_post_append_hooks(
    receipt: Dict[str, Any], *, state_dir: Optional[Path] = None, advisory: bool = True,
) -> None:
    """Best-effort hooks fired after a receipt is successfully appended.

    Each hook is isolated: a failure in one does not prevent the others from
    running, and no exception is propagated to the caller. The NDJSON record
    is already durable at this point.

    Two groups, because they answer to different callers:

    * ``advisory`` hooks (open-item registration, state rebuild, receipt
      classifier) belong to the enrichment the caller may opt out of with
      ``skip_enrichment``.
    * The confidence update is an OUTCOME hook. It reads only ``event_type``,
      ``status``, ``dispatch_id`` and ``terminal``, never anything enrichment
      adds, so it always runs. It writes to ``state_dir``: the store this very
      receipt was appended to, not one derived from this file's checkout.
    """
    if advisory:
        try:
            facade._register_quality_open_items(receipt)
        except Exception as exc:
            _emit("WARN", "oi_registration_post_hook_failed",
                  dispatch_id=str(receipt.get("dispatch_id") or ""),
                  error=str(exc))
    try:
        facade._update_confidence_from_receipt(receipt, state_dir=state_dir)
    except Exception as exc:
        _emit("WARN", "confidence_post_hook_failed", error=str(exc))
    if not advisory:
        return
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
    """Module-level wrapper so tests can monkeypatch ``payload_mod.resolve_central_data_dir``.

    OI-1043: the dual-write mirror must be pinnable. An explicit store pin —
    ``VNX_STATE_DIR`` (the literal override ``vnx_paths.resolve_paths()``
    honors) or ``VNX_DATA_DIR_EXPLICIT=1`` + ``VNX_DATA_DIR`` — is a
    deliberate statement that the store lives THERE, so the mirror's central
    base resolves through the same pin instead of blindly targeting
    ``~/.vnx-data/<project_id>``. A receipt writer that cannot be pinned can
    write to the wrong ledger in production too; before this fix the test
    suite's pinned appends leaked 684+ lines into the real central ledger
    through exactly this path. With no pin set, resolution delegates to
    ``vnx_paths.resolve_central_data_dir`` unchanged (migration dual-write
    semantics preserved).
    """
    state_pin = (os.environ.get("VNX_STATE_DIR") or "").strip()
    if state_pin:
        # resolve_paths() convention: VNX_STATE_DIR is <data_dir>/state, so
        # its parent is the data dir the mirror base is derived from.
        return Path(state_pin).expanduser().parent
    if os.environ.get("VNX_DATA_DIR_EXPLICIT") == "1":
        data_pin = (os.environ.get("VNX_DATA_DIR") or "").strip()
        if data_pin:
            return Path(data_pin).expanduser()
    from vnx_paths import resolve_central_data_dir as _resolve
    return _resolve(project_id)


def _isolation_guard_error_class():
    """Lazy import: TestIsolationGuardError for except-clauses (keeps the
    vnx_paths import off the module import path)."""
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
    from vnx_paths import TestIsolationGuardError
    return TestIsolationGuardError


def _refuse_real_store_write_under_test_runner(target: Path) -> None:
    """OI-1043 guard seam: refuse an imminent WRITE into the real central
    store (~/.vnx-data) while running under a test runner (pytest or unittest).
    No-op in any other process."""
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
    from vnx_paths import refuse_real_central_store_write_under_test_runner as _refuse
    _refuse(target)


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

    OI-1043: raises ``TestIsolationGuardError`` when the resolved central
    target is the real central store and the process runs under a test runner —
    that is an isolation violation, not retryable mirror debt, and callers
    must re-raise it rather than queue the record.
    """
    central_receipts = _resolve_central_receipts_path(receipt, primary_path)
    if central_receipts is None:
        return False
    _refuse_real_store_write_under_test_runner(central_receipts)
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
    except _isolation_guard_error_class():
        raise
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
            except _isolation_guard_error_class():
                # OI-1043: a test-isolation violation is not retryable mirror
                # debt — fail the test, do not queue the record.
                raise
            except Exception as exc:
                last_error = exc
                _keep_pending(entry)

        try:
            _mirror_receipt_to_central_or_raise(receipt, primary_path)
        except _isolation_guard_error_class():
            raise
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


_CHAIN_LINK_ENV_FIELDS = (
    ("VNX_PARENT_DISPATCH", "parent_dispatch"),
    ("VNX_TASK_CLASS", "task_class"),
    ("VNX_TIER_FROM", "tier_from"),
    ("VNX_TIER_TO", "tier_to"),
)


def _stamp_model_identity(receipt: Dict[str, Any]) -> None:
    """Normalize model to the canonical wave7_models.yaml key + chain-link env fallback.

    dispatch-20260802-model-ssot-en-ketenlink: the door exports the chain-link
    fields (parent_dispatch / task_class / tier_from / tier_to) as env vars that
    the tmux worker pane inherits, so a worker-authored receipt lands the same
    values the plan carried. Explicit caller-supplied fields are never
    overwritten. The model string is normalized to its canonical registry key
    (deepseek/deepseek-v4-pro -> deepseek-v4-pro, kimi-code/k3 -> kimi-k3,
    claude-sonnet-5 -> sonnet-5, ...) so the ledger is groupable per model.
    Best-effort: a normalizer failure leaves the raw string for the fail-closed
    validator to judge.
    """
    for env_name, field in _CHAIN_LINK_ENV_FIELDS:
        if receipt.get(field):
            continue
        env_val = (os.environ.get(env_name) or "").strip()
        if env_val:
            receipt[field] = env_val
    raw = receipt.get("model")
    if raw is None or str(raw).strip() == "":
        return
    try:
        sys.path.insert(0, str(SCRIPTS_DIR / "lib"))
        from providers.model_normalizer import normalize_model_name  # noqa: PLC0415
        receipt["model"] = normalize_model_name(raw)
    except Exception:  # noqa: BLE001 — normalization is best-effort; fail-closed validation still runs
        log.debug("payload: model normalization skipped: %s", raw, exc_info=True)


def _stamp_ingested_at(receipt: Dict[str, Any]) -> None:
    """Stamp the authoritative ingest time — ALWAYS set to now by this append
    layer, never preserved from a caller-supplied value.

    ``append_receipt.py`` accepts worker-authored JSON directly, so a
    pre-set ``ingested_at`` is exactly as forgeable as ``timestamp`` (which
    ``report_to_receipt_converter.py``/``report_parser.py`` copy straight out
    of the worker's own report frontmatter). Honoring a caller-supplied value
    would let a broken/adversarial worker pre-stamp an old ``ingested_at`` and
    defeat the staleness windowing (``contract_invalid_window.is_stale_contract_invalid``)
    this field exists to make forge-proof. A re-append of the same receipt
    content simply gets a fresh ingest time for that write — nothing
    downstream keys dedup off this field.
    """
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

    # ADR-035 §9 PR-5 (HIGH-6): stamped atomically with the trimmed v2 shape
    # in this same PR, on the one shared Path-2 entry point every writer
    # (report_parser.py output, governance_receipts.py, state_mutation.py,
    # raw worker-authored JSON via the append_receipt.py CLI) funnels
    # through — never touched separately from the shape change.
    receipt.setdefault("schema_version", CURRENT_SCHEMA_VERSION)

    receipt_path = _resolve_receipts_file(receipts_file).expanduser().resolve()

    _stamp_identity(receipt, identity_cwd=receipt_path.parent)
    _stamp_observability_tier(receipt)

    if not skip_enrichment:
        receipt = facade._enrich_completion_receipt(receipt)

    receipt.setdefault("open_items_created", facade._count_quality_violations(receipt))

    receipts_file = _maybe_reroute_to_gate_stream(receipt, receipts_file)
    # Keep the resolved receipt path aligned with any gate-stream reroute.
    receipt_path = _resolve_receipts_file(receipts_file).expanduser().resolve()

    # dispatch-20260802-model-ssot-en-ketenlink: normalize model to the
    # canonical registry key + stamp the door's chain-link fields before the
    # shared validator sees the receipt (the fail-closed model check runs in
    # _validate_receipt).
    _stamp_model_identity(receipt)

    # ADR-035 §9 PR-4 (fix-r1): pure classification of warnings[] (no side
    # effects — no OI-store writes, no counter increments) before the shared
    # validator sees it. Both write paths call this same classify step
    # (governance_emit.emit_dispatch_receipt is the other). The matching
    # side-effect commit (commit_receipt_v2_fields) runs only once
    # _write_receipt_under_lock confirms this receipt is not a duplicate and
    # will actually be written — see that function's pre_write_hook.
    classify_receipt_v2_warnings(receipt)

    event_name = _validate_receipt(receipt)
    idempotency_key = _compute_idempotency_key(receipt, event_name)

    # Stamped AFTER the idempotency key so a fresh ingest time never perturbs
    # dedup (IDEMPOTENCY_FIELDS does not include ingested_at).
    _stamp_ingested_at(receipt)

    # OI-1043: fail loud when a test is about to WRITE the primary ledger
    # straight into the real central store (the #1333 guard did not cover
    # the receipt append surfaces — the suite leaked 684+ lines through the
    # mirror, and an explicit receipts_file under ~/.vnx-data wrote through
    # unguarded). No-op outside a pytest or unittest run.
    _refuse_real_store_write_under_test_runner(receipt_path)

    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path = _cache_file_for(receipt_path)

    result = _write_receipt_under_lock(
        receipt,
        receipt_path,
        cache_path,
        idempotency_key,
        cache_window_seconds,
        pre_write_hook=commit_receipt_v2_fields,
    )

    # OI-948: _write_receipt_under_lock is annotated -> AppendResult and
    # every code path returns an AppendResult instance.  A None here would
    # mean a silent implicit return slipped in (e.g. a broad except that
    # swallows the raise without replacing it).  Fail closed with a precise
    # message so the source is traceable instead of surfacing downstream as
    # "'NoneType' object has no attribute 'status'" at payload.py:446.
    if result is None:
        raise AppendReceiptError(
            "internal_null_result",
            EXIT_UNEXPECTED_ERROR,
            "payload.py: _write_receipt_under_lock returned None — "
            "this is a framework-internal invariant violation; "
            "the append lock path exited without a result object",
        )

    # OI-1425: the register emit must fire for a "duplicate" (idempotent-skip)
    # result too, not only "appended". A duplicate means the identical receipt
    # content was already durably written earlier in this cache window — that
    # earlier append is exactly the one whose own register-emit attempt may
    # have failed (fail-open, per _emit_dispatch_register's contract below),
    # in which case this is the only remaining chance to record the register
    # event for this dispatch. Re-emitting for a genuine duplicate is a
    # harmless repeat of an already-idempotent event (register readers key on
    # dispatch_id + event, never on a per-emit count), so this is never gated
    # behind the "appended"-only side effects below (mirror drain, OI
    # registration, confidence update) — those must still run exactly once.
    if result.status in ("appended", "duplicate"):
        try:
            facade._emit_dispatch_register(receipt)
        except Exception as exc:
            _emit("WARN", "dispatch_register_post_hook_failed", error=str(exc))

    if result.status == "appended":
        # Phase 6 P3 dual-write: drain persisted mirror debt before attempting
        # the current central write so transient mirror failures are repaired.
        # Best-effort only — the durable local write already succeeded; a mirror
        # failure (including pending-queue I/O) must never fail the append.
        try:
            _drain_pending_mirrors_and_mirror_current(receipt, receipt_path, idempotency_key)
        except _isolation_guard_error_class():
            # OI-1043: never swallow a test-isolation violation as best-effort.
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("payload: central mirror drain failed (best-effort, ignoring): %s", exc)
        _run_post_append_hooks(
            receipt, state_dir=receipt_path.parent, advisory=not skip_enrichment,
        )

    return result


@contextmanager
def _confidence_update_lock(state_dir: Path) -> Iterator[None]:
    """Serialize receipt-driven confidence updates on one store.

    The lane, the report parser and the report converter can each book a
    receipt for the same dispatch, and each reaches the confidence hook. The
    recorded-check and the update must not interleave between two processes, or
    both would find nothing recorded and both would count the outcome.
    Fail-open: a store directory that cannot hold a lock file (it does not
    exist) gets no lock, the same posture as the rest of the best-effort hooks.
    """
    try:
        handle = (Path(state_dir) / "confidence_update.lock").open("a+", encoding="utf-8")
    except OSError:
        yield
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        handle.close()


def _confidence_event_recorded(db_path: Path, dispatch_id: str, outcome: str) -> bool:
    """True when confidence_events already holds this dispatch's outcome.

    One dispatch outcome counts once. ``update_confidence_from_outcome`` has no
    memory of its own (each call is one more observation, which its streak
    semantics rely on), so the receipt-driven caller asks first. Scoped to the
    project when the table carries ``project_id`` (ADR-007). An unreadable
    store or a store without the table has recorded nothing.
    """
    try:
        sys.path.insert(0, str(SCRIPTS_DIR / "lib"))
        from project_scope import current_project_id
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(confidence_events)")}
            if not columns:
                return False
            sql = "SELECT 1 FROM confidence_events WHERE dispatch_id = ? AND outcome = ?"
            params: List[Any] = [dispatch_id, outcome]
            if "project_id" in columns:
                sql += " AND project_id = ?"
                params.append(current_project_id())
            return conn.execute(sql + " LIMIT 1", params).fetchone() is not None
        finally:
            conn.close()
    except (sqlite3.Error, ValueError):
        return False


# Failure classes (failure_classification.FAILURE_CLASSES) that say something
# about the dispatch's WORK. `completion_without_execution` is the model
# claiming tool calls that left no change; `unknown` is unclassified and is not
# proven to be infrastructure. Every other class (auth_rejected,
# empty_completion, credit_exhausted, model_error, no_verdict, tool_missing,
# timeout) is the lane or the provider failing. Derived as a complement so a
# class added later defaults to "no outcome signal" instead of decaying
# patterns on a fault they had nothing to do with.
_WORK_FAILURE_CLASSES = frozenset({"completion_without_execution", "unknown"})


def _is_infrastructure_failure(receipt: Dict[str, Any]) -> bool:
    """True when the receipt's failure_class names a lane/provider fault."""
    failure_class = str(receipt.get("failure_class") or "").strip().lower()
    if not failure_class:
        return False
    sys.path.insert(0, str(SCRIPTS_DIR / "lib"))
    from failure_classification import FAILURE_CLASSES
    return failure_class in FAILURE_CLASSES and failure_class not in _WORK_FAILURE_CLASSES


def _update_confidence_from_receipt(
    receipt: Dict[str, Any], state_dir: Optional[Path] = None,
) -> None:
    """Wire dispatch outcome into pattern confidence scores (best-effort).

    OI-1148: the event_type + status -> outcome vocabulary used to be
    hand-copied here (drifting from register_emit.py's independent copy —
    e.g. this set already had "done", register_emit.py's didn't). Both now
    read event_outcome_semantics, the single canonical source.

    Scope is intentionally narrower than the canonical COMPLETION_EVENT_TYPES:
    this function has never classified "subprocess_completion" receipts (only
    "task_complete"/"task_completed"), and "timeout" is intentionally excluded
    from the failure vocabulary here — task_timeout events never reach this
    function's outcome branch at all (pre-existing behaviour, kept
    deliberately; confidence scoring treats a timeout as "no outcome signal",
    not as a failure). A failure whose ``failure_class`` names the lane or the
    provider (see ``_WORK_FAILURE_CLASSES``) is the same kind of non-signal:
    the patterns offered to a dispatch that never ran did not fail.

    ``state_dir`` is the store the receipt was appended to; the caller passes
    ``receipt_path.parent``. Without it the store resolves the way the receipt
    writer itself resolves it (``VNX_STATE_DIR``, else the central per-project
    store, ADR-026). It is never derived from this file's own git checkout:
    that landed in ``<checkout>/.vnx-data/state``, a store without the
    intelligence tables, so every update was a silent no-op.
    """
    try:
        sys.path.insert(0, str(SCRIPTS_DIR / "lib"))
        from event_outcome_semantics import FAILURE_STATUSES, SUCCESS_STATUSES

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

        if outcome == "failure" and _is_infrastructure_failure(receipt):
            return

        dispatch_id = str(receipt.get("dispatch_id") or "")
        # Path-1 lane receipts (ReceiptV2) carry ``terminal_id``, the
        # report-derived ones ``terminal``.
        terminal = str(receipt.get("terminal") or receipt.get("terminal_id") or "")
        if not dispatch_id:
            return

        if state_dir is None:
            state_dir = _resolve_state_dir_env_first()

        db_path = Path(state_dir) / "quality_intelligence.db"
        if not db_path.exists():
            return

        sys.path.insert(0, str(SCRIPTS_DIR / "lib"))
        from intelligence_persist import update_confidence_from_outcome
        with _confidence_update_lock(db_path.parent):
            if _confidence_event_recorded(db_path, dispatch_id, outcome):
                return
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
