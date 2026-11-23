"""Deltareview DR-3 (ADR-064 besluit 4): the parent chain as optional fields on RoundLine and the request record."""
from __future__ import annotations

import json
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List

import pytest

import review_stack_support  # noqa: F401  (puts scripts/ and scripts/lib on sys.path)
from review_stack_support import BRANCH, PR, isolate_config_registry

HEAD_A, HEAD_B = "a" * 40, "b" * 40
CHAIN: Dict[str, Any] = {
    "scope": "delta", "parent_head_sha": HEAD_A, "parent_round_ref": "round-1", "delta_base": HEAD_A,
    "changekind": "small_fix", "patch_id": "p" * 40, "full_review_reason": "", "finding_ids": ["F1", "F2"],
}
LEGACY_KEYS = {
    "project_id", "pr_number", "head_sha", "seat", "reviewer", "takeover_from", "verdict", "blocking_count",
    "report_path", "dispatch_id", "recorded_at", "branch", "source",
}


@pytest.fixture(autouse=True)
def _clean_config_registry(monkeypatch):
    yield from isolate_config_registry(monkeypatch)


def _line(head: str = HEAD_A, **extra: Any) -> Any:
    import review_rounds

    return review_rounds.RoundLine(
        project_id="p", pr_number=PR, head_sha=head, seat=review_rounds.CODEX_SEAT, reviewer="codex_gate",
        takeover_from="", verdict="pass", blocking_count=0, report_path="/r/x.md", dispatch_id="d-1",
        recorded_at="2026-11-12T00:00:00Z", branch=BRANCH, source="gate", **extra)


def _opened() -> Dict[str, Any]:
    return {"kind": "opened", "project_id": "p", "pr_number": PR, "basis": "new", "evidence": 0,
            "recorded_at": "2026-11-12T00:00:00Z", "source": "gate"}


def _write(path: Path, lines: List[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(x.to_dict() if hasattr(x, "to_dict") else x) + "\n" for x in lines))


def test_a_line_with_the_parent_chain_round_trips():
    import review_rounds

    line = _line(**{**CHAIN, "finding_ids": tuple(CHAIN["finding_ids"])})
    record = line.to_dict()
    assert record["parent_head_sha"] == HEAD_A and record["finding_ids"] == ["F1", "F2"]
    assert "full_review_reason" not in record  # empty is not written
    back = review_rounds.RoundLine.from_dict(json.loads(json.dumps(record)))
    assert back.parent_head_sha == HEAD_A and back.finding_ids == ("F1", "F2") and back.patch_id == "p" * 40


def test_a_line_without_the_fields_is_the_line_of_today():
    import review_rounds

    record = _line().to_dict()
    assert set(record) == LEGACY_KEYS
    back = review_rounds.RoundLine.from_dict(record)
    assert back == _line() and back.finding_ids == () and back.parent_head_sha == ""


@pytest.mark.parametrize("name,value", [("parent_head_sha", 7), ("finding_ids", "F1"), ("finding_ids", [1]), ("scope", ["x"])])
def test_a_malformed_chain_field_is_no_line(name, value):
    import review_rounds

    with pytest.raises(ValueError):
        review_rounds.RoundLine.from_dict({**_line().to_dict(), name: value})


def test_read_ledger_and_the_door_accept_a_line_with_the_fields(tmp_path):
    import review_rounds
    import review_seat_evaluation as rse

    state = tmp_path / "state"
    results = state / "review_gates" / "results"
    results.mkdir(parents=True)
    project = review_rounds.store_project_id(state / "review_gates")
    opened = {**_opened(), "project_id": project}
    line = _line(**{**CHAIN, "finding_ids": ("F1",)})
    line = review_rounds.RoundLine.from_dict({**line.to_dict(), "project_id": project})
    path = review_rounds.ledger_path(state / "review_gates")
    _write(path, [opened, line])

    ledger = review_rounds.read_ledger(path)
    assert ledger.for_pr(project, PR)[0].parent_head_sha == HEAD_A
    rounds = rse.door_rounds(rse.SeatScope(pr_id=str(PR), results_dir=results, head_sha=HEAD_A, branch=BRANCH))
    assert rounds is not None and rounds.refusal == "" and len(rounds.lines) == 1
    assert rounds.lines[0].changekind == "small_fix"


