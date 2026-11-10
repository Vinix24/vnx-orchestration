"""qi_learning_approval (ADR-068 besluit 18): every refusal names why and writes nothing; a claim is ledger first."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import qi_learning_approval as qla
from atomic_io import slot_lock

APPROVERS = frozenset({"operator", "T0"})
ROWS = [{"table": "antipatterns", "title": "failure x", "occurrence_count": 3, "first_seen": "2026-11-10T08:00"},
        {"table": "success_patterns", "title": "approach y", "confidence_score": 0.8, "valid_from": "2026-11-10"}]
PROPOSAL_ID, OTHER_ID = qla.preview(ROWS)["proposal_id"], "learn-" + "0" * 64
PERSIST = b'{"event_type":"learning_persist","proposal_id":"%s"}' % PROPOSAL_ID.encode()
HEAD, TORN = b'{"event_type":"x"}\n', PERSIST[:-5]  # TORN: a learning_persist line for PROPOSAL_ID cut short


def _marker(fragment=TORN, **override) -> bytes:
    """The torn_tail_closed line for ``fragment`` right after HEAD, with ``override`` patched in."""
    marker = {"event_type": "torn_tail_closed", "offset": len(HEAD), "length": len(fragment),
              "sha256": hashlib.sha256(fragment).hexdigest()}
    return _canonical(dict(marker, **override)).encode() + b"\n"


def _write_record(state: Path) -> Path:
    """The unused record for ROWS, under its lock as the approve CLI (split 3, with ``write_record``) writes it."""
    path = qla.record_path(state, PROPOSAL_ID)
    with slot_lock(path):
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


def _fail(*_args):
    raise OSError(28, "No space left on device")


def _snapshot(state: Path) -> dict:
    return {p: p.read_bytes() for p in sorted(state.rglob("*")) if p.is_file()}


def _claims(state: Path) -> list:
    events = (qla._parsed(json.loads, line) for line in (state / qla.USAGE_LOG_NAME).read_bytes().splitlines())
    return [e for e in events if isinstance(e, dict) and e["event_type"] == "learning_persist"]


def _assert_claim_refused(state: Path, match: str, rows=ROWS) -> None:
    before = _snapshot(state)
    with pytest.raises(qla.LearningApprovalRefused, match=match):
        qla.claim_approval(state, APPROVERS, rows)
    assert _snapshot(state) == before


def _assert_refused(state: Path, match: str) -> None:
    """load_approval refuses, then claim_approval does too; neither writes a byte."""
    before = _snapshot(state)
    with pytest.raises(qla.LearningApprovalRefused, match=match):
        qla.load_approval(state, APPROVERS)
    assert _snapshot(state) == before
    _assert_claim_refused(state, match)


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


@pytest.mark.parametrize("rows, match", [([dict(ROWS[0], occurrence_count=4), ROWS[1]], "hash to"),
                                         (ROWS[:1], "hash to"), (ROWS + [{"title": "extra"}], "hash to"),
                                         ([dict(ROWS[0], pattern_data=object()), ROWS[1]], "no canonical JSON"),
                                         (None, "no canonical JSON")])
def test_rows_other_than_the_approved_ones_refuse(approved, rows, match):
    _assert_claim_refused(approved[0], match, rows)


def test_valid_record_claims_exactly_the_given_rows_once(approved):
    state, path = approved
    created = json.loads(path.read_text(encoding="utf-8"))
    later_clock = [dict(ROWS[1], valid_from="2026-11-11"), dict(ROWS[0], first_seen="2026-11-11T00:00")]
    record_sha256 = qla.claim_approval(state, APPROVERS, later_clock)
    assert record_sha256 == hashlib.sha256(_canonical(created).encode("utf-8")).hexdigest()
    used = json.loads(path.read_text(encoding="utf-8"))
    assert used == dict(created, consumed_at=used["consumed_at"]) and used["consumed_at"]
    [claim] = _claims(state)
    assert [claim[k] for k in ("approval_record_sha256", "changes_sha256", "rows", "timestamp")] == [
        record_sha256, created["changes_sha256"], 2, used["consumed_at"]]
    assert sorted(p.name for p in path.parent.iterdir()) == [f".{path.name}.lock", path.name]
    _assert_refused(state, "already used")


def test_record_changed_while_the_claim_waited_for_the_lock_refuses(approved, monkeypatch):
    state, path = approved
    real_lock = qla.slot_lock

    def swap_then_lock(target):
        path.write_text(json.dumps(dict(json.loads(path.read_text()), reason="swapped")), encoding="utf-8")
        return real_lock(target)

    monkeypatch.setattr(qla, "slot_lock", swap_then_lock)
    with pytest.raises(qla.LearningApprovalRefused, match="changed while"):
        qla.claim_approval(state, APPROVERS, ROWS)
    assert json.loads(path.read_text())["consumed_at"] is None and not (state / qla.USAGE_LOG_NAME).exists()


def test_a_failed_append_records_no_claim_and_the_approval_stays_usable(approved, monkeypatch):
    state, path = approved
    with monkeypatch.context() as patched:
        patched.setattr(qla, "_append_event", _fail)
        _assert_claim_refused(state, "record is untouched")  # the record stays byte-identical, no log appears
    qla.claim_approval(state, APPROVERS, ROWS)
    assert json.loads(path.read_text())["consumed_at"] and len(_claims(state)) == 1


def test_a_failed_record_write_after_the_ledger_line_leaves_the_approval_used(approved, monkeypatch):
    state, path = approved
    created = path.read_bytes()
    with monkeypatch.context() as patched:
        patched.setattr(qla, "atomic_write_json", _fail)
        with pytest.raises(qla.LearningApprovalRefused, match="counts as used"):
            qla.claim_approval(state, APPROVERS, ROWS)
    assert path.read_bytes() == created and len(_claims(state)) == 1
    _assert_refused(state, "already used")


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


@pytest.mark.parametrize("log", [b'{"event_type":"x"}\n' + PERSIST[:40],  # a torn last append: no decision on it
                                 PERSIST.replace(PROPOSAL_ID.encode(), OTHER_ID.encode()) + b"\n"])
def test_a_torn_last_append_or_another_proposals_claim_does_not_block_a_first_claim(approved, log):
    state = approved[0]
    (state / qla.USAGE_LOG_NAME).write_bytes(log)
    qla.claim_approval(state, APPROVERS, ROWS)
    assert json.loads((state / qla.USAGE_LOG_NAME).read_bytes().splitlines()[-1])["proposal_id"] == PROPOSAL_ID


@pytest.mark.parametrize("fragment", [b'{"event_type":"x","n":1', TORN,  # half an event; a torn persist for this one
                                      PERSIST.replace(PROPOSAL_ID.encode(), OTHER_ID.encode())[:-5],  # or another
                                      pytest.param(b'{"pad":"' + b"a" * (1 << 17), id="longer-than-one-read-chunk")])
def test_a_torn_tail_an_append_closed_leaves_an_approval_usable_exactly_once(approved, fragment):
    state = approved[0]
    log = state / qla.USAGE_LOG_NAME
    log.write_bytes(HEAD + fragment)
    qla._append_event(state, {"event_type": "x"})
    qla.claim_approval(state, APPROVERS, ROWS)
    assert log.read_bytes().startswith(HEAD + fragment + b"\n" + _marker(fragment) + HEAD)  # no byte removed
    assert [claim["proposal_id"] for claim in _claims(state)] == [PROPOSAL_ID]
    _assert_refused(state, "already used")


def test_two_torn_tails_in_a_row_each_closed_by_its_own_marker_still_read(approved):
    state = approved[0]
    log = state / qla.USAGE_LOG_NAME
    log.write_bytes(HEAD + TORN)
    qla._append_event(state, {"event_type": "x", "n": 1})
    with open(log, "r+b") as handle:  # that append torn too: its event line lost its last 5 bytes
        handle.truncate(log.stat().st_size - 5)
    qla.claim_approval(state, APPROVERS, ROWS)
    lines = log.read_bytes().splitlines()
    assert [json.loads(lines[n])["event_type"] for n in (2, 4)] == ["torn_tail_closed"] * 2
    assert json.loads(lines[4])["offset"] == len(b"\n".join(lines[:3])) + 1 and len(_claims(state)) == 1
    _assert_refused(state, "already used")


@pytest.mark.parametrize("log", [
    HEAD + TORN + b"\n" + _marker(sha256="0" * 64),
    HEAD + TORN + b"\n" + _marker(offset=0),
    HEAD + TORN + b"\n" + _marker(offset=float(len(HEAD))),  # 19.0 == 19 in Python: the type must be int too
    HEAD + TORN + b"\n" + _marker(length=1),
    HEAD + TORN + b"\n" + _marker(closes="anything"),
    HEAD + TORN + b"\n" + HEAD + _marker(),  # the marker must be the very next line
    HEAD + TORN + b"\n" + HEAD,  # a closed fragment and no marker at all
    HEAD + b"\n" + _marker(b""),  # an empty line is no torn tail
    _marker(offset=0),  # a marker on the first line closes nothing
])
def test_a_torn_tail_marker_that_does_not_match_the_line_before_it_refuses(approved, log):
    (approved[0] / qla.USAGE_LOG_NAME).write_bytes(log)
    _assert_refused(approved[0], "not trustworthy")
