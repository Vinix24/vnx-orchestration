"""ADR-045 K-9c round 4: the closer seals only what does not parse, and one store rule.

Dispatch-ID: dlv-d991c477ca49-r4

Rule 1 (codex on #2384): ``_close_a_torn_tail`` used to seal a COMPLETE record
that merely lacked its final newline, turning a readable ``dispatch_finished``
into debris. It now reads the tail fragment only: a JSON object stands and gets
its newline, anything else is sealed exactly as round 3 did.

Rule 2: ``runtime_coordination.db`` that is a directory is unreadable, never
"no store", at all seven call sites, through one helper
(``coordination_db.store_state``).

Every ledger is written by the real writers and every store is a real
``runtime_coordination.db`` under ``tmp_path``; only ``dispatch_start._now`` is
pinned. No closer, helper or reader is patched.
"""

from __future__ import annotations

import importlib.util as _ilu
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

import coordination_db  # noqa: E402
import dispatch_lifecycle as lifecycle  # noqa: E402
import dispatch_start  # noqa: E402
import lifecycle_count  # noqa: E402
import ndjson_io  # noqa: E402
import project_id_migration  # noqa: E402
import report_lifecycle_mode as rlm  # noqa: E402
from append_receipt_internals import payload as payload_module  # noqa: E402
from append_receipt_internals.common import AppendReceiptError  # noqa: E402
from append_receipt_internals.lifecycle import end_intent_path, load_end_intent  # noqa: E402

PROJECT = "proj-alpha"
OTHER_PROJECT = "proj-beta"
DISPATCH = "d-k9c-r4"
OTHER_DISPATCH = "d-other"
ROLE = "backend-developer"
BASE_SHA = "a" * 40
T0 = "2026-11-03T10:00:00Z"
FRAGMENT = b'{"event_type": "dispatch_finished", "dispatch_id": "d-k9c-r4", "att'

_spec_0017 = _ilu.spec_from_file_location(
    "apply_0017_k9c_r4", SCRIPT_DIR / "lib" / "migrations" / "apply_0017.py")
_mod_0017 = _ilu.module_from_spec(_spec_0017)
_spec_0017.loader.exec_module(_mod_0017)


@pytest.fixture(autouse=True)
def _hermetic_append(monkeypatch):
    """Keep the append layer real, but never fire its process-spawning hooks."""
    real = payload_module.append_receipt_payload

    def _append(receipt, **kwargs):
        kwargs.setdefault("skip_enrichment", True)
        return real(receipt, **kwargs)

    monkeypatch.setattr(payload_module, "append_receipt_payload", _append)
    # The lifecycle writers (dispatch_start / dispatch_lifecycle) resolve the
    # append through the registered facade, so the same wrapper goes there too.
    monkeypatch.setattr("governance_receipts.append_receipt_payload", _append)
    monkeypatch.setattr(payload_module.facade, "_emit_dispatch_register", lambda receipt: False)


@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    """A real coordination store at the production shape (project_id + composite UNIQUE)."""
    sd = tmp_path / "state"
    sd.mkdir()
    coordination_db.init_schema(sd)
    db_path = coordination_db.db_path_from_state_dir(sd)
    project_id_migration.run_runtime_coordination_migration(db_path, default_project_id=PROJECT)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("BEGIN")
        _mod_0017._rebuild_dispatches(conn)
        conn.execute("COMMIT")
    finally:
        conn.close()
    return sd


@pytest.fixture()
def spec_file(tmp_path: Path) -> Path:
    path = tmp_path / "SPEC.md"
    path.write_text("# staged spec\n\nbody\n", encoding="utf-8")
    return path


@pytest.fixture()
def v3(monkeypatch):
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")


@pytest.fixture()
def clock(monkeypatch):
    """Only ``_now`` is pinned; every line is still written by ``begin``."""
    monkeypatch.setattr(dispatch_start, "_now", lambda: T0)


def _ledger(state_dir: Path) -> Path:
    return state_dir / "t0_receipts.ndjson"


def _begin(state_dir: Path, spec_file: Path) -> dispatch_start.Handover:
    return dispatch_start.begin(project_id=PROJECT, dispatch_id=DISPATCH, role=ROLE,
                                base_sha=BASE_SHA, spec_file=spec_file, target_slot="T1",
                                state_dir=state_dir)


def _end_line(*, end_state: str = "completed") -> dict:
    return {
        "timestamp": "2026-11-03T10:30:00Z",
        "event_type": "dispatch_finished",
        "project_id": PROJECT,
        "dispatch_id": DISPATCH,
        "attempt": 1,
        "end_state": end_state,
        "head_sha": "c" * 40,
        "model": "deepseek-flash",
        "no_push": f"no_push:{end_state}",
    }


