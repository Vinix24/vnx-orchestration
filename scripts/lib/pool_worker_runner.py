#!/usr/bin/env python3
"""pool_worker_runner.py — Single-claim pool worker (ADR-018 Rule 2 + ADR-007 + FM-4).

No loop. Pool manager re-spawns on each tick. BILLING SAFETY: No Anthropic SDK.
Claude: deliver_with_recovery (subprocess.Popen). Non-Claude: provider_dispatch.main.
"""
from __future__ import annotations
import logging, os, re, sys
from pathlib import Path
from typing import Optional

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from runtime_coordination import claim_next_queued_dispatch, get_connection, get_dispatch  # noqa: E402

logger = logging.getLogger(__name__)

# Public exit codes — contract for pool manager (ADR-018 pool tick)
EXIT_OK = 0
EXIT_DELIVERY_FAILED = 1
EXIT_NO_WORK = 3           # ADR-018 Rule 2: queue empty; re-spawn on next tick
EXIT_PROJECT_MISMATCH = 4  # ADR-018 FM-4: claimed dispatch project_id != worker project_id
EXIT_BUNDLE_MISSING = 5    # bundle.json or prompt.txt absent for claimed dispatch
EXIT_INVALID_DISPATCH_ID = 6  # dispatch_id failed security validation (path-traversal guard)

# Safe slug: alphanumerics, hyphens, underscores, dots — no path separators or traversal
_SAFE_DISPATCH_ID_RE = re.compile(r'^[A-Za-z0-9_\-\.]+$')


def _validate_dispatch_id(dispatch_id: str, dispatch_dir: Path) -> Path:
    """Validate dispatch_id is a safe slug and the resolved bundle path stays within dispatch_dir.

    Raises ValueError with a descriptive message on any violation.
    Returns the resolved bundle path on success.
    """
    if not dispatch_id or not _SAFE_DISPATCH_ID_RE.match(dispatch_id):
        raise ValueError(
            f"dispatch_id {dispatch_id!r} contains illegal characters (path separators, '..', "
            "or is empty); refusing to resolve bundle path"
        )
    # Realpath-based containment check: resolved path must be strictly inside dispatch_dir
    canonical_root = os.path.realpath(dispatch_dir)
    resolved = os.path.realpath(Path(dispatch_dir) / dispatch_id)
    if not resolved.startswith(canonical_root + os.sep) and resolved != canonical_root:
        raise ValueError(
            f"dispatch_id {dispatch_id!r} resolved to {resolved!r} which is outside "
            f"dispatch root {canonical_root!r}; refusing"
        )
    return Path(resolved)


def _resolve_state_dir() -> Path:
    # Treat empty-string env vars as unset (VNX_STATE_DIR='' must fall through to VNX_DATA_DIR)
    vnx_state = os.environ.get("VNX_STATE_DIR") or ""
    vnx_data = os.environ.get("VNX_DATA_DIR") or ""
    if vnx_state:
        return Path(vnx_state)
    if vnx_data:
        return Path(vnx_data) / "state"
    return _LIB_DIR.parents[1] / ".vnx-data" / "state"


def _resolve_dispatch_dir() -> Path:
    data = os.environ.get("VNX_DATA_DIR")
    return Path(data) / "dispatches" if data else _LIB_DIR.parents[1] / ".vnx-data" / "dispatches"


def _deliver_claude(terminal_id: str, dispatch_id: str, instruction: str,
                    model: str, role: Optional[str], gate: str) -> int:
    try:
        from subprocess_dispatch import deliver_with_recovery  # noqa: PLC0415
        ok = deliver_with_recovery(
            terminal_id=terminal_id, instruction=instruction, model=model,
            dispatch_id=dispatch_id, role=role, gate=gate, max_retries=1,
        )
        return EXIT_OK if ok else EXIT_DELIVERY_FAILED
    except Exception as exc:
        logger.error("Claude delivery exception for %r: %s", dispatch_id, exc)
        return EXIT_DELIVERY_FAILED


def _deliver_provider(provider: str, terminal_id: str, dispatch_id: str,
                      instruction: str, model: str, role: Optional[str], gate: str) -> int:
    try:
        import provider_dispatch as pd  # noqa: PLC0415
        argv = ["--provider", provider, "--terminal-id", terminal_id,
                "--dispatch-id", dispatch_id, "--instruction", instruction, "--model", model]
        if role:
            argv += ["--role", role]
        if gate:
            argv += ["--gate", gate]
        return EXIT_OK if (pd.main(argv) or 0) == 0 else EXIT_DELIVERY_FAILED
    except Exception as exc:
        logger.error("Provider delivery exception for %r: %s", dispatch_id, exc)
        return EXIT_DELIVERY_FAILED


