"""P4 (track provider-agnostic-review-lane): `vnx pr-ready` declares a gate from the obligations
plus the request records the door wrote, and a loosely written request stays off the door.

ADR-046 Amendment 1, A6: the door stamps ``origin: "door"`` in ``_write_request_record``; the
field is compared exactly; a record without it is off the door; nothing is rewritten after the fact.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT / "scripts" / "lib", REPO_ROOT / "scripts"):
    sys.path.insert(0, str(_p))

import gate_recorder
import pr_ready
import pr_readiness as pr
from gate_obligations import obligations_dir
from gate_request_handler import GateRequestHandlerMixin
from gate_status import REQUEST_ORIGIN_DOOR, request_is_from_the_door


class _Handler(GateRequestHandlerMixin):
    """The mixin's request writer needs no state of its own."""


PR = 2500
HEAD = "c" * 40


def _request(state: Path, gate: str, **fields) -> Path:
    requests = state / "review_gates" / "requests"
    requests.mkdir(parents=True, exist_ok=True)
    record = {"gate": gate, "status": "completed", "provider": gate.split("_")[0], "pr_number": PR, **fields}
    path = requests / f"pr-{PR}-{gate}.json"
    path.write_text(json.dumps(record))
    return path


def _obligation(state: Path, gate: str) -> None:
    obligations_dir(state).mkdir(parents=True, exist_ok=True)
    record = {"schema_version": 1, "dispatch_id": f"d-{gate}", "gate": gate, "pr_number": PR, "pr_id": None}
    (obligations_dir(state) / f"d-{gate}.json").write_text(json.dumps(record))


def _fills(*seats: str):
    def fill(seat):
        return SimpleNamespace(
            seat=seat, filled=True, reviewer=seat, route="own", message="ok", record=None, evidence_via="",
        )
    return SimpleNamespace(
        required=SimpleNamespace(error=None), fills=[fill(s) for s in seats], blocking=[],
    )


@pytest.fixture()
def assessed(tmp_path, monkeypatch):
    """`assess` over a real state dir; only the forge (gh, CI) and the seat evaluation are replaced."""
    import review_seat_evaluation

    state = tmp_path / "state"
    monkeypatch.setattr(pr, "fetch_pr_facts", lambda *a, **k: {"headRefOid": HEAD, "headRefName": "b", "state": "OPEN"})
    monkeypatch.setattr(pr.ci_contexts, "evaluate_commit", lambda *a, **k: [])
    monkeypatch.setattr(
        review_seat_evaluation, "evaluate_review_seats", lambda *a, **k: _fills("codex_gate", "kimi_gate"),
    )
    return lambda: (pr.assess(PR, tmp_path, state), state)


def _by_gate(report):
    return {g.gate: g for g in report.gates}


def test_a_second_seat_the_door_requested_is_not_off_the_door(assessed, tmp_path):
    state = tmp_path / "state"
    _obligation(state, "codex_gate")
    _request(state, "codex_gate", origin="door")
    _request(state, "kimi_gate", origin="door")

    report, _ = assessed()

    assert "[off the door" not in pr_ready._gate_line(report)
    assert all(g.declared for g in report.gates)
    assert report.door_requested_gates == ["codex_gate", "kimi_gate"]


@pytest.mark.parametrize(
    "fields",
    [{}, {"origin": None}, {"origin": "Door"}, {"origin": " door"}, {"origin": ["door"]}, {"origin": 1}, {"origin": "shim"}],
    ids=["missing", "none", "case", "padded", "list", "number", "other"],
)
def test_a_loose_request_record_keeps_the_off_the_door_label(assessed, tmp_path, fields):
    state = tmp_path / "state"
    _obligation(state, "codex_gate")
    _request(state, "kimi_gate", **fields)

    report, _ = assessed()

    assert report.door_requested_gates == []
    assert _by_gate(report)["codex_gate"].declared is True
    assert _by_gate(report)["kimi_gate"].declared is False
    assert "kimi_gate" in pr_ready._gate_line(report)
    assert "[off the door" in pr_ready._gate_line(report)


def test_a_result_the_door_never_requested_stays_off_the_door(assessed, tmp_path):
    state = tmp_path / "state"
    _obligation(state, "codex_gate")
    _request(state, "codex_gate", origin="door")

    report, _ = assessed()

    assert _by_gate(report)["kimi_gate"].declared is False


