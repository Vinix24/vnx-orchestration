"""ADR-045 (audit-spoor-kern K-8a): the report-path decision, and its refusals.

Dispatch-ID: dlv-55bcc59a796e

``report_lifecycle_mode`` decides whether a worker report is booked as the
legacy task-shaped receipt or as one ``report_ingested``. Nothing calls it yet
(K-8b wires the writers), so this file drives it directly against real stores:
the lifecycle lines are written by the real ``dispatch_start.begin`` and
``dispatch_lifecycle.finalize`` — no hand-written lifecycle line — with only
``dispatch_start._now`` pinned for distinct start times. One block per
acceptance rule of the dispatch:

1. the store is explicit (``None`` is a TypeError, never legacy) and the
   destination helper is the append layer's own resolver;
2. the mode: v3 from either readable source, undecidable when either is
   unreadable, legacy only when both are readable and neither says v3;
3. one tenant, or ``project_ambiguous``;
4. the attempt: the receipt's own, the only started one, or the latest start at
   or before the caller's write time;
5. the end link: only an exact-key ``dispatch_finished``; a legacy completion
   never links;
6. the v3 line is a copy with exactly one end link and every payload field;
7. no writer changes — this file imports no writer;
8. round 3 (OI-2528): the ledger is read strictly — only the torn tail may be
   unparseable, an interior break refuses ``lifecycle_mode_undecidable``;
9. round 4: that refusal keeps the ``ledger: …; row: …`` wording the restore scan
   (K-8c) reads out of ``detail``, with the ledger's reason in full on its side;
10. round 5 (OI-2528/OI-2529): the refusal is scoped to this dispatch's own broken
   lines (another dispatch's is skipped), a newline-terminated final break refuses,
   and a database path that is not a regular file is unreadable, never legacy.
"""

from __future__ import annotations

import importlib.util as _ilu
import json
import os
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
import receipt_schema  # noqa: E402
import report_lifecycle_mode as rlm  # noqa: E402
from append_receipt_internals import payload as payload_module  # noqa: E402
from coordination_db import get_connection  # noqa: E402

PROJECT = "proj-alpha"
OTHER_PROJECT = "proj-beta"
DISPATCH = "d-k8a"
BASE_SHA = "a" * 40
ROLE = "backend-developer"
T0 = "2026-11-03T10:00:00Z"
T1 = "2026-11-03T11:00:00Z"
T2 = "2026-11-03T12:00:00Z"

_RUNNER_PATH_0017 = SCRIPT_DIR / "lib" / "migrations" / "apply_0017.py"
_spec_0017 = _ilu.spec_from_file_location("apply_0017_k8a", _RUNNER_PATH_0017)
_mod_0017 = _ilu.module_from_spec(_spec_0017)
_spec_0017.loader.exec_module(_mod_0017)


def _make_state_dir(root: Path) -> Path:
    """A real coordination store at the production shape (project_id + composite UNIQUE)."""
    sd = root / "state"
    sd.mkdir(parents=True)

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
def state_dir(tmp_path: Path) -> Path:
    return _make_state_dir(tmp_path)


@pytest.fixture()
def spec_file(tmp_path: Path) -> Path:
    path = tmp_path / "SPEC.md"
    path.write_text("# staged spec\n\nbody\n", encoding="utf-8")
    return path


@pytest.fixture()
def v3(monkeypatch):
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")


@pytest.fixture()
def legacy(monkeypatch):
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "0")


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
def clock(monkeypatch):
    """Only ``_now`` is pinned; every line is still written by ``begin``."""
    state = {"now": T0}
    monkeypatch.setattr(dispatch_start, "_now", lambda: state["now"])
    return state


def _begin(state_dir: Path, spec_file: Path, **overrides) -> dispatch_start.Handover:
    kwargs = dict(project_id=PROJECT, dispatch_id=DISPATCH, role=ROLE, base_sha=BASE_SHA,
                  spec_file=spec_file, target_slot="T1", state_dir=state_dir)
    kwargs.update(overrides)
    return dispatch_start.begin(**kwargs)


def _end_line(*, project_id: str = PROJECT, attempt: int = 1,
              end_state: str = "completed", timestamp: str = "2026-11-03T10:30:00Z") -> dict:
    return {
        "timestamp": timestamp,
        "event_type": "dispatch_finished",
        "project_id": project_id,
        "dispatch_id": DISPATCH,
        "attempt": attempt,
        "end_state": end_state,
        "head_sha": "c" * 40,
        "model": "deepseek-flash",
    }