def run(terminal_id: str, project_id: str, *,
        state_dir: Optional[Path] = None,
        dispatch_dir: Optional[Path] = None,
        model: Optional[str] = None) -> int:
    """Claim one queued dispatch and execute it. ADR-018 Rule 2: single-claim, no loop.

    ADR-007: DB access scoped to project_id.
    ADR-018 FM-4: post-claim project_id match guard (defense-in-depth).
    Returns EXIT_OK/EXIT_NO_WORK/EXIT_PROJECT_MISMATCH/EXIT_BUNDLE_MISSING/EXIT_DELIVERY_FAILED.
    """
    _sd = state_dir or _resolve_state_dir()
    _dd = dispatch_dir or _resolve_dispatch_dir()
    _model = model or os.environ.get("VNX_DISPATCH_MODEL", "sonnet")

    with get_connection(_sd) as conn:
        dispatch_id = claim_next_queued_dispatch(conn, terminal_id, project_id)

    if dispatch_id is None:
        logger.debug("No queued dispatches for project %r — exit no-work", project_id)
        return EXIT_NO_WORK

    logger.info("Claimed dispatch %r terminal=%s project=%s", dispatch_id, terminal_id, project_id)
    # ADR-005 ledger: claim_next_queued_dispatch (N-1) already appended dispatch_claimed +
    # dispatch_claim_provenance events inside the IMMEDIATE transaction. No duplicate emit here.

    # Security: validate dispatch_id before any filesystem access (path-traversal guard)
    try:
        _validate_dispatch_id(dispatch_id, _dd)
    except ValueError as exc:
        logger.error("Path-traversal guard triggered for dispatch_id %r: %s", dispatch_id, exc)
        return EXIT_INVALID_DISPATCH_ID

    # ADR-018 FM-4: verify project_id post-claim (defense-in-depth; claim already scoped)
    with get_connection(_sd) as conn:
        row = get_dispatch(conn, dispatch_id)
    if (row or {}).get("project_id") != project_id:
        logger.error("FM-4 mismatch: dispatch %r project_id=%r != worker %r — refusing",
                     dispatch_id, (row or {}).get("project_id"), project_id)
        return EXIT_PROJECT_MISMATCH

    from dispatch_broker import DispatchBroker  # noqa: PLC0415
    broker = DispatchBroker(state_dir=_sd, dispatch_dir=_dd, shadow_mode=False)

    bundle = broker.get_bundle(dispatch_id)
    if bundle is None:
        logger.error("Bundle missing for claimed dispatch %r", dispatch_id)
        return EXIT_BUNDLE_MISSING

    prompt_path = broker.get_bundle_path(dispatch_id) / "prompt.txt"
    if not prompt_path.exists():
        logger.error("prompt.txt missing for dispatch %r", dispatch_id)
        return EXIT_BUNDLE_MISSING

    instruction = prompt_path.read_text(encoding="utf-8")
    tp = bundle.get("target_profile") or {}
    provider = (tp.get("provider") or "claude").lower().strip()
    role: Optional[str] = tp.get("role") or None
    gate = (bundle.get("gate") or "").strip()

    logger.info("Executing %r provider=%r role=%r model=%r", dispatch_id, provider, role, _model)

    if provider == "claude":
        return _deliver_claude(terminal_id, dispatch_id, instruction, _model, role, gate)
    return _deliver_provider(provider, terminal_id, dispatch_id, instruction, _model, role, gate)


def main(argv=None) -> int:
    import argparse  # noqa: PLC0415
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(description="VNX single-claim pool worker (ADR-018 Rule 2)")
    p.add_argument("--terminal-id", required=True)
    p.add_argument("--project-id", required=True)
    p.add_argument("--pool-id", default=None)  # passed by pool_manager; reserved for future FM-4 scoping
    p.add_argument("--state-dir", default=None)
    p.add_argument("--dispatch-dir", default=None)
    p.add_argument("--model", default=None)
    a = p.parse_args(argv)
    return run(terminal_id=a.terminal_id, project_id=a.project_id,
               state_dir=Path(a.state_dir) if a.state_dir else None,
               dispatch_dir=Path(a.dispatch_dir) if a.dispatch_dir else None,
               model=a.model)


if __name__ == "__main__":
    sys.exit(main())
