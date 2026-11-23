"""Prevention-rule store: locked, atomic, a decided rule never returns to pending."""
from __future__ import annotations

import json
import multiprocessing
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import prevention_rule_store as store  # noqa: E402

NOW = "2026-11-10T12:00:00+00:00"


def _proposal(pattern="pat", terminal="T1", agent="", failure_class="timeout", **extra):
    base = {
        "pattern": pattern,
        "terminal_constraint": terminal,
        "agent_constraint": agent,
        "failure_class": failure_class,
        "prevention": "prevent it",
        "confidence": 0.8,
        "occurrence_count": 2,
        "source_dispatch_ids": ["d-1"],
    }
    base.update(extra)
    return base


def _queue(state_dir, *proposals):
    return store.queue_rules(state_dir, list(proposals), project_id="vnx-dev", now=NOW)


def _qpath(state_dir) -> Path:
    return Path(state_dir) / store.QUEUE_FILENAME


UNREADABLE = {
    "invalid_json": "{not json",
    "approved_then_truncated": json.dumps({"pending_rules": [{"id": "rule-aaaaaaaa", "status": "approved"}]})[:-2]
    + ', {"id": "rule-bb',
    "not_an_object": "[1, 2]",
    "rules_not_a_list": json.dumps({"pending_rules": {"a": 1}}),
    "entry_not_an_object": json.dumps({"pending_rules": ["x"]}),
}


@pytest.mark.parametrize("content", list(UNREADABLE.values()), ids=list(UNREADABLE))
def test_unreadable_queue_raises_and_stays_byte_equal(tmp_path, content):
    _qpath(tmp_path).write_text(content)
    before = _qpath(tmp_path).read_bytes()
    with pytest.raises(store.QueueUnreadable) as err:
        _queue(tmp_path, _proposal())
    assert str(_qpath(tmp_path)) in str(err.value)
    assert isinstance(err.value, ValueError)
    assert _qpath(tmp_path).read_bytes() == before
    with pytest.raises(store.QueueUnreadable):
        store.load_queue(tmp_path)


def _worker(state_dir, pattern, rounds, start):
    start.wait()
    for i in range(rounds):
        store.queue_rules(state_dir, [_proposal(pattern=f"{pattern}-{i}")], project_id="vnx-dev", now=NOW)


def test_two_processes_queue_concurrently_without_loss(tmp_path):
    rounds = 10
    ctx = multiprocessing.get_context("fork")
    start = ctx.Event()
    procs = [ctx.Process(target=_worker, args=(str(tmp_path), name, rounds, start)) for name in ("a", "b")]
    for proc in procs:
        proc.start()
    start.set()
    for proc in procs:
        proc.join(60)
        assert proc.exitcode == 0
    rules = json.loads(_qpath(tmp_path).read_text())["pending_rules"]
    assert sorted(r["pattern"] for r in rules) == sorted(f"{n}-{i}" for n in ("a", "b") for i in range(rounds))


def test_failing_replace_keeps_old_file_and_leaves_no_temp(tmp_path):
    _queue(tmp_path, _proposal(pattern="first"))
    before = _qpath(tmp_path).read_bytes()
    with patch.object(store.os, "replace", side_effect=OSError("disk gone")):
        with pytest.raises(OSError):
            _queue(tmp_path, _proposal(pattern="second"))
    assert _qpath(tmp_path).read_bytes() == before
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


@pytest.mark.parametrize("status", sorted(store.DECIDED_STATUSES))
def test_decided_entry_is_byte_equal_on_requeue(tmp_path, status):
    _queue(tmp_path, _proposal())
    rules = store.load_queue(tmp_path)
    rules[0]["status"] = status
    _qpath(tmp_path).write_text(json.dumps({"pending_rules": rules}, indent=2))
    before = _qpath(tmp_path).read_bytes()
    counts = _queue(tmp_path, _proposal(source_dispatch_ids=["d-9"], occurrence_count=9))
    assert counts == {"added": 0, "merged": 0, "unchanged": 1}
    assert _qpath(tmp_path).read_bytes() == before


def test_pending_entry_grows_evidence_and_keeps_created_at(tmp_path):
    first = store.queue_rules(tmp_path, [_proposal(source_dispatch_ids=["d-b", "d-a"])], project_id="p", now=NOW)
    assert first == {"added": 1, "merged": 0, "unchanged": 0}
    more = [f"d-{i:02d}" for i in range(30)]
    counts = store.queue_rules(
        tmp_path, [_proposal(source_dispatch_ids=more, occurrence_count=5)], project_id="p", now="later"
    )
    assert counts == {"added": 0, "merged": 1, "unchanged": 0}
    (entry,) = store.load_queue(tmp_path)
    assert entry["created_at"] == NOW
    assert entry["occurrence_count"] == 5
    assert entry["source_dispatch_ids"] == sorted(set(more) | {"d-a", "d-b"})[:20]
    assert len(entry["source_dispatch_ids"]) == 20
    assert entry["status"] == "pending"
    again = store.queue_rules(tmp_path, [_proposal(occurrence_count=1, source_dispatch_ids=["d-a"])], project_id="p", now="x")
    assert again == {"added": 0, "merged": 0, "unchanged": 1}


