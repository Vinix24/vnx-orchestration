"""ADR-045 (audit-spoor-kern K-9c): a broken ledger line is a named refusal on the
hot paths when it carries this dispatch's id; the torn tail is no record.

Dispatch-ID: dlv-d991c477ca49

One block per acceptance rule of the dispatch:

2. the door (``dispatch_start._lifecycle_lines``),
3. the finalizer (``dispatch_lifecycle._end_lines_from_offset``),
4. the append layer (``find_prior_end_line``'s tail window),
5. the sweep scan (``v3_in_flight``),
6. the report decision (``report_lifecycle_mode._read_ledger``),
7. a database path that is not a file (OI-2529).

Every ledger line is written by the real writers — ``dispatch_start.begin``, the
real finalizer and the append layer's intent writer; only ``dispatch_start._now``
is pinned. The damage is then done to the file BYTES: a whole line made
unparseable, or a torn fragment glued in front of the next record by removing
its separating newline. No reader is patched.
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
import project_id_migration  # noqa: E402
import report_lifecycle_mode as rlm  # noqa: E402
from append_receipt_internals import payload as payload_module  # noqa: E402
from append_receipt_internals.common import AppendReceiptError  # noqa: E402
from append_receipt_internals.lifecycle import (  # noqa: E402
    LIFECYCLE_TAIL_BYTES,
    build_end_intent,
    end_intent_path,
    find_prior_end_line,
    load_end_intent,
    prepare_end_line,
    write_end_intent,
)

PROJECT = "proj-alpha"
OTHER_PROJECT = "proj-beta"
DISPATCH = "d-k9c"
OTHER_DISPATCH = "d-other"
ROLE = "backend-developer"
BASE_SHA = "a" * 40
T0 = "2026-11-03T10:00:00Z"
START_DT = datetime(2026, 11, 3, 10, 0, 0, tzinfo=timezone.utc)

_RUNNER_PATH_0017 = SCRIPT_DIR / "lib" / "migrations" / "apply_0017.py"
_spec_0017 = _ilu.spec_from_file_location("apply_0017_k9c", _RUNNER_PATH_0017)
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
    state = {"now": T0}
    monkeypatch.setattr(dispatch_start, "_now", lambda: state["now"])
    return state


# ---------------------------------------------------------------------------
# real-writer helpers
# ---------------------------------------------------------------------------

def _ledger(state_dir: Path) -> Path:
    return state_dir / "t0_receipts.ndjson"


def _begin(state_dir: Path, spec_file: Path, *, dispatch_id: str = DISPATCH,
           project_id: str = PROJECT) -> dispatch_start.Handover:
    return dispatch_start.begin(project_id=project_id, dispatch_id=dispatch_id, role=ROLE,
                                base_sha=BASE_SHA, spec_file=spec_file, target_slot="T1",
                                state_dir=state_dir)


def _end_line(*, dispatch_id: str = DISPATCH, project_id: str = PROJECT,
              attempt: int = 1, end_state: str = "completed") -> dict:
    return {
        "timestamp": "2026-11-03T10:30:00Z",
        "event_type": "dispatch_finished",
        "project_id": project_id,
        "dispatch_id": dispatch_id,
        "attempt": attempt,
        "end_state": end_state,
        "head_sha": "c" * 40,
        "model": "deepseek-flash",
        "no_push": f"no_push:{end_state}",
    }


def _finalize(state_dir: Path, *, dispatch_id: str = DISPATCH, project_id: str = PROJECT,
              attempt: int = 1, end_line: dict | None = None) -> lifecycle.LifecycleResult:
    return lifecycle.finalize(
        state_dir=state_dir, project_id=project_id, dispatch_id=dispatch_id, attempt=attempt,
        end_state="completed", end_line=end_line or _end_line())


def _freeze_intent(state_dir: Path, *, dispatch_id: str = DISPATCH, project_id: str = PROJECT,
                   attempt: int = 1, end_line: dict | None = None):
    """The intent a real finalizer freezes, written by the append layer's own writer."""
    ledger = _ledger(state_dir)
    prepared = prepare_end_line(end_line or _end_line(), ledger_path=ledger)
    intent_path = end_intent_path(state_dir, project_id, dispatch_id, attempt)
    document = build_end_intent(
        project_id=prepared["project_id"], dispatch_id=prepared["dispatch_id"],
        attempt=prepared["attempt"], end_state=str(prepared["end_state"]),
        end_line=prepared, ledger_path=ledger,
        ledger_offset=ledger.stat().st_size if ledger.exists() else 0)
    assert write_end_intent(intent_path, document) is True
    return prepared, intent_path


