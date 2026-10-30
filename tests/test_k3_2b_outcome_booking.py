"""tests/test_k3_2b_outcome_booking.py — one ledger line per execution at the outcome commit (K3-2b).

Track ``kosten-per-model-in-het-grootboek``, ADR-043. K3 (#2329) was closed
after four codex rounds (OI-2444). Round 4 measured it on
``gate_outcome.commit_gate_outcome``: it booked at the slot's ``attempt_seq``,
and the slot's counter and the ledger's attempts are different counters, so a
commit timeout of run-a, a commit timeout of run-b and a re-commit of run-b's
recorded record produced THREE lines for TWO executions.

The booking now happens at the outcome commit, keyed on the record's own
``execution_id`` (K3-2a's identity, K3-1a's primitive):
``harness_run_cost.record_attempt_run`` → ``cost_ledger.record_once``. The same
execution committed twice is one line, whether the slot took its record, took
it again, or refused it; a separate execution whose record was refused gets its
own. No id, no line: a record from before K3-2a is never booked under a guessed
identity.

The result store and the cost ledger are REAL files in ``tmp_path``. Nothing is
patched but the forge publication (which would leave the machine) and the
paths.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import cost_ledger  # noqa: E402
import gate_outcome  # noqa: E402
import gate_recorder  # noqa: E402
import gate_status  # noqa: E402
import harness_run_cost  # noqa: E402
import provider_costs  # noqa: E402
import review_stack_support  # noqa: E402

GATE = "deepseek_gate"
PROVIDER = "deepseek-harness"
MODEL = "deepseek-flash"
KIMI_GATE, KIMI_PROVIDER, KIMI_MODEL = "kimi_gate", "kimi", "kimi-k3"
PR = 4242
HEAD = "c" * 40
EXEC_A = "a" * 32
EXEC_B = "b" * 32
NO_ID = "unbooked_no_execution_id"
STARTED = datetime(2026, 10, 30, 10, 0, 0, tzinfo=timezone.utc)
ENDED = datetime(2026, 10, 30, 10, 15, 0, tzinfo=timezone.utc)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A throwaway project store whose cost ledger is a real file under ``data_dir/events``."""
    env = review_stack_support.build_project_store(tmp_path, monkeypatch)
    env["ledger"] = env["data_dir"] / "events" / "provider_costs.ndjson"
    env["project"] = harness_run_cost.ledger_project_id(env["state_dir"])
    return env


def _lines(store) -> list:
    """Every cost-ledger run line in the store, in write order."""
    if not store["ledger"].exists():
        return []
    return [
        record for record in (
            json.loads(line) for line in store["ledger"].read_text(encoding="utf-8").splitlines() if line.strip()
        )
        if record.get("schema") == cost_ledger.RUN_SCHEMA
    ]


def _run_ids(store) -> list:
    return [line["run_id"] for line in _lines(store)]


def _timeout(execution_id: str = EXEC_A, *, gate: str = GATE, provider: str = PROVIDER,
             model: str = MODEL, dispatch: str = "deepseek-gate-pr4242-1", recorded: str = "2026-10-30T10:15:00Z",
             head: str = HEAD) -> dict:
    """A gate run that started (it has an id) and ended in a timeout: ``unavailable``, usage unknown."""
    return {
        "gate": gate, "status": "unavailable", "reason": "timeout",
        "reason_detail": "the run exceeded 900 s", "commit_sha": head, "branch": "dispatch/k3-2b",
        "pr_number": PR, "pr_id": "", "provider": provider, "model": model,
        "dispatch_id": dispatch, "duration_seconds": 900.0, "recorded_at": recorded,
        "stdout": "", "stderr": "", "partial_output_lines": 0, "runner_pid": 4321,
        "execution_id": execution_id,
    }


def _pass(execution_id: str = EXEC_A, *, head: str = HEAD, evidence: str = "h1") -> dict:
    """A decided, evidenced verdict: the overwrite guard protects this record."""
    return {
        "gate": GATE, "status": "pass", "commit_sha": head, "branch": "dispatch/k3-2b",
        "pr_number": PR, "pr_id": "", "provider": PROVIDER, "model": MODEL,
        "dispatch_id": "deepseek-gate-pr4242-1", "contract_hash": evidence,
        "report_path": f"/tmp/{evidence}.md", "blocking_findings": [], "advisory_findings": [],
        "residual_risk": "", "recorded_at": "2026-10-30T10:15:00Z", "execution_id": execution_id,
    }


def _commit(store, record: dict):
    """Commit one record through the one writer; ``(payload on disk, written)`` as it returns them."""
    gate = record["gate"]
    slot = gate_recorder.result_file_path(store["results_dir"], gate, pr_number=PR, pr_id="")
    return gate_outcome.commit_gate_outcome(
        gate_outcome.GateOutcome.from_record(gate, dict(record), pr_id=""),
        results_dir=store["results_dir"], result_path=slot,
    )


def _slot(store, gate: str = GATE) -> dict:
    return json.loads((store["results_dir"] / f"pr-{PR}-{gate}.json").read_text(encoding="utf-8"))


