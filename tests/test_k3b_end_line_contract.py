"""ADR-045 (audit-spoor-kern K-3b): what a ``dispatch_finished`` line may be.

Dispatch-ID: dlv-cab5e87f7c6c

The append layer owns the end line: the intent layout, the canonical form, the
full validation and the rule. This file pins the four findings the closed K-3
branch left open:

1. a line the append would refuse raises from ``prepare_end_line`` with the
   same code and claims no intent (codex round 3, blocker);
2. a normalised ``model`` and a caller-supplied ``ingested_at`` are canonical
   on both sides of the comparison (codex round 3, warning);
3. an identifier that is not one path component is refused and creates
   nothing (codex round 1, error);
4. only the append layer's own derived stamps are excluded — an extra caller
   decision such as ``no_push`` is a mismatch (codex round 1, error).

The real append layer runs against ``tmp_path``; only paths are patched, as in
``tests/test_k1_lifecycle_vocabulary.py``.
"""

from __future__ import annotations

import copy
import inspect
import json
import re
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(VNX_ROOT / "scripts"))

import append_receipt  # noqa: E402
from append_receipt_internals.common import AppendReceiptError  # noqa: E402
from append_receipt_internals.idempotency import (  # noqa: E402
    _cache_file_for,
    _write_receipt_under_lock,
)
from append_receipt_internals.lifecycle import (  # noqa: E402
    DERIVED_STAMP_FIELDS,
    DERIVED_WARNING_FIELDS,
    build_end_intent,
    end_intent_path,
    end_line_matches_intent,
    load_end_intent,
    prepare_end_line,
    project_end_line,
    write_end_intent,
)
from append_receipt_internals.warning_destination import commit_destination  # noqa: E402

FINISH = "dispatch_finished"

END_LINE = {
    "timestamp": "2026-10-27T10:00:00Z",
    "event_type": FINISH,
    "project_id": "vnx-dev",
    "dispatch_id": "d-k3b",
    "attempt": 1,
    "end_state": "completed",
    "head_sha": "c" * 40,
}


def _line(**overrides) -> dict:
    return dict(END_LINE, **overrides)


def _ledger(tmp_path: Path) -> Path:
    return tmp_path / "state" / "t0_receipts.ndjson"


def _append(ledger: Path, line: dict):
    return append_receipt.append_receipt_payload(
        dict(line), receipts_file=str(ledger), skip_enrichment=True,
    )


def _lines(ledger: Path) -> list:
    if not ledger.exists():
        return []
    return [json.loads(raw) for raw in ledger.read_text(encoding="utf-8").splitlines() if raw.strip()]


def _freeze(ledger: Path, line: dict):
    """The intent a K-3c finalizer would freeze from ``prepare_end_line``."""
    prepared = prepare_end_line(line, ledger_path=ledger)
    intent_path = end_intent_path(
        ledger.parent, prepared["project_id"], prepared["dispatch_id"], prepared["attempt"])
    offset = ledger.stat().st_size if ledger.exists() else 0
    document = build_end_intent(
        project_id=prepared["project_id"], dispatch_id=prepared["dispatch_id"],
        attempt=prepared["attempt"], end_state=str(prepared["end_state"]),
        end_line=prepared, ledger_path=ledger, ledger_offset=offset,
    )
    assert write_end_intent(intent_path, document) is True
    return prepared, intent_path


def _write_raw_intent(ledger: Path, text: str, *, dispatch_id: str = "d-k3b",
                      attempt: int = 1) -> Path:
    intent_path = end_intent_path(ledger.parent, "vnx-dev", dispatch_id, attempt)
    intent_path.parent.mkdir(parents=True, exist_ok=True)
    intent_path.write_text(text, encoding="utf-8")
    return intent_path


@pytest.fixture
def lifecycle_on(monkeypatch):
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")


# ---------------------------------------------------------------------------
# 1. the refusal happens before anything is frozen (codex round 3, blocker)
# ---------------------------------------------------------------------------

