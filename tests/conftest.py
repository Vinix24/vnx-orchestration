"""Shared pytest fixtures for VNX burn-in and snapshot tests.

Provides common fixtures used by test_burnin_certification.py,
test_vnx_snapshot_tooling.py, and the burn-in CI workflow tests.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

# Subsystem dirs that scripts/lib/vnx_paths.py's resolve_paths() honors as
# LITERAL env-var overrides — independent of VNX_DATA_DIR/VNX_DATA_DIR_EXPLICIT.
# A worker/T0 process inherits these pointed at the real central store
# (~/.vnx-data/<project>/state, .../dispatches, ...), so pinning VNX_DATA_DIR
# alone leaves isolation half-done: resolve_paths() still resolves these
# straight back to production regardless of what VNX_DATA_DIR is set to
# (w22/PR#1333 — this bit test_pr_dispatch_integration.py and
# test_pr_recommendation_integration.py, whose own fixtures had the same gap,
# and it bit the module-level pin below too: scripts/generate_t0_recommendations.py
# resolves and caches VNX_STATE_DIR/VNX_DISPATCH_DIR at import time, so an
# ambient-polluted env leaks into its cached paths for the rest of the
# session unless this pin closes the gap before any test module is collected).
_VNX_SUBSYSTEM_DIRS = {
    "VNX_STATE_DIR": "state",
    "VNX_DISPATCH_DIR": "dispatches",
    "VNX_LOGS_DIR": "logs",
    "VNX_PIDS_DIR": "pids",
    "VNX_LOCKS_DIR": "locks",
    "VNX_SOCKETS_DIR": "sockets",
    "VNX_REPORTS_DIR": "unified_reports",
    "VNX_DB_DIR": "database",
    "VNX_HEADLESS_REPORTS_DIR": "unified_reports/headless",
}

# ---------------------------------------------------------------------------
# Module-level isolation pin (import-time / collection-time guard)
# ---------------------------------------------------------------------------
# _pytest_db_isolation_guard detects pytest via sys.modules (active from
# collection onward, before PYTEST_CURRENT_TEST is set). This pin ensures
# VNX_DATA_DIR_EXPLICIT=1 and a temp VNX_DATA_DIR (+ every subsystem dir
# resolve_paths() honors directly) are in place from the moment conftest
# loads, so any module-level run() call during collection — or any
# module-level path caching, like generate_t0_recommendations.py's
# STATE_DIR/DISPATCHES_DIR constants — hits the isolated tmp tree instead of
# ~/.vnx-data, regardless of what the ambient shell environment set before
# pytest started.
# Per-module (_fsr_migration_module_isolation) and per-test (_vnx_data_dir_isolation)
# fixtures re-pin to tighter tmp dirs; this is the fallback floor.
_CONFTEST_ISOLATION_TMP = tempfile.mkdtemp(prefix="vnx_conftest_")
os.environ["VNX_DATA_DIR_EXPLICIT"] = "1"
os.environ["VNX_DATA_DIR"] = _CONFTEST_ISOLATION_TMP
for _env_key, _subdir in _VNX_SUBSYSTEM_DIRS.items():
    os.environ[_env_key] = str(Path(_CONFTEST_ISOLATION_TMP) / _subdir)
del _env_key, _subdir
# Keep the new data-dir guard from emitting warnings during normal tests.
# Tests that exercise the guard override this explicitly.
os.environ.setdefault("VNX_DATA_DIR_GUARD", "off")

# OI-975 git-target guard: disabled for the suite by default. When the suite
# runs INSIDE a dispatch worker the lanes export VNX_CURRENT_DISPATCH_ID /
# VNX_DISPATCH_ID, and the guard would refuse every tmp repo in the suite (a
# tmp repo is structurally the main checkout of its own repo). The guard's own
# tests (tests/test_git_target_guard.py) set VNX_GIT_TARGET_GUARD=1 explicitly.
os.environ.setdefault("VNX_GIT_TARGET_GUARD", "0")

# Make the repo root importable for all tests. pytest's prepend import mode
# only inserts the first non-package dir (tests/) into sys.path, so top-level
# packages like scripts.* are unreachable when pytest is invoked on an absolute
# path — which is exactly what CI does (pytest "$VNX_HOME/tests"). Inserting the
# root mirrors `python -m pytest` from the repo root, where cwd is on sys.path.
_ROOT_DIR = Path(__file__).resolve().parent.parent
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

# Make scripts/lib importable for all tests
_LIB_DIR = _ROOT_DIR / "scripts" / "lib"
_SCHEMAS_DIR = _ROOT_DIR / "schemas"

if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers used by integration / canary suites."""
    config.addinivalue_line(
        "markers",
        "integration: end-to-end integration tests (slower; opt-in via -m integration)",
    )
    config.addinivalue_line(
        "markers",
        "live: real headless-LLM inference (f39 replays; deselected by default — "
        "opt in with -m live or --dry-run, OI-908)",
    )


