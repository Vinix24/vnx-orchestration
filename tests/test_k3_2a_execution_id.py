"""tests/test_k3_2a_execution_id.py — one execution id per review-gate seat run (K3-2a, OI-2444).

Track ``kosten-per-model-in-het-grootboek``, ADR-043. The closed K3 took its
booking identity from the result slot's counter, and codex reproduced the
duplicate every round: round 4 measured a commit timeout of run-a, a commit
timeout of run-b and a re-commit of run-b's returned record as THREE lines for
TWO executions. The fix is an identity that exists before the slot does:

- ``gate_status.new_execution_id()`` mints 32 lower-case hex characters, once
  per run, before the run's first model call (``GateRunner._mark_executing``
  for the gate runner, ``governed_lane.run_gate_head`` for the standalone
  gates). A takeover run is a run of its own and gets its own id.
- Every record of that run carries ``gate_status.EXECUTION_ID_FIELD``. It is
  CARRIED, never recomputed: a record handed back to a caller and committed
  again keeps it.
- ``--reprocess`` calls no model and mints nothing; it carries the id the run
  whose report it rebuilt recorded with that report, and none when it recorded
  none.
- K2b's line for a live CLI run writes the same id, so K3-2b can see that the
  run already has its line.

Everything here runs the REAL record builders (``kimi_gate.main``,
``GateRunner.run``, ``gate_recorder.record_failure``,
``gate_outcome.commit_gate_outcome``) on a temporary store; only the model
call and the paths around it are replaced.
"""
from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import cost_ledger  # noqa: E402
import gate_outcome  # noqa: E402
import gate_recorder  # noqa: E402
import gate_runner  # noqa: E402
import gate_status  # noqa: E402
import kimi_gate  # noqa: E402
from gate_runner import GateRunner  # noqa: E402

# Read through ``getattr`` so this module COLLECTS on the pre-K3-2a head: the red run then
# reports per-test assertion failures (no id on any record) instead of one import error.
FIELD = getattr(gate_status, "EXECUTION_ID_FIELD", "execution_id")
PROJECT = "vnx-dev"
KIMI_MODEL = "kimi-k3"
DIFF = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n"

_PASS_BODY = 'Reviewed the diff.\n\n```json\n{"verdict": "pass", "findings": [], "residual_risk": null}\n```\n'


def _kimi_report(*, measured: bool = False, input_tokens: int = 0, output_tokens: int = 0) -> str:
    """A report in the shape the governed lane writes: frontmatter + verdict body."""
    return (
        "---\n"
        "schema_version: 1\n"
        "dispatch_id: kimi-gate-pr4242-1788000240\n"
        "provider: kimi\n"
        "model: kimi-k3\n"
        "role: review-gate\n"
        "exit_code: 0\n"
        "token_usage:\n"
        f"  input: {input_tokens}\n"
        f"  output: {output_tokens}\n"
        "  cache_read: 0\n"
        f"token_usage_measured: {'true' if measured else 'false'}\n"
        "---\n"
        "\n" + _PASS_BODY
    )


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A temporary store: the gates' own records and the real cost ledger both live here."""
    data = tmp_path / "data"
    state = data / "state"
    reports = data / "unified_reports"
    for path in (state / "review_gates" / "requests", state / "review_gates" / "results", reports):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("VNX_DATA_DIR", str(data))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_STATE_DIR", str(state))
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)
    return SimpleNamespace(
        data=data, state=state, reports=reports,
        ledger=data / "events" / "provider_costs.ndjson",
        results=state / "review_gates" / "results",
    )


def _ledger_lines(store) -> list:
    """Every cost-ledger run line in the store, in write order."""
    if not store.ledger.exists():
        return []
    return [
        record for record in (
            json.loads(line) for line in store.ledger.read_text(encoding="utf-8").splitlines() if line.strip()
        )
        if record.get("schema") == cost_ledger.RUN_SCHEMA
    ]


def _slot(store, gate: str, pr: int) -> dict:
    return json.loads((store.results / f"pr-{pr}-{gate}.json").read_text(encoding="utf-8"))