def _finalize(state_dir: Path, **overrides) -> lifecycle.LifecycleResult:
    kwargs = dict(state_dir=state_dir, project_id=PROJECT, dispatch_id=DISPATCH,
                  attempt=1, end_state="completed", end_line=_end_line())
    kwargs.update(overrides)
    return lifecycle.finalize(**kwargs)


def _ledger(state_dir: Path) -> Path:
    return state_dir / "t0_receipts.ndjson"


def _lines(state_dir: Path) -> list:
    ledger = _ledger(state_dir)
    if not ledger.exists():
        return []
    return [json.loads(raw) for raw in ledger.read_text(encoding="utf-8").splitlines() if raw.strip()]


def _completed_line(state_dir: Path, *, project_id: str = PROJECT, attempt: int = 1) -> dict:
    finished = [line for line in _lines(state_dir)
                if line.get("event_type") == "dispatch_finished"
                and line.get("project_id") == project_id
                and line.get("attempt") == attempt]
    assert finished, f"no dispatch_finished for {project_id!r} attempt {attempt}"
    return finished[-1]


def _insert_row(state_dir: Path, *, project_id: str = PROJECT, attempt_count: int = 1,
                metadata: str = "{}", state: str = "running") -> None:
    with get_connection(state_dir) as conn:
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state, attempt_count, metadata_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (DISPATCH, project_id, state, attempt_count, metadata),
        )
        conn.commit()


def _set_row_metadata(state_dir: Path, metadata: dict, *, project_id: str = PROJECT) -> None:
    with get_connection(state_dir) as conn:
        conn.execute(
            "UPDATE dispatches SET metadata_json = ? WHERE dispatch_id = ? AND project_id = ?",
            (json.dumps(metadata, sort_keys=True), DISPATCH, project_id),
        )
        conn.commit()


def _make_ledger_unreadable(state_dir: Path) -> None:
    _ledger(state_dir).mkdir(parents=True, exist_ok=True)


def _corrupt_ledger_line(state_dir: Path, event_type: str) -> int:
    """Rewrite the first *event_type* line as an unparseable fragment; return its 1-based line.

    Byte-level damage on a whole line, the shape a bad write leaves in the interior of
    the file: the line count is unchanged, so the reading that follows is well defined.
    The fragment keeps this dispatch's id — an interior break only refuses when its raw
    text carries the id (OI-2528), so a fragment without it is another dispatch's damage.
    """
    ledger = _ledger(state_dir)
    lines = ledger.read_text(encoding="utf-8").splitlines()
    index = next(i for i, raw in enumerate(lines)
                 if raw.strip() and json.loads(raw).get("event_type") == event_type)
    lines[index] = f'{{"event_type": "{event_type}", "dispatch_id": "{DISPATCH}", "att'
    ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return index + 1


def _insert_broken_line(state_dir: Path, fragment: str, index: int) -> None:
    """Insert an unparseable line at *index*, leaving the file newline-terminated."""
    ledger = _ledger(state_dir)
    lines = ledger.read_text(encoding="utf-8").splitlines()
    lines.insert(index, fragment)
    ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _append_broken_line(state_dir: Path, fragment: str) -> None:
    """Append an unparseable line WITH its newline: a committed broken record."""
    with _ledger(state_dir).open("a", encoding="utf-8") as fh:
        fh.write(fragment + "\n")


def _append_torn_tail(state_dir: Path, fragment: str) -> None:
    """Append an unterminated partial record: exactly what a crash mid-append leaves."""
    with _ledger(state_dir).open("a", encoding="utf-8") as fh:
        fh.write(fragment)


def _receipt(**overrides) -> dict:
    receipt = {
        "timestamp": T1,
        "event_type": "task_complete",
        "receipt_kind": "dispatch",
        "dispatch_id": DISPATCH,
        "project_id": PROJECT,
        "attempt": 1,
        "status": "success",
        "model": "deepseek-flash",
        "provider": "deepseek-harness",
        "role": ROLE,
        "report_path": "/tmp/does-not-exist-dlv-55bcc59a796e.md",
        "verification": "pytest tests/test_k8a_report_path_decision.py",
        "findings": [],
    }
    receipt.update(overrides)
    return receipt