def _unbooked(store) -> int:
    return harness_run_cost.booking_refusal_counts().get(NO_ID, 0)


def _gate_outcome_warnings(caplog) -> list:
    return [r for r in caplog.records if r.levelno == logging.WARNING and r.name == "gate_outcome"]


# ---------------------------------------------------------------------------
# The round-4 reproduction
# ---------------------------------------------------------------------------


def test_the_round_4_reproduction_is_two_lines_for_two_executions(store):
    """commit run-a, commit run-b, re-commit run-b's record, commit run-a's record again: TWO lines.

    The closed K3 booked at the slot's ``attempt_seq``, so the third and fourth
    commits got fresh numbers and booked ``run-a:1``/``run-b:1`` twice over. The
    identity is the execution now, so only two executions exist and only two
    lines are written.
    """
    stored_a, written_a = _commit(store, _timeout(EXEC_A, recorded="2026-10-30T10:15:00Z"))
    stored_b, written_b = _commit(store, _timeout(EXEC_B, recorded="2026-10-30T10:45:00Z"))
    assert written_a is True and written_b is True

    _commit(store, stored_b)
    _commit(store, stored_a)

    assert _run_ids(store) == [
        f"{store['project']}:{GATE}:{EXEC_A}:1", f"{store['project']}:{GATE}:{EXEC_B}:1",
    ]
    assert _slot(store)["execution_id"] == EXEC_A, "the last commit's record is the one on the slot"


def test_one_record_committed_twice_books_one_line(store):
    record = _timeout()
    _commit(store, record)
    _commit(store, record)

    assert len(_lines(store)) == 1
    assert _slot(store)["attempt_seq"] == 2, "the slot still counts its own writes"


# ---------------------------------------------------------------------------
# A refused overwrite of a different execution
# ---------------------------------------------------------------------------


def test_a_refused_overwrite_of_a_separate_execution_books_its_own_line(store):
    """The pass is protected on this head; the second run's record is refused — and still cost money."""
    _commit(store, _pass(EXEC_A))

    refused, written = _commit(store, _timeout(EXEC_B, head=HEAD))

    assert written is False
    assert _slot(store)["status"] == "pass" and _slot(store)["execution_id"] == EXEC_A, (
        "the slot keeps the decided verdict"
    )
    assert _run_ids(store) == [
        f"{store['project']}:{GATE}:{EXEC_A}:1", f"{store['project']}:{GATE}:{EXEC_B}:1",
    ]


# ---------------------------------------------------------------------------
# The three exclusions
# ---------------------------------------------------------------------------


def _k2b_books(store, execution_id: str, *, gate: str = KIMI_GATE, provider: str = KIMI_PROVIDER,
               model: str = KIMI_MODEL) -> dict:
    """K2b's own booking at the lane's measured end: ``<project>:<gate>:<ref>:<n>``, naming the execution."""
    run = harness_run_cost.cli_run(
        provider=provider, model=model, started=STARTED, ended=ENDED, execution_id=execution_id,
        token_usage={"input_tokens": 1000, "output_tokens": 50},
    )
    record, reason = harness_run_cost.record_kimi_run(
        run, project_id=store["project"], producer=gate, ref=f"{gate}-pr{PR}-1",
        ledger_path=provider_costs.costs_path_in(store["data_dir"]),
    )
    assert reason is None and record is not None, reason
    return record


def test_a_run_k2b_booked_gets_no_second_line(store):
    """K2b measured this run at its own end; the commit must find that line, not book another."""
    k2b = _k2b_books(store, EXEC_A)

    _commit(store, _timeout(EXEC_A, gate=KIMI_GATE, provider=KIMI_PROVIDER, model=KIMI_MODEL))

    assert _run_ids(store) == [k2b["run_id"]]
    assert _lines(store)[0]["execution_id"] == EXEC_A


def test_reprocessed_evidence_books_nothing(store):
    """``--reprocess`` rebuilds a report an earlier run wrote: it calls no model, so it costs nothing."""
    record = _pass(EXEC_A)
    record[gate_status.EVIDENCE_SOURCE_FIELD] = gate_status.EVIDENCE_SOURCE_REPROCESSED

    _commit(store, record)

    assert _lines(store) == []


def test_a_run_that_never_started_books_nothing(store):
    """A refusal names no execution; it is not an anomaly and it is not a line either."""
    record = {
        "gate": GATE, "status": "not_executable", "reason": "provider_not_installed",
        "reason_detail": "the provider binary is not on PATH", "commit_sha": HEAD,
        "pr_number": PR, "pr_id": "", "provider": PROVIDER, "model": MODEL,
        "dispatch_id": "deepseek-gate-pr4242-2",
    }

    _commit(store, record)

    assert _lines(store) == []