def _finalize(state_dir: Path) -> lifecycle.LifecycleResult:
    return lifecycle.finalize(state_dir=state_dir, project_id=PROJECT, dispatch_id=DISPATCH,
                              attempt=1, end_state="completed", end_line=_end_line())


def _next_append(state_dir: Path) -> None:
    """The real append path: the closer runs under the lock, then the record lands."""
    result = payload_module.append_receipt_payload(
        {"timestamp": T0, "event_type": "task_complete", "project_id": OTHER_PROJECT,
         "dispatch_id": OTHER_DISPATCH, "terminal": "T1", "status": "success"},
        receipts_file=str(_ledger(state_dir)))
    assert result.status == "appended", result


def _as_directory(state_dir: Path) -> Path:
    """Turn the store path into a directory — an existing path that is not a database."""
    db_path = coordination_db.db_path_from_state_dir(state_dir)
    db_path.unlink(missing_ok=True)
    db_path.mkdir()
    return db_path


def _missing(state_dir: Path) -> Path:
    db_path = coordination_db.db_path_from_state_dir(state_dir)
    db_path.unlink(missing_ok=True)
    return db_path


# ---------------------------------------------------------------------------
# rule 1: a complete record missing only its newline is not debris
# ---------------------------------------------------------------------------

def test_the_closer_keeps_a_complete_record_that_only_lacks_its_newline(state_dir, v3):
    """Codex's reproduction: the strict reader loses the record before the fix."""
    _finalize(state_dir)
    ledger = _ledger(state_dir)
    data = ledger.read_bytes()
    assert data.endswith(b"\n")
    ledger.write_bytes(data[:-1])  # the crash state: complete JSON, no final newline
    assert [json.loads(raw) for raw in ledger.read_bytes().split(b"\n")][-1]["dispatch_id"] == DISPATCH

    _next_append(state_dir)

    records = ndjson_io.read_ndjson_strict(ledger)  # never raises: a broken line would
    assert [record.get("dispatch_id") for record in records] == [DISPATCH, OTHER_DISPATCH]
    assert ndjson_io.TORN_TAIL_CLOSED not in ledger.read_bytes()
    assert ledger.read_bytes().endswith(b"\n")


def test_a_real_torn_fragment_is_still_sealed_and_skipped(state_dir):
    (state_dir / "t0_receipts.ndjson").write_bytes(FRAGMENT)

    _next_append(state_dir)

    data = _ledger(state_dir).read_bytes()
    assert data.startswith(FRAGMENT + ndjson_io.TORN_TAIL_CLOSED + b"\n")
    assert data.count(ndjson_io.TORN_TAIL_CLOSED) == 1
    records = ndjson_io.read_ndjson_strict(_ledger(state_dir))
    assert [record.get("dispatch_id") for record in records] == [OTHER_DISPATCH]


def test_a_fragment_longer_than_the_tail_window_is_sealed(state_dir):
    """Not a record this writer produced: never parsed, sealed as round 3 did."""
    long_fragment = b'{"event_type": "dispatch_finished", "pad": "' + b"x" * 70_000
    _ledger(state_dir).write_bytes(long_fragment)

    _next_append(state_dir)

    data = _ledger(state_dir).read_bytes()
    assert data.startswith(long_fragment + ndjson_io.TORN_TAIL_CLOSED + b"\n")


# ---------------------------------------------------------------------------
# rule 2: one rule for a database path, at every call site
# ---------------------------------------------------------------------------

def test_store_state_tells_absent_present_and_unreadable_apart(tmp_path):
    path = tmp_path / "runtime_coordination.db"
    assert coordination_db.store_state(path) == coordination_db.STORE_ABSENT
    path.write_bytes(b"")
    assert coordination_db.store_state(path) == coordination_db.STORE_PRESENT
    path.unlink()
    path.mkdir()
    assert coordination_db.store_state(path) == coordination_db.STORE_UNREADABLE


def test_the_finalizer_projection_fails_the_step_and_leaves_the_intent(tmp_path, v3):
    state = tmp_path / "dir-store"
    state.mkdir()
    _as_directory(state)

    with pytest.raises(AppendReceiptError) as excinfo:
        _finalize(state)

    assert excinfo.value.code == "store_unreadable"
    intent = load_end_intent(end_intent_path(state, PROJECT, DISPATCH, 1))
    assert intent is not None and not intent.get("published_at")  # recover can retry


def test_the_finalizer_projection_with_no_store_is_still_no_store(tmp_path, v3):
    state = tmp_path / "no-store"
    state.mkdir()
    _missing(state)

    result = _finalize(state)

    assert result.projection["action"] == lifecycle.PROJECTION_NO_STORE


