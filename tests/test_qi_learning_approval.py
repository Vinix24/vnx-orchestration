"""qi_learning_approval on its own (ADR-068 besluit 18): every refusal writes nothing."""
from __future__ import annotations

import hashlib
import json
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

import qi_learning_approval as qla

APPROVERS = frozenset({"operator", "T0"})
ROWS = [{"table": "antipatterns", "title": "failure x", "occurrence_count": 3, "first_seen": "2026-11-10T08:00"},
        {"table": "success_patterns", "title": "approach y", "confidence_score": 0.8, "valid_from": "2026-11-10"}]
OTHER_ID = "learn-" + "0" * 64


@pytest.fixture()
def approved(tmp_path, monkeypatch):
    """An unused record for ROWS, selected through the env pointer."""
    proposal_id = qla.preview(ROWS)["proposal_id"]
    path = qla.write_record(tmp_path, proposal_id, "operator", "read the preview", APPROVERS)
    monkeypatch.setenv(qla.APPROVED_ENV, proposal_id)
    return tmp_path, path


def _snapshot(state: Path) -> dict:
    return {p: p.read_bytes() for p in sorted(state.rglob("*")) if p.is_file()}


def _events(state: Path, event_type: str) -> list:
    lines = (state / qla.USAGE_LOG_NAME).read_text(encoding="utf-8").splitlines()
    return [e for e in map(json.loads, lines) if e["event_type"] == event_type]


def _assert_refused(state: Path, match: str, rows=ROWS, call=None) -> None:
    before = _snapshot(state)
    with pytest.raises(qla.LearningApprovalRefused, match=match):
        (call or (lambda: qla.claim_approval(state, APPROVERS, rows)))()
    assert _snapshot(state) == before


@pytest.mark.parametrize("value, match", [(None, "is not set"), ("", "is not set"), ("learn-123", "no proposal id"),
                                          ("../" + OTHER_ID, "no proposal id"), (OTHER_ID.upper(), "no proposal id")])
def test_no_or_empty_or_malformed_id_refuses(approved, monkeypatch, value, match):
    if value is None:
        monkeypatch.delenv(qla.APPROVED_ENV)
    else:
        monkeypatch.setenv(qla.APPROVED_ENV, value)
    _assert_refused(approved[0], match)


@pytest.mark.parametrize("setup, match", [("none", "no approval record"), ("other", "no approval record"),
                                          ("directory", "no JSON")])
def test_missing_record_pointer_to_no_file_or_a_directory_refuses(tmp_path, monkeypatch, setup, match):
    if setup == "other":
        qla.write_record(tmp_path, qla.preview(ROWS)["proposal_id"], "operator", "other", APPROVERS)
    if setup == "directory":
        qla.record_path(tmp_path, OTHER_ID).mkdir(parents=True)
    monkeypatch.setenv(qla.APPROVED_ENV, OTHER_ID)
    _assert_refused(tmp_path, match)


@pytest.mark.parametrize("mutate, match", [
    (lambda r: b"{not json", "no JSON"),
    (lambda r: b"\xff\xfe", "no JSON"),
    (lambda r: b"[]", "fields"),
    (lambda r: {k: v for k, v in r.items() if k != "reason"}, "fields"),
    (lambda r: dict(r, signed_by="nobody"), "fields"),
    (lambda r: dict(r, proposal_id=OTHER_ID), "another proposal"),
    (lambda r: dict(r, changes_sha256="0" * 64), "another proposal"),
    (lambda r: dict(r, reason=None), "no reason"),
    (lambda r: dict(r, approved_at=None), "ISO approved_at"),
    (lambda r: dict(r, approved_at="yesterday"), "ISO approved_at"),
    (lambda r: dict(r, consumed_at="2026-11-10T09:00:00+00:00"), "already used"),
    (lambda r: dict(r, consumed_at=""), "already used"),
    (lambda r: dict(r, approved_by="intruder"), "approved_by"),
    (lambda r: dict(r, approved_by=None), "approved_by"),
])
def test_malformed_used_or_unknown_approver_record_refuses(approved, mutate, match):
    state, path = approved
    content = mutate(json.loads(path.read_text(encoding="utf-8")))
    path.write_bytes(content if isinstance(content, bytes) else json.dumps(content).encode("utf-8"))
    _assert_refused(state, match)


@pytest.mark.parametrize("rows, match", [([dict(ROWS[0], occurrence_count=4), ROWS[1]], "hash to"),
                                         (ROWS[:1], "hash to"), (ROWS + [{"title": "extra"}], "hash to"),
                                         ([dict(ROWS[0], pattern_data=object()), ROWS[1]], "no canonical JSON")])
def test_rows_other_than_the_approved_ones_refuse(approved, rows, match):
    _assert_refused(approved[0], match, rows)


def test_valid_record_claims_exactly_the_given_rows_once(approved):
    state, path = approved
    created = json.loads(path.read_text(encoding="utf-8"))
    later_clock = [dict(ROWS[1], valid_from="2026-11-11"), dict(ROWS[0], first_seen="2026-11-11T00:00")]
    record_sha256 = qla.claim_approval(state, APPROVERS, later_clock)
    canonical = json.dumps(created, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert record_sha256 == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    used = json.loads(path.read_text(encoding="utf-8"))
    assert used == dict(created, consumed_at=used["consumed_at"]) and used["consumed_at"]
    [receipt], [approval] = _events(state, "learning_persist"), _events(state, "learning_approval")
    assert receipt["approval_record_sha256"] == approval["approval_record_sha256"] == record_sha256
    assert [receipt[k] for k in ("changes_sha256", "rows", "timestamp")] == [created["changes_sha256"], 2, used["consumed_at"]]
    assert sorted(p.name for p in path.parent.iterdir()) == [f".{path.name}.lock", path.name]
    _assert_refused(state, "already used")


def test_record_changed_while_the_claim_waited_for_the_lock_refuses(approved, monkeypatch):
    state, path = approved
    real_lock = qla.slot_lock

    @contextmanager
    def lock_then_swap(target):
        with real_lock(target):
            path.write_text(json.dumps(dict(json.loads(path.read_text()), reason="swapped")), encoding="utf-8")
            yield

    monkeypatch.setattr(qla, "slot_lock", lock_then_swap)
    with pytest.raises(qla.LearningApprovalRefused, match="changed while"):
        qla.claim_approval(state, APPROVERS, ROWS)
    assert json.loads(path.read_text())["consumed_at"] is None and _events(state, "learning_persist") == []


@pytest.mark.parametrize("approved_by, reason, match", [
    ("intruder", "ok", "approved_by"), ("operator", " ", "non-empty"), ("operator", "again", "never overwritten")])
def test_write_record_refuses_without_writing(approved, approved_by, reason, match):
    state, path = approved
    _assert_refused(state, match, call=lambda: qla.write_record(state, path.stem, approved_by, reason, APPROVERS))