# ---------------------------------------------------------------------------
# byte-level damage helpers (the readers are never patched)
# ---------------------------------------------------------------------------

def _rewrite(state_dir: Path, parts: list[bytes]) -> None:
    _ledger(state_dir).write_bytes(b"\n".join(parts))


def _replace_matching(state_dir: Path, *, event_type: str, replacement: bytes,
                      dispatch_id: str = DISPATCH) -> int:
    """Replace the whole line carrying *event_type*/*dispatch_id*; return its 1-based number.

    The replacement is terminated by the same newline the original had (the line
    layout is preserved), so an interior broken line stays interior.
    """
    parts = _ledger(state_dir).read_bytes().split(b"\n")
    for index, raw in enumerate(parts):
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if (isinstance(record, dict) and record.get("event_type") == event_type
                and record.get("dispatch_id") == dispatch_id):
            parts[index] = replacement
            _rewrite(state_dir, parts)
            return index + 1
    raise AssertionError(f"no {event_type} line for {dispatch_id!r} in the ledger")


def _glue_before(state_dir: Path, *, event_type: str, fragment: bytes,
                 dispatch_id: str = DISPATCH) -> int:
    """Glue a torn fragment in front of the matching line, as a crash then an append leaves it."""
    parts = _ledger(state_dir).read_bytes().split(b"\n")
    for index, raw in enumerate(parts):
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if (isinstance(record, dict) and record.get("event_type") == event_type
                and record.get("dispatch_id") == dispatch_id):
            parts[index] = fragment + parts[index]
            _rewrite(state_dir, parts)
            return index + 1
    raise AssertionError(f"no {event_type} line for {dispatch_id!r} in the ledger")


def _append_raw(state_dir: Path, text: bytes) -> None:
    with _ledger(state_dir).open("ab") as handle:
        handle.write(text)


def _prepend_raw(state_dir: Path, text: bytes) -> None:
    _ledger(state_dir).write_bytes(text + _ledger(state_dir).read_bytes())


def _parsed_end_lines(state_dir: Path) -> list:
    """The parseable ``dispatch_finished`` records the ledger carries."""
    found = []
    for raw in _ledger(state_dir).read_bytes().split(b"\n"):
        try:
            record = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(record, dict) and record.get("event_type") == "dispatch_finished":
            found.append(record)
    return found


# ---------------------------------------------------------------------------
# 2. the door
# ---------------------------------------------------------------------------

def test_the_door_refuses_an_interior_broken_line_of_this_dispatch(
        state_dir, spec_file, v3, clock):
    _begin(state_dir, spec_file)
    line_number = _replace_matching(
        state_dir, event_type="dispatch_started",
        replacement=b'{"event_type": "dispatch_started", "dispatch_id": "d-k9c", "att')

    with pytest.raises(dispatch_start.StartRefused) as exc:
        _begin(state_dir, spec_file)

    assert exc.value.code == "ledger_corrupt"
    assert f"line {line_number}" in exc.value.detail


def test_the_door_skips_an_interior_broken_line_of_another_dispatch(
        state_dir, spec_file, v3, clock):
    _append_raw(state_dir, b'{"event_type": "dispatch_started", "dispatch_id": "d-other", "att\n')

    handover = _begin(state_dir, spec_file)

    assert handover.attempt == 1
    assert handover.lifecycle_mode == "v3"


def test_the_door_tolerates_the_same_broken_line_as_an_unterminated_final_fragment(
        state_dir, spec_file, v3, clock):
    _append_raw(state_dir, b'{"event_type": "dispatch_started", "dispatch_id": "d-k9c", "att')

    handover = _begin(state_dir, spec_file)

    assert handover.attempt == 1
    assert handover.lifecycle_mode == "v3"


