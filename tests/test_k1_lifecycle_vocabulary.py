"""ADR-045 (audit-spoor-kern K-1): the lifecycle vocabulary and its write rules.

Dispatch-ID: dlv-164797402583

What this file pins, deterministically:

  - the three line types exist in the closed vocabulary and each one's required
    fields are enforced, with the MISSING FIELD named in the refusal;
  - the three types — and only they — are stamped ``schema_version: 3`` and
    ``writer_sha`` by the append layer; a ``task_complete`` keeps its v2 stamp;
  - a ``dispatch_finished`` is written at most once per
    ``(project_id, dispatch_id, attempt)``; attempt 2 and the same dispatch_id
    in another project are their own lines;
  - ``dispatch_started`` and ``report_ingested`` are never an outcome, and
    ``dispatch_finished`` projects its ``end_state`` to the ADR-045 §4 status;
  - with ``ledger_lifecycle`` off (the shipped default) no lifecycle line is
    written at all.

Everything here is deterministic: no model is involved in the write path, so
every assertion is a fixed rule, not a judgment.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(VNX_ROOT / "scripts"))

import append_receipt  # noqa: E402
import check_no_file_derived_data_paths  # noqa: E402
import config_registry  # noqa: E402
import event_outcome_semantics  # noqa: E402
import receipt_outcome  # noqa: E402
import receipt_schema  # noqa: E402
from append_receipt_internals import outcome_identity  # noqa: E402
from append_receipt_internals.idempotency import _compute_idempotency_key  # noqa: E402
from append_receipt_internals.outcome_identity import (  # noqa: E402
    compute_correction_key,
    compute_outcome_id,
)
from append_receipt_internals.common import AppendReceiptError  # noqa: E402
from append_receipt_internals.lifecycle import (  # noqa: E402
    build_end_intent,
    end_intent_path,
)
from dispatch_lifecycle import create_intent  # noqa: E402

START = "dispatch_started"
FINISH = "dispatch_finished"
REPORT = "report_ingested"

START_FIELDS = {
    "project_id": "vnx-dev",
    "dispatch_id": "d-k1",
    "attempt": 1,
    "role": "backend-developer",
    "base_sha": "a" * 40,
    "spec_sha256": "b" * 64,
    "requested_by": "T0",
    "lifecycle_mode": "v3",
    "door_pid": 4242,
    "door_started_at": "2026-10-27T09:59:59Z",
}
FINISH_FIELDS = {
    "project_id": "vnx-dev",
    "dispatch_id": "d-k1",
    "attempt": 1,
    "end_state": "completed",
    "head_sha": "c" * 40,
}
REPORT_FIELDS = {
    "project_id": "vnx-dev",
    "dispatch_id": "d-k1",
    "attempt": 1,
    "end_pending": True,
}


def _receipt(event_type: str, fields: dict, **overrides) -> dict:
    receipt = {"timestamp": "2026-10-27T10:00:00Z", "event_type": event_type}
    receipt.update(fields)
    receipt.update(overrides)
    return receipt


def _ledger(tmp_path: Path) -> Path:
    return tmp_path / "state" / "t0_receipts.ndjson"


def _ensure_end_intent(ledger: Path, receipt: dict) -> None:
    """Write the winner's end intent for the line about to be appended (K-3 rule).

    ADR-045 §3, as K-3 filled it: a ``dispatch_finished`` is accepted only when
    the winner's intent exists for its ``(project_id, dispatch_id, attempt)`` and
    its canonical form is byte-equal to the intent's line. These cases exercise
    the line (its stamp, its status projection, its refusal), not the intent, so
    the intent is the line itself. Cases that deliberately omit a field the
    intent needs skip this and are refused by the field contract.
    """
    needed = ("project_id", "dispatch_id", "attempt", "end_state")
    if any(receipt.get(field) in (None, "") for field in needed):
        return
    if not isinstance(receipt.get("attempt"), int):
        return
    intent_path = end_intent_path(
        ledger.parent, receipt["project_id"], receipt["dispatch_id"], receipt["attempt"])
    if intent_path.exists():
        return  # the first line's intent stands; a second line must match it
    offset = ledger.stat().st_size if ledger.exists() else 0
    create_intent(intent_path, build_end_intent(
        project_id=receipt["project_id"], dispatch_id=receipt["dispatch_id"],
        attempt=receipt["attempt"], end_state=str(receipt["end_state"]),
        end_line=receipt, ledger_path=ledger, ledger_offset=offset,
    ))


def _append(ledger: Path, receipt: dict):
    if str(receipt.get("event_type") or "") == FINISH:
        _ensure_end_intent(ledger, receipt)
    return append_receipt.append_receipt_payload(
        dict(receipt), receipts_file=str(ledger), skip_enrichment=True,
    )


def _lines(ledger: Path) -> list[dict]:
    if not ledger.exists():
        return []
    return [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture
def lifecycle_on(monkeypatch):
    """The setting on: the only state in which any lifecycle line is written."""
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")


# ---------------------------------------------------------------------------
# 1. each type is accepted and stamped
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("event_type,fields", [
    (START, START_FIELDS),
    (FINISH, FINISH_FIELDS),
    (REPORT, REPORT_FIELDS),
])
def test_each_lifecycle_type_is_accepted_and_stamped(tmp_path, lifecycle_on, event_type, fields):
    ledger = _ledger(tmp_path)
    result = _append(ledger, _receipt(event_type, fields))
    assert result.status == "appended"

    written = _lines(ledger)
    assert len(written) == 1
    line = written[0]
    assert line["event_type"] == event_type
    assert line["schema_version"] == 3
    assert isinstance(line["writer_sha"], str) and len(line["writer_sha"]) == 40


def test_writer_sha_is_the_fabric_head(tmp_path, lifecycle_on):
    ledger = _ledger(tmp_path)
    _append(ledger, _receipt(START, START_FIELDS))
    head = subprocess.run(
        ["git", "-C", str(VNX_ROOT), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert _lines(ledger)[0]["writer_sha"] == head


def test_the_lifecycle_module_passes_the_central_mode_path_gate():
    """``lifecycle.py``'s own source holds no un-grandfathered repo-root anchor.

    ``writer_sha`` asks git for the commit of THIS checkout, deliberately without
    the project-root resolver (OI-1145 shape 3a in
    ``scripts/check_no_file_derived_data_paths.py``). This runs the gate on the
    single file so the module cannot regress on its own.
    """
    source = (
        VNX_ROOT / "scripts" / "lib" / "append_receipt_internals" / "lifecycle.py"
    ).read_text(encoding="utf-8")
    violations = check_no_file_derived_data_paths.check_source(source)
    assert violations == [], (
        "append_receipt_internals/lifecycle.py has an un-grandfathered central-mode "
        f"path violation (OI-1145 shape 3a): {violations}"
    )


# ---------------------------------------------------------------------------
# 2. a missing required field is refused, with its name
# ---------------------------------------------------------------------------

# The required fields of ADR-045 §2/§8, stated here independently of the
# implementation: this is the requirement, and the next test pins the code to it.
REQUIRED = {
    START: (
        "project_id", "dispatch_id", "attempt", "role", "base_sha", "spec_sha256",
        "requested_by", "lifecycle_mode", "door_pid", "door_started_at", "writer_sha",
    ),
    FINISH: ("project_id", "dispatch_id", "attempt", "end_state", "writer_sha"),
    REPORT: ("project_id", "dispatch_id", "attempt"),
}

FIELD_CASES = [(event_type, field) for event_type, fields in REQUIRED.items() for field in fields]
INPUT_FIELDS = {START: START_FIELDS, FINISH: FINISH_FIELDS, REPORT: REPORT_FIELDS}


def test_required_field_vocabulary_matches_the_requirement():
    assert receipt_schema.LIFECYCLE_REQUIRED_FIELDS == REQUIRED


@pytest.mark.parametrize("event_type,missing", FIELD_CASES)
def test_required_field_contract_names_the_missing_field(event_type, missing):
    line = _receipt(event_type, INPUT_FIELDS[event_type])
    # ``writer_sha`` is stamped by the append layer and never supplied by the
    # caller, so it is already absent from the input — the contract still has to
    # refuse it when it is missing (that is exactly the stamped-field contract).
    line.pop(missing, None)
    with pytest.raises(receipt_schema.LifecycleReceiptError) as excinfo:
        receipt_schema.validate_lifecycle_receipt(line)
    assert excinfo.value.code == "missing_lifecycle_field"
    assert excinfo.value.field == missing


@pytest.mark.parametrize("event_type,fields,missing", [
    (START, START_FIELDS, "role"),
    (FINISH, FINISH_FIELDS, "end_state"),
    (REPORT, REPORT_FIELDS, "project_id"),
])
def test_append_refuses_a_missing_required_field_with_its_name(
    tmp_path, lifecycle_on, event_type, fields, missing,
):
    line = _receipt(event_type, fields)
    line.pop(missing)
    with pytest.raises(AppendReceiptError) as excinfo:
        _append(_ledger(tmp_path), line)
    assert excinfo.value.code == "missing_lifecycle_field"
    assert missing in str(excinfo.value)
    assert _lines(_ledger(tmp_path)) == []


def test_end_state_outside_the_closed_set_is_refused(tmp_path, lifecycle_on):
    line = _receipt(FINISH, FINISH_FIELDS, end_state="hung")
    with pytest.raises(AppendReceiptError) as excinfo:
        _append(_ledger(tmp_path), line)
    assert excinfo.value.code == "invalid_lifecycle_end_state"


def test_end_line_without_push_marker_is_refused(tmp_path, lifecycle_on):
    line = _receipt(FINISH, FINISH_FIELDS)
    line.pop("head_sha")
    with pytest.raises(AppendReceiptError) as excinfo:
        _append(_ledger(tmp_path), line)
    assert excinfo.value.code == "missing_lifecycle_push_marker"


def test_end_line_accepts_no_push_instead_of_head_sha(tmp_path, lifecycle_on):
    ledger = _ledger(tmp_path)
    line = _receipt(FINISH, FINISH_FIELDS, no_push="no_push:nothing-to-push")
    line.pop("head_sha")
    assert _append(ledger, line).status == "appended"
    assert _lines(ledger)[0]["no_push"] == "no_push:nothing-to-push"


# ---------------------------------------------------------------------------
# 3. one end line per (project_id, dispatch_id, attempt)
# ---------------------------------------------------------------------------

def test_second_end_line_for_the_same_attempt_is_refused(tmp_path, lifecycle_on):
    ledger = _ledger(tmp_path)
    assert _append(ledger, _receipt(FINISH, FINISH_FIELDS)).status == "appended"

    second = _receipt(FINISH, FINISH_FIELDS, end_state="failed",
                      timestamp="2026-10-27T10:05:00Z")
    with pytest.raises(AppendReceiptError) as excinfo:
        _append(ledger, second)
    assert excinfo.value.code == "duplicate_lifecycle_end"
    assert len(_lines(ledger)) == 1


def test_second_end_line_is_refused_from_the_ledger_tail_without_the_index(tmp_path, lifecycle_on):
    """The index write is fail-open by design; the bounded tail scan backstops it."""
    ledger = _ledger(tmp_path)
    assert _append(ledger, _receipt(FINISH, FINISH_FIELDS)).status == "appended"
    index = ledger.parent / "receipt_outcome_index.json"
    assert index.exists()
    index.unlink()

    second = _receipt(FINISH, FINISH_FIELDS, end_state="failed",
                      timestamp="2026-10-27T10:06:00Z")
    with pytest.raises(AppendReceiptError) as excinfo:
        _append(ledger, second)
    assert excinfo.value.code == "duplicate_lifecycle_end"


def test_attempt_two_is_its_own_end_line(tmp_path, lifecycle_on):
    ledger = _ledger(tmp_path)
    assert _append(ledger, _receipt(FINISH, FINISH_FIELDS)).status == "appended"
    attempt_two = _receipt(FINISH, FINISH_FIELDS, attempt=2, timestamp="2026-10-27T10:20:00Z")
    assert _append(ledger, attempt_two).status == "appended"
    assert [line["attempt"] for line in _lines(ledger)] == [1, 2]


def test_same_dispatch_id_in_another_project_is_its_own_end_line(tmp_path, lifecycle_on):
    ledger = _ledger(tmp_path)
    assert _append(ledger, _receipt(FINISH, FINISH_FIELDS)).status == "appended"
    other = _receipt(FINISH, FINISH_FIELDS, project_id="another-project",
                     timestamp="2026-10-27T10:30:00Z")
    assert _append(ledger, other).status == "appended"
    assert [line["project_id"] for line in _lines(ledger)] == ["vnx-dev", "another-project"]


# ---------------------------------------------------------------------------
# 4. outcome semantics of the three types
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["success", "failure", "done", "failed", "timeout", None])
def test_start_line_is_never_an_outcome(status):
    assert event_outcome_semantics.classify_event_outcome(START, status) is None


@pytest.mark.parametrize("status", ["success", "failure", "done", "failed", "timeout", None])
def test_report_line_is_never_an_outcome(status):
    assert event_outcome_semantics.classify_event_outcome(REPORT, status) is None


def test_report_line_is_a_report_type_but_not_an_outcome_line():
    assert REPORT in receipt_outcome.REPORT_EVENT_TYPES
    assert REPORT not in receipt_outcome.OUTCOME_EVENT_TYPES
    assert receipt_outcome.is_outcome_line({"event_type": REPORT, "dispatch_id": "d-1"}) is False
    assert receipt_outcome.is_outcome_line(
        {"event_type": REPORT, "dispatch_id": "d-1", "report_file": "r.md"}) is False


def test_start_line_is_not_an_outcome_line():
    assert receipt_outcome.is_outcome_line({"event_type": START, "dispatch_id": "d-1"}) is False


@pytest.mark.parametrize("end_state,status", [
    ("completed", "success"),
    ("failed", "failure"),
    ("timed_out", "timeout"),
    ("abandoned", "timeout"),
])
def test_end_state_projects_to_the_status_in_the_plan_table(end_state, status):
    assert event_outcome_semantics.end_state_status(end_state) == status
    # The status the table names is read by the ordinary completion rule: a
    # success/failure literal is a governed success/failure, and "timeout" is
    # the vocabulary's deliberate no-signal literal for a completion.
    expected = {"success": "success", "failure": "failure", "timeout": None}[status]
    assert event_outcome_semantics.classify_event_outcome(FINISH, status) == expected


def test_unknown_end_state_has_no_projection():
    assert event_outcome_semantics.end_state_status("hung") is None
    assert event_outcome_semantics.end_state_status(None) is None


@pytest.mark.parametrize("end_state,status", [
    ("completed", "success"),
    ("failed", "failure"),
    ("timed_out", "timeout"),
    ("abandoned", "timeout"),
])
def test_append_stamps_the_projected_status_on_the_end_line(
    tmp_path, lifecycle_on, end_state, status,
):
    ledger = _ledger(tmp_path)
    line = _receipt(FINISH, FINISH_FIELDS, end_state=end_state,
                    dispatch_id=f"d-{end_state}")
    assert _append(ledger, line).status == "appended"
    assert _lines(ledger)[0]["status"] == status


def test_a_status_that_contradicts_end_state_is_refused(tmp_path, lifecycle_on):
    line = _receipt(FINISH, FINISH_FIELDS, status="failure")
    with pytest.raises(AppendReceiptError) as excinfo:
        _append(_ledger(tmp_path), line)
    assert excinfo.value.code == "lifecycle_status_conflict"


# ---------------------------------------------------------------------------
# 5. the setting is the brake, and existing types keep their stamp
# ---------------------------------------------------------------------------

def test_no_lifecycle_line_is_written_while_the_setting_is_off(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_LEDGER_LIFECYCLE", raising=False)
    ledger = _ledger(tmp_path)
    with pytest.raises(AppendReceiptError) as excinfo:
        _append(ledger, _receipt(START, START_FIELDS))
    assert excinfo.value.code == "ledger_lifecycle_disabled"
    assert _lines(ledger) == []


def test_the_setting_is_registered_and_defaults_off():
    entry = config_registry.CONFIG_REGISTRY["VNX_LEDGER_LIFECYCLE"]
    assert entry.default == "0"
    assert entry.type == "bool"


def test_task_complete_keeps_its_v2_stamp(tmp_path, lifecycle_on):
    """The golden: an existing type is byte-unchanged by the lifecycle generation."""
    ledger = _ledger(tmp_path)
    receipt = {
        "timestamp": "2026-10-27T11:00:00Z",
        "event_type": "task_complete",
        "dispatch_id": "d-legacy",
        "status": "success",
        "terminal": "T1",
    }
    assert _append(ledger, receipt).status == "appended"
    line = _lines(ledger)[0]
    assert line["schema_version"] == 2
    assert "writer_sha" not in line


# ---------------------------------------------------------------------------
# 6. the attempt triple is in EVERY lifecycle line's identity — all three
#    types, both identity calculations (audit-spoor-kern K-1 round 2)
# ---------------------------------------------------------------------------

LIFECYCLE_INPUTS = {START: START_FIELDS, FINISH: FINISH_FIELDS, REPORT: REPORT_FIELDS}


@pytest.mark.parametrize("event_type", [START, REPORT, FINISH])
def test_attempt_two_of_one_dispatch_is_its_own_line(tmp_path, lifecycle_on, event_type):
    """attempt 2 of one dispatch is a different line for ALL three types.

    Before this round only the end line was scoped by the attempt triple; a
    second ``dispatch_started`` / ``report_ingested`` kept attempt 1's keys and
    was discarded as its duplicate.
    """
    ledger = _ledger(tmp_path)
    fields = LIFECYCLE_INPUTS[event_type]

    assert _append(ledger, _receipt(event_type, fields)).status == "appended"
    result = _append(ledger, _receipt(event_type, fields, attempt=2))

    assert result.status == "appended", (
        f"attempt 2's {event_type} was discarded as attempt 1's duplicate")
    lines = _lines(ledger)
    assert [line["attempt"] for line in lines] == [1, 2]
    assert len({line["outcome_id"] for line in lines}) == 2, (
        f"attempt 1 and attempt 2 must not share one {event_type} outcome_id")


@pytest.mark.parametrize("event_type", [START, REPORT, FINISH])
def test_same_dispatch_id_in_another_project_is_its_own_line(tmp_path, lifecycle_on, event_type):
    """The same dispatch_id in another project is a different line, per type."""
    ledger = _ledger(tmp_path)
    fields = LIFECYCLE_INPUTS[event_type]

    assert _append(ledger, _receipt(event_type, fields)).status == "appended"
    result = _append(ledger, _receipt(event_type, fields, project_id="another-project"))

    assert result.status == "appended", (
        f"another project's {event_type} was discarded as this project's duplicate")
    lines = _lines(ledger)
    assert [line["project_id"] for line in lines] == ["vnx-dev", "another-project"]
    assert len({line["outcome_id"] for line in lines}) == 2


@pytest.mark.parametrize("event_type", [START, REPORT, FINISH])
def test_the_short_window_key_is_scoped_by_project_and_attempt(event_type):
    """Both identity calculations are scoped, not just the durable one.

    A differing attempt or project must also give a different in-process cache
    key: the short-window check runs first, so a collision there drops the
    second line before the outcome index is ever consulted.
    """
    fields = LIFECYCLE_INPUTS[event_type]
    base = _compute_idempotency_key(_receipt(event_type, fields), event_type)

    assert _compute_idempotency_key(
        _receipt(event_type, fields, attempt=2), event_type) != base, (
        f"attempt 2's {event_type} kept attempt 1's short-window key")
    assert _compute_idempotency_key(
        _receipt(event_type, fields, project_id="another-project"), event_type) != base, (
        f"another project's {event_type} kept this project's short-window key")


def test_one_helper_decides_the_lifecycle_identity_fields():
    """One decision for the three lifecycle types, none for every other shape."""
    decide = outcome_identity.lifecycle_identity_fields
    for event_type in (START, FINISH, REPORT):
        assert set(decide(_receipt(event_type, LIFECYCLE_INPUTS[event_type]))) == {
            "project_id", "attempt"}
    assert decide({"event_type": "task_complete", "dispatch_id": "d-legacy"}) == ()


def test_another_attempt_and_another_project_do_not_supersede_the_first_end_line(
    tmp_path, lifecycle_on,
):
    """The correction key is scoped like the identity: only attempt 1 itself
    can be superseded by a later attempt-1 line."""
    ledger = _ledger(tmp_path)
    assert _append(ledger, _receipt(FINISH, FINISH_FIELDS)).status == "appended"
    assert _append(ledger, _receipt(FINISH, FINISH_FIELDS, attempt=2)).status == "appended"
    assert _append(ledger, _receipt(FINISH, FINISH_FIELDS, project_id="another-project")).status == "appended"

    lines = _lines(ledger)
    assert len(lines) == 3
    assert len({line["outcome_id"] for line in lines}) == 3
    first_outcome_id = lines[0]["outcome_id"]
    assert "supersedes" not in lines[1], (
        "attempt 2's end line must not be booked as a correction of attempt 1")
    assert "supersedes" not in lines[2], (
        "another project's end line must not be booked as a correction of this project's")
    assert lines[1].get("supersedes") != first_outcome_id
    assert lines[2].get("supersedes") != first_outcome_id


@pytest.mark.parametrize("event_type", [START, REPORT, FINISH])
def test_a_real_duplicate_is_still_discarded(tmp_path, lifecycle_on, event_type):
    """Widening the identity must not turn a true duplicate into a second line."""
    ledger = _ledger(tmp_path)
    fields = LIFECYCLE_INPUTS[event_type]

    assert _append(ledger, _receipt(event_type, fields)).status == "appended"
    assert _append(ledger, _receipt(event_type, fields)).status == "duplicate"
    assert len(_lines(ledger)) == 1


def test_a_real_correction_within_one_attempt_still_supersedes(tmp_path, lifecycle_on):
    """Same project, dispatch, attempt and type — only the status differs.

    ``status`` is the one identity input outside the correction key (that is
    exactly what ADR-038 makes a correction: a changed outcome in an unchanged
    family), and on a report line it is inert for outcome classification (pinned
    in section 4 above). Scoping the family by the attempt triple must never
    block a genuine correction inside it.
    """
    ledger = _ledger(tmp_path)
    assert _append(ledger, _receipt(REPORT, REPORT_FIELDS, status="unavailable")).status == "appended"
    assert _append(ledger, _receipt(REPORT, REPORT_FIELDS, status="success")).status == "appended"

    lines = _lines(ledger)
    assert len(lines) == 2
    assert "supersedes" not in lines[0]
    assert lines[1].get("supersedes") == lines[0]["outcome_id"]


def test_legacy_identity_and_correction_keys_are_unchanged():
    """Golden values taken on main before this round.

    A non-lifecycle shape's outcome and correction keys are already recorded in
    every project's durable index; the lifecycle widening must leave them
    byte-identical (its own identity extras stay behind the vocabulary check).
    """
    task_complete = {
        "event_type": "task_complete",
        "dispatch_id": "d-legacy", "status": "success",
        "terminal": "T1", "commit_sha": "a" * 40,
    }
    assert compute_outcome_id(task_complete) == (
        "8fb15f4e0fd44aea28c56738da36e4467e4366ad8a1ffa0d4515cef30a010508")
    assert compute_correction_key(task_complete) == (
        "e350640c5ff3cfa94c609b9b7c3ffac98bab72af4e75e1d7a58d7636b5f68b89")
    assert _compute_idempotency_key(task_complete, "task_complete") == (
        "54d42a8d2c7cfae0943e8148ce3d22149bcd528abe9629b1b94f48c2e849d767")

    review_gate = {
        "event_type": "review_gate_result",
        "dispatch_id": "d-legacy", "status": "pass",
        "gate": "codex_gate", "pr_number": 2307,
    }
    assert compute_outcome_id(review_gate) == (
        "28c2a006575dae6ee77c15dd44ed929709d72ac4d548a74939a1465af33d0c4a")
    assert compute_correction_key(review_gate) == (
        "e898a5b1e5d0e23c99f03220e891e033ef7bc2b74fdab0b00a8c348220619064")
