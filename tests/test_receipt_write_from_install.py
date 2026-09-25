"""OI-1788: a receipt write from a read-only central install lands in the
project's central store, never in the install's own version directory.

Measured on mission-control (v1.6.3, probe D-615f4dc5): the worker succeeded,
``envelope._govern`` logged ``VNX_RECEIPT_EMIT_FAILURE work_status=success:
... Failed to acquire append lock: [Errno 13] Permission denied:
'~/.vnx-system/versions/v1.6.3/.vnx-data'``, and the ledger booked a failed
dispatch with no receipt (75 rows in ``receipt_emit_failures.ndjson`` since 15-09).

The lock was never the problem. ``spec.state_dir`` is the door's own central
store and the ledger path is correct. The failing call is the receipt's
pre-write hook: a ``warnings[]`` entry that resolves to ``counted`` bumps the
warning-recurrence counter, and ``warning_destination._default_counter_path``
built that path with ``project_root.resolve_state_dir(__file__)``. Inside an
install that anchors on the version directory (``dr-xr-xr-x``, no ``.vnx-data``),
so the ``mkdir`` raised ``PermissionError``. ``_write_receipt_under_lock`` then
relabelled every ``OSError`` from that hook as "Failed to acquire append lock",
which sent the first diagnosis after the lock path.

These tests run the REAL code from a COPY of ``scripts/`` placed the way the
central install places it (read-only tree, own ``.git``, ``.vnx-install-mode``
= ``central``) in a subprocess with a tmp HOME and every ``VNX_*`` variable
removed, so nothing can be rescued by an env pin and nothing touches the real
``~/.vnx-data`` or ``~/.vnx-system``.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SCRIPTS_LIB = _REPO / "scripts" / "lib"
if str(_SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_LIB))

from append_receipt_internals.common import AppendReceiptError  # noqa: E402
from append_receipt_internals.idempotency import (  # noqa: E402
    _cache_file_for,
    _write_receipt_under_lock,
)
from append_receipt_internals.warning_destination import _COUNTER_FILENAME  # noqa: E402

_PROJECT_ID = "consumer-proj"
_DISPATCH_ID = "20260924-oi1788-install-probe"

# Runs inside the subprocess. Exercises the envelope lane end to end: a worker
# that succeeded but wrote a report without the four contract headings, so
# _govern stamps a report_contract_violated warning on the receipt.
_DRIVER = r"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

engine_lib, state_dir, data_dir, dispatch_id = sys.argv[1:5]
sys.path.insert(0, engine_lib)

from envelope_govern import _govern
from envelope_types import EnvelopeSpec, _AdapterResult

spec = EnvelopeSpec(
    dispatch_id=dispatch_id,
    terminal_id="T1",
    provider="deepseek-harness",
    model="deepseek-v4-pro",
    instruction="do the thing",
    role="backend-developer",
    pr_id=None,
    state_dir=Path(state_dir),
    data_dir=Path(data_dir),
)
reports = Path(data_dir) / "unified_reports"
reports.mkdir(parents=True, exist_ok=True)
(reports / (dispatch_id + ".md")).write_text(
    "# Dispatch " + dispatch_id + "\n\nNo contract headings in this report.\n",
    encoding="utf-8",
)
result = _AdapterResult(returncode=0, completion_text="done", status="success")
end = datetime.now(timezone.utc)
report_path, receipt_path = _govern(spec, result, end - timedelta(seconds=5), end)
print(json.dumps({
    "report": str(report_path) if report_path else None,
    "receipt": str(receipt_path) if receipt_path else None,
}))
"""


# Same warning branch through the other receipt writer (append_receipt_payload,
# the path the report converter and the gate runners book through).
_PATH2_DRIVER = r"""
import sys

engine_scripts, receipts_file = sys.argv[1:3]
sys.path.insert(0, engine_scripts)

import append_receipt as ar

result = ar.append_receipt_payload(
    {
        "timestamp": "2026-09-24T10:00:00Z",
        "event_type": "task_complete",
        "dispatch_id": "20260924-oi1788-path2",
        "status": "done",
        "warnings": [{"code": "oi1788_path2_probe", "severity": "warn", "message": "m"}],
    },
    receipts_file=receipts_file,
    skip_enrichment=True,
)
print(result.status)
"""


def _run(cmd, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kwargs)


