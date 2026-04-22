"""Shared pytest fixtures for VNX burn-in and snapshot tests.

Provides common fixtures used by test_burnin_certification.py,
test_vnx_snapshot_tooling.py, and the burn-in CI workflow tests.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest

# Make scripts/lib importable for all tests
_LIB_DIR = Path(__file__).resolve().parent.parent / "scripts" / "lib"
_SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "schemas"

if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))


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
