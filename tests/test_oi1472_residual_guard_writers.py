"""Regression tests for OI-1472: the two result-record writers that still
bypassed the OI-1469/OI-1470 overwrite guard.

#1696 guarded the four writers that had actually destroyed evidence on
2026-08-26. Two production writers of a ``review_gates/results/`` record were
left calling ``Path.write_text`` directly, and the shared primitive's
docstring nonetheless claimed the guard applied "by construction":

  * ``gate_report_generator._write_failure_result`` — sits directly below its
    sibling ``_write_not_executable_result``, which WAS converted. Its payload
    is exactly the shape the guard exists for: terminal (``status="failed"``)
    with an EMPTY ``report_path``, i.e. a decided outcome carrying no
    evidence. Landing that over an evidenced pass is OI-1470's loss with a
    different status word. Latent only because it has no call site today.

  * ``gate_artifacts._write_result_record`` — one production call site, in
    ``materialize_artifacts``. Its own payload is evidenced and the guard
    would admit it, so the leak is the other direction: a bare ``write_text``
    is not atomic, so this writer could MANUFACTURE the torn file the guard
    refuses on, and it bypassed the corrupt-existing read entirely.

Each test below drives the REAL production writer. The last one is the
docstring's own guard: it fails the moment a new writer of a results record
appears outside the enumerated set.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import gate_artifacts
import gate_recorder
import governance_receipts
from gate_report_generator import GateReportGeneratorMixin

REPO_ROOT = Path(__file__).resolve().parents[1]

# A decided verdict WITH a complete evidence trail — the thing the guard
# protects. Mirrors PR #1691's real codex-gate pass record.
DECIDED_AND_EVIDENCED = {
    "gate": "codex_gate",
    "pr_number": 1691,
    "status": "completed",
    "contract_hash": "466cd2ca75d7a7fb",
    "report_path": "/unified_reports/codex-gate-pr1691.md",
    "dispatch_id": "codex-gate-pr1691-1787753482",
    "blocking_findings": [],
}


class _Manager(GateReportGeneratorMixin):
    """Minimal stand-in for ReviewGateManager: only the two path helpers the
    mixin's writers call. The writers themselves are real production code."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)

    def _result_path(self, gate: str, pr_number: int) -> Path:
        return self.state_dir / "review_gates" / "results" / f"pr-{pr_number}-{gate}.json"

    def _contract_result_path(self, gate: str, pr_id: str) -> Path:
        slug = pr_id.lower().replace("-", "")
        return self.state_dir / "review_gates" / "results" / f"{slug}-{gate}-contract.json"


@pytest.fixture
def results_dir(tmp_path: Path) -> Path:
    d = tmp_path / "review_gates" / "results"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def stub_rgm(monkeypatch):
    """``_write_failure_result`` imports ``_utc_now`` from review_gate_manager;
    importing the real module drags in the whole manager. Stand-in only."""
    fake = types.ModuleType("review_gate_manager")
    fake._utc_now = governance_receipts.utc_now_iso
    monkeypatch.setitem(sys.modules, "review_gate_manager", fake)
    return fake


def _failure_result(manager: _Manager, **over):
    kwargs = dict(
        gate="codex_gate", pr_number=1691, pr_id="",
        reason="timeout", reason_detail="gate exceeded 900s",
        duration_seconds=900.0, partial_output_lines=0, runner_pid=4242,
    )
    kwargs.update(over)
    return manager._write_failure_result(**kwargs)


# ---------------------------------------------------------------------------
# 1. _write_failure_result — the OI-1470 shape it could produce
# ---------------------------------------------------------------------------


def test_failure_result_refuses_to_downgrade_an_evidenced_pass(tmp_path, stub_rgm):
    """A timeout must not erase a decided, evidenced verdict."""
    manager = _Manager(tmp_path)
    slot = manager._result_path("codex_gate", 1691)
    slot.parent.mkdir(parents=True)
    slot.write_text(json.dumps(DECIDED_AND_EVIDENCED), encoding="utf-8")

    payload, written = _failure_result(manager)

    assert written is False, "the guard must refuse a failed/no-evidence write over an evidenced pass"
    on_disk = json.loads(slot.read_text(encoding="utf-8"))
    assert on_disk == DECIDED_AND_EVIDENCED, "the decided verdict must survive byte-for-byte"
    assert payload == DECIDED_AND_EVIDENCED, "a refusal returns what is ON DISK, not the failure payload"


def test_failure_result_writes_into_an_empty_slot(tmp_path, stub_rgm):
    """The guard only refuses downgrades; a fresh failure still books."""
    manager = _Manager(tmp_path)
    slot = manager._result_path("codex_gate", 1691)
    slot.parent.mkdir(parents=True)

    payload, written = _failure_result(manager)

    assert written is True
    assert json.loads(slot.read_text(encoding="utf-8"))["status"] == "failed"
    assert payload["reason"] == "timeout"
    assert list(slot.parent.glob("*.tmp")) == [], "write must be atomic: no temp file left behind"