def _legacy_completion(**overrides) -> dict:
    line = {
        "timestamp": T1,
        "event_type": "task_complete",
        "receipt_kind": "dispatch",
        "dispatch_id": DISPATCH,
        "project_id": PROJECT,
        "status": "success",
        "model": "deepseek-flash",
        "provider": "deepseek-harness",
        "role": ROLE,
    }
    line.update(overrides)
    return line


def _append(state_dir: Path, line: dict):
    return payload_module.append_receipt_payload(
        dict(line), receipts_file=str(_ledger(state_dir)), skip_enrichment=True)


# ---------------------------------------------------------------------------
# Rule 1: the store is explicit; the destination helper is the append's own
# ---------------------------------------------------------------------------

def test_state_dir_none_is_a_type_error_never_legacy():
    with pytest.raises(TypeError):
        rlm.decide(project_id=PROJECT, dispatch_id=DISPATCH, state_dir=None)
    with pytest.raises(TypeError):
        rlm.report_line(_receipt(), state_dir=None)


def test_destination_helper_is_the_append_layers_own_resolver(tmp_path, monkeypatch):
    from append_receipt_internals.idempotency import _resolve_state_dir_env_first

    monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path / "env-state"))
    assert rlm.append_destination_state_dir() == _resolve_state_dir_env_first()

    monkeypatch.delenv("VNX_STATE_DIR", raising=False)
    assert rlm.append_destination_state_dir() == _resolve_state_dir_env_first()


# ---------------------------------------------------------------------------
# Rule 2: the mode
# ---------------------------------------------------------------------------

def test_v3_by_start_line(state_dir, spec_file, v3, clock):
    _begin(state_dir, spec_file)

    path = rlm.decide(project_id=PROJECT, dispatch_id=DISPATCH, state_dir=state_dir)

    assert path.mode == rlm.MODE_V3
    assert path.project_id == PROJECT
    assert path.attempt == 1
    assert path.end_pending is True
    assert path.end_outcome_id is None


def test_v3_by_row_only_uses_the_rows_attempt_count(state_dir, v3):
    _insert_row(state_dir, attempt_count=2, metadata=json.dumps({"lifecycle_mode": "v3"}))

    path = rlm.decide(project_id=PROJECT, dispatch_id=DISPATCH, state_dir=state_dir)

    assert path.mode == rlm.MODE_V3
    assert path.attempt == 2
    assert path.end_pending is True


def test_row_says_legacy_but_the_start_line_says_v3_is_v3(state_dir, spec_file, v3, clock):
    _begin(state_dir, spec_file)
    _set_row_metadata(state_dir, {"lifecycle_mode": "legacy"})

    path = rlm.decide(project_id=PROJECT, dispatch_id=DISPATCH, state_dir=state_dir)

    assert path.mode == rlm.MODE_V3


def test_both_readable_and_neither_v3_is_legacy_and_returns_the_same_object(state_dir, legacy):
    receipt = _receipt()

    assert rlm.report_line(receipt, state_dir=state_dir) is receipt


def test_unreadable_row_with_a_readable_ledger_refuses(state_dir, v3):
    _insert_row(state_dir, metadata="this is not json")

    with pytest.raises(rlm.ReportPathRefused) as exc:
        rlm.decide(project_id=PROJECT, dispatch_id=DISPATCH, state_dir=state_dir)

    assert exc.value.code == "lifecycle_mode_undecidable"


def test_a_store_file_that_is_not_a_database_refuses(state_dir, v3):
    coordination_db.db_path_from_state_dir(state_dir).write_bytes(b"not a database\n")

    with pytest.raises(rlm.ReportPathRefused) as exc:
        rlm.decide(project_id=PROJECT, dispatch_id=DISPATCH, state_dir=state_dir)

    assert exc.value.code == "lifecycle_mode_undecidable"


def test_a_database_path_that_is_a_directory_refuses_never_legacy(state_dir, legacy):
    """OI-2529: only a path that does not exist is ``ROW_ABSENT``; a directory is unreadable."""
    db_path = coordination_db.db_path_from_state_dir(state_dir)
    db_path.unlink()
    db_path.mkdir()

    with pytest.raises(rlm.ReportPathRefused) as exc:
        rlm.report_line(_receipt(), state_dir=state_dir)

    assert exc.value.code == "lifecycle_mode_undecidable"
    assert "row: unreadable" in exc.value.detail
    assert "is not a regular file" in exc.value.detail