def _run_gate(gate_module, argv) -> tuple:
    """``(exit code, the record the run RETURNED)`` — the gate prints it with ``--json``."""
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        rc = gate_module.main(argv)
    printed = out.getvalue().strip()
    return rc, (json.loads(printed) if printed.startswith("{") else None)


def _kimi_live(
    store, monkeypatch, *, report: str = "", error: Exception | None = None,
    sha: str = "sha-1", pr: int = 4242,
) -> tuple:
    """One real ``kimi_gate`` run; only the diff fetch, the dispatcher and the head lookups are doubles.

    The dispatcher double writes its report the way ``governance_emit`` does, so
    ``--reprocess`` later finds the same file a real run leaves. ``error`` makes
    the lane raise instead — a provider outage, which books ``unavailable``.
    """
    diff_file = store.data / "x.diff"
    diff_file.write_text(DIFF, encoding="utf-8")

    def _make(*_a, **_k):
        def _dispatch(_provider, _model, _instruction, dispatch_id):
            if error is not None:
                raise error
            (store.reports / f"{dispatch_id}.md").write_text(report, encoding="utf-8")
            return report
        return _dispatch

    monkeypatch.setattr(kimi_gate, "_get_diff", lambda _pr, _df: DIFF)
    monkeypatch.setattr(kimi_gate, "_make_default_dispatcher", _make)
    monkeypatch.setattr(kimi_gate, "get_pr_head_branch", lambda _pr: "feature/k3-2a")
    monkeypatch.setattr(kimi_gate, "get_pr_head_sha", lambda _pr: sha)
    return _run_gate(kimi_gate, [
        "--pr", str(pr), "--data-dir", str(store.data), "--model", KIMI_MODEL, "--json",
    ])


def _kimi_reprocess(store, monkeypatch, dispatch_id: str, *, sha: str = "sha-1", pr: int = 4242) -> tuple:
    """``--reprocess``: no diff fetch and no dispatcher — the lane must not be called at all."""
    monkeypatch.setattr(kimi_gate, "_make_default_dispatcher", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("--reprocess must not call the lane")))
    monkeypatch.setattr(kimi_gate, "get_pr_head_branch", lambda _pr: "feature/k3-2a")
    monkeypatch.setattr(kimi_gate, "get_pr_head_sha", lambda _pr: sha)
    return _run_gate(kimi_gate, [
        "--pr", str(pr), "--reprocess", dispatch_id, "--data-dir", str(store.data),
        "--model", KIMI_MODEL, "--json",
    ])


def _gate_request(store, gate: str, pr: int, **over) -> dict:
    payload = {
        "gate": gate, "status": "requested", "branch": "dispatch/k3-2a",
        "pr_number": pr, "pr_id": "", "commit_sha": "abc1234",
        "report_path": str(store.reports / f"pr-{pr}-{gate}.md"),
        "prompt": "Review this code", "risk_class": "medium",
    }
    payload.update(over)
    return payload


def _harness_runner(store, monkeypatch, report: str) -> GateRunner:
    """A GateRunner whose harness lane returns ``report`` without touching git or a provider."""
    monkeypatch.setattr(gate_runner, "fetch_branch", lambda *a, **k: None)
    monkeypatch.setattr(gate_runner, "_capture_main_checkout_state", lambda root: {})
    monkeypatch.setattr(gate_runner, "_build_harness_lane_dispatcher", lambda *a, **k: (lambda *a2, **k2: report))
    return GateRunner(state_dir=store.state, reports_dir=store.reports)


def _codex_runner(store, monkeypatch, result: dict) -> GateRunner:
    """A GateRunner whose codex subprocess boundary returns ``result``; no worktree, no real binary.

    The provider-installed check sits on the path under test, so the binary
    lookup is answered here too: on a runner without codex on PATH that check
    books ``not_executable``/``provider_not_installed`` before the run ever
    reaches the patched subprocess boundary.
    """
    monkeypatch.setattr(gate_runner, "create_gate_worktree", lambda **kw: store.data / "_wt")
    monkeypatch.setattr(gate_runner, "remove_gate_worktree", lambda *a, **kw: None)
    monkeypatch.setattr(gate_runner.shutil, "which", lambda binary: f"/usr/local/bin/{binary}")
    monkeypatch.setattr(GateRunner, "_run_with_stall_detection", lambda self, **kw: result)
    return GateRunner(state_dir=store.state, reports_dir=store.reports)


