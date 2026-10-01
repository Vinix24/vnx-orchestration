"""receipt_writer — receipt persistence + auto-commit/auto-stash for dispatches.

DEPRECATED (Wave 7 PR-7.6): _write_receipt is subprocess-adapter-specific and
retained for backward compatibility. New code should call
``governance_emit.emit_dispatch_receipt`` / ``governance_emit.emit_unified_report``
for the canonical governance receipt format (includes provider, model, token_usage).
``emit_dispatch_receipt`` is re-exported below for callers already importing from
this internal module path.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .path_utils import _get_dirty_files
from .state_paths import _default_state_dir

logger = logging.getLogger(__name__)

# Re-export shared module surface so callers can migrate at their own pace.
try:
    import sys as _sys
    _lib_dir = str(Path(__file__).resolve().parents[1])
    if _lib_dir not in _sys.path:
        _sys.path.insert(0, _lib_dir)
    from governance_emit import emit_dispatch_receipt, emit_unified_report  # noqa: F401
except ImportError:
    pass  # governance_emit not yet on path — callers will import directly


def _resolve_reports_dir() -> Path:
    """Resolve the unified_reports store, honoring ``$VNX_REPORTS_DIR`` first.

    ``$VNX_REPORTS_DIR`` is the fabric's explicit override (receipt_processor.sh
    reads it as the canonical store).  When it is unset the store is derived
    from the state/data dir — never skipped: a completed dispatch always ends
    with a report on disk, so this resolution must not depend on an optional
    environment variable being present.
    """
    reports_env = os.environ.get("VNX_REPORTS_DIR", "").strip()
    if reports_env:
        return Path(reports_env).expanduser()
    try:
        return _default_state_dir().parent / "unified_reports"
    except Exception as exc:  # vnx-silent-except: resolution must never block close-out; falls back to VNX_DATA_DIR
        logger.warning(
            "_ensure_unified_report: state dir resolution failed (%s); "
            "falling back to VNX_DATA_DIR", exc,
        )
    data_env = os.environ.get("VNX_DATA_DIR", "").strip()
    if data_env:
        return Path(data_env).expanduser() / "unified_reports"
    return Path.cwd() / ".vnx-data" / "unified_reports"


def _closeout_report_body(
    dispatch_id: str,
    terminal_id: str,
    status: str,
    model: "str | None",
    provider: "str | None",
    changed_files: "list[str] | None",
) -> str:
    """Contract-valid body for a dispatch the worker closed out without a report.

    Carries all four required sections (Summary, Changes, Verification, Open
    Items) and the identity block the receipt converter requires (Dispatch-ID,
    Model, Provider) — the same shape ``governance_emit.fallback_report_body``
    produces for a crash, but for the success path.
    """
    identity = [f"**Dispatch-ID**: {dispatch_id}"]
    if model:
        identity.append(f"**Model**: {model}")
    if provider:
        identity.append(f"**Provider**: {provider}")
    if terminal_id:
        identity.append(f"**Terminal**: {terminal_id}")
    identity.append(f"**Status**: {status}")
    identity.append("**Auto-Generated**: lane close-out (no worker report)")

    if changed_files:
        changes = (
            "The worker wrote no unified report of its own, so the lane recorded "
            "the files this dispatch touched (from its event stream):\n\n"
            + "\n".join(f"- `{path}`" for path in changed_files)
            + "\n"
        )
    else:
        changes = (
            "The worker wrote no unified report of its own and its event stream "
            "recorded no touched paths. Any changes it made are on this dispatch's "
            "branch/worktree — inspect them there before deciding the outcome.\n"
        )

    return (
        f"# Dispatch {dispatch_id}\n\n"
        + "\n".join(identity) + "\n\n"
        "## Summary\n\n"
        "The subprocess lane closed this dispatch out successfully, but the worker "
        "wrote no unified report of its own, so the lane wrote this one in its "
        "place. The dispatch reached its end state with a report that satisfies the "
        "report body contract; the lane receipt records the delivery outcome, model "
        "and cost.\n\n"
        "## Changes\n\n" + changes + "\n"
        "## Verification\n\n"
        "No verification was recorded by the worker. The lane's own delivery checks "
        "(worker health monitor, event stream, commit check) decided success and are "
        "recorded on this dispatch's receipt.\n\n"
        "## Open Items\n\n"
        "- None.\n"
    )


def _ensure_unified_report(
    dispatch_id: str,
    terminal_id: str,
    status: str = "done",
    *,
    model: "str | None" = None,
    provider: "str | None" = None,
    changed_files: "list[str] | None" = None,
) -> "Path | None":
    """Ensure a contract-valid unified report exists for a completed dispatch.

    Called on the subprocess lane's success path when the worker wrote no report
    of its own. The body it writes passes ``report_body_contract.validate_body``
    (Summary, Changes, Verification, Open Items) and carries the identity block
    (Dispatch-ID, Model, Provider, ...) ``append_receipt_internals.validation``
    requires, so the dispatch's receipt can actually be written. The write goes
    through the one writer, ``governance_emit.emit_unified_report``.

    There is no silent skip: ``$VNX_REPORTS_DIR`` is honored when set, but when
    it is unset the store is derived from the state/data dir (see
    ``_resolve_reports_dir``).

    Idempotent across all accepted filename forms (canonical ``<id>.md`` and the
    two legacy forms): a worker-authored report is never overwritten or altered.

    Never raises — a close-out failure is logged loudly, not propagated into the
    dispatch's success path.
    """
    from report_path import _FILENAME_FORMS
    try:
        reports_dir = _resolve_reports_dir()
        # Check all filename forms (canonical + two legacy) for idempotency.
        for form in _FILENAME_FORMS:
            if (reports_dir / form.format(dispatch_id=dispatch_id)).exists():
                logger.debug(
                    "_ensure_unified_report: worker report already present for %s",
                    dispatch_id,
                )
                return None
        body = _closeout_report_body(
            dispatch_id, terminal_id, status, model, provider, changed_files,
        )
        # The one writer derives its store as <data_dir>/unified_reports, so the
        # data dir is the reports dir's parent (VNX_REPORTS_DIR invariable ends
        # in "unified_reports" — vnx_paths.sh, new_worktree.sh, receipt_processor.sh).
        from governance_emit import emit_unified_report
        report_path = emit_unified_report(
            dispatch_id=dispatch_id,
            terminal_id=terminal_id,
            provider=provider or "claude",
            instruction="",
            response_text="",
            findings=None,
            duration_seconds=0.0,
            data_dir=reports_dir.parent,
            body_override=body,
            model=model,
        )
        if report_path.parent.resolve() != reports_dir.resolve():
            logger.warning(
                "_ensure_unified_report: report for %s landed at %s, not the "
                "resolved store %s (VNX_REPORTS_DIR basename is not "
                "'unified_reports')",
                dispatch_id, report_path, reports_dir,
            )
        logger.info(
            "_ensure_unified_report: contract-valid report written for "
            "dispatch=%s terminal=%s status=%s model=%s",
            dispatch_id, terminal_id, status, model,
        )
        return report_path
    except Exception as exc:
        logger.error(
            "_ensure_unified_report: failed for dispatch=%s: %s", dispatch_id, exc
        )
        return None


def _write_receipt(
    dispatch_id: str,
    terminal_id: str,
    status: str,
    *,
    event_count: int = 0,
    session_id: str | None = None,
    attempt: int | None = None,
    failure_reason: str | None = None,
    commit_missing: bool = False,
    committed: bool = False,
    commit_hash_before: str = "",
    commit_hash_after: str = "",
    manifest_path: str | None = None,
    stuck_event_count: int = 0,
    token_usage: dict | None = None,
    cost_usd: float | None = None,
    pr_id: str | None = None,
    provider: str | None = None,
    sub_provider: str | None = None,
    model: str | None = None,
    lane: str | None = None,
    mandate_id: str | None = None,
) -> Path:
    """Append a subprocess completion receipt to t0_receipts.ndjson.

    Returns the path to the receipt file.
    """
    receipt = _build_receipt_payload(
        dispatch_id=dispatch_id,
        terminal_id=terminal_id,
        status=status,
        event_count=event_count,
        session_id=session_id,
        attempt=attempt,
        failure_reason=failure_reason,
        commit_missing=commit_missing,
        committed=committed,
        commit_hash_before=commit_hash_before,
        commit_hash_after=commit_hash_after,
        manifest_path=manifest_path,
        stuck_event_count=stuck_event_count,
        token_usage=token_usage,
        cost_usd=cost_usd,
        pr_id=pr_id,
        provider=provider,
        sub_provider=sub_provider,
        model=model,
        lane=lane,
        mandate_id=mandate_id,
    )
    return _persist_receipt(receipt, dispatch_id, terminal_id, status)


def _build_receipt_payload(
    *,
    dispatch_id: str,
    terminal_id: str,
    status: str,
    event_count: int,
    session_id: str | None,
    attempt: int | None,
    failure_reason: str | None,
    commit_missing: bool,
    committed: bool,
    commit_hash_before: str,
    commit_hash_after: str,
    manifest_path: str | None,
    stuck_event_count: int,
    token_usage: dict | None = None,
    cost_usd: float | None = None,
    pr_id: str | None = None,
    provider: str | None = None,
    sub_provider: str | None = None,
    model: str | None = None,
    lane: str | None = None,
    mandate_id: str | None = None,
) -> dict:
    """Assemble the receipt dict from the named fields."""
    receipt = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": "subprocess_completion",
        "receipt_kind": "dispatch",
        "dispatch_id": dispatch_id,
        "terminal": terminal_id,
        "terminal_id": terminal_id,
        "status": status,
        "event_count": event_count,
        "session_id": session_id,
        "source": "subprocess",
    }
    if provider is not None:
        receipt["provider"] = provider
    if sub_provider is not None:
        receipt["sub_provider"] = sub_provider
    if model is not None:
        receipt["model"] = model
    if lane is not None:
        receipt["lane"] = lane
    if mandate_id:
        receipt["mandate_id"] = mandate_id
    if commit_hash_before:
        receipt["commit_hash_before"] = commit_hash_before
    if commit_hash_after:
        receipt["commit_hash_after"] = commit_hash_after
    if commit_hash_before and commit_hash_after:
        receipt["committed"] = committed or (commit_hash_before != commit_hash_after)
    elif committed:
        receipt["committed"] = True
    if manifest_path:
        receipt["manifest_path"] = manifest_path
    if attempt is not None:
        receipt["attempt"] = attempt
    if failure_reason:
        receipt["failure_reason"] = failure_reason
    if commit_missing:
        receipt["commit_missing"] = True
    if stuck_event_count:
        receipt["stuck_event_count"] = stuck_event_count
    if token_usage is not None:
        receipt["token_usage"] = token_usage
    if cost_usd is not None:
        receipt["cost_usd"] = cost_usd
    if pr_id is not None:
        receipt["pr_id"] = pr_id
    return receipt


def _persist_receipt(
    receipt: dict, dispatch_id: str, terminal_id: str, status: str,
) -> Path:
    """Persist receipt via append_receipt_payload, falling back to bare write."""
    _scripts_dir = Path(__file__).resolve().parents[2]
    try:
        sys.path.insert(0, str(_scripts_dir))
        from append_receipt import append_receipt_payload
        result = append_receipt_payload(receipt)
        receipt_path = result.receipts_file
        if result.status == "duplicate":
            logger.debug(
                "Receipt already appended (idempotent skip): dispatch=%s", dispatch_id
            )
        else:
            logger.info(
                "Receipt written: dispatch=%s terminal=%s status=%s",
                dispatch_id, terminal_id, status,
            )
        return receipt_path
    except Exception as exc:
        # Fallback: bare write to prevent receipt loss on import error (e.g. circular import).
        # Resolve _default_state_dir via the facade so test patches at
        # ``subprocess_dispatch._default_state_dir`` are honoured.
        logger.warning(
            "append_receipt_payload failed (%s); falling back to bare write", exc
        )
        # Stamp project_id from env when _stamp_identity is unavailable (circular-import fallback).
        if not receipt.get("project_id"):
            _fallback_pid = os.environ.get("VNX_PROJECT_ID", "").strip()
            if _fallback_pid:
                receipt["project_id"] = _fallback_pid
        import subprocess_dispatch as _sd
        receipt_path = _sd._default_state_dir() / "t0_receipts.ndjson"
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        with open(receipt_path, "a") as f:
            f.write(json.dumps(receipt) + "\n")
        logger.info(
            "Receipt written (bare): dispatch=%s terminal=%s status=%s",
            dispatch_id, terminal_id, status,
        )
        return receipt_path


def _filter_dispatch_files(
    cwd: Path,
    pre_dispatch_dirty: "set[str]",
    dispatch_touched_files: "frozenset[str] | set[str]",
    manifest_paths: "list[str] | None",
    dispatch_id: str,
    op_name: str,
) -> list[str]:
    """Compute the in-scope file set for auto_commit/auto_stash.

    Returns an empty list when there are no eligible files; callers must
    have already validated that pre_dispatch_dirty / dispatch_touched_files
    are not None and that the working tree is non-empty.
    """
    import subprocess_dispatch as _sd
    current_dirty = _sd._get_dirty_files(cwd)
    new_during_dispatch = current_dirty - pre_dispatch_dirty
    touched = set(dispatch_touched_files)
    files_in_scope = sorted(new_during_dispatch & touched)
    if manifest_paths is not None:
        from dispatch_paths import filter_paths
        before = list(files_in_scope)
        files_in_scope = filter_paths(before, manifest_paths)
        dropped = sorted(set(before) - set(files_in_scope))
        if dropped:
            logger.info(
                "%s: dispatch %s manifest excluded %d out-of-scope files: %s",
                op_name, dispatch_id, len(dropped), dropped,
            )
    return files_in_scope


def _validate_scope_args(
    op_name: str,
    dispatch_id: str,
    pre_dispatch_dirty,
    dispatch_touched_files,
    manifest_paths,
) -> bool:
    """Validate fail-safe scoping kwargs; return True iff caller may proceed."""
    if pre_dispatch_dirty is None:
        logger.warning(
            "%s: pre_dispatch_dirty=None — refusing for dispatch %s "
            "(would otherwise sweep unrelated dirty files via git add -A)",
            op_name, dispatch_id,
        )
        return False
    if dispatch_touched_files is None:
        logger.warning(
            "%s: dispatch_touched_files=None — refusing for dispatch %s "
            "(cannot distinguish this worker's writes from concurrent edits in a shared worktree)",
            op_name, dispatch_id,
        )
        return False
    if manifest_paths is None:
        logger.warning(
            "%s: manifest_paths absent for dispatch %s — using legacy "
            "pre_dispatch_dirty scoping only (callers should declare paths via "
            "dispatch_paths.write_manifest for parallel-worktree safety)",
            op_name, dispatch_id,
        )
    return True


def _log_no_eligible_files(
    op_name: str,
    dispatch_id: str,
    cwd: Path,
    pre_dispatch_dirty,
    dispatch_touched_files,
) -> None:
    """Emit the appropriate refusal/no-op message when no files match the scope."""
    import subprocess_dispatch as _sd
    current_dirty = _sd._get_dirty_files(cwd)
    new_during_dispatch = current_dirty - pre_dispatch_dirty
    ignored_dispatch_dirty = sorted(new_during_dispatch - set(dispatch_touched_files))
    if ignored_dispatch_dirty:
        logger.warning(
            "%s: %d dispatch-window dirty file(s) not in "
            "touched_files — refusing (likely concurrent edits from another terminal). "
            "dispatch=%s files=%s",
            op_name, len(ignored_dispatch_dirty),
            dispatch_id, ignored_dispatch_dirty[:10],
        )
    else:
        logger.debug(
            "%s: no dispatch-touched files dirty for dispatch %s "
            "(all dirty files pre-existed the dispatch or fell outside manifest)",
            op_name, dispatch_id,
        )


def _auto_commit_changes(
    dispatch_id: str,
    terminal_id: str,
    gate: str = "",
    pre_dispatch_dirty: "set[str] | None" = None,
    dispatch_touched_files: "frozenset[str] | set[str] | None" = None,
    manifest_paths: "list[str] | None" = None,
    model: "str | None" = None,
) -> bool:
    """Stage and commit dispatch-introduced changes (returns True on commit).

    See ``_validate_scope_args`` for the fail-safe contract on the scoping
    kwargs.  Never raises; all exceptions are logged and swallowed.
    """
    if not _validate_scope_args(
        "auto_commit", dispatch_id,
        pre_dispatch_dirty, dispatch_touched_files, manifest_paths,
    ):
        return False
    import subprocess_dispatch as _sd
    try:
        cwd = Path(__file__).resolve().parents[3]
        status_proc = _sd.subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=15, cwd=cwd,
        )
        dirty_lines = [l for l in status_proc.stdout.splitlines() if l.strip()]
        if not dirty_lines:
            logger.debug("auto_commit: working tree clean for dispatch %s", dispatch_id)
            return False
        files_to_stage = _filter_dispatch_files(
            cwd, pre_dispatch_dirty, dispatch_touched_files,
            manifest_paths, dispatch_id, "auto_commit",
        )
        if not files_to_stage:
            _log_no_eligible_files(
                "auto_commit", dispatch_id, cwd,
                pre_dispatch_dirty, dispatch_touched_files,
            )
            return False
        return _stage_and_commit(
            cwd, files_to_stage, gate or dispatch_id[:12],
            dispatch_id, terminal_id, model=model,
        )
    except Exception as exc:
        logger.warning("auto_commit: unexpected error for dispatch %s: %s", dispatch_id, exc)
        return False


def _build_commit_message(
    gate_tag: str, dispatch_id: str, terminal_id: str, model: "str | None",
) -> str:
    """Compose the auto-commit message including worker-attribution trailers.

    OI-1198: subprocess workers commit under the operator's git identity,
    so the commit message must record that the change was machine-authored
    on a worker terminal.  Trailers are emitted in git-trailer style so that
    ``git interpret-trailers`` and downstream analytics can parse them.
    """
    lines = [
        f"feat({gate_tag}): auto-commit from headless worker {terminal_id}",
        "",
        f"Dispatch-ID: {dispatch_id}",
        f"Worker-Terminal: {terminal_id}",
    ]
    if model:
        lines.append(f"Worker-Model: {model}")
    return "\n".join(lines)


def _stage_and_commit(
    cwd: Path, files_to_stage: list[str], gate_tag: str,
    dispatch_id: str, terminal_id: str, *, model: "str | None" = None,
) -> bool:
    """Run git add + git commit for the in-scope file set.  Returns True on commit."""
    import subprocess_dispatch as _sd
    add_proc = _sd.subprocess.run(
        ["git", "add", "--"] + files_to_stage,
        capture_output=True, text=True, timeout=15,
        cwd=cwd,
    )
    if add_proc.returncode != 0:
        logger.warning("auto_commit: git add failed for %s: %s", dispatch_id, add_proc.stderr)
        return False

    commit_msg = _build_commit_message(gate_tag, dispatch_id, terminal_id, model)
    commit_proc = _sd.subprocess.run(
        ["git", "commit", "-m", commit_msg],
        capture_output=True, text=True, timeout=30,
        cwd=cwd,
    )
    if commit_proc.returncode == 0:
        logger.info(
            "Auto-committed uncommitted changes from dispatch %s (terminal=%s)",
            dispatch_id, terminal_id,
        )
        return True
    logger.warning(
        "auto_commit: git commit failed for %s: %s",
        dispatch_id, commit_proc.stderr,
    )
    return False


def _auto_stash_changes(
    dispatch_id: str,
    terminal_id: str,
    pre_dispatch_dirty: "set[str] | None" = None,
    dispatch_touched_files: "frozenset[str] | set[str] | None" = None,
    manifest_paths: "list[str] | None" = None,
) -> bool:
    """Stash dispatch-introduced changes after failure (returns True on stash).

    Shares the fail-safe scoping contract with ``_auto_commit_changes``.
    Never raises; all exceptions are logged and swallowed.
    """
    if not _validate_scope_args(
        "auto_stash", dispatch_id,
        pre_dispatch_dirty, dispatch_touched_files, manifest_paths,
    ):
        return False
    import subprocess_dispatch as _sd
    try:
        cwd = Path(__file__).resolve().parents[3]
        status_proc = _sd.subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=15, cwd=cwd,
        )
        dirty_lines = [l for l in status_proc.stdout.splitlines() if l.strip()]
        if not dirty_lines:
            return False
        files_to_stash = _filter_dispatch_files(
            cwd, pre_dispatch_dirty, dispatch_touched_files,
            manifest_paths, dispatch_id, "auto_stash",
        )
        if not files_to_stash:
            _log_no_eligible_files(
                "auto_stash", dispatch_id, cwd,
                pre_dispatch_dirty, dispatch_touched_files,
            )
            return False

        return _run_stash_push(cwd, files_to_stash, dispatch_id, terminal_id)
    except Exception as exc:
        logger.warning("auto_stash: unexpected error for dispatch %s: %s", dispatch_id, exc)
        return False


def _run_stash_push(
    cwd: Path, files_to_stash: list[str], dispatch_id: str, terminal_id: str,
) -> bool:
    """Run `git stash push -u` for the in-scope file set.  Returns True on stash."""
    import subprocess_dispatch as _sd
    stash_name = f"vnx-auto-stash-{dispatch_id}"
    # -u includes untracked files matching the specified paths so
    # newly-created files from the failed dispatch are also captured.
    stash_cmd = ["git", "stash", "push", "-u", "-m", stash_name, "--"] + files_to_stash

    stash_proc = _sd.subprocess.run(
        stash_cmd,
        capture_output=True, text=True, timeout=30,
        cwd=cwd,
    )
    if stash_proc.returncode == 0:
        logger.info(
            "Stashed %d dispatch-produced file(s) from failed dispatch %s "
            "(terminal=%s, stash=%s)",
            len(files_to_stash), dispatch_id, terminal_id, stash_name,
        )
        return True
    logger.warning(
        "auto_stash: git stash failed for %s: %s",
        dispatch_id, stash_proc.stderr,
    )
    return False
