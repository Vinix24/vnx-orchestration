#!/usr/bin/env python3
"""pool_worker_runner.py — Single-claim pool worker (ADR-018 Rule 2 + ADR-007 + FM-4).

No loop. Pool manager re-spawns on each tick. BILLING SAFETY: No Anthropic SDK.
Claude: deliver_with_recovery (subprocess.Popen). Non-Claude: provider_dispatch.main.

Lease ownership (ADR-042 besluit 6, amended 2026-10-27): the pool starts this runner by
default and delivers through the single-entry door. From the moment the pool manager writes
the `terminal_leases` row for a worker, THIS worker owns it: it renews
`last_heartbeat_at` while it runs (``_PoolLease``) and releases the row when it ends. The
legacy ``subprocess_dispatch._pool_heartbeat_loop`` still renews for the rollback route
(``VNX_SINGLE_ENTRY_DISPATCH=0`` / ``VNX_DISPATCH_LEGACY=1``); T-G1 keeps it because that
rollback net is hard-parked (plan-gate point 1, 2026-10-27).
"""
from __future__ import annotations
import logging, os, re, sys, threading, time
from pathlib import Path
from typing import Optional

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from coordination_db import db_path_from_state_dir  # noqa: E402
from pool_state_repo import PoolStateRepository  # noqa: E402
from runtime_coordination import claim_next_queued_dispatch, get_connection, get_dispatch  # noqa: E402

logger = logging.getLogger(__name__)

# Renewal cadence for the lease this worker owns. The env override exists so tests and
# slow hosts can shorten it; a malformed value falls back to the normal cadence (loudly).
LEASE_HEARTBEAT_ENV = "VNX_POOL_LEASE_HEARTBEAT_SECONDS"
LEASE_HEARTBEAT_DEFAULT_S = 15.0

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


def _canonical_data_dir() -> Optional[Path]:
    """Resolve VNX_DATA_DIR via the canonical vnx_paths resolver (central-mode aware)."""
    try:
        from vnx_paths import resolve_paths
        return Path(resolve_paths()["VNX_DATA_DIR"])
    except Exception:
        return None


def _resolve_state_dir() -> Path:
    # Treat empty-string env vars as unset (VNX_STATE_DIR='' must fall through to VNX_DATA_DIR)
    vnx_state = os.environ.get("VNX_STATE_DIR") or ""
    vnx_data = os.environ.get("VNX_DATA_DIR") or ""
    if vnx_state:
        return Path(vnx_state)
    if vnx_data:
        return Path(vnx_data) / "state"
    # A raw _LIB_DIR.parents[1] walk resolves the KEYSTONE (not the project's
    # ~/.vnx-data/<project>) in a central install. See #1023.
    canonical = _canonical_data_dir()
    if canonical is not None:
        return canonical / "state"
    return _LIB_DIR.parents[1] / ".vnx-data" / "state"


def _resolve_dispatch_dir() -> Path:
    data = os.environ.get("VNX_DATA_DIR")
    if data:
        return Path(data) / "dispatches"
    canonical = _canonical_data_dir()
    if canonical is not None:
        return canonical / "dispatches"
    return _LIB_DIR.parents[1] / ".vnx-data" / "dispatches"


def _lease_heartbeat_interval() -> float:
    """Renewal interval in seconds (``VNX_POOL_LEASE_HEARTBEAT_SECONDS`` overrides 15s)."""
    raw = os.environ.get(LEASE_HEARTBEAT_ENV)
    if not raw:
        return LEASE_HEARTBEAT_DEFAULT_S
    try:
        interval = float(raw)
    except ValueError:
        logger.warning("Ignoring non-numeric %s=%r; renewing every %ss",
                       LEASE_HEARTBEAT_ENV, raw, LEASE_HEARTBEAT_DEFAULT_S)
        return LEASE_HEARTBEAT_DEFAULT_S
    if interval <= 0:
        logger.warning("Ignoring non-positive %s=%r; renewing every %ss",
                       LEASE_HEARTBEAT_ENV, raw, LEASE_HEARTBEAT_DEFAULT_S)
        return LEASE_HEARTBEAT_DEFAULT_S
    return interval


class _PoolLease:
    """Owns the ``terminal_leases`` row of the terminal this worker serves (ADR-042 §6).

    The pool manager writes the row right after it spawns a worker
    (``PoolStateRepository.add_or_refresh_pool_lease``, ``worker_pid`` = this process). From
    then on the worker owns renewal: ``__enter__`` takes one heartbeat immediately and
    starts a daemon thread that renews ``last_heartbeat_at`` every interval; ``__exit__``
    stops the thread and releases the row, so a finished worker never leaves a live-looking
    lease behind or blocks the re-spawn of its terminal (single-claim, ADR-018 Rule 2).

    A failed renewal or release is a liveness signal, not evidence: it is logged and the
    worker keeps running (the pool reaper's dead-PID path still collects a dead worker).
    """

    def __init__(self, state_dir: Path, project_id: str, terminal_id: str,
                 interval: Optional[float] = None) -> None:
        self.terminal_id = terminal_id
        self.project_id = project_id
        self.interval = _lease_heartbeat_interval() if interval is None else interval
        self._repo = PoolStateRepository(db_path_from_state_dir(state_dir), project_id)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _renew(self) -> None:
        try:
            self._repo.update_heartbeat_by_terminal(self.terminal_id, time.time())
        except Exception as exc:  # vnx-silent-except: liveness signal, see class docstring
            logger.warning("lease heartbeat failed for terminal=%s: %s", self.terminal_id, exc)

    def _renew_loop(self) -> None:
        while not self._stop.wait(self.interval):
            self._renew()

    def __enter__(self) -> "_PoolLease":
        self._renew()
        self._thread = threading.Thread(
            target=self._renew_loop, daemon=True, name=f"pool-lease-{self.terminal_id}",
        )
        self._thread.start()
        return self

    def __exit__(self, *_exc_info) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        try:
            self._repo.release_pool_lease(self.terminal_id, "pool_worker_exit", time.time())
        except Exception as exc:  # vnx-silent-except: liveness signal, see class docstring
            logger.warning("lease release failed for terminal=%s: %s", self.terminal_id, exc)
        return None