def test_a_door_request_for_another_pr_declares_nothing_here(assessed, tmp_path):
    state = tmp_path / "state"
    _obligation(state, "codex_gate")
    _request(state, "kimi_gate", origin="door", pr_number=PR + 1)

    report, _ = assessed()

    assert report.door_requested_gates == []
    assert _by_gate(report)["kimi_gate"].declared is False


def test_an_unreadable_or_gateless_request_never_declares(tmp_path):
    requests = tmp_path / "review_gates" / "requests"
    requests.mkdir(parents=True)
    (requests / "pr-2500-kimi_gate.json").write_text("{not json")
    (requests / "pr-2500-glm_gate.json").write_text(json.dumps({"origin": "door", "pr_number": PR}))
    (requests / "pr-2500-deepseek_gate.json").write_text(json.dumps({"origin": "door", "pr_number": PR, "gate": 7}))
    (requests / "pr-2500-claude_gate.json").write_text(json.dumps(["origin", "door"]))
    (requests / "pr-2500-test_gate.json").write_text(
        json.dumps({"origin": "door", "pr_number": PR, "gate": "codex_gate", "test_run": True})
    )
    assert pr.door_requested_gates_for_pr(requests, PR) == []
    assert pr.door_requested_gates_for_pr(tmp_path / "missing", PR) == []


def test_a_request_naming_the_pr_by_pr_id_counts(tmp_path):
    requests = tmp_path / "requests"
    requests.mkdir()
    (requests / "a.json").write_text(json.dumps({"origin": "door", "pr_id": "PR-2500", "gate": "kimi_gate"}))
    assert pr.door_requested_gates_for_pr(requests, PR) == ["kimi_gate"]


@pytest.mark.parametrize("value", [None, "", "Door", "door ", 1, True, ["door"], {"door": 1}])
def test_the_origin_predicate_is_an_exact_match(value):
    assert request_is_from_the_door({"origin": value}) is False


def test_the_origin_predicate_accepts_only_door_on_an_object():
    assert request_is_from_the_door({"origin": "door"}) is True
    assert request_is_from_the_door({}) is False
    assert request_is_from_the_door(None) is False
    assert request_is_from_the_door("door") is False


def test_the_door_writer_stamps_the_origin(tmp_path):
    handler = _Handler()
    path = tmp_path / "pr-1-kimi_gate.json"
    payload = {"gate": "kimi_gate", "provider": "kimi"}

    handler._write_request_record(path, payload)

    written = json.loads(path.read_text())
    assert written["origin"] == REQUEST_ORIGIN_DOOR == "door"
    assert written["provider"] == "kimi"  # the provider labels stay byte-equal
    assert "origin" not in payload  # the receipt and the result mirror book this dict


@pytest.mark.parametrize("provider", ["kimi", "glm", "deepseek", "claude"])
def test_the_provider_label_survives_the_stamp(tmp_path, provider):
    handler = _Handler()
    path = tmp_path / f"pr-1-{provider}_gate.json"
    handler._write_request_record(path, {"gate": f"{provider}_gate", "provider": provider})
    assert json.loads(path.read_text())["provider"] == provider


def test_persist_request_carries_the_origin_through_a_rewrite(tmp_path):
    requests = tmp_path / "requests"
    requests.mkdir()
    handler = _Handler()
    path = gate_recorder.result_file_path(requests, "kimi_gate", PR, "")
    handler._write_request_record(path, {"gate": "kimi_gate", "status": "requested", "pr_number": PR})

    on_disk = gate_recorder.read_request_record(path).record
    ended = {**on_disk, "status": "completed"}  # what gate_outcome._finish_request builds
    gate_recorder.persist_request(requests, "kimi_gate", ended, pr_number=PR, pr_id="")

    assert request_is_from_the_door(gate_recorder.read_request_record(path).record)


def test_an_old_record_is_not_rewritten_by_a_read(tmp_path):
    requests = tmp_path / "requests"
    path = _request(tmp_path, "kimi_gate")
    before = path.read_text()
    pr.door_requested_gates_for_pr(path.parent, PR)
    assert path.read_text() == before
    assert "origin" not in json.loads(before)
