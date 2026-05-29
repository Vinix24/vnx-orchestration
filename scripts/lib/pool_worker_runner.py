#!/usr/bin/env python3
"""pool_worker_runner.py — Single-claim pool worker entrypoint.

ADR-018 Rule 2: single-claim, no loop. Pool manager re-spawns on each tick.
ADR-007: all DB access scoped to project_id.
ADR-018 FM-4: post-claim project_id match guard enforced as defense-in-depth.

BILLING SAFETY: No Anthropic SDK. Claude dispatch uses deliver_with_recovery
(subprocess.Popen(["claude", ...])); non-Claude uses provider_dispatch.main.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from runtime_coordination import (  # noqa: E402
    claim_next_queued_dispatch,
    get_connection,
    get_dispatch,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exit codes — public contract for callers (ADR-018 pool tick)
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_DELIVERY_FAILED = 1
EXIT_NO_WORK = 3           # ADR-018 Rule 2: queue empty; re-spawn on next tick
EXIT_PROJECT_MISMATCH = 4  # ADR-018 FM-4: claimed dispatch project_id != worker project_id
EXIT_BUNDLE_MISSING = 5    # bundle.json or prompt.txt absent for claimed dispatch


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def _resolve_state_dir() -> Path:
    env = os.environ.get("VNX_STATE_DIR", "")
    if env:
        return Path(env)
    data_dir = os.environ.get("VNX_DATA_DIR", "")
    if data_dir:
        return Path(data_dir) / "state"
    return _LIB_DIR.parents[1] / ".vnx-data" / "state"


def _resolve_dispatch_dir() -> Path:
    data_dir = os.environ.get("VNX_DATA_DIR", "")
    if data_dir:
        return Path(data_dir) / "dispatches"
    return _LIB_DIR.parents[1] / ".vnx-data" / "dispatches"


# ---------------------------------------------------------------------------
# Delivery delegates (lazy imports — avoid loading heavy delivery deps)
# ---------------------------------------------------------------------------

def _deliver_claude(
    terminal_id: str,
    dispatch_id: str,
    instruction: str,
    model: str,
    role: Optional[str],
    gate: str,
) -> int:
    """Delegate to deliver_with_recovery (subprocess.Popen(["claude", ...]))."""
    try:
        from subprocess_dispatch import deliver_with_recovery  # noqa: PLC0415
        success = deliver_with_recovery(
            terminal_id=terminal_id,
            instruction=instruction,
            model=model,
            dispatch_id=dispatch_id,
            role=role,
            gate=gate,
            max_retries=1,
        )
        return EXIT_OK if success else EXIT_DELIVERY_FAILED
    except Exception as exc:
        logger.error("Claude delivery exception for %r: %s", dispatch_id, exc)
        return EXIT_DELIVERY_FAILED


def _deliver_provider(
    provider: str,
    terminal_id: str,
    dispatch_id: str,
    instruction: str,
    model: str,
    role: Optional[str],
    gate: str,
) -> int:
    """Delegate to provider_dispatch.main for non-Claude providers."""
    try:
        import provider_dispatch as pd  # noqa: PLC0415
        argv = [
            "--provider", provider,
            "--terminal-id", terminal_id,
            "--dispatch-id", dispatch_id,
            "--instruction", instruction,
            "--model", model,
        ]
        if role:
            argv += ["--role", role]
        if gate:
            argv += ["--gate", gate]
        exit_code = pd.main(argv)
        return EXIT_OK if (exit_code or 0) == 0 else EXIT_DELIVERY_FAILED
    except Exception as exc:
        logger.error("Provider delivery exception for %r: %s", dispatch_id, exc)
        return EXIT_DELIVERY_FAILED


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

def run(
    terminal_id: str,
    project_id: str,
    *,
    state_dir: Optional[Path] = None,
    dispatch_dir: Optional[Path] = None,
    model: Optional[str] = None,
) -> int:
    """Claim one queued dispatch and execute it. ADR-018 Rule 2: single-claim, no loop.

    ADR-007: DB access scoped to project_id.
    ADR-018 FM-4: post-claim project_id match guard (defense-in-depth).

    Returns:
        EXIT_OK (0)               — dispatch executed successfully.
        EXIT_NO_WORK (3)          — queue empty; re-spawn on next pool tick.
        EXIT_PROJECT_MISMATCH (4) — FM-4: project_id mismatch; not executed.
        EXIT_BUNDLE_MISSING (5)   — bundle.json or prompt.txt not found.
        EXIT_DELIVERY_FAILED (1)  — delivery path returned failure.
    """
    _state_dir = state_dir or _resolve_state_dir()
    _dispatch_dir = dispatch_dir or _resolve_dispatch_dir()
    _model = model or os.environ.get("VNX_DISPATCH_MODEL", "sonnet")

    # ADR-007: claim is scoped to project_id — worker never sees other projects' rows
    with get_connection(_state_dir) as conn:
        dispatch_id = claim_next_queued_dispatch(conn, terminal_id, project_id)

    if dispatch_id is None:
        logger.debug("No queued dispatches for project %r — exit no-work", project_id)
        return EXIT_NO_WORK

    logger.info("Claimed dispatch %r terminal=%s project=%s", dispatch_id, terminal_id, project_id)

    # ADR-018 FM-4: verify project_id post-claim as defense-in-depth
    with get_connection(_state_dir) as conn:
        row = get_dispatch(conn, dispatch_id)

    dispatch_project_id = (row or {}).get("project_id") or ""
    if dispatch_project_id != project_id:
        logger.error(
            "FM-4 mismatch: dispatch %r project_id=%r, worker project_id=%r — refusing",
            dispatch_id, dispatch_project_id, project_id,
        )
        return EXIT_PROJECT_MISMATCH

    # Load dispatch bundle
    from dispatch_broker import DispatchBroker  # noqa: PLC0415

    broker = DispatchBroker(
        state_dir=_state_dir,
        dispatch_dir=_dispatch_dir,
        shadow_mode=False,
    )
    bundle = broker.get_bundle(dispatch_id)
    if bundle is None:
        logger.error("Bundle missing for claimed dispatch %r", dispatch_id)
        return EXIT_BUNDLE_MISSING

    prompt_path = broker.get_bundle_path(dispatch_id) / "prompt.txt"
    if not prompt_path.exists():
        logger.error("prompt.txt missing for dispatch %r", dispatch_id)
        return EXIT_BUNDLE_MISSING

    instruction = prompt_path.read_text(encoding="utf-8")

    target_profile = bundle.get("target_profile") or {}
    provider = (target_profile.get("provider") or "claude").lower().strip()
    role: Optional[str] = target_profile.get("role") or None
    gate = (bundle.get("gate") or "").strip()

    logger.info(
        "Executing %r provider=%r role=%r gate=%r model=%r",
        dispatch_id, provider, role, gate, _model,
    )

    if provider == "claude":
        return _deliver_claude(terminal_id, dispatch_id, instruction, _model, role, gate)
    return _deliver_provider(provider, terminal_id, dispatch_id, instruction, _model, role, gate)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    import argparse  # noqa: PLC0415

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="VNX single-claim pool worker runner (ADR-018 Rule 2)")
    parser.add_argument("--terminal-id", required=True, help="Worker terminal ID (e.g. T1)")
    parser.add_argument("--project-id", required=True, help="VNX project ID (ADR-007)")
    parser.add_argument("--state-dir", default=None, help="Override VNX state directory")
    parser.add_argument("--dispatch-dir", default=None, help="Override dispatch bundle root")
    parser.add_argument("--model", default=None, help="Model alias override")
    parsed = parser.parse_args(argv)
    return run(
        terminal_id=parsed.terminal_id,
        project_id=parsed.project_id,
        state_dir=Path(parsed.state_dir) if parsed.state_dir else None,
        dispatch_dir=Path(parsed.dispatch_dir) if parsed.dispatch_dir else None,
        model=parsed.model,
    )


if __name__ == "__main__":
    sys.exit(main())