# ---------------------------------------------------------------------------
# No id, no line — and one WARNING, counted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["absent", None, "", "a" * 31])
def test_a_started_run_without_a_valid_id_books_nothing_and_is_counted(store, caplog, field):
    record = _timeout(dispatch="deepseek-gate-pr4242-7")
    if field == "absent":
        record.pop("execution_id")
    else:
        record["execution_id"] = field
    before = _unbooked(store)

    with caplog.at_level(logging.WARNING, logger="gate_outcome"):
        _commit(store, record)

    assert _lines(store) == [], "no id, no line — never a guessed identity"
    (warning,) = _gate_outcome_warnings(caplog)
    assert NO_ID in warning.getMessage()
    assert "deepseek-gate-pr4242-7" in warning.getMessage(), "the WARNING names the dispatch"
    assert _unbooked(store) == before + 1


def test_a_never_started_run_without_an_id_is_not_counted(store, caplog):
    """The refused retry of K3-2a round 3 carries no id BY DESIGN: no line, no warning, no count."""
    record = {"gate": GATE, "status": "not_executable", "reason": "unsupported_gate_type",
              "commit_sha": HEAD, "pr_number": PR, "pr_id": "", "dispatch_id": "deepseek-gate-pr4242-3"}
    before = _unbooked(store)

    with caplog.at_level(logging.WARNING, logger="gate_outcome"):
        _commit(store, record)

    assert _lines(store) == []
    assert _gate_outcome_warnings(caplog) == []
    assert _unbooked(store) == before


def test_an_orphan_cleanup_record_is_not_a_run(store, caplog):
    """GE-8's cleanup ends a stale request it found in a scan: no line, and no anomaly either.

    It carries a provider and a model like a real run, so nothing but the
    ``abandoned_orphan_request`` reason can keep it out of the count — that
    record is a scan's answer, not a run whose identity went missing.
    """
    record = {
        "gate": GATE, "status": "unavailable", "reason": "abandoned_orphan_request",
        "abandoned_by": "cleanup_orphan_gates", "age_hours": 12.5, "commit_sha": HEAD,
        "pr_number": PR, "pr_id": "", "provider": PROVIDER, "model": MODEL,
        "dispatch_id": "deepseek-gate-pr4242-9",
    }
    before = _unbooked(store)

    with caplog.at_level(logging.WARNING, logger="gate_outcome"):
        _commit(store, record)

    assert _lines(store) == []
    assert _gate_outcome_warnings(caplog) == []
    assert _unbooked(store) == before


def test_the_inherited_id_case_books_one_line_and_never_warns(store, caplog):
    """An outage that minted an id, then a retry refused before ``executing``: ONE line, no warning.

    This is K3-2a round 3's shape end to end. The refused retry inherits the
    outage's id on the request record it was handed, and ``GateRunner.run`` pops
    it before the first refusal — so the record it commits names no execution,
    and must neither book a second line nor be counted as an anomaly.
    """
    _commit(store, _timeout(EXEC_A, dispatch="deepseek-gate-pr4242-4"))
    assert len(_lines(store)) == 1
    before = _unbooked(store)

    refused = {"gate": GATE, "status": "not_executable", "reason": "provider_not_installed",
               "reason_detail": "the provider binary is not on PATH", "commit_sha": HEAD,
               "pr_number": PR, "pr_id": "", "provider": PROVIDER, "model": MODEL,
               "dispatch_id": "deepseek-gate-pr4242-5"}
    with caplog.at_level(logging.WARNING, logger="gate_outcome"):
        _commit(store, refused)

    assert _run_ids(store) == [f"{store['project']}:{GATE}:{EXEC_A}:1"]
    assert _gate_outcome_warnings(caplog) == []
    assert _unbooked(store) == before
    assert _slot(store)["status"] == "not_executable"


# ---------------------------------------------------------------------------
# A booking never changes a verdict
# ---------------------------------------------------------------------------


def test_a_booking_the_ledger_refuses_leaves_the_outcome_unchanged(store, caplog):
    """The store's ``events`` path is a file, so no line can be written: the record lands and still decides."""
    store["ledger"].parent.write_text("this is not a directory")

    with caplog.at_level(logging.WARNING, logger="harness_run_cost"):
        stored, written = _commit(store, _pass(EXEC_A))

    assert written is True and stored["status"] == "pass"
    assert _slot(store)["status"] == "pass"
    assert _lines(store) == [], "nothing could be written, and the verdict stands"
    refused = [
        r for r in caplog.records if r.levelno == logging.WARNING and r.name == "harness_run_cost"
    ]
    assert refused, "a refused booking is never silent"
    assert "CostLedgerWriteError" in refused[0].getMessage()


# ---------------------------------------------------------------------------
# One format check for both modules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [EXEC_A, "0" * 32, None, "", "nope", "A" * 32, "a" * 31, "a" * 33, 42, b"a" * 32, "a" * 31 + "g"],
)
def test_cost_ledger_and_gate_status_give_the_same_answer_on_every_id(value):
    """The format lives in ONE place; the ledger ASKS it instead of re-deriving it (K3-2b)."""
    accepted = gate_status.is_execution_id(value)
    try:
        run_id = cost_ledger.execution_run_id("vnx-dev", GATE, value)
    except cost_ledger.InvalidExecutionId:
        assert accepted is False
        return
    assert accepted is True
    assert run_id == f"vnx-dev:{GATE}:{value}:1"