# ---------------------------------------------------------------------------
# Future-state / migration module-level isolation (R8.6, PR-0)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def _fsr_migration_module_isolation(tmp_path_factory: pytest.TempPathFactory):
    """Module-scoped isolation for future-state and migration test modules.

    Ensures VNX_DATA_DIR_EXPLICIT=1 + VNX_DATA_DIR pointing at a per-module
    tmp dir for the duration of each test module. Complements the per-function
    _vnx_data_dir_isolation fixture below.

    Cannot use monkeypatch (function-scoped); uses os.environ directly and
    restores it via yield teardown.

    Targets: test_future_state_reconciliation.py, test_migrate_future_system.py,
    test_migrate_0022_preflight.py — and is harmlessly applied to all other
    modules in this directory (extra isolation is always safe).
    """
    isolated = tmp_path_factory.mktemp("_fsr_module")
    _prev = {
        "VNX_DATA_DIR": os.environ.get("VNX_DATA_DIR"),
        "VNX_DATA_DIR_EXPLICIT": os.environ.get("VNX_DATA_DIR_EXPLICIT"),
    }
    os.environ["VNX_DATA_DIR"] = str(isolated)
    os.environ["VNX_DATA_DIR_EXPLICIT"] = "1"
    yield isolated
    for key, val in _prev.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val


# ---------------------------------------------------------------------------
# Production events-dir contamination guard
# ---------------------------------------------------------------------------