def _assert_minted(execution_id: object) -> None:
    assert gate_status.is_execution_id(execution_id), execution_id
    assert len(execution_id) == 32 and execution_id == execution_id.lower()


# ---------------------------------------------------------------------------
# (1) the id itself: minted, one per run, never shared
# ---------------------------------------------------------------------------


def test_the_field_names_are_constants_on_gate_status():
    assert gate_status.EXECUTION_ID_FIELD == "execution_id"
    assert gate_status.EVIDENCE_SOURCE_FIELD == "evidence_source"


def test_the_minted_id_is_32_lower_case_hex_characters():
    minted = gate_status.new_execution_id()

    _assert_minted(minted)
    assert gate_status.new_execution_id() != minted


@pytest.mark.parametrize("value", [None, "", "nope", "A" * 32, "0" * 31, "0" * 33, 12345, b"0" * 32])
def test_anything_that_is_not_a_minted_id_is_no_id(value):
    """Read-side evidence: a lookalike must never match a run (fail-closed)."""
    assert gate_status.is_execution_id(value) is False


def test_two_runs_of_the_same_seat_on_the_same_head_get_two_ids(store, monkeypatch):
    """Same gate, same PR, same head — an outage and then a clean review are two executions.

    The first run books ``unavailable`` (the lane raised), the second the same
    seat's pass on the same head: two records, two runs, two ids. (Two PASSES
    on one head would be refused by the round ledger — a different rule, not
    an identity one.)
    """
    _, first = _kimi_live(store, monkeypatch, error=RuntimeError("harness exploded"))
    _, second = _kimi_live(store, monkeypatch, report=_kimi_report())

    assert first["status"] == "unavailable" and second["status"] == "pass"
    _assert_minted(first[FIELD])
    _assert_minted(second[FIELD])
    assert first[FIELD] != second[FIELD]


def test_a_takeover_run_gets_its_own_id(store, monkeypatch):
    """A successor gate taking a seat over is a run of its own, not a re-stamp of the seat it took."""
    runner = _harness_runner(store, monkeypatch, _PASS_BODY)
    seat = runner.run(
        gate="kimi_gate", pr_number=4242, pr_id="",
        request_payload=_gate_request(store, "kimi_gate", 4242),
    )
    takeover = runner.run(
        gate="deepseek_gate", pr_number=4242, pr_id="",
        request_payload=_gate_request(
            store, "deepseek_gate", 4242, takeover=True, takeover_from="kimi_gate",
        ),
    )

    assert takeover["takeover"] is True and takeover["takeover_from"] == "kimi_gate"
    _assert_minted(seat[FIELD])
    _assert_minted(takeover[FIELD])
    assert seat[FIELD] != takeover[FIELD]


# ---------------------------------------------------------------------------
# (2) the record a run returns: carried, not recomputed
# ---------------------------------------------------------------------------


def test_a_returned_record_committed_twice_keeps_its_id(store, monkeypatch, tmp_path):
    """The record the caller holds names ONE execution, however often it is committed."""
    _, record = _kimi_live(store, monkeypatch, report=_kimi_report())
    _assert_minted(record[FIELD])

    results_dir = tmp_path / "slot" / "results"
    slot = gate_recorder.result_file_path(results_dir, "kimi_gate", pr_number=4242, pr_id="")
    stored = []
    for _ in range(2):
        payload_on_disk, written = gate_outcome.commit_gate_outcome(
            gate_outcome.GateOutcome.from_record("kimi_gate", dict(record), pr_id=""),
            results_dir=results_dir, result_path=slot,
        )
        assert written is True
        stored.append(payload_on_disk)

    assert [entry[FIELD] for entry in stored] == [record[FIELD], record[FIELD]]
    assert json.loads(Path(slot).read_text(encoding="utf-8"))[FIELD] == record[FIELD]


