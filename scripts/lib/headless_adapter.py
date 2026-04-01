#!/usr/bin/env python3
"""
VNX Headless CLI Adapter — Subprocess-based dispatch execution for non-coding work.

Executes dispatches via CLI subprocess (claude --print, codex --quiet, etc.)
without pretending they are tmux panes. Produces durable attempts and receipts
in the same runtime flow as interactive workers.

Contracts:
  G-R2: Coding stays interactive (headless adapter refuses coding_interactive)
  G-R3: Headless execution is durable and receipt-producing
  G-R8: No execution-mode change bypasses T0 authority or receipts
  A-R3: Headless adapters are CLI-based, no Agent SDK
  A-R8: Legacy tmux behavior remains available as fallback
  A-R9: Execution mode cutover is reversible

Feature flags:
  VNX_HEADLESS_ENABLED   "0" (default, shadow phase) = disabled, "1" = enabled
  VNX_HEADLESS_TIMEOUT   seconds before subprocess kill (default 600)
  VNX_HEADLESS_CLI       CLI binary to invoke (default "claude")
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from runtime_coordination import (
    _append_event,
    _now_utc,
    create_attempt,
    get_connection,
    get_dispatch,
    increment_attempt_count,
    transition_dispatch,
    update_attempt,
)


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HEADLESS_ELIGIBLE_TASK_CLASSES = frozenset({
    "research_structured",
    "docs_synthesis",
})

HEADLESS_CLI_DEFAULTS = {
    "headless_claude_cli": {
        "binary": "claude",
        "args": ["--print", "--output-format", "text"],
    },
    "headless_codex_cli": {
        "binary": "codex",
        "args": ["--quiet"],
    },
}

DEFAULT_TIMEOUT = 600  # 10 minutes

# ---------------------------------------------------------------------------
# Gate-specific timeout and stall thresholds (GATE-6, GATE-7)
# ---------------------------------------------------------------------------

GATE_TIMEOUT_DEFAULTS: Dict[str, int] = {
    "gemini_review": 300,
    "codex_gate": 600,
    "claude_github_optional": 300,
}

GATE_TIMEOUT_ENV: Dict[str, str] = {
    "gemini_review": "VNX_GEMINI_GATE_TIMEOUT",
    "codex_gate": "VNX_CODEX_GATE_TIMEOUT",
    "claude_github_optional": "VNX_CLAUDE_GITHUB_GATE_TIMEOUT",
}

GATE_STALL_DEFAULTS: Dict[str, int] = {
    "gemini_review": 60,
    "codex_gate": 120,
    "claude_github_optional": 60,
}

GATE_STALL_ENV: Dict[str, str] = {
    "gemini_review": "VNX_GEMINI_STALL_THRESHOLD",
    "codex_gate": "VNX_CODEX_STALL_THRESHOLD",
    "claude_github_optional": "VNX_CLAUDE_GITHUB_STALL_THRESHOLD",
}


def gate_timeout(gate_type: str) -> int:
    """Return execution timeout in seconds for a specific gate type (GATE-6)."""
    env_var = GATE_TIMEOUT_ENV.get(gate_type)
    if env_var:
        raw = os.environ.get(env_var)
        if raw is not None:
            try:
                return int(raw)
            except ValueError:
                pass
    return GATE_TIMEOUT_DEFAULTS.get(gate_type, DEFAULT_TIMEOUT)


def gate_stall_threshold(gate_type: str) -> int:
    """Return stall detection threshold in seconds for a specific gate type (GATE-7)."""
    env_var = GATE_STALL_ENV.get(gate_type)
    if env_var:
        raw = os.environ.get(env_var)
        if raw is not None:
            try:
                return int(raw)
            except ValueError:
                pass
    return GATE_STALL_DEFAULTS.get(gate_type, 60)


# ---------------------------------------------------------------------------
# Feature flag helpers
# ---------------------------------------------------------------------------

def headless_enabled() -> bool:
    """Return True when VNX_HEADLESS_ENABLED == "1"."""
    return os.environ.get("VNX_HEADLESS_ENABLED", "0").strip() == "1"


def headless_timeout() -> int:
    """Return subprocess timeout in seconds."""
    try:
        return int(os.environ.get("VNX_HEADLESS_TIMEOUT", str(DEFAULT_TIMEOUT)))
    except ValueError:
        return DEFAULT_TIMEOUT


def headless_cli_binary() -> str:
    """Return the CLI binary name for headless execution."""
    return os.environ.get("VNX_HEADLESS_CLI", "claude").strip()


def headless_config_from_env() -> Dict[str, Any]:
    """Return headless adapter config from environment."""
    return {
        "enabled": headless_enabled(),
        "timeout": headless_timeout(),
        "cli_binary": headless_cli_binary(),
    }


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class HeadlessAdapterError(Exception):
    """Base error for headless adapter failures."""


class HeadlessDisabledError(HeadlessAdapterError):
    """Raised when headless execution is attempted while disabled."""


class HeadlessIneligibleError(HeadlessAdapterError):
    """Raised when a dispatch's task class is not eligible for headless execution."""