def pin_vnx_data_dir(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    """Pin VNX_DATA_DIR and every subsystem dir resolve_paths() derives from it.

    Sets VNX_DATA_DIR_EXPLICIT=1 plus VNX_DATA_DIR, and ALSO pins
    VNX_STATE_DIR / VNX_DISPATCH_DIR / etc. (see _VNX_SUBSYSTEM_DIRS) to the
    same isolated tree. Without the subsystem pins, an ambient
    VNX_STATE_DIR/VNX_DISPATCH_DIR inherited from a real worker/T0 shell
    environment wins outright in resolve_paths() — VNX_DATA_DIR alone does
    not shadow it. Shared by the module-level isolation fixture below and by
    tests/test_pr_dispatch_integration.py + tests/test_pr_recommendation_integration.py,
    which need the same full pin for their own PRQueueManager() writes.
    """
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    for env_key, subdir in _VNX_SUBSYSTEM_DIRS.items():
        monkeypatch.setenv(env_key, str(data_dir / subdir))


@pytest.fixture(autouse=True)
def _vnx_data_dir_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect VNX_DATA_DIR (+ subsystem dirs) so EventStore() without an
    explicit events_dir cannot write to ~/.vnx-data during any test run.

    Sets VNX_DATA_DIR_EXPLICIT=1 so the explicit-path branch in _events_dir()
    is taken. Tests that need a specific value can override via their own
    monkeypatch.setenv — the last setenv wins within the same function scope.
    Tests that need the fallback behaviour (no explicit flag) can monkeypatch
    delenv("VNX_DATA_DIR_EXPLICIT") to undo this guard for that test only.

    This is the "point the store at tmp_path" half of the class-wide guard
    (w19c/OI-934). It is NOT itself the backstop: a test that opts out of the
    explicit flag (or a subprocess that loses it) still has a legitimate
    no-flag resolution path (fresh-install / project-local fallbacks),
    so this fixture cannot unconditionally refuse — and neither can the
    resolver itself (``vnx_paths.resolve_paths()`` / ``_resolve_state_root()``
    are pure computations with plenty of legitimate read-only callers that
    must keep resolving to wherever production would, real central store
    included, without failing).

    The "fail loud when about to WRITE into the real ~/.vnx-data" half lives
    at the actual write surfaces instead:
    ``vnx_paths.refuse_real_central_store_write_under_pytest`` is called from
    ``PRQueueManager.__init__`` (scripts/pr_queue_manager.py) and from
    ``vnx_mode._guard_mode_write_target`` (scripts/lib/vnx_mode.py, alongside
    the existing OI-911 divergence/cross-project checks). New write surfaces
    should call it too — see that function's docstring in
    scripts/lib/vnx_paths.py. Regression tests:
    tests/test_vnx_data_dir_real_store_guard.py.
    """
    isolated = tmp_path / "_vnx_test_data"
    isolated.mkdir(parents=True, exist_ok=True)
    pin_vnx_data_dir(monkeypatch, isolated)


# ---------------------------------------------------------------------------
# DB / registry fixtures  (shared with test_burnin_certification)
# ---------------------------------------------------------------------------

@pytest.fixture()
def vnx_state_dir(tmp_path: Path) -> Path:
    """Temp state directory with initialized runtime-coordination schema."""
    from runtime_coordination import init_schema

    sd = tmp_path / "state"
    sd.mkdir()
    init_schema(sd, _SCHEMAS_DIR / "runtime_coordination.sql")
    return sd


@pytest.fixture()
def vnx_registry(vnx_state_dir: Path):
    """HeadlessRunRegistry backed by a fresh in-memory-like state dir."""
    from headless_run_registry import HeadlessRunRegistry

    return HeadlessRunRegistry(vnx_state_dir)


@pytest.fixture()
def vnx_artifact_dir(tmp_path: Path) -> Path:
    d = tmp_path / "artifacts"
    d.mkdir()
    return d


@pytest.fixture()
def vnx_dispatch_dir(tmp_path: Path) -> Path:
    d = tmp_path / "dispatches"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# Snapshot / project-layout fixtures  (shared with test_vnx_snapshot_tooling)
# ---------------------------------------------------------------------------

@pytest.fixture()
def vnx_fake_project(tmp_path: Path) -> Path:
    """Minimal project layout with .vnx-data skeleton."""
    vnx_data = tmp_path / ".vnx-data"
    state = vnx_data / "state"
    (vnx_data / "dispatches" / "active").mkdir(parents=True)
    (vnx_data / "dispatches" / "pending").mkdir(parents=True)
    state.mkdir(parents=True)
    (state / "t0_receipts.ndjson").write_text("{}\n", encoding="utf-8")
    return tmp_path


@pytest.fixture()
def vnx_snapshot_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Override ~/vnx-snapshots to a temp dir so tests don't touch home."""
    snap_dir = tmp_path / "vnx-snapshots"
    snap_dir.mkdir()
    monkeypatch.setattr("vnx_snapshot._snapshots_dir", lambda: snap_dir)
    return snap_dir


# ---------------------------------------------------------------------------
# Dispatch-bundle builder helper
# ---------------------------------------------------------------------------

def make_dispatch_bundle(
    dispatch_dir: Path,
    dispatch_id: str | None = None,
    prompt: str = "Summarize the architecture.",
    task_class: str = "research_structured",
) -> tuple[str, Path]:
    """Create a minimal dispatch bundle on disk.  Returns (dispatch_id, bundle_path)."""
    did = dispatch_id or f"fixture-dispatch-{uuid.uuid4().hex[:8]}"
    bundle_path = dispatch_dir / did
    bundle_path.mkdir(parents=True, exist_ok=True)
    (bundle_path / "bundle.json").write_text(
        json.dumps({"dispatch_id": did, "task_class": task_class}),
        encoding="utf-8",
    )
    (bundle_path / "prompt.txt").write_text(prompt, encoding="utf-8")
    return did, bundle_path


@pytest.fixture()
def make_vnx_dispatch_bundle(vnx_dispatch_dir: Path):
    """Fixture that returns a callable for creating dispatch bundles in the shared dispatch dir."""

    def _make(
        dispatch_id: str | None = None,
        prompt: str = "Summarize the architecture.",
        task_class: str = "research_structured",
    ) -> tuple[str, Path]:
        return make_dispatch_bundle(vnx_dispatch_dir, dispatch_id, prompt, task_class)

    return _make