def test_unreadable_ledger_with_a_readable_legacy_row_refuses(state_dir, v3):
    _insert_row(state_dir, metadata="{}")
    _make_ledger_unreadable(state_dir)

    with pytest.raises(rlm.ReportPathRefused) as exc:
        rlm.decide(project_id=PROJECT, dispatch_id=DISPATCH, state_dir=state_dir)

    assert exc.value.code == "lifecycle_mode_undecidable"


def test_unreadable_ledger_with_a_v3_row_refuses(state_dir, v3):
    _insert_row(state_dir, metadata=json.dumps({"lifecycle_mode": "v3"}))
    _make_ledger_unreadable(state_dir)

    with pytest.raises(rlm.ReportPathRefused) as exc:
        rlm.decide(project_id=PROJECT, dispatch_id=DISPATCH, state_dir=state_dir)

    assert exc.value.code == "ledger_unreadable"


# ---------------------------------------------------------------------------
# Rule 3: one tenant
# ---------------------------------------------------------------------------

def test_two_tenants_on_the_start_lines_without_project_id_refuse(state_dir, spec_file, v3,
                                                                  clock):
    _begin(state_dir, spec_file)
    _begin(state_dir, spec_file, project_id=OTHER_PROJECT)

    with pytest.raises(rlm.ReportPathRefused) as exc:
        rlm.decide(project_id=None, dispatch_id=DISPATCH, state_dir=state_dir)

    assert exc.value.code == "project_ambiguous"


def test_one_tenant_on_the_start_lines_is_that_tenant(state_dir, spec_file, v3, clock):
    _begin(state_dir, spec_file)

    path = rlm.decide(project_id=None, dispatch_id=DISPATCH, state_dir=state_dir)

    assert path.project_id == PROJECT
    assert path.mode == rlm.MODE_V3


# ---------------------------------------------------------------------------
# Rule 4: the attempt
# ---------------------------------------------------------------------------

def _two_attempts(state_dir: Path, spec_file: Path, clock) -> None:
    """Attempt 1 started and ended, attempt 2 started; report time T1 sits between."""
    clock["now"] = T0
    _begin(state_dir, spec_file)
    _finalize(state_dir, attempt=1)
    clock["now"] = T2
    _begin(state_dir, spec_file)


def test_a_report_written_between_the_starts_belongs_to_attempt_1(state_dir, spec_file, v3,
                                                                  clock):
    _two_attempts(state_dir, spec_file, clock)
    ended = _completed_line(state_dir, attempt=1)

    path = rlm.decide(project_id=PROJECT, dispatch_id=DISPATCH, state_dir=state_dir,
                      report_written_at=T1)

    assert path.attempt == 1
    assert path.end_pending is False
    assert path.end_outcome_id == ended["outcome_id"]
    assert path.end_outcome_id


def test_the_callers_write_time_wins_over_the_canonical_path_mtime(state_dir, spec_file, v3,
                                                                    clock, tmp_path):
    _two_attempts(state_dir, spec_file, clock)
    canonical = tmp_path / "canonical-report.md"
    canonical.write_text("report\n", encoding="utf-8")
    later = datetime(2026, 11, 3, 13, 0, tzinfo=timezone.utc).timestamp()
    os.utime(canonical, (later, later))
    # No attempt on the receipt: only the write time can attribute this report.
    receipt = _receipt(report_path=str(canonical), attempt=None)

    with_time = rlm.report_line(receipt, state_dir=state_dir, report_written_at=T1)
    by_mtime = rlm.report_line(receipt, state_dir=state_dir)

    assert with_time["attempt"] == 1
    assert by_mtime["attempt"] == 2


def test_the_receipts_own_attempt_wins(state_dir, spec_file, v3, clock):
    _two_attempts(state_dir, spec_file, clock)

    path = rlm.decide(project_id=PROJECT, dispatch_id=DISPATCH, state_dir=state_dir,
                      attempt=1, report_written_at=T2)

    assert path.attempt == 1


def test_a_receipt_attempt_without_a_start_line_refuses(state_dir, spec_file, v3, clock):
    _two_attempts(state_dir, spec_file, clock)

    with pytest.raises(rlm.ReportPathRefused) as exc:
        rlm.decide(project_id=PROJECT, dispatch_id=DISPATCH, state_dir=state_dir, attempt=3)

    assert exc.value.code == "attempt_unknown"


