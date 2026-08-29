"""A verdict about another commit is not evidence about this one (OI-1488).

The overwrite guard preserves a decided, evidenced result rather than let a
later, less-decided write replace it (OI-1469/OI-1470). That is correct and is
not changed here: the record on disk stays, and the caller is still told the
true on-disk state, which is what that contract requires.

What was missing sat one level up. A run against a NEW head can come back
holding the OLD head's record, and that record does not announce itself: it is
complete and valid, every field populated, including a ``commit_sha`` for a
commit nobody asked about. Measured on PR #1705: a request for a verdict on
``1425faa1`` was answered with the full record of ``109181d2`` — findings,
duration, ``recorded_at``, ``has_required_failure``. That instance was
conservative because the preserved verdict was a FAIL. The mechanism is
symmetric, and a preserved PASS reads as a clean review of code that is no
longer there.

None of the seven headless-review invariants catches it. The request record
exists, the result record exists, ``contract_hash`` is non-empty, ``report_path``
is non-empty, the report is on disk, and the JSON and the report agree — with
each other, about the wrong commit. The only thing that caught it was a human
comparing the record's sha to the PR head. This makes that comparison a
mechanism.

Two changes, and the second is the load-bearing one. A refused write is
annotated so a caller can see its result never landed; and the consumer that
turns a result into ``decided_pass`` refuses to count a record whose commit is
not the head being merged. The annotation alone would not be enough: a flag
only protects a caller that reads it, and the harm happens in a caller that
reads ``status`` and ``contract_hash``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import gate_executor
import gate_recorder
from gate_recorder import record_failure, record_not_executable

PRIOR_SHA = "109181d2c0ffee0000000000000000000000beef"
REQUESTED_SHA = "1425faa1deadbeef0000000000000000000012345"

PRIOR_RECORD = {
    "gate": "codex_gate",
    "pr_id": "1705",
    "pr_number": 1705,
    "status": "fail",
    "commit_sha": PRIOR_SHA,
    "contract_hash": "088a30754169bb91",
    "report_path": "/tmp/codex-1705-old.md",
    "dispatch_id": "codex-gate-pr1705-old",
    "recorded_at": "2026-08-28T18:49:22Z",
    "duration_seconds": 428.0,
    "blocking_findings": [{"severity": "blocking", "message": "old finding"}],
    "advisory_findings": [],
}


# ``pr_id`` selects the naming convention: non-empty picks
# ``{slug}-{gate}-contract.json``, empty picks ``pr-{n}-{gate}.json``. The
# measured case is the latter (``results/pr-1705-codex_gate.json``), so every
# call below passes ``pr_id=""`` — seeding one convention and writing the other
# leaves the guard looking at an absent file and refusing nothing.
@pytest.fixture
def dirs(tmp_path):
    requests = tmp_path / "state" / "review_gates" / "requests"
    results = tmp_path / "state" / "review_gates" / "results"
    state = tmp_path / "state"
    for d in (requests, results, state):
        d.mkdir(parents=True, exist_ok=True)
    (results / "pr-1705-codex_gate.json").write_text(
        json.dumps(PRIOR_RECORD), encoding="utf-8"
    )
    return requests, results, state


def _request_payload():
    return {
        "gate": "codex_gate", "pr_id": "1705", "pr_number": 1705,
        "branch": "fix/whatever", "commit_sha": REQUESTED_SHA,
        "contract_hash": "088a30754169bb91",
    }


# --------------------------------------------------------------------------
# The signal: a caller can see that its own write never landed.
# --------------------------------------------------------------------------

def test_a_refused_not_executable_is_marked_as_not_written(dirs):
    requests, results, state = dirs

    payload = record_not_executable(
        gate="codex_gate", pr_number=1705, pr_id="",
        reason="provider_unavailable", reason_detail="codex quota exhausted",
        request_payload=_request_payload(),
        requests_dir=requests, results_dir=results, state_dir=state,
    )

    assert payload["status"] == "fail", (
        "the on-disk contract (OI-1469/OI-1470) is unchanged: the caller is "
        "still told the true state of the store"
    )
    assert payload.get("write_refused") is True, (
        "nothing distinguishes this preserved record from a record this call "
        "actually wrote"
    )
    assert payload.get("attempted_status") == "not_executable"
    assert payload.get("attempted_commit_sha") == REQUESTED_SHA, (
        "without the attempted sha beside the preserved one, a reader cannot "
        "see that the two are about different commits"
    )


def test_a_refused_failure_is_marked_as_not_written(dirs):
    requests, results, _state = dirs

    payload = record_failure(
        gate="codex_gate", pr_number=1705, pr_id="",
        result={
            "reason": "timeout", "reason_detail": "codex stalled at 900s",
            "duration_seconds": 900.0, "partial_output_lines": 0, "runner_pid": 42,
        },
        request_payload=_request_payload(),
        requests_dir=requests, results_dir=results,
    )

    assert payload["status"] == "fail"
    assert payload.get("write_refused") is True
    assert payload.get("attempted_status") == "unavailable"


def test_a_landed_write_is_not_marked_refused(dirs):
    """The flag must distinguish, so it must be absent on success."""
    requests, results, state = dirs
    (results / "pr-1705-codex_gate.json").unlink()

    payload = record_not_executable(
        gate="codex_gate", pr_number=1705, pr_id="",
        reason="provider_unavailable", reason_detail="quota",
        request_payload=_request_payload(),
        requests_dir=requests, results_dir=results, state_dir=state,
    )

    assert "write_refused" not in payload
    assert payload["status"] == "not_executable"
    assert payload["commit_sha"] == REQUESTED_SHA


def test_the_decided_record_on_disk_is_untouched(dirs):
    requests, results, state = dirs

    record_not_executable(
        gate="codex_gate", pr_number=1705, pr_id="",
        reason="provider_unavailable", reason_detail="quota",
        request_payload=_request_payload(),
        requests_dir=requests, results_dir=results, state_dir=state,
    )

    on_disk = json.loads((results / "pr-1705-codex_gate.json").read_text(encoding="utf-8"))
    assert on_disk["commit_sha"] == PRIOR_SHA
    assert on_disk["status"] == "fail"
    assert "write_refused" not in on_disk, (
        "the annotation is for the caller, not for the store — writing it to "
        "disk would corrupt the preserved record"
    )


# --------------------------------------------------------------------------
# The protection: a record about another commit cannot be a decided pass.
# --------------------------------------------------------------------------

class _Stub(gate_executor.GateExecutorMixin):
    def __init__(self, exec_result):
        self._exec_result = exec_result
        self.executed = []

    def execute_gate(self, *, gate, pr_number):
        self.executed.append((gate, pr_number))
        return self._exec_result


def _requested(gate="codex_gate"):
    return {"requested": [{"gate": gate, "status": "requested", "required": True}]}


def _evidenced_pass(commit_sha):
    return {
        "gate": "codex_gate", "status": "pass", "commit_sha": commit_sha,
        "contract_hash": "088a30754169bb91",
        "report_path": "/tmp/codex-1705.md",
        "dispatch_id": "codex-gate-pr1705-new",
        "blocking_findings": [], "advisory_findings": [],
    }


def test_a_pass_for_another_commit_is_not_a_decided_pass(monkeypatch):
    """The symmetric case the measured instance was lucky enough to avoid."""
    monkeypatch.setattr(gate_recorder, "get_pr_head_sha", lambda _n: REQUESTED_SHA)
    stub = _Stub(_evidenced_pass(PRIOR_SHA))

    gates, has_required_failure = stub._execute_requested_gates(_requested(), 1705)

    assert gates[0]["passed"] is False, (
        "an evidenced PASS recording another commit counted as this head's "
        "gate evidence — a merge on code that is no longer there"
    )
    assert gates[0]["sha_binding"] == "mismatch"
    assert has_required_failure is True
    assert "109181d2" in gates[0]["pass_reason"]


def test_a_pass_for_this_commit_still_passes(monkeypatch):
    monkeypatch.setattr(gate_recorder, "get_pr_head_sha", lambda _n: REQUESTED_SHA)
    stub = _Stub(_evidenced_pass(REQUESTED_SHA))

    gates, has_required_failure = stub._execute_requested_gates(_requested(), 1705)

    assert gates[0]["passed"] is True
    assert gates[0]["sha_binding"] == "match"
    assert has_required_failure is False


def test_an_unknown_head_sha_does_not_downgrade_a_decided_gate(monkeypatch):
    """Unknown is its own bucket, not a polite mismatch.

    ``gh`` being unreachable says nothing about the code under review. Folding
    that into ``mismatch`` fails every gate the moment GitHub hiccups; folding
    it into ``match`` restores the hole. Three buckets, not two.
    """
    monkeypatch.setattr(gate_recorder, "get_pr_head_sha", lambda _n: "")
    stub = _Stub(_evidenced_pass(PRIOR_SHA))

    gates, has_required_failure = stub._execute_requested_gates(_requested(), 1705)

    assert gates[0]["sha_binding"] == "unknown"
    assert gates[0]["passed"] is True
    assert has_required_failure is False


def test_a_result_without_a_sha_is_unknown_not_mismatch(monkeypatch):
    monkeypatch.setattr(gate_recorder, "get_pr_head_sha", lambda _n: REQUESTED_SHA)
    stub = _Stub({**_evidenced_pass(REQUESTED_SHA), "commit_sha": ""})

    gates, _ = stub._execute_requested_gates(_requested(), 1705)

    assert gates[0]["sha_binding"] == "unknown"
    assert gates[0]["passed"] is True


def test_the_binding_is_reported_even_when_it_changes_nothing(monkeypatch):
    """A check whose result is invisible cannot be audited afterwards."""
    monkeypatch.setattr(gate_recorder, "get_pr_head_sha", lambda _n: REQUESTED_SHA)
    stub = _Stub(_evidenced_pass(REQUESTED_SHA))

    gates, _ = stub._execute_requested_gates(_requested(), 1705)

    assert gates[0]["head_sha"] == REQUESTED_SHA
    assert gates[0]["result_commit_sha"] == REQUESTED_SHA
    assert gates[0]["write_refused"] is False