def test_the_round_4_shape_is_two_ids_across_three_stored_records(store, monkeypatch):
    """run-a timeout, run-b timeout, run-b's returned record committed again: TWO executions.

    This is the shape codex reproduced in round 4 as three cost lines for two
    executions. The ids are the whole point here: the third record must carry
    run-b's id, not a fresh one and not run-a's.
    """
    timeout = {
        "status": "failed", "reason": "timeout", "reason_detail": "the run exceeded 900 s",
        "stdout": "", "stderr": "", "duration_seconds": 900.0,
        "partial_output_lines": 0, "runner_pid": 4242,
    }
    runner = _codex_runner(store, monkeypatch, timeout)
    run_a = runner.run(
        gate="codex_gate", pr_number=4242, pr_id="",
        request_payload=_gate_request(store, "codex_gate", 4242),
    )
    run_b = runner.run(
        gate="codex_gate", pr_number=4242, pr_id="",
        request_payload=_gate_request(store, "codex_gate", 4242),
    )
    recommitted, _written = gate_outcome.commit_gate_outcome(
        gate_outcome.GateOutcome.from_record("codex_gate", dict(run_b), pr_id=""),
        results_dir=store.results,
        result_path=gate_recorder.result_file_path(store.results, "codex_gate", pr_number=4242, pr_id=""),
    )

    assert run_a["status"] == run_b["status"] == "unavailable"
    ids = [run_a[FIELD], run_b[FIELD], recommitted[FIELD]]
    for execution_id in ids:
        _assert_minted(execution_id)
    assert len(set(ids)) == 2, ids
    assert recommitted[FIELD] == run_b[FIELD]
    assert _slot(store, "codex_gate", 4242)[FIELD] == run_b[FIELD]


def test_a_run_that_never_started_carries_no_id(store, monkeypatch):
    """A refusal (nobody registered a provider for this gate) is no run: no id on the record."""
    runner = GateRunner(state_dir=store.state, reports_dir=store.reports)
    record = runner.run(
        gate="nobody_registered_this_gate", pr_number=4242, pr_id="",
        request_payload=_gate_request(store, "nobody_registered_this_gate", 4242),
    )

    assert record["status"] == "not_executable" and record["reason"] == "unsupported_gate_type"
    assert FIELD not in record


def _request_on_disk(store, gate: str, pr: int) -> dict:
    """The request record as the last writer left it — the payload a retry is handed."""
    path = store.state / "review_gates" / "requests" / f"pr-{pr}-{gate}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_refused_retry_carries_no_id_inherited_from_the_earlier_run(store, monkeypatch):
    """A retry refused before execution must not stamp the id the OUTAGE left on its request.

    Reproduced (codex, round 2): run codex into a timeout (``unavailable``), reuse
    the request record that run left behind for a retry, and take the provider
    binary away. The retry never reaches ``_mark_executing``, so it is no run and
    names no execution. The cost ledger is keyed by this id, so an inherited one
    would book this refusal as the earlier execution.
    """
    timeout = {
        "status": "failed", "reason": "timeout", "reason_detail": "the run exceeded 900 s",
        "stdout": "", "stderr": "", "duration_seconds": 900.0,
        "partial_output_lines": 0, "runner_pid": 4242,
    }
    runner = _codex_runner(store, monkeypatch, timeout)
    outage = runner.run(
        gate="codex_gate", pr_number=4242, pr_id="",
        request_payload=_gate_request(store, "codex_gate", 4242),
    )
    assert outage["status"] == "unavailable"
    _assert_minted(outage[FIELD])
    retry_payload = _request_on_disk(store, "codex_gate", 4242)
    assert retry_payload[FIELD] == outage[FIELD], "the request the retry is handed names the outage"

    monkeypatch.setattr(gate_runner.shutil, "which", lambda _binary: None)
    refused = runner.run(
        gate="codex_gate", pr_number=4242, pr_id="", request_payload=retry_payload,
    )

    assert refused["status"] == "not_executable" and refused["reason"] == "provider_not_installed"
    assert FIELD not in refused
    assert FIELD not in _request_on_disk(store, "codex_gate", 4242)