def _drop_push_markers(line: dict) -> None:
    line.pop("head_sha", None)
    line.pop("no_push", None)


def _mark_dispatch_receipt(line: dict) -> None:
    line["receipt_kind"] = "dispatch"


def _break_end_state(line: dict) -> None:
    line["end_state"] = "hung"


@pytest.mark.parametrize("mutate,code", [
    (_drop_push_markers, "missing_lifecycle_push_marker"),
    (_mark_dispatch_receipt, "missing_model"),
    (_break_end_state, "invalid_lifecycle_end_state"),
])
def test_prepare_end_line_refuses_what_the_append_refuses(tmp_path, lifecycle_on, mutate, code):
    """The same line-only refusal, the same code, and no intent on disk."""
    line = _line()
    mutate(line)
    ledger = _ledger(tmp_path)

    with pytest.raises(AppendReceiptError) as append_error:
        _append(ledger, line)
    assert append_error.value.code == code

    with pytest.raises(AppendReceiptError) as prepare_error:
        prepare_end_line(line, ledger_path=ledger)
    assert prepare_error.value.code == code
    assert not (ledger.parent / "dispatch_end").exists()


def test_a_line_without_a_push_marker_claims_no_intent(tmp_path, lifecycle_on):
    """The exact round-3 reproduction: missing ``head_sha`` AND ``no_push``."""
    ledger = _ledger(tmp_path)
    line = _line()
    line.pop("head_sha")

    with pytest.raises(AppendReceiptError) as excinfo:
        prepare_end_line(line, ledger_path=ledger)
    assert excinfo.value.code == "missing_lifecycle_push_marker"
    assert not (ledger.parent / "dispatch_end").exists()


def test_prepare_end_line_refuses_a_line_that_is_not_an_end(tmp_path, lifecycle_on):
    line = _line(event_type="task_complete")
    ledger = _ledger(tmp_path)

    with pytest.raises(AppendReceiptError) as excinfo:
        prepare_end_line(line, ledger_path=ledger)
    assert excinfo.value.code == "invalid_end_line"
    assert not (ledger.parent / "dispatch_end").exists()


def test_prepare_end_line_does_not_mutate_the_callers_line(tmp_path, lifecycle_on):
    line = _line()
    before = copy.deepcopy(line)
    prepare_end_line(line, ledger_path=_ledger(tmp_path))
    assert line == before


# ---------------------------------------------------------------------------
# 2. canonicalisation on both sides (codex round 3, warning)
# ---------------------------------------------------------------------------

def test_a_normalised_model_is_frozen_normalised_and_accepted(tmp_path, lifecycle_on):
    """The intent holds the key the append normalises to, not the raw string."""
    ledger = _ledger(tmp_path)
    raw = _line(model="deepseek/deepseek-v4-pro")

    prepared, intent_path = _freeze(ledger, raw)
    assert prepared["model"] == "deepseek-v4-pro"
    assert json.loads(load_end_intent(intent_path)["line"])["model"] == "deepseek-v4-pro"

    assert _append(ledger, raw).status == "appended"
    assert _lines(ledger)[0]["model"] == "deepseek-v4-pro"


def test_a_caller_supplied_ingested_at_is_accepted(tmp_path, lifecycle_on):
    """``ingested_at`` is overwritten by the append, so it is never compared."""
    ledger = _ledger(tmp_path)
    raw = _line(ingested_at="2020-01-01T00:00:00Z")

    prepared, _ = _freeze(ledger, raw)
    assert "ingested_at" not in prepared

    assert _append(ledger, raw).status == "appended"
    assert _lines(ledger)[0]["ingested_at"] != "2020-01-01T00:00:00Z"