def test_the_sweep_row_reads_a_directory_as_unreadable_and_writes_no_abandoned(
        state_dir, spec_file, v3, clock):
    _begin(state_dir, spec_file)
    _as_directory(state_dir)
    attempt = lifecycle.InFlightAttempt(PROJECT, DISPATCH, 1, {})

    with pytest.raises(AppendReceiptError) as excinfo:
        lifecycle.sweep_attempt(state_dir, attempt, datetime.now(timezone.utc))

    assert excinfo.value.code == "store_unreadable"
    result = lifecycle.sweep(state_dir=state_dir)
    assert any("not a regular file" in error for error in result.errors), result.errors
    assert result.abandoned == 0
    assert not [raw for raw in _ledger(state_dir).read_bytes().split(b"\n")
                if b"dispatch_finished" in raw]


def test_the_sweep_row_reads_a_missing_path_as_no_store(tmp_path):
    state = tmp_path / "no-store"
    state.mkdir()

    assert lifecycle._dispatch_row(state, PROJECT, DISPATCH) is None


def test_record_lifecycle_mode_takes_its_loud_failure_path_for_a_directory(tmp_path, caplog):
    state = tmp_path / "dir-store"
    state.mkdir()
    _as_directory(state)
    handover = dispatch_start.Handover(
        project_id=PROJECT, dispatch_id=DISPATCH, attempt=1,
        lifecycle_mode=dispatch_start.MODE_V3, door_pid=1,
        door_started_at=T0, started_at=T0)

    mode = dispatch_start._record_lifecycle_mode(handover, state_dir=state, write_attempt=True)

    assert mode == dispatch_start.MODE_WRITE_FAILED
    assert "lifecycle_mode write failed" in caplog.text


def test_record_lifecycle_mode_with_no_store_is_still_write_ok(tmp_path):
    state = tmp_path / "no-store"
    state.mkdir()
    _missing(state)
    handover = dispatch_start.Handover(
        project_id=PROJECT, dispatch_id=DISPATCH, attempt=1,
        lifecycle_mode=dispatch_start.MODE_V3, door_pid=1,
        door_started_at=T0, started_at=T0)

    mode = dispatch_start._record_lifecycle_mode(handover, state_dir=state, write_attempt=True)

    assert mode == dispatch_start.MODE_WRITE_OK


def test_other_tenant_refuses_lifecycle_mode_undecidable_for_a_directory(tmp_path):
    state = tmp_path / "dir-store"
    state.mkdir()
    _as_directory(state)

    with pytest.raises(rlm.ReportPathRefused) as excinfo:
        rlm._other_tenant_v3(state, PROJECT, DISPATCH, [])

    assert excinfo.value.code == "lifecycle_mode_undecidable"


def test_other_tenant_with_no_store_finds_no_other_tenant(tmp_path):
    state = tmp_path / "no-store"
    state.mkdir()
    _missing(state)

    assert rlm._other_tenant_v3(state, PROJECT, DISPATCH, []) == []


def test_read_attempt_state_reads_a_directory_as_unreadable(tmp_path):
    state = tmp_path / "dir-store"
    state.mkdir()
    _as_directory(state)

    attempt = dispatch_start.read_attempt_state(state, PROJECT, DISPATCH)

    assert attempt.status == dispatch_start.ATTEMPT_UNREADABLE


def test_read_attempt_state_reads_a_missing_path_as_no_row(tmp_path):
    state = tmp_path / "no-store"
    state.mkdir()

    assert dispatch_start.read_attempt_state(state, PROJECT, DISPATCH).status == \
        dispatch_start.ATTEMPT_NO_ROW


def test_read_row_reads_a_directory_as_unreadable(tmp_path):
    state = tmp_path / "dir-store"
    state.mkdir()
    _as_directory(state)

    row = rlm._read_row(state, PROJECT, DISPATCH)

    assert row.status == rlm.ROW_UNREADABLE


def test_read_row_reads_a_missing_path_as_absent(tmp_path):
    state = tmp_path / "no-store"
    state.mkdir()

    assert rlm._read_row(state, PROJECT, DISPATCH).status == rlm.ROW_ABSENT


def test_the_count_is_unmeasured_for_a_directory(tmp_path):
    state = tmp_path / "dir-store"
    state.mkdir()
    _as_directory(state)
    key = (PROJECT, DISPATCH, 1)

    with pytest.raises(lifecycle_count.Unmeasured) as excinfo:
        lifecycle_count._defects(state, {key: {}}, {}, {}, datetime.now(timezone.utc), [])

    assert "exists but is not a regular file" in str(excinfo.value)


def test_the_count_with_no_store_has_no_rows_to_read(tmp_path):
    state = tmp_path / "no-store"
    state.mkdir()
    key = (PROJECT, DISPATCH, 1)

    found = lifecycle_count._defects(state, {key: {}}, {}, {}, datetime.now(timezone.utc), [])

    assert isinstance(found, dict) and set(found) == set(lifecycle_count.DEFECTS)
