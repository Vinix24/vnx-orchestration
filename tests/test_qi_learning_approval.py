"""qi_learning_approval, the read side (ADR-068 besluit 18): every refusal names why and writes nothing."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import qi_learning_approval as qla

APPROVERS = frozenset({"operator", "T0"})
ROWS = [{"table": "antipatterns", "title": "failure x", "occurrence_count": 3, "first_seen": "2026-11-10T08:00"},
        {"table": "success_patterns", "title": "approach y", "confidence_score": 0.8, "valid_from": "2026-11-10"}]
PROPOSAL_ID, OTHER_ID = qla.preview(ROWS)["proposal_id"], "learn-" + "0" * 64
PERSIST = b'{"event_type":"learning_persist","proposal_id":"%s"}' % PROPOSAL_ID.encode()


def _write_record(state: Path) -> Path:
    """The unused record for ROWS, as the approve CLI (split 3, with ``write_record``) writes it."""
    path = qla.record_path(state, PROPOSAL_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"proposal_id": PROPOSAL_ID, "changes_sha256": PROPOSAL_ID[6:], "reason": "read it",
                                "approved_by": "operator", "approved_at": "2026-11-10T08:00", "consumed_at": None}))
    return path


@pytest.fixture()
def approved(tmp_path, monkeypatch):
    """An unused record for ROWS, selected through the env pointer."""
    monkeypatch.setenv(qla.APPROVED_ENV, PROPOSAL_ID)
    return tmp_path, _write_record(tmp_path)


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _snapshot(state: Path) -> dict:
    return {p: p.read_bytes() for p in sorted(state.rglob("*")) if p.is_file()}


def _assert_refused(state: Path, match: str) -> None:
    before = _snapshot(state)
    with pytest.raises(qla.LearningApprovalRefused, match=match):
        qla.load_approval(state, APPROVERS)
    assert _snapshot(state) == before


def test_preview_drops_the_clock_fields_and_fixes_the_order():
    later_clock = [dict(ROWS[1], valid_from="2026-11-11"), dict(ROWS[0], first_seen="2026-11-11T00:00")]
    assert qla.preview(later_clock) == qla.preview(ROWS)
    rows = sorted(({k: v for k, v in r.items() if k not in ("first_seen", "valid_from")} for r in ROWS), key=_canonical)
    digest = hashlib.sha256(_canonical(rows).encode("utf-8")).hexdigest()
    assert qla.preview(ROWS) == {"proposal_id": f"learn-{digest}", "changes_sha256": digest, "changes": rows}
    assert qla.preview([dict(ROWS[0], occurrence_count=4), ROWS[1]])["proposal_id"] != PROPOSAL_ID


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
        _write_record(tmp_path)
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
    (lambda r: dict(r, changes_sha256=None), "another proposal"),
    (lambda r: dict(r, reason=None), "no reason"),
    (lambda r: dict(r, reason="  "), "no reason"),
    (lambda r: dict(r, approved_at=None), "ISO approved_at"),
    (lambda r: dict(r, approved_at="yesterday"), "ISO approved_at"),
    (lambda r: dict(r, consumed_at="2026-11-10T09:00:00+00:00"), "already used"),
    (lambda r: dict(r, consumed_at=""), "already used"),
    (lambda r: dict(r, consumed_at=False), "already used"),
    (lambda r: dict(r, approved_by="intruder"), "approved_by"),
    (lambda r: dict(r, approved_by=None), "approved_by"),
    (lambda r: dict(r, approved_by=["operator"]), "approved_by"),
])
def test_malformed_used_or_unknown_approver_record_refuses(approved, mutate, match):
    state, path = approved
    content = mutate(json.loads(path.read_text(encoding="utf-8")))
    path.write_bytes(content if isinstance(content, bytes) else json.dumps(content).encode("utf-8"))
    _assert_refused(state, match)


@pytest.mark.parametrize("log", [PERSIST + b"\n",  # the crash between the ledger line and the record
                                 b'{"event_type":"x"}\n' + PERSIST])  # only its newline lost: the decision is whole
def test_a_learning_persist_line_in_the_log_with_consumed_at_null_in_the_record_refuses(approved, log):
    state, path = approved
    (state / qla.USAGE_LOG_NAME).write_bytes(log)
    assert json.loads(path.read_text(encoding="utf-8"))["consumed_at"] is None
    _assert_refused(state, "already used")


@pytest.mark.parametrize("log, match", [
    (b'{"event_type":"learning_persist"}\n', "not trustworthy"),
    (b'{"event_type":"learning_persist","proposal_id":null}\n', "not trustworthy"),
    (b'{"event_type":"learning_persist","proposal_id":"learn-1"}', "not trustworthy"),
    (b'{"event_type":"learning_persist","proposal_id":["%s"]}\n' % PROPOSAL_ID.encode(), "not trustworthy"),
    (b'{"event_type":"x"}\n{not json\n{"event_type":"x"}\n', "not trustworthy"),
    (b'\n{"event_type":"x"}\n', "not trustworthy"),
    (b'[]\n', "not trustworthy"),
    (b'\xff\xfe\n', "not trustworthy"),
    (None, "unreadable"),
])
def test_an_untrustworthy_log_refuses(approved, log, match):
    state = approved[0]
    if log is None:
        (state / qla.USAGE_LOG_NAME).mkdir()
    else:
        (state / qla.USAGE_LOG_NAME).write_bytes(log)
    _assert_refused(state, match)


@pytest.mark.parametrize("log", [None,  # a missing log holds no claim
                                 b'{"event_type":"x"}\n' + PERSIST[:40],  # a torn last append: no decision on it
                                 PERSIST.replace(PROPOSAL_ID.encode(), OTHER_ID.encode()) + b"\n"])
def test_a_valid_record_loads_unchanged_when_the_log_holds_no_claim_for_it(approved, log):
    state, path = approved
    if log is not None:
        (state / qla.USAGE_LOG_NAME).write_bytes(log)
    before = _snapshot(state)
    assert qla.load_approval(state, APPROVERS) == json.loads(path.read_text(encoding="utf-8"))
    assert _snapshot(state) == before