def test_an_unknown_kind_still_refuses_the_pr(tmp_path):
    import review_rounds
    import review_seat_evaluation as rse

    state = tmp_path / "state"
    results = state / "review_gates" / "results"
    results.mkdir(parents=True)
    project = review_rounds.store_project_id(state / "review_gates")
    path = review_rounds.ledger_path(state / "review_gates")
    _write(path, [{**_opened(), "project_id": project},
                  {"kind": "delta", "project_id": project, "pr_number": PR, "parent_head_sha": HEAD_A}])
    rounds = rse.door_rounds(rse.SeatScope(pr_id=str(PR), results_dir=results, head_sha=HEAD_A, branch=BRANCH))
    assert rounds is not None and "telt deze PR niet" in rounds.refusal


def test_the_patch_id_is_in_the_line_before_the_lock_is_taken(tmp_path, monkeypatch):
    import atomic_io
    import review_rounds

    taken: List[str] = []

    @contextmanager
    def failing_lock(path):
        taken.append("lock")
        raise OSError("lock unavailable")
        yield  # pragma: no cover

    def no_git(*args, **kwargs):
        raise AssertionError("git ran under the ledger lock")

    monkeypatch.setattr(atomic_io, "slot_lock", failing_lock)
    monkeypatch.setattr(subprocess, "run", no_git)
    line = _line(patch_id="c" * 40)  # the caller computed it before: the writer takes it as data
    assert line.to_dict()["patch_id"] == "c" * 40
    with pytest.raises(review_rounds.RoundLedgerError):
        review_rounds.append_verdict(tmp_path / "rounds.ndjson", line)
    assert taken == ["lock"] and line.patch_id == "c" * 40


def test_line_from_payload_takes_the_chain_from_the_payload_only():
    import review_rounds

    payload = {
        "gate": "codex_gate", "pr_number": PR, "status": "pass", "commit_sha": HEAD_B, "contract_hash": "sha256:c",
        "report_path": "/r/x.md", "blocking_findings": [], **CHAIN,
    }
    line = review_rounds.line_from_payload(payload, gate="codex_gate", project_id="p")
    assert line is not None and line.parent_head_sha == HEAD_A and line.finding_ids == ("F1", "F2")
    plain = review_rounds.line_from_payload({k: v for k, v in payload.items() if k not in CHAIN}, gate="codex_gate", project_id="p")
    assert plain is not None and set(plain.to_dict()) == LEGACY_KEYS


def test_a_takeover_keeps_takeover_from_and_never_inherits_a_verdict_from_another_head(tmp_path):
    import review_rounds

    payload = {
        "gate": "deepseek_gate", "pr_number": PR, "status": "pass", "commit_sha": HEAD_B, "contract_hash": "sha256:d",
        "report_path": "/r/d.md", "blocking_findings": [], "seat": "codex_gate", "takeover": True,
        "takeover_from": "kimi_gate",
    }
    line = review_rounds.line_from_payload(payload, gate="deepseek_gate", project_id="p")
    assert line is not None and line.takeover_from == "kimi_gate" and line.head_sha == HEAD_B
    assert line.parent_head_sha == "" and "parent_head_sha" not in line.to_dict()
    # A chain on head A does not fill the seat on head B.
    path = tmp_path / "review_gates" / "rounds.ndjson"
    _write(path, [{**_opened(), "project_id": "p"}, _line(HEAD_A, **CHAIN)])
    ledger = review_rounds.read_ledger(path)
    assert ledger.line_for(("p", PR, HEAD_B, review_rounds.CODEX_SEAT)) is None


def test_the_request_record_carries_the_chain_only_when_set(tmp_path):
    from gate_request_handler import GateRequestHandlerMixin

    handler = GateRequestHandlerMixin()
    with_chain, without = tmp_path / "a.json", tmp_path / "b.json"
    handler._write_request_record(with_chain, {"gate": "codex_gate", "pr_number": PR, **CHAIN})
    handler._write_request_record(without, {"gate": "codex_gate", "pr_number": PR, "scope": "", "finding_ids": []})
    record = json.loads(with_chain.read_text())
    assert record["parent_head_sha"] == HEAD_A and record["finding_ids"] == ["F1", "F2"] and "full_review_reason" not in record
    plain = json.loads(without.read_text())
    assert not set(plain) & set(CHAIN)
    assert plain == {"gate": "codex_gate", "pr_number": PR, "origin": "door"}
    with pytest.raises(ValueError):
        handler._write_request_record(tmp_path / "c.json", {"gate": "codex_gate", "parent_head_sha": 5})