# ---------------------------------------------------------------------------
# 3. safe intent paths (codex round 1, error)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("project_id,dispatch_id,attempt", [
    ("vnx-dev", "../x", 1),
    ("vnx-dev", "a/b", 1),
    ("vnx-dev", "a\\b", 1),
    ("vnx-dev", "nul\x00byte", 1),
    ("vnx-dev", "..", 1),
    ("vnx-dev", ".", 1),
    ("", "d-k3b", 1),
    ("   ", "d-k3b", 1),
    ("vnx-dev", "d-k3b", 0),
    ("vnx-dev", "d-k3b", -1),
    ("vnx-dev", "d-k3b", True),
])
def test_an_unsafe_intent_key_is_refused_and_creates_nothing(
    tmp_path, project_id, dispatch_id, attempt,
):
    state = tmp_path / "state"
    with pytest.raises(AppendReceiptError) as excinfo:
        end_intent_path(state, project_id, dispatch_id, attempt)
    assert excinfo.value.code == "invalid_identifier"
    assert not state.exists()


def test_an_absolute_dispatch_id_is_refused_and_creates_nothing(tmp_path):
    outside = str(tmp_path / "escaped-dispatch")
    state = tmp_path / "state"

    with pytest.raises(AppendReceiptError) as excinfo:
        end_intent_path(state, "vnx-dev", outside, 1)
    assert excinfo.value.code == "invalid_identifier"
    assert not state.exists()
    assert not Path(f"{outside}.1.json").exists()


def test_a_symlinked_project_directory_that_resolves_outside_is_refused(tmp_path):
    state = tmp_path / "state"
    root = state / "dispatch_end"
    root.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / "vnx-dev").symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(AppendReceiptError) as excinfo:
        end_intent_path(state, "vnx-dev", "d-k3b", 1)
    assert excinfo.value.code == "invalid_identifier"
    assert list(elsewhere.iterdir()) == []


# ---------------------------------------------------------------------------
# 4. the rule itself (ADR-045 §3)
# ---------------------------------------------------------------------------

def test_an_end_line_without_an_intent_is_refused(tmp_path, lifecycle_on):
    ledger = _ledger(tmp_path)
    with pytest.raises(AppendReceiptError) as excinfo:
        _append(ledger, _line())
    assert excinfo.value.code == "missing_end_intent"
    assert _lines(ledger) == []
    assert not (ledger.parent / "dispatch_end").exists()


@pytest.mark.parametrize("raw", [
    "{not json",
    json.dumps({"version": 1, "project_id": "vnx-dev"}),
])
def test_a_malformed_intent_is_refused(tmp_path, lifecycle_on, raw):
    ledger = _ledger(tmp_path)
    _write_raw_intent(ledger, raw)

    with pytest.raises(AppendReceiptError) as excinfo:
        _append(ledger, _line())
    assert excinfo.value.code == "invalid_end_intent"
    assert _lines(ledger) == []


def test_a_tampered_intent_line_is_refused(tmp_path, lifecycle_on):
    ledger = _ledger(tmp_path)
    prepared, intent_path = _freeze(ledger, _line())
    document = load_end_intent(intent_path)
    document["line"] = json.dumps(dict(json.loads(document["line"]), end_state="failed"))
    intent_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(AppendReceiptError) as excinfo:
        _append(ledger, _line())
    assert excinfo.value.code == "invalid_end_intent"
    assert _lines(ledger) == []


def test_an_intent_for_another_attempt_is_refused(tmp_path, lifecycle_on):
    ledger = _ledger(tmp_path)
    other = prepare_end_line(_line(attempt=2), ledger_path=ledger)
    document = build_end_intent(
        project_id="vnx-dev", dispatch_id="d-k3b", attempt=2, end_state="completed",
        end_line=other, ledger_path=ledger, ledger_offset=0,
    )
    _write_raw_intent(ledger, json.dumps(document))  # frozen under attempt 1's name

    with pytest.raises(AppendReceiptError) as excinfo:
        _append(ledger, _line())
    assert excinfo.value.code == "invalid_end_intent"
    assert _lines(ledger) == []