def test_several_starts_and_no_write_time_refuse(state_dir, spec_file, v3, clock):
    _two_attempts(state_dir, spec_file, clock)

    with pytest.raises(rlm.ReportPathRefused) as exc:
        rlm.decide(project_id=PROJECT, dispatch_id=DISPATCH, state_dir=state_dir)

    assert exc.value.code == "attempt_ambiguous"


# ---------------------------------------------------------------------------
# Rule 5: the end link
# ---------------------------------------------------------------------------

def test_a_legacy_completion_never_links_to_a_running_v3_attempt(state_dir, spec_file, v3,
                                                                  clock):
    _begin(state_dir, spec_file)
    _append(state_dir, _legacy_completion(delivery_state="delivery_refused", attempt=1))

    path = rlm.decide(project_id=PROJECT, dispatch_id=DISPATCH, state_dir=state_dir)

    assert path.end_pending is True
    assert path.end_outcome_id is None


def test_a_legacy_completion_without_an_attempt_never_links(state_dir, spec_file, v3, clock):
    _begin(state_dir, spec_file)
    _append(state_dir, _legacy_completion())

    path = rlm.decide(project_id=PROJECT, dispatch_id=DISPATCH, state_dir=state_dir)

    assert path.end_pending is True
    assert path.end_outcome_id is None


def test_an_end_line_of_another_project_never_links(state_dir, spec_file, v3, clock):
    _begin(state_dir, spec_file)
    _finalize(state_dir, project_id=OTHER_PROJECT, end_line=_end_line(project_id=OTHER_PROJECT))

    path = rlm.decide(project_id=PROJECT, dispatch_id=DISPATCH, state_dir=state_dir)

    assert path.end_pending is True


def test_an_end_line_of_another_attempt_never_links(state_dir, spec_file, v3, clock):
    _begin(state_dir, spec_file)
    _finalize(state_dir, attempt=2, end_line=_end_line(attempt=2))

    path = rlm.decide(project_id=PROJECT, dispatch_id=DISPATCH, state_dir=state_dir)

    assert path.attempt == 1
    assert path.end_pending is True


# ---------------------------------------------------------------------------
# Rule 6: the line
# ---------------------------------------------------------------------------

def test_a_v3_report_line_is_a_copy_with_exactly_one_end_link(state_dir, spec_file, v3, clock):
    _begin(state_dir, spec_file)
    receipt = _receipt()

    line = rlm.report_line(receipt, state_dir=state_dir)

    assert line is not receipt
    assert receipt["event_type"] == "task_complete"
    assert line["event_type"] == "report_ingested"
    assert line["project_id"] == PROJECT
    assert line["attempt"] == 1
    assert line["end_pending"] is True
    assert "end_outcome_id" not in line
    for field in ("model", "provider", "role", "status", "report_path", "verification",
                  "findings", "timestamp"):
        assert line[field] == receipt[field]
    receipt_schema.validate_lifecycle_receipt(line)


def test_a_v3_report_line_updates_event_when_it_is_present(state_dir, spec_file, v3, clock):
    _begin(state_dir, spec_file)
    receipt = _receipt()
    receipt["event"] = "task_complete"

    line = rlm.report_line(receipt, state_dir=state_dir)

    assert line["event_type"] == "report_ingested"
    assert line["event"] == "report_ingested"


def test_a_v3_report_line_links_the_exact_end_outcome(state_dir, spec_file, v3, clock):
    _begin(state_dir, spec_file)
    _finalize(state_dir, attempt=1)
    ended = _completed_line(state_dir, attempt=1)

    line = rlm.report_line(_receipt(), state_dir=state_dir)

    assert line["end_outcome_id"] == ended["outcome_id"]
    assert "end_pending" not in line
    receipt_schema.validate_lifecycle_receipt(line)


# ---------------------------------------------------------------------------
# Rule 8 (K-8a r2, OI-2520/OI-2515): the tenant is resolved here, once
# ---------------------------------------------------------------------------

def _tenantless_receipt(**overrides) -> dict:
    """The receipt shape the converter and the parser build: no ``project_id`` yet."""
    receipt = _receipt()
    receipt.pop("project_id")
    receipt.update(overrides)
    return receipt