class HeadlessTimeoutError(HeadlessAdapterError):
    """Raised when a headless subprocess exceeds its timeout."""


class HeadlessBinaryNotFoundError(HeadlessAdapterError):
    """Raised when the CLI binary is not found in PATH."""


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class HeadlessExecutionResult:
    """Result of a headless dispatch execution."""
    success: bool
    dispatch_id: str
    target_id: str
    target_type: str
    attempt_id: Optional[str] = None
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    failure_reason: Optional[str] = None
    output_path: Optional[str] = None
    failure_class: Optional[str] = None
    classification_evidence: Optional[Dict[str, Any]] = None
    log_artifact_path: Optional[str] = None
    output_artifact_path: Optional[str] = None


# ---------------------------------------------------------------------------
# HeadlessAdapter
# ---------------------------------------------------------------------------

class HeadlessAdapter:
    """CLI subprocess adapter for headless dispatch execution.

    Executes structured non-coding dispatches by spawning a CLI process,
    capturing stdout/stderr, and recording attempts and receipts in the
    canonical runtime state.

    Args:
        state_dir:    Directory containing runtime_coordination.db.
        dispatch_dir: Root directory for dispatch bundles.
        output_dir:   Directory for headless execution output capture.
    """

    def __init__(
        self,
        state_dir: str | Path,
        dispatch_dir: str | Path,
        output_dir: Optional[str | Path] = None,
        artifact_dir: Optional[str | Path] = None,
    ) -> None:
        self._state_dir = Path(state_dir)
        self._dispatch_dir = Path(dispatch_dir)
        self._output_dir = Path(output_dir) if output_dir else self._state_dir.parent / "headless_output"
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._artifact_dir = Path(artifact_dir) if artifact_dir else self._state_dir.parent / "headless_artifacts"
        self._artifact_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Eligibility checks
    # ------------------------------------------------------------------

    @staticmethod
    def is_eligible(task_class: str) -> bool:
        """Return True if task_class is eligible for headless execution."""
        return task_class in HEADLESS_ELIGIBLE_TASK_CLASSES

    def validate_eligibility(self, dispatch_id: str, task_class: Optional[str]) -> None:
        """Raise HeadlessIneligibleError if dispatch cannot run headless.

        G-R2: coding_interactive MUST NOT route headless.
        """
        if not headless_enabled():
            raise HeadlessDisabledError(
                "Headless execution is disabled (VNX_HEADLESS_ENABLED != 1)"
            )
        if task_class is None:
            raise HeadlessIneligibleError(
                f"Dispatch {dispatch_id!r} has no task_class; cannot determine headless eligibility"
            )
        if task_class not in HEADLESS_ELIGIBLE_TASK_CLASSES:
            raise HeadlessIneligibleError(
                f"Task class {task_class!r} is not eligible for headless execution. "
                f"Eligible: {sorted(HEADLESS_ELIGIBLE_TASK_CLASSES)}"
            )

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute(
        self,
        dispatch_id: str,
        target_id: str,
        target_type: str,
        *,
        task_class: Optional[str] = None,
        terminal_id: Optional[str] = None,
        actor: str = "headless_adapter",
    ) -> HeadlessExecutionResult:
        """Execute a dispatch headlessly via CLI subprocess.

        Full lifecycle:
          1. Validate eligibility
          2. Load dispatch bundle from disk
          3. Create attempt record (claimed -> delivering -> accepted -> running)
          4. Spawn CLI subprocess with prompt
          5. Capture stdout/stderr and write to output_dir
          6. Record success/failure in attempts and coordination_events
          7. Transition dispatch to completed or failed_delivery

        Returns HeadlessExecutionResult with captured output and status.
        """
        self.validate_eligibility(dispatch_id, task_class)

        bundle = self._load_bundle(dispatch_id)
        if bundle is None:
            return HeadlessExecutionResult(
                success=False,
                dispatch_id=dispatch_id,
                target_id=target_id,
                target_type=target_type,
                failure_reason=f"Bundle not found for dispatch {dispatch_id!r}",
            )

        prompt = bundle.get("_prompt", "")
        if not prompt:
            return HeadlessExecutionResult(
                success=False,
                dispatch_id=dispatch_id,
                target_id=target_id,
                target_type=target_type,
                failure_reason=f"Empty prompt in dispatch bundle {dispatch_id!r}",
            )

        attempt_id = self._create_attempt(
            dispatch_id, terminal_id or "headless", actor=actor,
        )

        self._transition_through_delivery(dispatch_id, attempt_id, actor=actor)

        cli_config = HEADLESS_CLI_DEFAULTS.get(target_type, {
            "binary": headless_cli_binary(),
            "args": ["--print"],
        })

        result = self._run_subprocess(
            dispatch_id=dispatch_id,
            target_id=target_id,
            target_type=target_type,
            attempt_id=attempt_id,
            prompt=prompt,
            binary=cli_config["binary"],
            cli_args=cli_config["args"],
        )

        self._record_outcome(result, attempt_id, actor=actor)
        return result

    # ------------------------------------------------------------------
    # Bundle loading
    # ------------------------------------------------------------------

    def _load_bundle(self, dispatch_id: str) -> Optional[Dict[str, Any]]:
        """Load bundle.json and prompt.txt from the dispatch directory."""
        bundle_dir = self._dispatch_dir / dispatch_id
        bundle_path = bundle_dir / "bundle.json"
        prompt_path = bundle_dir / "prompt.txt"

        if not bundle_path.exists():
            return None

        try:
            bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

        if prompt_path.exists():
            bundle["_prompt"] = prompt_path.read_text(encoding="utf-8")
        else:
            bundle["_prompt"] = ""

        return bundle

    # ------------------------------------------------------------------
    # Attempt lifecycle
    # ------------------------------------------------------------------

    def _create_attempt(
        self,
        dispatch_id: str,
        terminal_id: str,
        *,
        actor: str,
    ) -> str:
        """Create a dispatch attempt and claim the dispatch. Returns attempt_id."""
        with get_connection(self._state_dir) as conn:
            dispatch = get_dispatch(conn, dispatch_id)
            if dispatch is None:
                raise HeadlessAdapterError(f"Dispatch not found: {dispatch_id!r}")

            current_state = dispatch["state"]
            if current_state == "queued":
                transition_dispatch(
                    conn,
                    dispatch_id=dispatch_id,
                    to_state="claimed",
                    actor=actor,
                    reason=f"claimed by headless adapter for {terminal_id}",
                    metadata={"target_type": "headless", "terminal_id": terminal_id},
                )
            attempt_count = increment_attempt_count(conn, dispatch_id)

            attempt = create_attempt(
                conn,
                dispatch_id=dispatch_id,
                terminal_id=terminal_id,
                attempt_number=attempt_count,
                metadata={"adapter": "headless", "actor": actor},
                actor=actor,
            )
            conn.commit()
        return attempt["attempt_id"]

    def _transition_through_delivery(
        self,
        dispatch_id: str,
        attempt_id: str,
        *,
        actor: str,
    ) -> None:
        """Transition dispatch: claimed -> delivering -> accepted -> running."""
        with get_connection(self._state_dir) as conn:
            transition_dispatch(
                conn, dispatch_id=dispatch_id, to_state="delivering",
                actor=actor, reason="headless delivery started",
                metadata={"attempt_id": attempt_id},
            )
            update_attempt(conn, attempt_id=attempt_id, state="delivering", actor=actor)

            transition_dispatch(
                conn, dispatch_id=dispatch_id, to_state="accepted",
                actor=actor, reason="headless adapter accepted dispatch",
                metadata={"attempt_id": attempt_id},
            )
            update_attempt(conn, attempt_id=attempt_id, state="succeeded", actor=actor)

            transition_dispatch(
                conn, dispatch_id=dispatch_id, to_state="running",
                actor=actor, reason="headless subprocess starting",
                metadata={"attempt_id": attempt_id},
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Subprocess execution
    # ------------------------------------------------------------------

    def _run_subprocess(
        self,
        *,
        dispatch_id: str,
        target_id: str,
        target_type: str,
        attempt_id: str,
        prompt: str,
        binary: str,
        cli_args: List[str],
        run_id: Optional[str] = None,
        started_at: Optional[str] = None,
    ) -> HeadlessExecutionResult:
        """Spawn CLI subprocess with prompt, capture output, classify exit, write artifacts."""
        import shutil
        from exit_classifier import classify_exit
        from log_artifact import write_log_artifact, write_output_artifact

        binary_not_found = shutil.which(binary) is None
        if binary_not_found:
            classification = classify_exit(
                exit_code=None,
                binary_not_found=True,
            )
            return HeadlessExecutionResult(
                success=False,
                dispatch_id=dispatch_id,
                target_id=target_id,
                target_type=target_type,
                attempt_id=attempt_id,
                failure_reason=f"CLI binary not found in PATH: {binary!r}",
                failure_class=classification.failure_class,
                classification_evidence=_evidence_to_dict(classification),
            )

        timeout = headless_timeout()
        output_file = self._output_dir / f"{dispatch_id}.txt"

        cmd = [binary] + list(cli_args)

        self._emit_event(
            "headless_subprocess_start",
            dispatch_id=dispatch_id,
            metadata={
                "target_id": target_id,
                "target_type": target_type,
                "attempt_id": attempt_id,
                "binary": binary,
                "timeout": timeout,
            },
        )

        start = time.monotonic()
        timed_out = False
        stdout = ""
        stderr = ""
        exit_code = None

        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            duration = time.monotonic() - start
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            exit_code = proc.returncode

        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - start
            timed_out = True
            stdout = exc.stdout or "" if hasattr(exc, "stdout") and exc.stdout else ""
            stderr = exc.stderr or "" if hasattr(exc, "stderr") and exc.stderr else ""

        except OSError as exc:
            duration = time.monotonic() - start
            stderr = str(exc)

        # Classify the exit outcome
        classification = classify_exit(
            exit_code=exit_code,
            timed_out=timed_out,
            stderr=stderr,
            binary_not_found=False,
        )
        success = classification.failure_class == "SUCCESS"

        # Write output file (backward compatible)
        output_file.write_text(stdout, encoding="utf-8")

        # Write structured log artifact (Section 5.3)
        effective_run_id = run_id or f"{dispatch_id}_{attempt_id}"
        log_path = write_log_artifact(
            artifact_dir=self._artifact_dir,
            run_id=effective_run_id,
            dispatch_id=dispatch_id,
            target_type=target_type,
            started_at=started_at or _now_utc(),
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            failure_class=classification.failure_class if not success else None,
            duration_seconds=duration,
        )

        # Write separate output artifact for structured output
        output_artifact = write_output_artifact(
            artifact_dir=self._artifact_dir,
            run_id=effective_run_id,
            stdout=stdout,
        )

        failure_reason = None
        if not success:
            failure_reason = classification.classification_reason
            if stderr:
                failure_reason += f": {stderr[:500]}"

        return HeadlessExecutionResult(
            success=success,
            dispatch_id=dispatch_id,
            target_id=target_id,
            target_type=target_type,
            attempt_id=attempt_id,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
            failure_reason=failure_reason,
            output_path=str(output_file),
            failure_class=classification.failure_class,
            classification_evidence=_evidence_to_dict(classification),
            log_artifact_path=str(log_path),
            output_artifact_path=str(output_artifact) if output_artifact else None,
        )

    # ------------------------------------------------------------------
    # Outcome recording
    # ------------------------------------------------------------------

    def _record_outcome(
        self,
        result: HeadlessExecutionResult,
        attempt_id: str,
        *,
        actor: str,
    ) -> None:
        """Record execution outcome in dispatch state and coordination events.

        Includes failure classification and log artifact pointers per Contract
        Sections 4.3 and 5.3.
        """
        with get_connection(self._state_dir) as conn:
            if result.success:
                transition_dispatch(
                    conn,
                    dispatch_id=result.dispatch_id,
                    to_state="completed",
                    actor=actor,
                    reason="headless execution completed successfully",
                    metadata={
                        "attempt_id": attempt_id,
                        "exit_code": result.exit_code,
                        "duration_seconds": result.duration_seconds,
                        "output_path": result.output_path,
                        "log_artifact_path": result.log_artifact_path,
                        "output_artifact_path": result.output_artifact_path,
                        "failure_class": result.failure_class,
                        "stdout_chars": len(result.stdout),
                    },
                )
                _append_event(
                    conn,
                    event_type="headless_execution_completed",
                    entity_type="dispatch",
                    entity_id=result.dispatch_id,
                    from_state="running",
                    to_state="completed",
                    actor=actor,
                    reason="headless subprocess succeeded",
                    metadata={
                        "target_id": result.target_id,
                        "target_type": result.target_type,
                        "exit_code": result.exit_code,
                        "duration_seconds": result.duration_seconds,
                        "output_path": result.output_path,
                        "log_artifact_path": result.log_artifact_path,
                        "output_artifact_path": result.output_artifact_path,
                        "failure_class": result.failure_class,
                    },
                )
            else:
                transition_dispatch(
                    conn,
                    dispatch_id=result.dispatch_id,
                    to_state="failed_delivery",
                    actor=actor,
                    reason=result.failure_reason or "headless execution failed",
                    metadata={
                        "attempt_id": attempt_id,
                        "exit_code": result.exit_code,
                        "duration_seconds": result.duration_seconds,
                        "failure_reason": result.failure_reason,
                        "failure_class": result.failure_class,
                        "classification_evidence": result.classification_evidence,
                        "log_artifact_path": result.log_artifact_path,
                    },
                )
                _append_event(
                    conn,
                    event_type="headless_execution_failed",
                    entity_type="dispatch",
                    entity_id=result.dispatch_id,
                    from_state="running",
                    to_state="failed_delivery",
                    actor=actor,
                    reason=result.failure_reason,
                    metadata={
                        "target_id": result.target_id,
                        "target_type": result.target_type,
                        "exit_code": result.exit_code,
                        "duration_seconds": result.duration_seconds,
                        "failure_class": result.failure_class,
                        "classification_evidence": result.classification_evidence,
                        "log_artifact_path": result.log_artifact_path,
                    },
                )
            conn.commit()

    # ------------------------------------------------------------------
    # Event helper
    # ------------------------------------------------------------------

    def _emit_event(
        self,
        event_type: str,
        *,
        dispatch_id: str,
        metadata: Optional[Dict[str, Any]] = None,
        actor: str = "headless_adapter",
    ) -> None:
        """Append a coordination event. Silently no-ops if DB unavailable."""
        try:
            with get_connection(self._state_dir) as conn:
                _append_event(
                    conn,
                    event_type=event_type,
                    entity_type="dispatch",
                    entity_id=dispatch_id,
                    actor=actor,
                    metadata=metadata,
                )
                conn.commit()
        except Exception:
            logger.debug(
                "Event emission failed: event_type=%s dispatch_id=%s",
                event_type,
                dispatch_id,
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _evidence_to_dict(classification) -> Dict[str, Any]:
    """Convert a ClassificationResult to a serializable dict."""
    return {
        "failure_class": classification.failure_class,
        "exit_code": classification.exit_code,
        "signal": classification.signal,
        "stderr_tail": classification.stderr_tail,
        "classification_reason": classification.classification_reason,
        "retryable": classification.retryable,
        "operator_hint": classification.operator_hint,
    }


# ---------------------------------------------------------------------------
# Module-level factory
# ---------------------------------------------------------------------------

def load_headless_adapter(
    state_dir: str | Path,
    dispatch_dir: str | Path,
    output_dir: Optional[str | Path] = None,
    artifact_dir: Optional[str | Path] = None,
) -> Optional[HeadlessAdapter]:
    """Return a HeadlessAdapter if VNX_HEADLESS_ENABLED=1, else None."""
    if not headless_enabled():
        return None
    return HeadlessAdapter(state_dir, dispatch_dir, output_dir, artifact_dir)
