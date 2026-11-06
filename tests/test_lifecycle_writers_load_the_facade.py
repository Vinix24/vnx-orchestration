"""K-act regression: the lifecycle start and end line load the ``append_receipt`` facade.

Dispatch-ID: dlv-701da8f221b3

``append_receipt_payload`` reads the ``common.facade`` proxy, which is only
populated once ``scripts/append_receipt.py`` has been imported. The door runs
``python3 scripts/lib/dispatch_cli.py`` — ``scripts/lib`` on ``sys.path``, and
nothing on the start path imported the facade — so the start line raised
``AttributeError: facade not registered`` and every real dispatch was refused
before the spawn. In-process pytest never saw it: something in the suite
imports ``append_receipt`` first, which registers the facade for the whole
process. So each test here starts a FRESH interpreter with exactly what the
door has (``scripts/lib`` only, no facade imported) and proves the line lands.

Hermetic: the child gets its own ``HOME`` and ``VNX_*`` data/state dirs under
``tmp_path``; the append layer's central mirror therefore stays in the tmp
tree, and every test asserts the real ``~/.vnx-data/kactfacade`` was untouched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = VNX_ROOT / "scripts"
LIB_DIR = SCRIPT_DIR / "lib"

PROJECT = "kactfacade"
DISPATCH = "d-kactfacade"
ATTEMPT = 1
ROLE = "backend-developer"
BASE_SHA = "a" * 40
DOOR_STARTED_AT = "Mon Nov  3 10:00:00 2026"
STARTED_AT = "2026-11-03T10:00:00Z"

#: Runs in the child. argv: lib dir, mode, project, dispatch id, state dir, spec file.
CHILD_SOURCE = '''\
import sys
from pathlib import Path

lib_dir = Path(sys.argv[1]).resolve()
mode, project, dispatch_id = sys.argv[2], sys.argv[3], sys.argv[4]
state_dir, spec_file = Path(sys.argv[5]), Path(sys.argv[6])

# Exactly the door's import surface: scripts/lib, and neither scripts/ nor tests/.
_keep = [p for p in sys.path if p and Path(p).resolve() not in {
    lib_dir, lib_dir.parent, lib_dir.parent.parent} and Path(p).resolve() != Path.cwd()]
sys.path[:] = [str(lib_dir)] + _keep
assert sys.path[0] == str(lib_dir), sys.path
assert "append_receipt" not in sys.modules, "the facade was imported before the writer ran"

if mode == "start":
    import dispatch_start

    handover = dispatch_start.Handover(
        project_id=project, dispatch_id=dispatch_id, attempt=1,
        lifecycle_mode=dispatch_start.MODE_V3, door_pid=123456,
        door_started_at="Mon Nov  3 10:00:00 2026", started_at="2026-11-03T10:00:00Z")
    dispatch_start._append_start_line(
        handover, role="backend-developer", base_sha="a" * 40, spec_file=spec_file,
        target_slot="T1", state_dir=state_dir)
else:
    import dispatch_lifecycle
    from append_receipt_internals.lifecycle import (
        build_end_intent, end_intent_path, prepare_end_line, write_end_intent)

    ledger = state_dir / "t0_receipts.ndjson"
    line = {"timestamp": "2026-11-03T10:05:00Z", "event_type": "dispatch_finished",
            "project_id": project, "dispatch_id": dispatch_id, "attempt": 1,
            "end_state": "completed", "head_sha": "c" * 40, "model": "deepseek-flash"}
    prepared = prepare_end_line(line, ledger_path=ledger)
    document = build_end_intent(
        project_id=project, dispatch_id=dispatch_id, attempt=1, end_state="completed",
        end_line=prepared, ledger_path=ledger,
        ledger_offset=ledger.stat().st_size if ledger.exists() else 0)
    assert write_end_intent(end_intent_path(state_dir, project, dispatch_id, 1), document) is True
    dispatch_lifecycle._append_end_line(document)
'''


@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    sd = tmp_path / "state"
    sd.mkdir()
    return sd


@pytest.fixture()
def spec_file(tmp_path: Path) -> Path:
    path = tmp_path / "SPEC.md"
    path.write_text("# staged spec\n\nbody\n", encoding="utf-8")
    return path


def _child_env(tmp_path: Path, state_dir: Path) -> dict:
    """Only the door's env: no inherited VNX_PROJECT_ID (OI-2584), everything in tmp_path."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "VNX_DATA_DIR": str(tmp_path / "vnx-data"),
        "VNX_DATA_DIR_EXPLICIT": "1",
        "VNX_DATA_HOME": str(tmp_path / "account-home"),
        "VNX_STATE_DIR": str(state_dir),
        "VNX_LEDGER_LIFECYCLE": "1",
        "VNX_PROJECT_ID": PROJECT,
        "LC_ALL": "C",
    }


def _run_child(tmp_path: Path, state_dir: Path, spec_file: Path, mode: str) -> subprocess.CompletedProcess:
    script = tmp_path / "child.py"
    script.write_text(CHILD_SOURCE, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(script), str(LIB_DIR), mode, PROJECT, DISPATCH,
         str(state_dir), str(spec_file)],
        capture_output=True, text=True, env=_child_env(tmp_path, state_dir), cwd=str(tmp_path),
        timeout=180,
    )


def _lines(state_dir: Path) -> list:
    ledger = state_dir / "t0_receipts.ndjson"
    if not ledger.exists():
        return []
    return [json.loads(raw) for raw in ledger.read_text(encoding="utf-8").splitlines() if raw.strip()]


def _of_type(state_dir: Path, event_type: str) -> list:
    return [line for line in _lines(state_dir)
            if line.get("event_type") == event_type and line.get("dispatch_id") == DISPATCH]


def _assert_real_store_untouched() -> None:
    assert not (Path.home() / ".vnx-data" / PROJECT).exists()


def test_the_facade_raises_the_error_class_the_writers_catch():
    """Same object, not a copy: the facade re-exports the internals' exception."""
    sys.path.insert(0, str(LIB_DIR))
    import dispatch_lifecycle
    import dispatch_start
    import governance_receipts
    from append_receipt_internals.common import AppendReceiptError

    assert governance_receipts.AppendReceiptError is AppendReceiptError
    assert dispatch_start.AppendReceiptError is AppendReceiptError
    assert dispatch_lifecycle.AppendReceiptError is AppendReceiptError


def test_the_start_line_loads_the_facade_in_a_door_like_process(tmp_path, state_dir, spec_file):
    result = _run_child(tmp_path, state_dir, spec_file, "start")

    assert result.returncode == 0, result.stderr
    assert "facade not registered" not in result.stderr
    starts = _of_type(state_dir, "dispatch_started")
    assert len(starts) == 1, _lines(state_dir)
    assert starts[0]["project_id"] == PROJECT and starts[0]["attempt"] == ATTEMPT
    assert starts[0]["role"] == ROLE and starts[0]["base_sha"] == BASE_SHA
    _assert_real_store_untouched()


def test_the_end_line_loads_the_facade_in_a_door_like_process(tmp_path, state_dir, spec_file):
    result = _run_child(tmp_path, state_dir, spec_file, "end")

    assert result.returncode == 0, result.stderr
    assert "facade not registered" not in result.stderr
    ends = _of_type(state_dir, "dispatch_finished")
    assert len(ends) == 1, _lines(state_dir)
    assert ends[0]["project_id"] == PROJECT and ends[0]["end_state"] == "completed"
    _assert_real_store_untouched()