def test_entry_fields(tmp_path):
    _queue(tmp_path, _proposal())
    (entry,) = store.load_queue(tmp_path)
    assert entry["schema"] == "pending_rule/1"
    assert entry["project_id"] == "vnx-dev"
    assert entry["id"] == store.rule_id(_proposal())
    assert entry["proposal_hash"] == store.proposal_hash(_proposal())
    assert entry["source"] == "learning_loop" and entry["agent_constraint"] is None
    assert json.loads(_qpath(tmp_path).read_text()).keys() == {"pending_rules"}


@pytest.mark.parametrize(
    "rule",
    [
        {"id": "rule-x", "status": "weird", "schema": "pending_rule/1"},
        {"id": "rule-x", "status": "pending"},
        {"id": "rule-x", "status": None, "schema": "pending_rule/1"},
        {"id": "rule-x", "status": "pending", "schema": "pending_rule/2"},
        {"id": "rule-x", "status": "Pending", "schema": "pending_rule/1"},
    ],
    ids=["unknown_status", "no_schema", "status_none", "unknown_schema", "case_variant"],
)
def test_unknown_status_or_missing_schema_stays_byte_equal(tmp_path, rule):
    rule = dict(rule, id=store.rule_id(_proposal()))
    _qpath(tmp_path).write_text(json.dumps({"pending_rules": [rule]}))
    before = _qpath(tmp_path).read_bytes()
    counts = _queue(tmp_path, _proposal(source_dispatch_ids=["d-9"]))
    assert counts == {"added": 0, "merged": 0, "unchanged": 1}
    assert _qpath(tmp_path).read_bytes() == before


def test_rule_id_is_deterministic_and_covers_four_fields():
    base = _proposal()
    assert store.rule_id(base) == store.rule_id(dict(base)) and store.rule_id(base).startswith("rule-")
    variants = [
        _proposal(pattern="other"),
        _proposal(terminal="T2"),
        _proposal(agent="kimi"),
        _proposal(failure_class="crash"),
    ]
    ids = {store.rule_id(v) for v in variants} | {store.rule_id(base)}
    assert len(ids) == 5
    assert store.rule_id(_proposal(pattern="ab", terminal="c")) != store.rule_id(_proposal(pattern="a", terminal="bc"))
    assert store.rule_id(_proposal(terminal=None)) == store.rule_id(_proposal(terminal="any"))


def test_proposal_hash_changes_with_each_bound_field():
    base = _proposal()
    hashes = {store.proposal_hash(base)}
    for change in (
        {"pattern": "o"}, {"terminal_constraint": "T9"}, {"agent_constraint": "a"},
        {"prevention": "o"}, {"failure_class": "o"},
    ):
        hashes.add(store.proposal_hash(dict(base, **change)))
    assert len(hashes) == 6
    assert store.proposal_hash(base) == store.proposal_hash(dict(base, confidence=0.1))


@pytest.mark.parametrize("project_id", [None, "", 7, "  "])
def test_bad_project_id_raises_before_any_file_is_touched(tmp_path, project_id):
    target = tmp_path / "state"
    with pytest.raises(ValueError):
        store.queue_rules(target, [_proposal()], project_id=project_id, now=NOW)
    assert not target.exists()


@pytest.mark.parametrize(
    "bad",
    [
        {"pattern": ""}, {"prevention": None}, {"confidence": None}, {"confidence": True},
        {"occurrence_count": 0}, {"occurrence_count": "2"}, {"source_dispatch_ids": "d-1"},
    ],
)
def test_malformed_proposal_raises_and_writes_nothing(tmp_path, bad):
    with pytest.raises(ValueError):
        _queue(tmp_path, _proposal(pattern="ok"), _proposal(**bad))
    assert not _qpath(tmp_path).exists()


def test_module_has_no_database_access():
    source = (REPO_ROOT / "scripts" / "lib" / "prevention_rule_store.py").read_text()
    assert "sqlite3" not in source and "execute(" not in source


def test_missing_queue_loads_as_empty(tmp_path):
    assert store.load_queue(tmp_path) == []


def test_open_item_fields_are_carried_through(tmp_path):
    _queue(tmp_path, _proposal(failure_class="", signal="open_items", source_oi_ids=["OI-1", "OI-2"]))
    row = store.load_queue(tmp_path)[0]
    assert (row["signal"], row["source_oi_ids"]) == ("open_items", ["OI-1", "OI-2"])


def test_absent_open_item_fields_stay_absent(tmp_path):
    _queue(tmp_path, _proposal())
    assert not {"signal", "source_oi_ids"} & set(store.load_queue(tmp_path)[0])


@pytest.mark.parametrize("bad", [{"signal": ""}, {"source_oi_ids": "OI-1"}])
def test_malformed_open_item_fields_raise_and_write_nothing(tmp_path, bad):
    with pytest.raises(ValueError):
        _queue(tmp_path, _proposal(**bad))
    assert not _qpath(tmp_path).exists()


def test_existing_row_is_unchanged_by_the_optional_fields(tmp_path):
    base = _proposal()
    _queue(tmp_path, base)
    first = _qpath(tmp_path).read_bytes()
    assert store.rule_id(base) == store.rule_id({**base, "signal": "x", "source_oi_ids": ["OI-9"]})
    assert store.proposal_hash(base) == store.proposal_hash({**base, "signal": "x", "source_oi_ids": ["OI-9"]})
    counts = _queue(tmp_path, {**base, "signal": "x", "source_oi_ids": ["OI-9"]})
    assert counts == {"added": 0, "merged": 0, "unchanged": 1}
    assert _qpath(tmp_path).read_bytes() == first
