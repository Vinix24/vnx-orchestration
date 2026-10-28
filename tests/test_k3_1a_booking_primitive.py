"""tests/test_k3_1a_booking_primitive.py — one ledger line per execution identity (K3-1a).

Track ``kosten-per-model-in-het-grootboek`` (ADR-043, ADR-049). K3 (#2329) was
closed after four codex rounds because every round booked a cost run at the
result slot's counter (``attempt_seq``): slot writes and ledger attempts are
different counters, so the same execution could book twice (``run-b:1`` and
``run-b:2``) or not at all. This PR provides the half that lives in the ledger:
the key is a caller-supplied execution identity.

- ``cost_ledger.record_once(project_id, producer, execution_id, make_run, ...)``
  appends one line for ``(project_id, producer, execution_id)`` under the
  ledger's own lock, or returns the line already there and writes nothing. The
  line's ``run_id`` is derived from that key alone.
- No identity, no line: a missing, ``None``, empty, non-string or non-``uuid4``
  ``execution_id`` raises ``InvalidExecutionId`` before any lock is taken.
- ``harness_run_cost.AttemptRun`` carries the required ``execution_id``;
  ``record_attempt_run`` books through ``record_once``, a door refusal books
  ``unavailable:<reason>`` with amount 0, and ``never_started`` books nothing.
- ``harness_run_cost.ledger_project_id`` reads the tenant from the store path
  first (ADR-007), so an ambient ``VNX_PROJECT_ID`` never overrides it.

The ledger files are real; nothing is patched but paths.
"""

from __future__ import annotations

import json
import multiprocessing
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "scripts"), str(REPO_ROOT / "scripts" / "lib")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import cost_ledger  # noqa: E402
import harness_run_cost  # noqa: E402
import provider_costs  # noqa: E402

PROJECT = "vnx-dev"
PRODUCER = "glm_gate"
EXEC_A = "a" * 32
EXEC_B = "b" * 32
STARTED = datetime(2026, 10, 27, 10, 0, 0, tzinfo=timezone.utc)
ENDED = datetime(2026, 10, 27, 10, 1, 0, tzinfo=timezone.utc)
_ZERO = {"input_uncached": 0, "cache_hit": 0, "cache_write_5m": 0, "cache_write_1h": 0, "output": 0}


def _make_run(run_id: str) -> "cost_ledger.RunCost":
    return cost_ledger.RunCost(
        run_id=run_id, project_id=PROJECT, role="review", provider="glm-harness", model="glm-5.2",
        billing="metered", occurred_from="2026-10-27T10:00:00Z", occurred_to="2026-10-27T10:01:00Z",
        tokens={**_ZERO, "input_uncached": 1000, "output": 200}, usage_completeness="complete",
        cost_usd=0.5, cost_source="provider_reported",
    )


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    return tmp_path / "store" / "events" / "provider_costs.ndjson"