def test_a_retry_that_reaches_execution_mints_a_new_id(store, monkeypatch):
    """The same inherited payload, a run that DOES start: the new run gets its own id."""
    inherited = gate_status.new_execution_id()
    runner = _harness_runner(store, monkeypatch, _PASS_BODY)

    record = runner.run(
        gate="kimi_gate", pr_number=4242, pr_id="",
        request_payload=_gate_request(store, "kimi_gate", 4242, **{FIELD: inherited}),
    )

    assert record["status"] == "completed"
    _assert_minted(record[FIELD])
    assert record[FIELD] != inherited
    assert _request_on_disk(store, "kimi_gate", 4242)[FIELD] == record[FIELD]


# ---------------------------------------------------------------------------
# (3) --reprocess: no new id, the original run's id or none
# ---------------------------------------------------------------------------


def test_a_reprocess_carries_the_original_runs_id(store, monkeypatch):
    _, live = _kimi_live(store, monkeypatch, report=_kimi_report())
    _assert_minted(live[FIELD])

    rc, rebuilt = _kimi_reprocess(store, monkeypatch, live["dispatch_id"])

    assert rc == 0 and rebuilt["evidence_source"] == "reprocessed"
    assert rebuilt[FIELD] == live[FIELD]
    assert _slot(store, "kimi_gate", 4242)[FIELD] == live[FIELD]


def test_a_reprocess_of_a_run_without_a_recorded_id_carries_none(store, monkeypatch):
    """The run made no model call, so a fresh id would name an execution that never happened."""
    _, live = _kimi_live(store, monkeypatch, report=_kimi_report())
    stored = _slot(store, "kimi_gate", 4242)
    stored.pop(FIELD)
    (store.results / "pr-4242-kimi_gate.json").write_text(json.dumps(stored, indent=2), encoding="utf-8")

    rc, rebuilt = _kimi_reprocess(store, monkeypatch, live["dispatch_id"])

    assert rc == 0 and rebuilt["evidence_source"] == "reprocessed"
    assert FIELD not in rebuilt
    assert FIELD not in _slot(store, "kimi_gate", 4242)


# ---------------------------------------------------------------------------
# (4) K2b: the live CLI run's ledger line names the same execution
# ---------------------------------------------------------------------------


def test_the_k2b_line_of_a_live_cli_run_carries_the_runs_id(store, monkeypatch):
    _, record = _kimi_live(
        store, monkeypatch, report=_kimi_report(measured=True, input_tokens=1000, output_tokens=50),
    )
    _assert_minted(record[FIELD])

    (line,) = _ledger_lines(store)
    assert line[FIELD] == record[FIELD]
    assert line["provider"] == "kimi" and line["role"] == "review"


def test_the_ledger_refuses_a_lookalike_execution_id(store):
    """A line whose id K3-2b cannot match is refused, never written (fail-closed)."""
    with pytest.raises(cost_ledger.InvalidRecord):
        cost_ledger.build_record(cost_ledger.RunCost(
            run_id=f"{PROJECT}:kimi_gate:ref:1", project_id=PROJECT, role="review",
            provider="kimi", model=KIMI_MODEL, occurred_from="2026-10-28T10:00:00Z",
            occurred_to="2026-10-28T10:01:00Z", tokens=dict.fromkeys(cost_ledger.TOKEN_FIELDS),
            usage_completeness="unknown:cli_reports_no_usage", billing="subscription",
            execution_id="not-an-id",
        ))


def test_a_request_carrying_no_minted_id_stamps_nothing():
    """The stamp is positive evidence only: an absent or lookalike id leaves the field absent."""
    for request_value in ({}, {FIELD: ""}, {FIELD: "ABCDEF"}, {FIELD: None}):
        assert FIELD not in gate_recorder.stamp_request_identity({}, request_value)

    minted = gate_status.new_execution_id()
    assert gate_recorder.stamp_request_identity({}, {FIELD: minted})[FIELD] == minted