def test_the_door_refuses_a_torn_fragment_glued_before_this_dispatches_end_line(
        state_dir, spec_file, v3, clock):
    _begin(state_dir, spec_file)
    _finalize(state_dir)
    _glue_before(state_dir, event_type="dispatch_finished",
                 fragment=b'{"event_type": "dispatch_finished", "att')

    with pytest.raises(dispatch_start.StartRefused) as exc:
        _begin(state_dir, spec_file)

    assert exc.value.code == "ledger_corrupt"


# ---------------------------------------------------------------------------
# 3. the finalizer
# ---------------------------------------------------------------------------

def _freeze_and_append_end(state_dir: Path):
    """A frozen intent whose end line the append layer has already written."""
    prepared, intent_path = _freeze_intent(state_dir)
    result = payload_module.append_receipt_payload(
        json.loads(json.dumps(prepared)), receipts_file=str(_ledger(state_dir)))
    assert result.status == "appended", result
    return prepared, intent_path


def test_the_finalizer_refuses_an_interior_broken_line_of_this_dispatch(
        state_dir, spec_file, v3, clock):
    _begin(state_dir, spec_file)
    _prepared, intent_path = _freeze_and_append_end(state_dir)
    _replace_matching(
        state_dir, event_type="dispatch_finished",
        replacement=b'{"event_type": "dispatch_finished", "dispatch_id": "d-k9c", "att')
    before = _ledger(state_dir).read_bytes()

    with pytest.raises(AppendReceiptError) as exc:
        lifecycle.recover(state_dir=state_dir, project_id=PROJECT, dispatch_id=DISPATCH, attempt=1)

    assert exc.value.code == "ledger_corrupt"
    assert _ledger(state_dir).read_bytes() == before  # nothing appended
    assert _parsed_end_lines(state_dir) == []  # no second dispatch_finished
    document = load_end_intent(intent_path)
    assert document is not None and not document.get("published_at")  # the intent stays


def test_the_finalizer_skips_an_interior_broken_line_of_another_dispatch(
        state_dir, spec_file, v3, clock):
    _begin(state_dir, spec_file)
    _freeze_intent(state_dir)
    _append_raw(state_dir, b'{"event_type": "dispatch_finished", "dispatch_id": "d-other", "att\n'
                            b'{"event_type": "task_complete", "dispatch_id": "d-other"}\n')

    result = lifecycle.recover(state_dir=state_dir, project_id=PROJECT, dispatch_id=DISPATCH,
                               attempt=1)

    assert result.status == lifecycle.STATUS_RECOVERED
    assert [(line["project_id"], line["dispatch_id"]) for line in _parsed_end_lines(state_dir)] == [
        (PROJECT, DISPATCH)]  # exactly ours, once


def test_the_finalizer_tolerates_the_same_broken_line_as_an_unterminated_final_fragment(
        state_dir, spec_file, v3, clock):
    _begin(state_dir, spec_file)
    _freeze_intent(state_dir)
    # Another dispatch's fragment: the append layer closes the torn tail (one
    # newline, append-only), so this key is untouched and the append proceeds.
    _append_raw(state_dir, b'{"event_type": "dispatch_finished", "dispatch_id": "d-other", "att')

    result = lifecycle.recover(state_dir=state_dir, project_id=PROJECT, dispatch_id=DISPATCH,
                               attempt=1)

    assert result.status == lifecycle.STATUS_RECOVERED


def test_the_finalizer_refuses_a_torn_fragment_glued_before_this_dispatches_end_line(
        state_dir, spec_file, v3, clock):
    _begin(state_dir, spec_file)
    _prepared, intent_path = _freeze_and_append_end(state_dir)
    _glue_before(state_dir, event_type="dispatch_finished",
                 fragment=b'{"event_type": "dispatch_finished", "att')
    before = _ledger(state_dir).read_bytes()

    with pytest.raises(AppendReceiptError) as exc:
        lifecycle.recover(state_dir=state_dir, project_id=PROJECT, dispatch_id=DISPATCH, attempt=1)

    assert exc.value.code == "ledger_corrupt"
    assert _ledger(state_dir).read_bytes() == before
    assert _parsed_end_lines(state_dir) == []
    document = load_end_intent(intent_path)
    assert document is not None and not document.get("published_at")