def _lines(ledger_path: Path) -> list:
    if not ledger_path.is_file():
        return []
    return [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _spool_dir(ledger_path: Path) -> Path:
    return cost_ledger.spool_dir_for(ledger_path)


def _lock_file(ledger_path: Path) -> Path:
    return provider_costs.ledger_lock_path(ledger_path)


def _assert_nothing_written(ledger_path: Path) -> None:
    assert _lines(ledger_path) == []
    assert not _lock_file(ledger_path).exists()
    assert not _spool_dir(ledger_path).exists()


def _book_one(payload: tuple) -> None:
    """A child process: book one key on the shared ledger."""
    ledger_path, execution_id, gate = payload
    gate.wait(timeout=30)
    cost_ledger.record_once(PROJECT, PRODUCER, execution_id, _make_run, ledger_path=Path(ledger_path))


# ---------------------------------------------------------------------------
# One key, one line
# ---------------------------------------------------------------------------


def test_the_same_key_twice_writes_one_line_and_returns_it(ledger):
    first = cost_ledger.record_once(PROJECT, PRODUCER, EXEC_A, _make_run, ledger_path=ledger)
    second = cost_ledger.record_once(PROJECT, PRODUCER, EXEC_A, _make_run, ledger_path=ledger)

    assert _lines(ledger) == [first]
    assert dict(second) == dict(first)
    assert second["run_id"] == f"{PROJECT}:{PRODUCER}:{EXEC_A}:1"


def test_the_run_id_is_derived_from_the_key_alone(ledger):
    record = cost_ledger.record_once(PROJECT, PRODUCER, EXEC_A, _make_run, ledger_path=ledger)

    assert record["run_id"] == cost_ledger.execution_run_id(PROJECT, PRODUCER, EXEC_A)
    assert record["project_id"] == PROJECT


def test_run_a_run_b_then_b_again_then_a_again_is_two_lines(ledger):
    """The round-4 reproduction, expressed in execution keys: only two executions happened."""
    cost_ledger.record_once(PROJECT, PRODUCER, EXEC_A, _make_run, ledger_path=ledger)
    cost_ledger.record_once(PROJECT, PRODUCER, EXEC_B, _make_run, ledger_path=ledger)
    again_b = cost_ledger.record_once(PROJECT, PRODUCER, EXEC_B, _make_run, ledger_path=ledger)
    again_a = cost_ledger.record_once(PROJECT, PRODUCER, EXEC_A, _make_run, ledger_path=ledger)

    lines = _lines(ledger)
    assert len(lines) == 2
    assert [line["run_id"] for line in lines] == [
        f"{PROJECT}:{PRODUCER}:{EXEC_A}:1", f"{PROJECT}:{PRODUCER}:{EXEC_B}:1",
    ]
    assert again_b["run_id"] == lines[1]["run_id"]
    assert again_a["run_id"] == lines[0]["run_id"]


def test_two_processes_booking_one_key_write_one_line(ledger):
    ledger.parent.mkdir(parents=True, exist_ok=True)
    context = multiprocessing.get_context("spawn")
    gate = context.Barrier(2)
    processes = [
        context.Process(target=_book_one, args=((str(ledger), EXEC_A, gate),)) for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=60)
        assert process.exitcode == 0, f"child exited with {process.exitcode}"

    assert [line["run_id"] for line in _lines(ledger)] == [f"{PROJECT}:{PRODUCER}:{EXEC_A}:1"]


# ---------------------------------------------------------------------------
# No identity, no line
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "execution_id",
    [pytest.param(None, id="none"), pytest.param("", id="empty"), pytest.param(42, id="int"),
     pytest.param("run-b", id="not-hex"), pytest.param("A" * 32, id="uppercase"),
     pytest.param("a" * 31, id="short"), pytest.param("a" * 33, id="long")],
)
def test_an_invalid_execution_id_is_a_named_error_and_books_nothing(ledger, execution_id):
    with pytest.raises(cost_ledger.InvalidExecutionId) as raised:
        cost_ledger.record_once(PROJECT, PRODUCER, execution_id, _make_run, ledger_path=ledger)

    assert isinstance(raised.value, ValueError)
    _assert_nothing_written(ledger)


def test_a_missing_execution_id_is_a_named_error_and_books_nothing(ledger):
    with pytest.raises(TypeError):
        cost_ledger.record_once(
            project_id=PROJECT, producer=PRODUCER, make_run=_make_run, ledger_path=ledger,
        )
    _assert_nothing_written(ledger)


def test_the_same_key_under_another_producer_or_tenant_is_another_line(ledger):
    """The producer and the project are part of the key: the identity is the whole tuple."""
    cost_ledger.record_once(PROJECT, PRODUCER, EXEC_A, _make_run, ledger_path=ledger)
    cost_ledger.record_once(PROJECT, "codex_gate", EXEC_A, _make_run, ledger_path=ledger)

    assert [line["run_id"] for line in _lines(ledger)] == [
        f"{PROJECT}:{PRODUCER}:{EXEC_A}:1", f"{PROJECT}:codex_gate:{EXEC_A}:1",
    ]


# ---------------------------------------------------------------------------
# An attempt: refused books zero, never started books nothing
# ---------------------------------------------------------------------------


def _attempt(**overrides) -> "harness_run_cost.AttemptRun":
    fields = dict(
        provider="glm-harness", model="glm-5.2", execution_id=EXEC_A,
        started=STARTED, ended=ENDED, outcome=harness_run_cost.ATTEMPT_REFUSED, reason="delivery_refused",
    )
    fields.update(overrides)
    return harness_run_cost.AttemptRun(**fields)