def test_a_tenantless_legacy_receipt_with_a_legacy_row_is_legacy(state_dir, legacy, monkeypatch):
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)
    _insert_row(state_dir, metadata="{}")
    receipt = _tenantless_receipt()

    assert rlm.report_line(receipt, state_dir=state_dir) is receipt
    assert "project_id" not in receipt


def test_a_tenantless_legacy_receipt_with_no_row_and_with_no_db_is_legacy(
        state_dir, legacy, monkeypatch, tmp_path):
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)
    receipt = _tenantless_receipt()

    assert rlm.report_line(receipt, state_dir=state_dir) is receipt
    coordination_db.db_path_from_state_dir(state_dir).unlink()
    assert rlm.report_line(receipt, state_dir=state_dir) is receipt


def test_a_v3_row_under_the_resolved_tenant_books_a_report(state_dir, legacy, monkeypatch):
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)
    _insert_row(state_dir, metadata=json.dumps({"lifecycle_mode": "v3"}))
    # The converter's receipt shape: no project_id (the append layer stamps it) and no attempt.
    receipt = _tenantless_receipt(attempt=None)

    line = rlm.report_line(receipt, state_dir=state_dir)

    assert line is not receipt
    assert line["event_type"] == "report_ingested"
    assert line["project_id"] == PROJECT
    assert line["attempt"] == 1
    assert line["end_pending"] is True


def test_a_v3_start_and_row_under_another_tenant_refuse_project_mismatch(
        state_dir, spec_file, v3, clock, monkeypatch):
    monkeypatch.setenv("VNX_PROJECT_ID", OTHER_PROJECT)
    _begin(state_dir, spec_file)

    with pytest.raises(rlm.ReportPathRefused) as exc:
        rlm.report_line(_receipt(project_id=OTHER_PROJECT), state_dir=state_dir)

    assert exc.value.code == "project_mismatch"
    assert PROJECT in exc.value.detail and OTHER_PROJECT in exc.value.detail


def test_a_v3_row_under_another_tenant_refuses_with_the_env_tenant(
        state_dir, legacy, monkeypatch):
    monkeypatch.setenv("VNX_PROJECT_ID", OTHER_PROJECT)
    _insert_row(state_dir, project_id=PROJECT, metadata=json.dumps({"lifecycle_mode": "v3"}))

    with pytest.raises(rlm.ReportPathRefused) as exc:
        rlm.report_line(_tenantless_receipt(), state_dir=state_dir)

    assert exc.value.code == "project_mismatch"
    assert PROJECT in exc.value.detail and OTHER_PROJECT in exc.value.detail


def test_a_legacy_row_under_another_tenant_is_still_legacy(state_dir, legacy, monkeypatch):
    monkeypatch.setenv("VNX_PROJECT_ID", OTHER_PROJECT)
    _insert_row(state_dir, project_id=PROJECT, metadata="{}")
    receipt = _tenantless_receipt()

    assert rlm.report_line(receipt, state_dir=state_dir) is receipt


def test_an_unresolvable_tenant_with_a_db_refuses_never_legacy(
        state_dir, legacy, monkeypatch, tmp_path):
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    monkeypatch.chdir(tmp_path)
    receipt = _tenantless_receipt()

    with pytest.raises(rlm.ReportPathRefused) as exc:
        rlm.report_line(receipt, state_dir=state_dir)

    assert exc.value.code == "lifecycle_mode_undecidable"


# ---------------------------------------------------------------------------
# Rule 9 (K-8a r3, OI-2528): a hole in the ledger refuses, only the torn tail is read past
# ---------------------------------------------------------------------------

def test_an_interior_broken_start_line_refuses_never_books_a_completion(
        state_dir, spec_file, v3, clock):
    """Codex K-9: the broken line carried the v3 marker and a valid end followed it.

    The v3 evidence vanishes with the unreadable line, so a lenient reader answers
    legacy and books a second completion. Nothing is booked here: the call raises.
    """
    _begin(state_dir, spec_file)
    _finalize(state_dir, attempt=1)
    line_number = _corrupt_ledger_line(state_dir, "dispatch_started")

    with pytest.raises(rlm.ReportPathRefused) as exc:
        rlm.report_line(_receipt(), state_dir=state_dir)

    assert exc.value.code == "lifecycle_mode_undecidable"
    assert "ledger: ledger_corrupt" in exc.value.detail
    assert f"line {line_number}" in exc.value.detail


