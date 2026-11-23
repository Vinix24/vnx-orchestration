"""operator_queue_dream_analyzer: readers for source 3 and 4 on tmp_path stores (ADR-076 decisions 3, 5, 7)."""

import json

import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import operator_queue as oq
import operator_queue_dream_analyzer as qa

P = "vnx-test"
NOW = "2026-11-15T10:00:00Z"
CYCLE = "cyc-1"


class Store:
    def __init__(self, tmp_path):
        self.root = tmp_path / "store"
        self.state = self.root / "state"
        self.queue = tmp_path / "queue"
        (self.state / "dream").mkdir(parents=True)
        (self.root / "events" / "dream").mkdir(parents=True)

    def kw(self):
        return dict(data_root=self.root, now=NOW, state_dir=self.queue)

    def review(self, flag=True, cycle=CYCLE):
        path = self.state / "dream" / f"{cycle}{qa.REVIEW_SUFFIX}.json"
        path.write_text(json.dumps({"project_id": P, "cycle_id": cycle, "requires_operator_review": flag,
                                    "created_at": "2026-11-10T10:00:00Z"}))
        return path

    def event(self, etype="dream_cycle_approved", **extra):
        with open(self.root / "events" / "dream" / "e.ndjson", "a") as handle:
            handle.write(json.dumps({"event_type": etype, "cycle_id": CYCLE, "timestamp": "2026-11-14T10:00:00Z", **extra}) + "\n")

    def put(self, name, payload):
        (self.state / name).write_text(json.dumps(payload))

    def edits(self, *rows):
        self.put("pending_edits.json", {"edits": list(rows)})

    def points(self):
        return list(oq.read_state(P, state_dir=self.queue)["points"].values())

    def ledger(self):
        path = self.queue / oq.LEDGER_FILENAME
        return path.read_text().count("\n") if path.exists() else 0


def test_pending_review_opens_one_point_and_twice_writes_one_opened(tmp_path):
    s = Store(tmp_path)
    s.review()
    first = qa.read_dream(P, **s.kw())
    second = qa.read_dream(P, **s.kw())
    assert len(first["opened"]) == 1 and second["opened"] == []
    assert first["dekking"] == "gelezen" and first["bron"] == 3
    assert s.ledger() == 1 and s.points()[0]["ref"] == f"dream:{CYCLE}-pending-review"


def test_review_false_with_event_closes_with_event_id_and_actor(tmp_path):
    s = Store(tmp_path)
    s.review()
    qa.read_dream(P, **s.kw())
    s.review(False)
    s.event(event_id="ev-77", actor="vincent")
    out = qa.read_dream(P, **s.kw())
    point = s.points()[0]
    assert out["closed"] == [point["point_id"]] and point["state"] == "beantwoord" and point["bewijs"] == "ev-77"
    assert qa.read_dream(P, **s.kw())["closed"] == []


def test_review_false_without_event_stays_open_as_unknown(tmp_path):
    s = Store(tmp_path)
    s.review()
    qa.read_dream(P, **s.kw())
    s.review(False)
    out = qa.read_dream(P, **s.kw())
    assert out["closed"] == [] and len(out["unknown"]) == 1 and s.points()[0]["state"] == "open"


def test_review_back_to_true_opens_revision_two_and_keeps_old_closed(tmp_path):
    s = Store(tmp_path)
    s.review()
    qa.read_dream(P, **s.kw())
    s.review(False)
    s.event()
    qa.read_dream(P, **s.kw())
    s.review(True)
    out = qa.read_dream(P, **s.kw())
    revs = {p["revisie"]: p["state"] for p in s.points()}
    assert len(out["opened"]) == 1 and revs == {1: "beantwoord", 2: "open"}


def test_vanished_review_closes_on_event_else_unknown(tmp_path):
    s = Store(tmp_path)
    path = s.review()
    qa.read_dream(P, **s.kw())
    path.unlink()
    out = qa.read_dream(P, **s.kw())
    assert out["closed"] == [] and s.points()[0]["unknown_reden"] == "bestand weg"
    s.event(etype="dream_cycle_rejected")
    assert len(qa.read_dream(P, **s.kw())["closed"]) == 1