def test_a_refused_attempt_books_unavailable_with_amount_zero(ledger):
    record, reason = harness_run_cost.record_attempt_run(
        _attempt(), project_id=PROJECT, producer=PRODUCER, ref="glm-gate-pr1", role="review",
        ledger_path=ledger,
    )

    assert reason is None
    assert record["cost_source"] == "unavailable:delivery_refused"
    assert record["cost_usd"] == 0.0
    assert record["usage_completeness"] == "complete"
    assert _lines(ledger) == [record]


def test_a_never_started_attempt_books_nothing(ledger):
    record, reason = harness_run_cost.record_attempt_run(
        _attempt(outcome=harness_run_cost.ATTEMPT_NEVER_STARTED, reason=""), project_id=PROJECT,
        producer=PRODUCER, ref="glm-gate-pr1", role="review", ledger_path=ledger,
    )

    assert (record, reason) == (None, None)
    _assert_nothing_written(ledger)


def test_a_ran_attempt_without_usage_books_unknown_never_zero(ledger):
    record, reason = harness_run_cost.record_attempt_run(
        _attempt(outcome=harness_run_cost.ATTEMPT_RAN, reason=""), project_id=PROJECT,
        producer=PRODUCER, ref="glm-gate-pr1", role="review", ledger_path=ledger,
    )

    assert reason is None
    assert record["usage_completeness"] == "unknown:no_usage_fields"
    assert record["cost_usd"] is None
    assert _lines(ledger) == [record]


def test_committing_one_execution_twice_books_one_line(ledger):
    first, _ = harness_run_cost.record_attempt_run(
        _attempt(), project_id=PROJECT, producer=PRODUCER, ref="glm-gate-pr1", role="review",
        ledger_path=ledger,
    )
    second, _ = harness_run_cost.record_attempt_run(
        _attempt(), project_id=PROJECT, producer=PRODUCER, ref="glm-gate-pr1", role="review",
        ledger_path=ledger,
    )

    assert dict(second) == dict(first)
    assert len(_lines(ledger)) == 1


def test_an_attempt_without_an_execution_id_books_nothing_and_names_why(ledger):
    record, reason = harness_run_cost.record_attempt_run(
        _attempt(execution_id=None), project_id=PROJECT, producer=PRODUCER, ref="glm-gate-pr1",
        role="review", ledger_path=ledger,
    )

    assert record is None
    assert reason.startswith("InvalidExecutionId: ")
    _assert_nothing_written(ledger)


# ---------------------------------------------------------------------------
# The tenant comes from the store, not the ambient env (round 3)
# ---------------------------------------------------------------------------


def test_the_store_names_its_own_tenant_over_the_ambient_env(tmp_path, monkeypatch):
    state_dir = tmp_path / ".vnx-data" / "tenant-b" / "state"
    state_dir.mkdir(parents=True)
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")

    assert harness_run_cost.ledger_project_id(state_dir) == "tenant-b"


def test_a_store_that_names_no_project_falls_back_to_the_env(tmp_path, monkeypatch):
    state_dir = tmp_path / "store" / "state"
    state_dir.mkdir(parents=True)
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-env")

    assert harness_run_cost.ledger_project_id(state_dir) == "vnx-env"


def test_a_path_the_layout_does_not_recognise_names_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-env")

    assert harness_run_cost.ledger_project_id(tmp_path / "elsewhere") == "vnx-env"


# ---------------------------------------------------------------------------
# The door's reason: an unknown failure is not a refusal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("delivery_refused: lane busy", "delivery_refused"),
        ("REJECT [paid_lane_daily_budget_exceeded]", "paid_lane_daily_budget_exceeded"),
        ("the run timed out after 900s", None),
        ("", None),
        (None, None),
    ],
)
def test_door_refusal_reason_reads_only_a_provable_refusal(text, expected):
    """An unrecognised message is ``None``: the caller books unknown, never a zero."""
    assert harness_run_cost.door_refusal_reason(text) == expected


@pytest.mark.parametrize(
    "reason,expected",
    [("delivery_refused", "delivery_refused"), ("a whole paragraph, not a token", "spawn_refused"),
     (None, "spawn_refused"), ("", "spawn_refused")],
)
def test_attempt_reason_is_a_bounded_token_or_the_fallback(reason, expected):
    assert harness_run_cost.attempt_reason(reason, "spawn_refused") == expected