def test_a_torn_tail_behind_an_intact_start_line_still_books_the_v3_report(
        state_dir, spec_file, v3, clock):
    """The same damage on the FINAL line is a crash mid-append: the records before it count."""
    _begin(state_dir, spec_file)
    _finalize(state_dir, attempt=1)
    ended = _completed_line(state_dir, attempt=1)
    _append_torn_tail(state_dir, '{"event_type": "dispatch_started", "dispatch_id": "d-k8a", "life')

    line = rlm.report_line(_receipt(), state_dir=state_dir)

    assert line["event_type"] == "report_ingested"
    assert line["end_outcome_id"] == ended["outcome_id"]


def test_a_torn_final_line_on_another_dispatch_is_no_refusal(state_dir, spec_file, v3, clock):
    _begin(state_dir, spec_file)
    _append_torn_tail(state_dir, '{"event_type": "task_complete", "dispatch_id": "d-other", "sta')

    path = rlm.decide(project_id=PROJECT, dispatch_id=DISPATCH, state_dir=state_dir)

    assert path.mode == rlm.MODE_V3
    assert path.end_pending is True


def test_an_interior_broken_line_of_another_dispatch_is_no_refusal(state_dir, spec_file, v3,
                                                                   clock):
    """The scope (OI-2528): another dispatch's broken line is not this dispatch's evidence."""
    _begin(state_dir, spec_file)
    _finalize(state_dir, attempt=1)
    ended = _completed_line(state_dir, attempt=1)
    _insert_broken_line(state_dir, '{"event_type": "task_complete", "dispatch_id": "d-other", "sta', 1)

    line = rlm.report_line(_receipt(), state_dir=state_dir)

    assert line["event_type"] == "report_ingested"
    assert line["end_outcome_id"] == ended["outcome_id"]


def test_a_broken_final_line_that_ends_in_a_newline_of_this_dispatch_refuses(
        state_dir, spec_file, v3, clock):
    """A newline-terminated final line was written completely: committed corruption, not a tear."""
    _begin(state_dir, spec_file)
    _finalize(state_dir, attempt=1)
    _append_broken_line(
        state_dir, f'{{"event_type": "dispatch_started", "dispatch_id": "{DISPATCH}", "att')

    with pytest.raises(rlm.ReportPathRefused) as exc:
        rlm.report_line(_receipt(), state_dir=state_dir)

    assert exc.value.code == "lifecycle_mode_undecidable"
    assert "ledger: ledger_corrupt" in exc.value.detail
    assert "line 3" in exc.value.detail


# ---------------------------------------------------------------------------
# Rule 10 (K-8a r4): the refusal keeps the ``ledger: …; row: …`` wording K-8c reads
# ---------------------------------------------------------------------------

def test_the_undecidable_refusal_names_both_sources_for_the_restore_scan(
        state_dir, tmp_path, spec_file, v3, clock):
    """K-8c's restore scan asserts ``"row: unreadable" in refusal.detail`` and carries the
    detail to its log line, so the shape is a contract between the two modules. The ledger
    side keeps round 3's reason in full, line number included, when it is the broken one.
    """
    _begin(state_dir, spec_file)
    _finalize(state_dir, attempt=1)
    line_number = _corrupt_ledger_line(state_dir, "dispatch_started")

    with pytest.raises(rlm.ReportPathRefused) as ledger_exc:
        rlm.report_line(_receipt(), state_dir=state_dir)

    assert ledger_exc.value.code == "lifecycle_mode_undecidable"
    assert "one lifecycle source is unreadable" in ledger_exc.value.detail
    assert "ledger: ledger_corrupt" in ledger_exc.value.detail
    assert f"line {line_number}" in ledger_exc.value.detail

    row_state = _make_state_dir(tmp_path / "row-only")
    coordination_db.db_path_from_state_dir(row_state).write_bytes(b"not a database\n")

    with pytest.raises(rlm.ReportPathRefused) as row_exc:
        rlm.report_line(_receipt(), state_dir=row_state)

    assert row_exc.value.code == "lifecycle_mode_undecidable"
    assert "one lifecycle source is unreadable" in row_exc.value.detail
    assert "row: unreadable" in row_exc.value.detail