def test_a_second_end_line_for_the_key_is_a_duplicate(tmp_path, lifecycle_on):
    ledger = _ledger(tmp_path)
    _freeze(ledger, _line())
    assert _append(ledger, _line()).status == "appended"

    with pytest.raises(AppendReceiptError) as excinfo:
        _append(ledger, _line(end_state="failed", timestamp="2026-10-27T10:05:00Z"))
    assert excinfo.value.code == "duplicate_lifecycle_end"
    assert len(_lines(ledger)) == 1


def test_a_line_that_matches_the_intent_is_accepted(tmp_path, lifecycle_on):
    ledger = _ledger(tmp_path)
    _, intent_path = _freeze(ledger, _line())
    assert _append(ledger, _line()).status == "appended"

    written = _lines(ledger)[0]
    frozen = json.loads(load_end_intent(intent_path)["line"])
    for field, expected in frozen.items():
        assert written[field] == expected, f"{field} differs between intent and ledger line"
    assert written["end_state"] == "completed"

    # A byte-identical replay is the same line; the append layer's dedup skips it.
    assert _append(ledger, _line()).status == "duplicate"
    assert len(_lines(ledger)) == 1


def test_a_line_with_a_different_end_state_than_the_intent_is_a_mismatch(tmp_path, lifecycle_on):
    ledger = _ledger(tmp_path)
    _freeze(ledger, _line())
    with pytest.raises(AppendReceiptError) as excinfo:
        _append(ledger, _line(end_state="timed_out"))
    assert excinfo.value.code == "end_line_mismatch"
    assert _lines(ledger) == []


# ---------------------------------------------------------------------------
# 5. only the append layer's own derived stamps are excluded (round 1, error 4)
# ---------------------------------------------------------------------------

STAMP_VALUES = {
    # lifecycle.stamp_lifecycle
    "schema_version": 2,
    "writer_sha": "f" * 40,
    "status": "success",
    # payload._stamp_identity / _stamp_observability_tier / _CHAIN_LINK_ENV_FIELDS
    "operator_id": "operator",
    "orchestrator_id": "T0",
    "agent_id": "deepseek-flash",
    "observability_tier": "tier-low",
    "parent_dispatch": "dlv-parent",
    "task_class": "01_code_generation",
    "tier_from": "tier-low",
    "tier_to": "tier-low",
    # payload._prepare_receipt / _stamp_ingested_at
    "open_items_created": 2,
    "ingested_at": "2020-01-01T00:00:00Z",
    # idempotency._write_receipt_under_lock and its pre-write hook
    "outcome_id": "0" * 64,
    "verdict": {"decision": "investigate"},
    "prev_hash": "0" * 64,
}


def test_the_derived_stamp_set_is_exactly_what_this_file_names(tmp_path, lifecycle_on):
    assert set(STAMP_VALUES) == DERIVED_STAMP_FIELDS


def test_the_derived_stamps_alone_are_not_a_mismatch(tmp_path, lifecycle_on):
    ledger = _ledger(tmp_path)
    _freeze(ledger, _line())
    assert _append(ledger, dict(_line(), **STAMP_VALUES)).status == "appended"
    assert len(_lines(ledger)) == 1


def test_an_extra_caller_no_push_that_the_intent_lacks_is_a_mismatch(tmp_path, lifecycle_on):
    """The codex round-1 reproduction: a field on one side and not the other."""
    ledger = _ledger(tmp_path)
    _freeze(ledger, _line())

    with pytest.raises(AppendReceiptError) as excinfo:
        _append(ledger, dict(_line(), no_push="no_push:nothing to push"))
    assert excinfo.value.code == "end_line_mismatch"
    assert _lines(ledger) == []

    assert _append(ledger, _line()).status == "appended"
    assert len(_lines(ledger)) == 1


# ---------------------------------------------------------------------------
# 6. a frozen line stays frozen through the final write (codex round 2, blocker)
# ---------------------------------------------------------------------------