# ---------------------------------------------------------------------------
# 4. the append layer's tail scan
# ---------------------------------------------------------------------------

def _receipt() -> dict:
    return {"project_id": PROJECT, "dispatch_id": DISPATCH, "attempt": 1,
            "event_type": "dispatch_finished"}


def test_the_append_layer_does_not_mistake_a_window_cut_for_corruption(state_dir):
    pad = b'{"pad": "' + b"x" * (LIFECYCLE_TAIL_BYTES + 4096) + b'", "dispatch_id": "d-k9c"}'
    end = json.dumps(_end_line(), separators=(",", ":")).encode("utf-8")
    _ledger(state_dir).write_bytes(pad + b"\n" + end + b"\n")

    assert find_prior_end_line(_receipt(), index_data=None,
                               ledger_path=_ledger(state_dir)) == "ledger_tail"


def test_the_append_layer_refuses_a_broken_end_line_of_this_key_inside_the_window(state_dir):
    end = json.dumps(_end_line(), separators=(",", ":")).encode("utf-8")
    _ledger(state_dir).write_bytes(
        end + b"\n"
        + b'{"event_type": "dispatch_finished", "dispatch_id": "d-k9c", "att\n'
        + b'{"event_type": "task_complete", "dispatch_id": "d-other"}\n')

    with pytest.raises(AppendReceiptError) as exc:
        find_prior_end_line(_receipt(), index_data=None, ledger_path=_ledger(state_dir))

    assert exc.value.code == "lifecycle_end_lookup_failed"
    assert exc.value.message.startswith("ledger_corrupt")


def test_the_append_layer_tolerates_a_torn_final_fragment_that_names_this_key(state_dir):
    end = json.dumps(_end_line(), separators=(",", ":")).encode("utf-8")
    _ledger(state_dir).write_bytes(end + b"\n" + b'{"event_type": "dispatch_finished", "dispatch_id": "d-k9c", "att')

    assert find_prior_end_line(_receipt(), index_data=None,
                               ledger_path=_ledger(state_dir)) == "ledger_tail"


def test_an_index_hit_answers_before_a_corrupt_window_is_scanned(state_dir):
    from receipt_schema import lifecycle_end_key

    key = lifecycle_end_key(_receipt())
    _ledger(state_dir).write_bytes(b'{"event_type": "dispatch_finished", "dispatch_id": "d-k9c", "att\n')

    assert find_prior_end_line(_receipt(), index_data={"lifecycle_index": {key: "out-1"}},
                               ledger_path=_ledger(state_dir)) == "out-1"


# ---------------------------------------------------------------------------
# 5. the sweep scan and its refusal to abandon
# ---------------------------------------------------------------------------

def test_the_sweep_scan_reports_a_broken_end_line_and_the_run_writes_no_abandoned(
        state_dir, spec_file, v3, clock):
    _begin(state_dir, spec_file)
    _finalize(state_dir)
    line_number = _replace_matching(
        state_dir, event_type="dispatch_finished",
        replacement=b'{"event_type": "dispatch_finished", "dispatch_id": "d-k9c", "att')
    before = _ledger(state_dir).read_bytes()

    flights, errors = lifecycle.v3_in_flight(state_dir)

    assert [(attempt.dispatch_id, attempt.attempt) for attempt in flights] == [(DISPATCH, 1)]
    assert f"ledger_corrupt line {line_number}" in errors
    assert f"ledger_corrupt line {line_number}" in lifecycle.in_flight_counts(state_dir)["unreadable"]

    with pytest.raises(AppendReceiptError) as exc:
        lifecycle.sweep(state_dir=state_dir,
                        now=datetime(2026, 11, 3, 12, 0, 0, tzinfo=timezone.utc))

    assert exc.value.code == "ledger_corrupt"
    assert _ledger(state_dir).read_bytes() == before  # no abandoned end, byte-identical
    assert b'"end_state":"abandoned"' not in before
    heartbeat = json.loads(
        (state_dir / "dispatch_end" / "sweep_heartbeat.json").read_text(encoding="utf-8"))
    assert "ledger_corrupt" in str(heartbeat.get("error"))