def test_failure_result_overwrites_an_unevidenced_terminal(tmp_path, stub_rgm):
    """A terminal record with no evidence is not 'decided' — it must not
    freeze the slot permanently (the guard's own stated boundary)."""
    manager = _Manager(tmp_path)
    slot = manager._result_path("codex_gate", 1691)
    slot.parent.mkdir(parents=True)
    slot.write_text(
        json.dumps({"gate": "codex_gate", "status": "not_executable",
                    "contract_hash": "", "report_path": ""}),
        encoding="utf-8",
    )

    _payload, written = _failure_result(manager)

    assert written is True
    assert json.loads(slot.read_text(encoding="utf-8"))["status"] == "failed"


def test_failure_result_returns_the_sibling_contract_without_a_path(tmp_path, stub_rgm):
    """No pr_id and no pr_number: no slot to write. Mirrors
    ``_write_not_executable_result``'s ``(payload, True)`` exactly, so the two
    siblings cannot be read two different ways."""
    manager = _Manager(tmp_path)

    payload, written = _failure_result(manager, pr_number=None, pr_id="")

    assert written is True
    assert payload["status"] == "failed"


def test_both_siblings_return_the_same_shape(tmp_path, stub_rgm):
    """The asymmetry OI-1472 is about: one sibling returned a dict, the other
    a (dict, bool). A caller could not treat them uniformly."""
    manager = _Manager(tmp_path)
    (tmp_path / "review_gates" / "results").mkdir(parents=True)

    failure = _failure_result(manager, pr_number=1)
    not_exec = manager._write_not_executable_result(
        gate="codex_gate", pr_number=2, pr_id="",
        reason="binary_not_found", reason_detail="codex not on PATH",
    )

    for name, got in (("_write_failure_result", failure), ("_write_not_executable_result", not_exec)):
        assert isinstance(got, tuple) and len(got) == 2, f"{name} must return (payload, written)"
        assert isinstance(got[0], dict) and isinstance(got[1], bool), name


# ---------------------------------------------------------------------------
# 2. gate_artifacts._write_result_record — atomicity + corrupt-existing
# ---------------------------------------------------------------------------


def _result_payload(**over):
    payload = {
        "gate": "codex_gate",
        "pr_id": "",
        "pr_number": 1691,
        "status": "completed",
        "contract_hash": "deadbeefcafe0001",
        "report_path": "/unified_reports/codex-gate-pr1691.md",
        "dispatch_id": "codex-gate-pr1691-1787999999",
        "blocking_findings": [],
    }
    payload.update(over)
    return payload


def test_result_record_write_is_atomic(results_dir, tmp_path):
    report = tmp_path / "report.md"
    report.write_text("# report\n", encoding="utf-8")

    err = gate_artifacts._write_result_record(
        results_dir, "codex_gate", 1691, "", _result_payload(), report,
    )

    assert err is None
    slot = results_dir / "pr-1691-codex_gate.json"
    assert json.loads(slot.read_text(encoding="utf-8"))["status"] == "completed"
    assert list(results_dir.glob("*.tmp")) == [], "tmp+replace must leave no temp file"


def test_a_torn_write_cannot_reach_the_live_record(results_dir, tmp_path, monkeypatch):
    """The reason atomicity matters here, stated as behaviour.

    A bare ``write_text`` that dies mid-write leaves the LIVE record torn —
    manufacturing exactly the unreadable-but-present shape the guard refuses
    on, from the writer the guard was supposed to protect. tmp + ``os.replace``
    can only tear the temp file.
    """
    slot = results_dir / "pr-1691-codex_gate.json"
    slot.write_text(json.dumps(DECIDED_AND_EVIDENCED), encoding="utf-8")
    report = tmp_path / "report.md"
    report.write_text("# report\n", encoding="utf-8")

    real_write_text = Path.write_text

    def dying_write_text(self, data, *args, **kwargs):
        """Write the first 20 bytes, then die — a full disk or a SIGKILL."""
        real_write_text(self, data[:20], *args, **kwargs)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "write_text", dying_write_text)
    err = gate_artifacts._write_result_record(
        results_dir, "codex_gate", 1691, "", _result_payload(), report,
    )
    monkeypatch.undo()

    assert err is not None and err.startswith("Failed to write result record")
    assert json.loads(slot.read_text(encoding="utf-8")) == DECIDED_AND_EVIDENCED, (
        "the live record must be untouched by a write that died halfway; a raw "
        "write_text would have truncated it to 20 bytes of JSON"
    )


def test_result_record_refuses_over_a_corrupt_existing_record(results_dir, tmp_path):
    """A torn file may be a truncated decided verdict. Refuse rather than
    guess — and say so in a detail string that is not an I/O error."""
    slot = results_dir / "pr-1691-codex_gate.json"
    slot.write_text('{"gate": "codex_gate", "status": "comp', encoding="utf-8")
    report = tmp_path / "report.md"
    report.write_text("# report\n", encoding="utf-8")

    err = gate_artifacts._write_result_record(
        results_dir, "codex_gate", 1691, "", _result_payload(), report,
    )

    assert err is not None
    assert "refused by the overwrite guard" in err
    assert not err.startswith("Failed to write result record"), (
        "a guard refusal is a decision, not an I/O failure — the caller must be "
        "able to tell them apart"
    )
    assert slot.read_text(encoding="utf-8") == '{"gate": "codex_gate", "status": "comp'
    assert report.exists(), (
        "the fresh report must survive a refusal: deleting it repeats exactly the "
        "evidence loss OI-1469/OI-1470 built this guard to stop"
    )