CODEX_WARNING = {"code": "review_probe", "severity": "warn", "message": "probe"}


def test_a_warning_bearing_end_line_is_accepted_and_still_matches_its_intent(
    tmp_path, lifecycle_on,
):
    """The round-2 reproduction: the commit hook rewrites ``reason`` after the freeze."""
    ledger = _ledger(tmp_path)
    line = _line(warnings=[dict(CODEX_WARNING)])

    prepared, intent_path = _freeze(ledger, line)
    assert "reason" not in prepared["warnings"][0]
    assert "destination" not in prepared["warnings"][0]

    assert _append(ledger, line).status == "appended"

    written = _lines(ledger)[0]
    assert written["warnings"][0]["destination"] == "counted"
    assert written["warnings"][0]["reason"] is None
    assert end_line_matches_intent(written, load_end_intent(intent_path)) is True


def test_the_projection_drops_only_the_lock_time_warning_keys(tmp_path):
    """A frozen ``counted`` entry matches the committed ``oi`` entry it becomes."""
    frozen_entry = {
        "code": "review_probe", "severity": "warn", "message": "probe",
        "requires_tracking": True, "destination": "counted", "oi_id": None,
        "reason": "__pending_append_commit__",
    }
    expected_entry = {
        "code": "review_probe", "severity": "warn", "message": "probe",
        "requires_tracking": True,
    }
    assert project_end_line({"warnings": [frozen_entry]})["warnings"] == [expected_entry]

    document = build_end_intent(
        project_id="vnx-dev", dispatch_id="d-k3b", attempt=1, end_state="completed",
        end_line=_line(warnings=[expected_entry]), ledger_path=_ledger(tmp_path),
        ledger_offset=0,
    )
    committed = _line(warnings=[
        dict(frozen_entry, destination="oi", oi_id="OI-1", reason=None)])
    assert end_line_matches_intent(committed, document) is True


@pytest.mark.parametrize("field,value", [
    ("message", "another message"),
    ("severity", "info"),
])
def test_a_changed_warning_message_or_severity_is_still_a_mismatch(
    tmp_path, lifecycle_on, field, value,
):
    ledger = _ledger(tmp_path)
    prepared, intent_path = _freeze(ledger, _line(warnings=[dict(CODEX_WARNING)]))
    changed = copy.deepcopy(prepared)
    changed["warnings"][0][field] = value
    assert end_line_matches_intent(changed, load_end_intent(intent_path)) is False

    with pytest.raises(AppendReceiptError) as excinfo:
        _append(ledger, _line(warnings=[dict(CODEX_WARNING, **{field: value})]))
    assert excinfo.value.code == "end_line_mismatch"
    assert _lines(ledger) == []


def test_the_derived_warning_set_is_exactly_what_commit_destination_can_change():
    """The same pin as the stamp set: only what ``commit_destination`` writes."""
    source = inspect.getsource(commit_destination)
    assigned = set(re.findall(r'result\["([a-z_]+)"\]\s*=', source))
    assert assigned == DERIVED_WARNING_FIELDS


def test_a_pre_write_hook_that_changes_a_frozen_field_writes_nothing(
    tmp_path, lifecycle_on,
):
    """The post-hook guard: a non-derived change is refused before the write."""
    ledger = _ledger(tmp_path)
    prepared, _intent_path = _freeze(ledger, _line(warnings=[dict(CODEX_WARNING)]))
    ledger.parent.mkdir(parents=True, exist_ok=True)

    def rewrite_the_message(receipt: dict) -> None:
        receipt["warnings"][0]["message"] = "changed after the freeze"

    with pytest.raises(AppendReceiptError) as excinfo:
        _write_receipt_under_lock(
            copy.deepcopy(prepared), ledger, _cache_file_for(ledger),
            "k3b-post-hook-guard", 300, pre_write_hook=rewrite_the_message,
        )
    assert excinfo.value.code == "end_line_mismatch"
    assert _lines(ledger) == []
