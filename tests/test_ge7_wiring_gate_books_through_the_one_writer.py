"""GE-7 (dlv-20e9ed688893): the wiring gate's outcome is written by the ONE writer.

ADR-046 §2 makes ``commit_gate_outcome`` the only place that lands a result
record, its one ``review_gate_result`` receipt and the request's end, under the
result slot's lock; ``_request_wiring_gate`` was the last writer outside it (GE-6
Open Item 2). All three of GE-6's branches now go through it, booking the
advisory mapping with them (a shadow-mode finding books ``fail`` /
``required: false``, so its receipt lands while the merge stays unblocked —
``deterministic_gate._WIRING_STATUS``, consumers in the shadow-mode test). The
last group proves the GE-3 writer guard covers this module.
"""

from __future__ import annotations

import importlib
import json
import sys
import threading
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import review_stack_support
import wiring_gate
from atomic_io import slot_lock
from gate_request_handler import GateRequestHandlerMixin

PR, BRANCH = 1591, "dispatch/ge-7-fixture"
HEAD_SHA = review_stack_support.HEAD_SHA


class _Requests(GateRequestHandlerMixin):
    def __init__(self, store):
        self.requests_dir, self.results_dir = store["requests_dir"], store["results_dir"]
        self.state_dir = store["state_dir"]


def _slot(store, kind: str) -> Path:
    return store[f"{kind}_dir"] / f"pr-{PR}-wiring_gate.json"


def _receipts(store) -> list:
    rows = [json.loads(raw) for name in ("t0_receipts.ndjson", "gate_events.ndjson")
            if (store["state_dir"] / name).exists()
            for raw in (store["state_dir"] / name).read_text(encoding="utf-8").splitlines() if raw.strip()]
    return [row for row in rows if row.get("event_type") == "review_gate_result"]


def _drive(branch: str, store, monkeypatch) -> dict:
    """Run the wiring request down one of GE-6's three branches."""
    if branch == "gh_missing":
        monkeypatch.setattr(_Requests, "_wiring_gate_available", lambda self: False)
    elif branch == "wiring_error":
        def explode(pr_number):
            raise wiring_gate.WiringGateError("ast-grep exited 3")

        monkeypatch.setattr(wiring_gate, "check_pr_wiring", explode)
    else:
        wired = wiring_gate.WiringGateResult(status="pass", total_checked=7, summary="7 checked, all wired")
        monkeypatch.setattr(wiring_gate, "check_pr_wiring", lambda pr_number: wired)
    return _Requests(store)._request_wiring_gate(PR, BRANCH)


CASES = [  # branch, record status, the request's own lifecycle word, is a verdict?
    ("gh_missing", "not_executable", "not_executable", False),
    ("wiring_error", "fail", "completed", True),
    ("analysis_ran", "pass", "completed", True),
]


@pytest.mark.parametrize("branch,record_status,request_status,decided", CASES)
def test_every_branch_books_through_commit_gate_outcome(tmp_path, monkeypatch, branch, record_status,
                                                        request_status, decided):
    """One writer call per request; the record, its receipt and the request's end all land."""
    import gate_outcome

    store = review_stack_support.build_project_store(tmp_path, monkeypatch)
    monkeypatch.setenv("VNX_WIRING_GATE_REQUIRED", "1")  # read at call time
    seen, real = [], gate_outcome.commit_gate_outcome

    def spy(outcome, **kwargs):
        seen.append(outcome)
        return real(outcome, **kwargs)

    monkeypatch.setattr(gate_outcome, "commit_gate_outcome", spy)
    payload = _drive(branch, store, monkeypatch)

    assert [outcome.gate for outcome in seen] == ["wiring_gate"], f"{branch} must book through the one writer"
    assert seen[0].pr_id == "", "an empty pr_id keeps the slot named pr-<N>-wiring_gate.json"
    record = json.loads(_slot(store, "results").read_text())
    assert payload["status"] == record["status"] == record_status
    assert (record["required"], record["attempt_seq"]) == (True, 1)
    assert json.loads(_slot(store, "requests").read_text())["status"] == request_status, "the request's lifecycle word"

    receipts = _receipts(store)
    if not decided:
        assert "receipt_pending" not in record and receipts == [], "nothing decided owes no receipt (ADR-046 §2)"
        return
    assert record["commit_sha"] == HEAD_SHA, "the record names the head it judged"
    assert len(receipts) == 1, f"one review_gate_result per decided attempt, got {receipts}"
    assert (receipts[0]["gate"], receipts[0]["gate_status"], receipts[0]["commit_sha"]) == (
        "wiring_gate", record_status, HEAD_SHA,
    )


def test_the_record_and_its_request_end_land_under_the_result_slot_lock(tmp_path, monkeypatch):
    """Holding the result slot blocks BOTH writes: they happen inside one lock."""
    store = review_stack_support.build_project_store(tmp_path, monkeypatch)
    passing = wiring_gate.WiringGateResult(status="pass", total_checked=3, summary="3 checked, all wired")
    monkeypatch.setattr(wiring_gate, "check_pr_wiring", lambda pr_number: passing)
    started, outcome = threading.Event(), {}

    def run():
        started.set()
        try:
            outcome["payload"] = _Requests(store)._request_wiring_gate(PR, BRANCH)
        except BaseException as exc:  # vnx-silent-except: re-raised after the join
            outcome["error"] = exc

    with slot_lock(_slot(store, "results")):
        thread = threading.Thread(target=run)
        thread.start()
        assert started.wait(5)
        thread.join(0.3)
        assert thread.is_alive() and not _slot(store, "results").exists(), "the record waits for the slot lock"
        assert not _slot(store, "requests").exists(), "the request's end is booked inside the same lock"
    thread.join(10)
    assert not thread.is_alive() and "error" not in outcome, outcome.get("error")
    assert json.loads(_slot(store, "results").read_text())["attempt_seq"] == 1
    assert json.loads(_slot(store, "requests").read_text())["status"] == "completed"


_MUTATION_ANCHOR = "        record = self._wiring_gate_record(context, available)"
_RAW_WRITE = "\n        self._result_path('wiring_gate', pr_number).write_text(json.dumps(record))"


def test_a_raw_write_of_a_wiring_result_record_makes_the_writer_guard_fail(tmp_path, monkeypatch):
    """The GE-3 residue scan rejects a raw write of a wiring result — run over a mutated copy."""
    guard = importlib.import_module("test_oi1472_residual_guard_writers")
    module = "scripts/lib/gate_request_handler.py"
    assert module in guard._WRITER_MODULES, "the wiring writer is one of the guarded modules"
    source = (VNX_ROOT / module).read_text(encoding="utf-8")
    assert _MUTATION_ANCHOR in source, "the mutation anchor moved; the guard would go quiet"

    copy = tmp_path / "guard_root" / module
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_text(source, encoding="utf-8")
    monkeypatch.setattr(guard, "REPO_ROOT", copy.parents[2])
    monkeypatch.setattr(guard, "_WRITER_MODULES", (module,))

    guard.test_no_writer_bypasses_the_guard_with_a_raw_write()
    copy.write_text(source.replace(_MUTATION_ANCHOR, _MUTATION_ANCHOR + _RAW_WRITE), encoding="utf-8")
    with pytest.raises(AssertionError) as refused:
        guard.test_no_writer_bypasses_the_guard_with_a_raw_write()
    assert module in str(refused.value) and "write_text" in str(refused.value), str(refused.value)