def test_result_record_still_cleans_up_on_a_real_write_failure(results_dir, tmp_path):
    """The pre-existing orphan-cleanup contract is unchanged for genuine
    failures (here: neither pr_id nor pr_number, so there is no slot)."""
    report = tmp_path / "report.md"
    report.write_text("# report\n", encoding="utf-8")

    err = gate_artifacts._write_result_record(
        results_dir, "codex_gate", None, "", _result_payload(pr_number=None), report,
    )

    assert err is not None and err.startswith("Failed to write result record")
    assert not report.exists(), "an orphan report is still cleaned up on a real failure"


def test_result_record_uses_the_shared_path_helper(results_dir, tmp_path):
    """pr_id and pr_number filenames must match gate_recorder.result_file_path
    exactly — the inline duplicate this replaced could drift from it."""
    report = tmp_path / "report.md"
    report.write_text("# report\n", encoding="utf-8")

    gate_artifacts._write_result_record(
        results_dir, "codex_gate", None, "PR-1691", _result_payload(pr_id="PR-1691"), report,
    )

    expected = gate_recorder.result_file_path(results_dir, "codex_gate", None, "PR-1691")
    assert expected.exists()


# ---------------------------------------------------------------------------
# 3. End-to-end through the real production call site
# ---------------------------------------------------------------------------


def test_materialize_artifacts_keeps_a_corrupt_record_and_its_fresh_report(tmp_path):
    """The one production call site, driven end to end."""
    state = tmp_path / "state"
    results = state / "review_gates" / "results"
    requests = state / "review_gates" / "requests"
    reports = tmp_path / "unified_reports"
    for d in (results, requests, reports):
        d.mkdir(parents=True)

    torn = results / "pr-1691-codex_gate.json"
    torn.write_text('{"status": "completed", "contract_ha', encoding="utf-8")
    report_path = reports / "codex-gate-pr1691.md"

    payload = gate_artifacts.materialize_artifacts(
        gate="codex_gate", pr_number=1691, pr_id="",
        stdout="line one of the review\nline two of the review\nline three of the review\n",
        request_payload={
            "gate": "codex_gate", "pr_number": 1691,
            "report_path": str(report_path), "prompt": "review this",
            "dispatch_id": "codex-gate-pr1691-1787999999",
        },
        duration_seconds=12.5,
        requests_dir=requests, results_dir=results, reports_dir=reports,
    )

    assert torn.read_text(encoding="utf-8") == '{"status": "completed", "contract_ha', (
        "the unreadable record must not be overwritten by a guess"
    )
    assert report_path.exists(), "the fresh report stays on disk as evidence of the refused run"
    assert payload["status"] != "completed", (
        "materialize must not report success for a write that never landed"
    )


# ---------------------------------------------------------------------------
# 4. The docstring's own guard
# ---------------------------------------------------------------------------

# Every module that writes a review_gates/results/ record, and the writers in
# it that gate_recorder.write_result_guarded's docstring enumerates as the
# complete set. A new writer added here without routing through the guard is
# the exact regression OI-1472 describes.
_WRITER_MODULES = (
    "scripts/lib/gate_recorder.py",
    "scripts/lib/gate_artifacts.py",
    "scripts/lib/gate_report_generator.py",
)


def test_no_writer_bypasses_the_guard_with_a_raw_write():
    """``write_result_guarded``'s docstring claims the guard applies by
    construction. That claim is only worth making because this fails when it
    stops being true."""
    offenders = []
    for rel in _WRITER_MODULES:
        for lineno, line in enumerate(
            (REPO_ROOT / rel).read_text(encoding="utf-8").splitlines(), start=1
        ):
            stripped = line.strip()
            if stripped.startswith("#") or "write_text" not in stripped:
                continue
            if "result_file" in stripped or "result_path" in stripped or "slot" in stripped:
                offenders.append(f"{rel}:{lineno}: {stripped}")

    assert offenders == [], (
        "these lines write a gate result record without the overwrite guard "
        "(OI-1472); route them through gate_recorder.write_result_guarded and "
        "add them to the table in its docstring:\n  " + "\n  ".join(offenders)
    )


def test_the_enumerated_writers_all_exist():
    """The docstring names six writers. A rename that leaves the table stale
    makes the claim unverifiable — catch it here instead."""
    assert callable(gate_recorder.record_terminal_result)
    assert callable(gate_recorder.record_not_executable)
    assert callable(gate_recorder.record_failure)
    assert callable(gate_recorder.record_failure_simple)
    assert callable(gate_artifacts._write_result_record)
    assert callable(GateReportGeneratorMixin._write_not_executable_result)
    assert callable(GateReportGeneratorMixin._write_failure_result)
