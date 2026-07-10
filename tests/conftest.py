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

# ---------------------------------------------------------------------------
# Module-level isolation pin (import-time / collection-time guard)
# ---------------------------------------------------------------------------
# _pytest_db_isolation_guard detects pytest via sys.modules (active from
# collection onward, before PYTEST_CURRENT_TEST is set). This pin ensures
# VNX_DATA_DIR_EXPLICIT=1 and a temp VNX_DATA_DIR are in place from the
# moment conftest loads, so any module-level run() call during collection
# hits the guard instead of touching ~/.vnx-data.
# Per-module (_fsr_migration_module_isolation) and per-test (_vnx_data_dir_isolation)
# fixtures re-pin to tighter tmp dirs; this is the fallback floor.
_CONFTEST_ISOLATION_TMP = tempfile.mkdtemp(prefix="vnx_conftest_")
os.environ["VNX_DATA_DIR_EXPLICIT"] = "1"
os.environ["VNX_DATA_DIR"] = _CONFTEST_ISOLATION_TMP
# Keep the new data-dir guard from emitting warnings during normal tests.
# Tests that exercise the guard override this explicitly.
os.environ.setdefault("VNX_DATA_DIR_GUARD", "off")

# Make scripts/lib importable for all tests
_LIB_DIR = Path(__file__).resolve().parent.parent / "scripts" / "lib"
_SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "schemas"

if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers used by integration / canary suites."""
    config.addinivalue_line(
        "markers",
        "integration: end-to-end integration tests (slower; opt-in via -m integration)",
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

@pytest.fixture(autouse=True)
def _vnx_data_dir_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect VNX_DATA_DIR so EventStore() without an explicit events_dir
    cannot write to ~/.vnx-data during any test run.

    Sets VNX_DATA_DIR_EXPLICIT=1 so the explicit-path branch in _events_dir()
    is taken. Tests that need a specific value can override via their own
    monkeypatch.setenv — the last setenv wins within the same function scope.
    Tests that need the fallback behaviour (no explicit flag) can monkeypatch
    delenv("VNX_DATA_DIR_EXPLICIT") to undo this guard for that test only.
    """
    isolated = tmp_path / "_vnx_test_data"
    isolated.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("VNX_DATA_DIR", str(isolated))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")


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