def _deliver_claude(terminal_id: str, dispatch_id: str, instruction: str,
                    model: str, role: Optional[str], gate: str,
                    deadline_seconds: int = 900,
                    skill: Optional[str] = None) -> int:
    # PR-12: route through the single-entry door when VNX_SINGLE_ENTRY_DISPATCH=1; OFF default =
    # the legacy deliver_with_recovery call (the lambda), byte-identical.
    try:
        from subprocess_dispatch import deliver_with_recovery  # noqa: PLC0415
        import dispatch_bridge  # noqa: PLC0415
        ok = dispatch_bridge.deliver_via_door(
            lambda: deliver_with_recovery(
                terminal_id=terminal_id, instruction=instruction, model=model,
                dispatch_id=dispatch_id, role=role, gate=gate, max_retries=1,
                total_deadline=float(deadline_seconds), skill=skill,
            ),
            instruction_text=instruction, dispatch_id=dispatch_id, role=role,
            target_slot=terminal_id, provider="claude", model=model, gate=gate,
            deadline_seconds=deadline_seconds, skill=skill,
        )
        return EXIT_OK if ok else EXIT_DELIVERY_FAILED
    except Exception as exc:
        logger.error("Claude delivery exception for %r: %s", dispatch_id, exc)
        return EXIT_DELIVERY_FAILED


def _deliver_provider(provider: str, terminal_id: str, dispatch_id: str,
                      instruction: str, model: str, role: Optional[str], gate: str,
                      deadline_seconds: int = 900,
                      skill: Optional[str] = None) -> int:
    try:
        import provider_dispatch as pd  # noqa: PLC0415
        import dispatch_bridge  # noqa: PLC0415

        def _legacy() -> bool:
            argv = ["--provider", provider, "--terminal-id", terminal_id,
                    "--dispatch-id", dispatch_id, "--instruction", instruction, "--model", model,
                    "--deadline-seconds", str(deadline_seconds)]
            if role:
                argv += ["--role", role]
            if gate:
                argv += ["--gate", gate]
            if skill:
                argv += ["--skill", skill]
            return (pd.main(argv) or 0) == 0

        ok = dispatch_bridge.deliver_via_door(
            _legacy, instruction_text=instruction, dispatch_id=dispatch_id, role=role,
            target_slot=terminal_id, provider=provider, model=model, gate=gate,
            deadline_seconds=deadline_seconds, skill=skill,
        )
        return EXIT_OK if ok else EXIT_DELIVERY_FAILED
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
    _model = model or os.environ.get("VNX_DISPATCH_MODEL", "kimi-k3")

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
    # worker-provider-kimi-flip (20260723): this runtime fallback stays kimi when the
    # bundle's target_profile doesn't declare a provider. The registry default in
    # vnx_workers.default.yaml is claude/sonnet; the two are decided separately.
    provider = (tp.get("provider") or "kimi").lower().strip()
    role: Optional[str] = tp.get("role") or None
    gate = (bundle.get("gate") or "").strip()

    # Read deadline_seconds and the spec-bound skill from the staged dispatch-spec.json
    # (default 900 matches spawn_claude's own default so dispatches without a deadline
    # are unchanged).
    deadline_seconds = 900
    skill: Optional[str] = None
    spec_path = broker.get_bundle_path(dispatch_id) / "dispatch-spec.json"
    if spec_path.exists():
        try:
            import json as _json
            _spec_data = _json.loads(spec_path.read_text(encoding="utf-8"))
            deadline_seconds = int(_spec_data.get("deadline_seconds", 900))
            # OI-2372: the skill staging bound to this dispatch rides the same spec
            # read as the deadline. Absent / null / blank = no skill, so the prompt
            # is unchanged. A non-string is not a skill name: treat it as absent
            # rather than handing a Python object to the seam.
            _raw_skill = _spec_data.get("skill")
            if isinstance(_raw_skill, str) and _raw_skill.strip():
                skill = _raw_skill.strip()
        except Exception:
            logger.warning(
                "Failed to read deadline_seconds/skill from spec %s; falling back to 900s "
                "and no skill",
                spec_path, exc_info=True,
            )

    logger.info("Executing %r provider=%r role=%r model=%r deadline=%ds skill=%r",
                dispatch_id, provider, role, _model, deadline_seconds, skill)

    if provider == "claude":
        return _deliver_claude(terminal_id, dispatch_id, instruction, _model, role, gate,
                               deadline_seconds=deadline_seconds, skill=skill)
    return _deliver_provider(provider, terminal_id, dispatch_id, instruction, _model, role, gate,
                             deadline_seconds=deadline_seconds, skill=skill)


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
    state_dir = Path(a.state_dir) if a.state_dir else _resolve_state_dir()
    dispatch_dir = Path(a.dispatch_dir) if a.dispatch_dir else _resolve_dispatch_dir()
    # The worker process owns its terminal lease for as long as it runs (ADR-042 §6).
    with _PoolLease(state_dir, a.project_id, a.terminal_id):
        return run(terminal_id=a.terminal_id, project_id=a.project_id,
                   state_dir=state_dir, dispatch_dir=dispatch_dir, model=a.model)


if __name__ == "__main__":
    sys.exit(main())