def test_unreadable_review_is_unknown_with_reason_and_closes_nothing(tmp_path):
    s = Store(tmp_path)
    path = s.review()
    qa.read_dream(P, **s.kw())
    path.write_text("{not json")
    out = qa.read_dream(P, **s.kw())
    assert out["closed"] == [] and len(out["unknown"]) == 1
    assert out["dekking"] == "unknown" and "onleesbaar" in out["reden"]
    assert s.points()[0]["state"] == "open" and "onleesbaar" in s.points()[0]["unknown_reden"]


def test_review_without_flag_or_with_unknown_value_never_closes(tmp_path):
    s = Store(tmp_path)
    path = s.review()
    qa.read_dream(P, **s.kw())
    s.event(event_id="ev-1")
    for value in ({"project_id": P, "cycle_id": CYCLE}, {"project_id": P, "cycle_id": CYCLE, "requires_operator_review": None}):
        path.write_text(json.dumps(value))
        assert qa.read_dream(P, **s.kw())["closed"] == []
    assert s.points()[0]["state"] == "open"


def test_pending_edit_opens_and_rejected_closes_with_time(tmp_path):
    s = Store(tmp_path)
    s.edits({"id": 4, "status": "pending", "category": "memory"})
    first = qa.read_analyzer(P, **s.kw())
    assert len(first["opened"]) == 1 and qa.read_analyzer(P, **s.kw())["opened"] == []
    s.edits({"id": 4, "status": "rejected", "rejected_at": "2026-11-14T12:00:00Z"})
    out = qa.read_analyzer(P, **s.kw())
    point = s.points()[0]
    assert len(out["closed"]) == 1 and point["state"] == "beantwoord"
    assert "status=rejected" in point["bewijs"] and "2026-11-14T12:00:00Z" in point["bewijs"]
    assert point["ref"] == "edit:4"


def test_pending_archival_opens_and_decided_closes(tmp_path):
    s = Store(tmp_path)
    s.edits()
    s.put("pending_archival.json", {"pending_archival": [{"pattern_id": "pat-9", "title": "old", "status": "pending"}]})
    assert len(qa.read_analyzer(P, **s.kw())["opened"]) == 1
    s.put("pending_archival.json", {"pending_archival": [{"pattern_id": "pat-9", "status": "approved", "decided_at": "2026-11-14T09:00:00Z"}]})
    out = qa.read_analyzer(P, **s.kw())
    assert len(out["closed"]) == 1 and "decided_at=2026-11-14T09:00:00Z" in s.points()[0]["bewijs"]


def test_edit_vanishing_with_history_entry_is_beantwoord(tmp_path):
    s = Store(tmp_path)
    s.edits({"id": 5, "status": "pending", "category": "rule"})
    qa.read_analyzer(P, **s.kw())
    s.edits()
    s.put("edit_history.json", [{"id": 5, "status": "applied", "applied_at": "2026-11-14T08:00:00Z"}])
    out = qa.read_analyzer(P, **s.kw())
    assert len(out["closed"]) == 1 and "edit_history.json" in s.points()[0]["bewijs"]


def test_edit_vanishing_without_history_entry_is_unknown(tmp_path):
    s = Store(tmp_path)
    s.edits({"id": 6, "status": "pending", "category": "rule"})
    qa.read_analyzer(P, **s.kw())
    s.edits()
    out = qa.read_analyzer(P, **s.kw())
    assert out["closed"] == [] and len(out["unknown"]) == 1
    assert s.points()[0]["unknown_reden"] == "edit weg zonder edit_history" and s.points()[0]["state"] == "open"


_NO_STATUS = object()


@pytest.mark.parametrize("status,closes", [(_NO_STATUS, False), ("unknown", False), ("pending", False), ("", False),
                                           (42, False), (None, False), ("applied", True)])
def test_history_entry_closes_only_on_a_decision_status(tmp_path, status, closes):
    s = Store(tmp_path)
    s.edits({"id": 8, "status": "pending", "category": "rule"})
    qa.read_analyzer(P, **s.kw())
    s.edits()
    entry = {"id": 8, "applied_at": "2026-11-14T08:00:00Z"}
    if status is not _NO_STATUS:
        entry["status"] = status
    s.put("edit_history.json", [entry])
    out = qa.read_analyzer(P, **s.kw())
    assert bool(out["closed"]) is closes
    if not closes:
        reden = s.points()[0]["unknown_reden"]
        assert s.points()[0]["state"] == "open" and "edit_history.json: id=8" in reden
        assert repr(entry.get("status")) in reden or "ontbreekt" in reden