def test_the_sweep_scan_tolerates_an_unterminated_final_fragment(state_dir, spec_file, v3, clock):
    _begin(state_dir, spec_file)
    _append_raw(state_dir, b'{"event_type": "dispatch_finished", "dispatch_id": "d-k9c", "att')

    flights, errors = lifecycle.v3_in_flight(state_dir)

    assert [(attempt.dispatch_id, attempt.attempt) for attempt in flights] == [(DISPATCH, 1)]
    assert errors == ()


# ---------------------------------------------------------------------------
# 6. the report decision
# ---------------------------------------------------------------------------

def test_the_report_decision_refuses_an_interior_broken_line_of_this_dispatch(
        state_dir, spec_file, v3, clock):
    _begin(state_dir, spec_file)
    _finalize(state_dir)
    line_number = _replace_matching(
        state_dir, event_type="dispatch_started",
        replacement=b'{"event_type": "dispatch_started", "dispatch_id": "d-k9c", "att')

    with pytest.raises(rlm.ReportPathRefused) as exc:
        rlm.decide(project_id=PROJECT, dispatch_id=DISPATCH, state_dir=state_dir, attempt=1)

    # K-8a round 4's wording (the form test_restore_stranded_reports reads out of
    # ``detail``) carries the ledger's own reason in full on its side.
    assert exc.value.detail.startswith("one lifecycle source is unreadable (ledger: ledger_corrupt:")
    assert f"line {line_number}" in exc.value.detail


def test_the_report_decision_skips_an_interior_broken_line_of_another_dispatch(
        state_dir, spec_file, v3, clock):
    _begin(state_dir, spec_file)
    _finalize(state_dir)
    ended = [json.loads(raw) for raw in _ledger(state_dir).read_bytes().split(b"\n")
             if b"dispatch_finished" in raw][0]
    _prepend_raw(state_dir, b'{"event_type": "dispatch_started", "dispatch_id": "d-other", "att\n')

    path = rlm.decide(project_id=PROJECT, dispatch_id=DISPATCH, state_dir=state_dir, attempt=1)

    assert path.mode == rlm.MODE_V3
    assert path.end_outcome_id == ended["outcome_id"]


def test_the_report_decision_tolerates_the_same_broken_line_as_a_torn_final_fragment(
        state_dir, spec_file, v3, clock):
    _begin(state_dir, spec_file)
    _finalize(state_dir)
    ended = [json.loads(raw) for raw in _ledger(state_dir).read_bytes().split(b"\n")
             if b"dispatch_finished" in raw][0]
    _append_raw(state_dir, b'{"event_type": "dispatch_started", "dispatch_id": "d-k9c", "att')

    path = rlm.decide(project_id=PROJECT, dispatch_id=DISPATCH, state_dir=state_dir, attempt=1)

    assert path.mode == rlm.MODE_V3
    assert path.end_outcome_id == ended["outcome_id"]


# ---------------------------------------------------------------------------
# 7. a database path that exists but is not a database file (OI-2529)
# ---------------------------------------------------------------------------

def test_a_database_that_is_a_directory_refuses_never_legacy(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    coordination_db.db_path_from_state_dir(state).mkdir()

    with pytest.raises(rlm.ReportPathRefused) as exc:
        rlm.decide(project_id=PROJECT, dispatch_id=DISPATCH, state_dir=state)

    assert exc.value.code == "lifecycle_mode_undecidable"
    assert "not a regular file" in exc.value.detail


def test_a_missing_database_path_still_answers_legacy(tmp_path):
    state = tmp_path / "state"
    state.mkdir()

    path = rlm.decide(project_id=PROJECT, dispatch_id=DISPATCH, state_dir=state)

    assert path.mode == rlm.MODE_LEGACY