def _copy_engine(root: Path, *, central: bool) -> Path:
    """Copy scripts/ to ``root`` the way an install lays it out; own git repo."""
    shutil.copytree(
        _REPO / "scripts",
        root / "scripts",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    (root / "VERSION").write_text("9.9.9\n", encoding="utf-8")
    if central:
        (root / ".vnx-install-mode").write_text("central\n", encoding="utf-8")
    _run(["git", "init", "-q", str(root)])
    return root


def _set_tree_mode(root: Path, *, read_only: bool) -> None:
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            p = Path(dirpath) / name
            if p.is_symlink():
                continue
            p.chmod(p.stat().st_mode & ~stat.S_IWUSR if read_only else p.stat().st_mode | stat.S_IWUSR)
        d = Path(dirpath)
        d.chmod(d.stat().st_mode & ~stat.S_IWUSR if read_only else d.stat().st_mode | stat.S_IWUSR)


def _clean_env(home: Path) -> Dict[str, str]:
    """A launchd-like environment: no VNX_* pin, no inherited project state."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VNX_", "PYTEST_"))}
    env.pop("PROJECT_ROOT", None)
    env["HOME"] = str(home)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("PYTHONPATH", None)
    return env


def _tree_snapshot(root: Path) -> set:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


class _World:
    def __init__(self, home: Path, engine: Path, project: Path):
        self.home = home
        self.engine = engine
        self.project = project
        self.store = home / ".vnx-data" / _PROJECT_ID
        self.state_dir = self.store / "state"

    def govern(self, dispatch_id: str = _DISPATCH_ID) -> Dict[str, Optional[str]]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [
                sys.executable, "-c", _DRIVER,
                str(self.engine / "scripts" / "lib"),
                str(self.state_dir), str(self.store), dispatch_id,
            ],
            cwd=self.project,
            env=_clean_env(self.home),
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, f"driver crashed:\n{proc.stdout}\n{proc.stderr}"
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def append_payload(self) -> str:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [
                sys.executable, "-c", _PATH2_DRIVER,
                str(self.engine / "scripts"),
                str(self.state_dir / "t0_receipts.ndjson"),
            ],
            cwd=self.project,
            env=_clean_env(self.home),
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, f"path 2 driver crashed:\n{proc.stdout}\n{proc.stderr}"
        return proc.stdout.strip().splitlines()[-1]


def _build_world(base: Path, *, central: bool) -> _World:
    home = base / "home"
    project = home / "projects" / "consumer"
    project.mkdir(parents=True)
    (project / ".vnx-project-id").write_text(_PROJECT_ID + "\n", encoding="utf-8")
    _run(["git", "init", "-q", str(project)])
    if central:
        engine = home / ".vnx-system" / "versions" / "v9.9.9"
    else:
        engine = base / "fabric-checkout"
    engine.mkdir(parents=True)
    _copy_engine(engine, central=central)
    return _World(home, engine, project)


@pytest.fixture(scope="module")
def install_world(tmp_path_factory):
    world = _build_world(tmp_path_factory.mktemp("install"), central=True)
    _set_tree_mode(world.engine, read_only=True)
    yield world
    _set_tree_mode(world.engine, read_only=False)


@pytest.fixture(scope="module")
def fabric_world(tmp_path_factory):
    return _build_world(tmp_path_factory.mktemp("fabric"), central=False)


def _receipt_lines(state_dir: Path):
    ledger = state_dir / "t0_receipts.ndjson"
    return [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# From the install: the receipt lands in the central store, the version dir stays clean
# ---------------------------------------------------------------------------


def test_receipt_write_from_install_lands_in_central_store(install_world):
    before = _tree_snapshot(install_world.engine)

    out = install_world.govern()

    trail = install_world.state_dir / "receipt_emit_failures.ndjson"
    detail = trail.read_text(encoding="utf-8") if trail.exists() else "(no failure trail)"
    assert out["receipt"] is not None, f"the receipt write failed inside the install:\n{detail}"
    assert Path(out["receipt"]) == install_world.state_dir / "t0_receipts.ndjson"
    lines = [r for r in _receipt_lines(install_world.state_dir) if r["dispatch_id"] == _DISPATCH_ID]
    assert len(lines) == 1
    # The envelope stamped the body-contract warning, so this run took the
    # warn -> counted -> counter-increment branch that broke on mission-control.
    assert [w["code"] for w in lines[0]["warnings"]] == ["report_contract_violated"]
    assert lines[0]["warnings"][0]["destination"] == "counted"

    assert not (install_world.state_dir / "receipt_emit_failures.ndjson").exists()
    assert not (install_world.engine / ".vnx-data").exists()
    assert _tree_snapshot(install_world.engine) == before


def test_warning_counter_lands_beside_the_ledger_from_install(install_world):
    install_world.govern("20260924-oi1788-install-counter")

    counter_file = install_world.state_dir / _COUNTER_FILENAME
    assert counter_file.is_file(), "the recurrence counter was not written into the central state dir"
    counter = json.loads(counter_file.read_text(encoding="utf-8"))
    assert counter["report_contract_violated"] >= 1


def test_append_receipt_payload_from_install_counts_beside_the_ledger(install_world):
    before = _tree_snapshot(install_world.engine)

    status = install_world.append_payload()

    assert status == "appended"
    lines = [r for r in _receipt_lines(install_world.state_dir) if r["dispatch_id"] == "20260924-oi1788-path2"]
    assert len(lines) == 1
    assert lines[0]["warnings"][0]["destination"] == "counted"
    counter = json.loads((install_world.state_dir / _COUNTER_FILENAME).read_text(encoding="utf-8"))
    assert counter["oi1788_path2_probe"] == 1
    assert not (install_world.engine / ".vnx-data").exists()
    assert _tree_snapshot(install_world.engine) == before


# ---------------------------------------------------------------------------
# The fabric repo itself (no install) keeps writing where it always did
# ---------------------------------------------------------------------------


def test_receipt_write_from_fabric_checkout_is_unchanged(fabric_world):
    out = fabric_world.govern("20260924-oi1788-fabric-probe")

    assert out["receipt"] is not None
    assert Path(out["receipt"]) == fabric_world.state_dir / "t0_receipts.ndjson"
    lines = [r for r in _receipt_lines(fabric_world.state_dir) if r["dispatch_id"] == "20260924-oi1788-fabric-probe"]
    assert len(lines) == 1
    assert lines[0]["status"] == "contract_invalid"
    assert lines[0]["warnings"][0]["code"] == "report_contract_violated"
    assert not (fabric_world.state_dir / "receipt_emit_failures.ndjson").exists()


# ---------------------------------------------------------------------------
# An explicit VNX_STATE_DIR still wins over the canonical resolution
# ---------------------------------------------------------------------------


def test_default_counter_path_honours_explicit_state_dir(monkeypatch, tmp_path):
    import append_receipt_internals.warning_destination as wd

    pinned = tmp_path / "pinned-state"
    monkeypatch.setenv("VNX_STATE_DIR", str(pinned))

    assert wd._default_counter_path().parent == pinned


# ---------------------------------------------------------------------------
# The relabel: a hook failure is not a lock failure
# ---------------------------------------------------------------------------


def _receipt() -> Dict[str, object]:
    return {
        "dispatch_id": "20260924-oi1788-hook",
        "terminal": "T1",
        "event_type": "task_complete",
        "status": "success",
        "timestamp": "2026-09-24T10:00:00Z",
    }


def test_pre_write_hook_oserror_is_not_reported_as_a_lock_failure(tmp_path):
    ledger = tmp_path / "state" / "t0_receipts.ndjson"
    ledger.parent.mkdir()

    def hook(_receipt_dict):
        raise PermissionError(13, "Permission denied", str(tmp_path / "ro" / ".vnx-data"))

    with pytest.raises(AppendReceiptError) as exc_info:
        _write_receipt_under_lock(
            _receipt(), ledger, _cache_file_for(ledger), "key-1", 300, pre_write_hook=hook,
        )

    assert "acquire append lock" not in str(exc_info.value)
    assert exc_info.value.code == "pre_write_hook_failed"
    assert str(tmp_path / "ro" / ".vnx-data") in str(exc_info.value)
    assert not ledger.exists(), "a failed hook must not leave a half-written ledger line"


def test_unwritable_lock_location_is_still_a_lock_failure(tmp_path):
    ledger = tmp_path / "missing-dir" / "t0_receipts.ndjson"

    with pytest.raises(AppendReceiptError) as exc_info:
        _write_receipt_under_lock(_receipt(), ledger, _cache_file_for(ledger), "key-2", 300)

    assert exc_info.value.code == "lock_failed"
    assert "acquire append lock" in str(exc_info.value)