def test_edit_with_missing_or_unknown_status_never_closes(tmp_path):
    s = Store(tmp_path)
    s.edits({"id": 7, "status": "pending", "category": "rule"})
    qa.read_analyzer(P, **s.kw())
    for row in ({"id": 7}, {"id": 7, "status": None}):
        s.edits(row)
        assert qa.read_analyzer(P, **s.kw())["closed"] == []
    assert s.points()[0]["state"] == "open"


@pytest.mark.parametrize("kind", ["edit", "archival"])
@pytest.mark.parametrize("status,closes", [("unknown", False), ("weird", False), ("PENDING ", False), ("Rejected", False),
                                           ("stale", False), ("accepted", True), ("approved", True)])
def test_only_a_recognised_decision_status_closes(tmp_path, kind, status, closes):
    s = Store(tmp_path)
    if kind == "edit":
        s.edits({"id": 9, "status": "pending", "category": "rule"})
    else:
        s.edits()
        s.put("pending_archival.json", {"pending_archival": [{"pattern_id": "p9", "status": "pending"}]})
    qa.read_analyzer(P, **s.kw())
    if kind == "edit":
        s.edits({"id": 9, "status": status})
    else:
        s.put("pending_archival.json", {"pending_archival": [{"pattern_id": "p9", "status": status}]})
    out = qa.read_analyzer(P, **s.kw())
    assert bool(out["closed"]) is closes
    if not closes:
        assert s.points()[0]["state"] == "open" and repr(status) in s.points()[0]["unknown_reden"]


def test_no_pending_edits_file_is_unknown_coverage_not_gelezen(tmp_path):
    s = Store(tmp_path)
    out = qa.read_analyzer(P, **s.kw())
    assert out["dekking"] == "unknown" and out["reden"] == "geen pending_edits.json" and out["opened"] == []


def test_reminder_rounds_never_return_a_closed_point(tmp_path):
    s = Store(tmp_path)
    path = s.review()
    s.edits({"id": 8, "status": "pending", "category": "rule"})
    assert [name for name, _ in qa.READERS] == ["dream", "analyzer"]
    snapshot = {p: p.read_bytes() for p in (path, s.state / "pending_edits.json")}
    for _, reader in qa.READERS:
        reader(P, **s.kw())
    assert {p: p.read_bytes() for p in snapshot} == snapshot  # a reader never writes into its source
    s.review(False)
    s.event(event_id="ev-9")
    s.edits({"id": 8, "status": "rejected", "rejected_at": "2026-11-14T12:00:00Z"})
    for _, reader in qa.READERS:
        reader(P, **s.kw())
    assert {p["state"] for p in s.points()} == {"beantwoord"}
    before = s.ledger()
    for _ in range(3):
        for _, reader in qa.READERS:
            out = reader(P, **s.kw())
            assert out["opened"] == out["closed"] == out["unknown"] == []
        assert [p for p in s.points() if p["state"] == "open"] == []
    assert s.ledger() == before


def test_approved_edit_closes_the_point_as_a_decision(tmp_path):
    s = Store(tmp_path)
    s.edits({"id": 6, "status": "pending", "category": "role"})
    assert len(qa.read_analyzer(P, **s.kw())["opened"]) == 1
    s.edits({"id": 6, "status": "approved", "approved_at": "2026-11-19T09:00:00Z", "dispatch_id": "edit-6-abcd1234"})
    out = qa.read_analyzer(P, **s.kw())
    assert len(out["closed"]) == 1 and "status=approved" in s.points()[0]["bewijs"]


def test_applied_history_entry_with_applied_ref_closes_the_point(tmp_path):
    s = Store(tmp_path)
    s.edits({"id": 7, "status": "pending", "category": "role"})
    assert len(qa.read_analyzer(P, **s.kw())["opened"]) == 1
    s.edits()
    s.put("edit_history.json", [{"id": 7, "status": "applied", "applied_at": "2026-11-19T10:00:00Z",
                                 "applied_ref": "#3060@0123456789ab"}])
    out = qa.read_analyzer(P, **s.kw())
    assert len(out["closed"]) == 1 and "edit_history.json: id=7 status=applied" in s.points()[0]["bewijs"]
