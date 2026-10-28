"""tests/test_k3_attempt_ledger_lines.py — every attempt is one ledger line (K3).

Track ``kosten-per-model-in-het-grootboek`` (ADR-043). K2b books a codex/kimi
gate run where that lane reports its own usage. Every other run ends somewhere
else — a gate's committed outcome, a plan-gate seat's dispatcher call, a
tiebreaker's answer — and until K3 those ends booked nothing at all. K3 books
one line per attempt AT that end:

- a gate attempt is booked by ``commit_gate_outcome`` after the slot lock, so
  the record, its receipt, its request end and its obligation are all on disk
  first and a slow or broken ledger can never touch them;
- a plan-gate seat books in ``_dispatch_one`` (its retry and re-extraction are
  attempts of their own);
- both tiebreakers book where their one model call returns.

The identity on every line is the RUNNER's: the provider and model the lane was
launched with, or the registry's for a gate that never launched. Never a model
named in the report text. A run that provably never started (a door refusal,
``lane_exhausted``, ``spawn_refused``, ``not_executable``) books a confirmed
zero; a run that started without usage books unknown, never zero.

The stores, records and writers here are real; only the model boundary is a
fake dispatcher.
"""
from __future__ import annotations

import errno
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(VNX_ROOT / "scripts"), str(VNX_ROOT / "scripts" / "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import cost_ledger  # noqa: E402
import gate_outcome  # noqa: E402
import gate_recorder  # noqa: E402
import gate_runner  # noqa: E402
import gate_status  # noqa: E402
import harness_run_cost  # noqa: E402
import plan_gate_panel as pgp  # noqa: E402
import plan_gate_tiebreaker as pgt  # noqa: E402
import review_cap_tiebreaker as rct  # noqa: E402
import review_cap_verdicts as rcv  # noqa: E402
import review_stack_support  # noqa: E402

GATE = "glm_gate"
PR = 4407
BRANCH = "dispatch/k3"
HEAD = "a" * 40
GLM_MODEL = "glm-5.2"
RECORDED_AT = "2026-10-27T10:01:00Z"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    return review_stack_support.build_project_store(tmp_path, monkeypatch)


def _ledger_path(env) -> Path:
    return env["data_dir"] / "events" / "provider_costs.ndjson"


def _lines(env) -> list:
    """Every ``provider_cost_run/1`` line in the store, in write order."""
    path = _ledger_path(env)
    if not path.exists():
        return []
    return [
        record for record in (
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
        )
        if record.get("schema") == cost_ledger.RUN_SCHEMA
    ]


def _record(status: str = "pass", *, pr_number: int | None = None, **over) -> dict:
    record = {
        "gate": GATE, "pr_id": "", "pr_number": pr_number or PR, "status": status,
        "commit_sha": HEAD, "branch": BRANCH, "contract_hash": "hash-k3",
        "report_path": "/tmp/does-not-exist.md", "dispatch_id": f"glm-gate-pr{pr_number or PR}-1",
        "blocking_findings": [] if status == "pass" else [{"message": "a real defect"}],
        "provider": "glm-harness", "model": GLM_MODEL,
        "duration_seconds": 61.5, "recorded_at": RECORDED_AT,
    }
    record.update(over)
    return record


def _commit(env, *, gate: str = GATE, status: str = "pass", record: dict | None = None, request=None):
    outcome = gate_outcome.GateOutcome(
        gate=gate, status=status, pr_id="", pr_number=(record or {}).get("pr_number", PR),
        record=record or _record(status),
    )
    return gate_outcome.commit_gate_outcome(
        outcome, results_dir=env["results_dir"], request=request,
    )


def _request(env, **extra):
    payload = {
        "gate": GATE, "status": "executing", "pr_number": PR, "pr_id": str(PR),
        "branch": BRANCH, "commit_sha": HEAD, "contract_hash": "hash-k3", **extra,
    }
    gate_recorder.persist_request(env["requests_dir"], GATE, payload, pr_number=PR, pr_id="")
    return payload


# ---------------------------------------------------------------------------
# Acceptance 1 — one test per attempt kind, exactly one line, identity from the runner
# ---------------------------------------------------------------------------


def test_own_seat_pass_books_one_line_with_the_runners_identity(env):
    _commit(env)
    lines = _lines(env)
    assert len(lines) == 1
    line = lines[0]
    assert (line["provider"], line["model"], line["role"]) == ("glm-harness", GLM_MODEL, "review")
    assert line["run_id"].startswith("vnx-dev:glm_gate:")
    assert line["cost_source"] == "unavailable:no_usage_fields"
    assert list(line["tokens"].values()) == [None] * 5
    assert line["usage_completeness"] == "unknown:no_usage_fields"


def test_own_seat_fail_books_one_line(env):
    _commit(env, status="fail", record=_record("fail", pr_number=PR + 1))
    lines = _lines(env)
    assert len(lines) == 1
    assert lines[0]["model"] == GLM_MODEL
    assert lines[0]["cost_source"] == "unavailable:no_usage_fields"


def test_takeover_books_the_provider_that_actually_ran(env):
    record = _record(
        "fail", pr_number=PR + 2, takeover=True, takeover_from="codex_gate",
        provider="glm-harness", model=GLM_MODEL, reason="takeover_after_lane_exhausted",
    )
    _commit(env, status="fail", record=record)
    lines = _lines(env)
    assert len(lines) == 1
    assert (lines[0]["provider"], lines[0]["model"]) == ("glm-harness", GLM_MODEL)


def test_lane_exhausted_books_a_confirmed_zero(env):
    _commit(env, status="unavailable", record=_record(
        "unavailable", pr_number=PR + 3, reason="lane_exhausted",
    ))
    lines = _lines(env)
    assert len(lines) == 1
    assert lines[0]["cost_source"] == "unavailable:lane_exhausted"
    assert lines[0]["usage_completeness"] == "complete"
    assert list(lines[0]["tokens"].values()) == [0] * 5
    assert lines[0]["cost_usd"] == 0.0


def test_started_attempt_without_usage_books_unknown_never_zero(env):
    _commit(env, status="unavailable", record=_record(
        "unavailable", pr_number=PR + 4, reason="timeout", provider="deepseek-harness",
        model="deepseek-v4-pro",
    ))
    lines = _lines(env)
    assert len(lines) == 1
    assert lines[0]["cost_source"] == "unavailable:timeout"
    assert lines[0]["usage_completeness"] == "unknown:timeout"
    assert list(lines[0]["tokens"].values()) == [None] * 5
    assert "cost_usd" not in lines[0] or lines[0]["cost_usd"] is None


def test_not_executable_gate_books_a_confirmed_zero_with_the_registry_identity(env):
    _commit(env, status="not_executable", record={
        "gate": GATE, "pr_id": "", "pr_number": PR + 5, "status": "not_executable",
        "reason": "provider_switched_off", "reason_detail": "glm is switched off",
        "commit_sha": HEAD, "branch": BRANCH, "contract_hash": "hash-k3",
        "report_path": "", "recorded_at": RECORDED_AT,
    })
    lines = _lines(env)
    assert len(lines) == 1
    assert (lines[0]["provider"], lines[0]["model"]) == ("glm-harness", GLM_MODEL)
    assert lines[0]["cost_source"] == "unavailable:not_executable"
    assert list(lines[0]["tokens"].values()) == [0] * 5


def test_recorded_spawn_refusal_books_a_confirmed_zero(env):
    payload = _request(env, provider="glm-harness", model=GLM_MODEL)
    gate_recorder.record_failure(
        gate=GATE, pr_number=PR + 6, pr_id="", result={
            "reason": "spawn_refused", "reason_detail": "the lane refused before launch",
            "duration_seconds": 0.0, "partial_output_lines": 0, "runner_pid": 4242,
        },
        request_payload=payload, requests_dir=env["requests_dir"], results_dir=env["results_dir"],
    )
    lines = _lines(env)
    assert len(lines) == 1
    assert lines[0]["cost_source"] == "unavailable:spawn_refused"
    assert lines[0]["usage_completeness"] == "complete"
    assert list(lines[0]["tokens"].values()) == [0] * 5


def test_a_report_naming_another_model_does_not_change_the_line(env, tmp_path):
    report = tmp_path / "report.md"
    report.write_text("---\nprovider: codex\nmodel: gpt-6-astra\n---\nreviewed by codex\n", encoding="utf-8")
    _commit(env, record=_record(pr_number=PR + 7, report_path=str(report)))
    lines = _lines(env)
    assert len(lines) == 1
    assert (lines[0]["provider"], lines[0]["model"]) == ("glm-harness", GLM_MODEL)


# ---------------------------------------------------------------------------
# Acceptance 2 — plan-gate seats and tiebreakers each write one line
# ---------------------------------------------------------------------------


def _seat_report(verdict: str = "pass") -> str:
    body = json.dumps({"verdict": verdict, "blocking_findings": [], "rationale": "read it"})
    return f"The review.\n```{pgp.VERDICT_FENCE}\n{body}\n```\n"


def _tiebreak_report(outcome: str = "START") -> str:
    body = json.dumps({"outcome": outcome, "required_change": "", "rationale": "decided"})
    return f"Decision.\n```{pgt.TIEBREAKER_FENCE}\n{body}\n```\n"


def _run_one_seat(env, dispatcher, *, provider="deepseek-harness", model_arg="deepseek-v4-pro"):
    return pgp.run_panel(
        doc_text="Plan body", track_id="track-k3", project_id="vnx-dev",
        data_dir=str(env["data_dir"]),
        panel=[{"label": "seat-a", "provider": provider, "model_arg": model_arg}],
        dispatcher=dispatcher,
        seat_ledger_path=env["data_dir"] / "plan-gate-seats.ndjson",
    )


def test_plan_gate_seat_run_books_one_line_from_its_identity(env, monkeypatch):
    monkeypatch.setenv("VNX_PANEL_RETRY", "0")
    calls = []

    def fake(provider, model_arg, instruction, dispatch_id):
        calls.append((provider, model_arg, dispatch_id))
        return _seat_report()

    _run_one_seat(env, fake)
    assert len(calls) == 1
    lines = _lines(env)
    assert len(lines) == 1
    line = lines[0]
    assert (line["provider"], line["model"], line["role"]) == ("deepseek-harness", "deepseek-v4-pro", "plan_gate")
    assert line["run_id"] == f"vnx-dev:plan_gate:{calls[0][2]}:1"
    assert line["cost_source"] == "unavailable:no_usage_fields"


def test_plan_gate_seat_door_refusal_books_a_confirmed_zero(env, monkeypatch):
    monkeypatch.setenv("VNX_PANEL_RETRY", "0")

    def fake(provider, model_arg, instruction, dispatch_id):
        raise RuntimeError(
            "no report for x (rc=3): stderr: REJECT [paid_lane_daily_budget_exceeded] delivery_refused"
        )

    _run_one_seat(env, fake)
    lines = _lines(env)
    assert len(lines) == 1
    assert lines[0]["cost_source"] == "unavailable:paid_lane_daily_budget_exceeded"
    assert list(lines[0]["tokens"].values()) == [0] * 5


def test_a_seat_and_its_reextraction_each_book_one_line(env, monkeypatch):
    monkeypatch.setenv("VNX_PANEL_RETRY", "0")
    calls = []

    def prose_then_fence(provider, model_arg, instruction, dispatch_id):
        calls.append(dispatch_id)
        if len(calls) == 1:
            return "The plan is fine, but I wrote no verdict block.\n"
        return _seat_report()

    _run_one_seat(env, prose_then_fence)
    assert len(calls) == 2
    assert calls[0] + "-reextract" == calls[1]
    lines = _lines(env)
    assert len(lines) == 2
    assert sorted(line["run_id"] for line in lines) == sorted(
        f"vnx-dev:plan_gate:{dispatch_id}:1" for dispatch_id in calls
    )


def test_plan_gate_tiebreaker_books_one_line(env):
    result = pgt.run_tiebreaker(
        doc_text="Plan body", track_id="track-k3", round_number=1,
        dispatcher=lambda provider, model, instruction, did: _tiebreak_report("START"),
        data_dir=str(env["data_dir"]),
    )
    assert result.outcome == "START"
    lines = _lines(env)
    assert len(lines) == 1
    assert lines[0]["role"] == "tiebreaker"
    assert lines[0]["provider"] == "deepseek-harness"
    assert lines[0]["model"] == "deepseek-v4-pro"
    assert lines[0]["run_id"].startswith("vnx-dev:plan_gate_tiebreaker:plan-tiebreak-track-k3-")


def test_tiebreaker_refused_at_the_door_books_one_unavailable_line_with_the_reason(env):
    def refuse(provider, model, instruction, did):
        raise RuntimeError("no report for x (rc=2): stderr: REJECT [paid_lane_daily_budget_exceeded]")

    with pytest.raises(pgt.TiebreakerNoAnswerError):
        pgt.run_tiebreaker(
            doc_text="Plan body", track_id="track-k3", round_number=1,
            dispatcher=refuse, data_dir=str(env["data_dir"]),
        )
    lines = _lines(env)
    assert len(lines) == 1
    assert lines[0]["cost_source"] == "unavailable:paid_lane_daily_budget_exceeded"
    assert list(lines[0]["tokens"].values()) == [0] * 5


def test_review_cap_tiebreaker_books_one_line(env):
    answer = json.dumps({"outcome": rcv.TERUG_NAAR_PLAN, "rationale": "no convergence"})
    report = f"Decision.\n```{rct.FENCE}\n{answer}\n```\n"
    parsed, model = rct.ask(
        "the brief", [rcv.TERUG_NAAR_PLAN, rcv.MERGEN_MET_OI],
        dispatcher=lambda provider, model, instruction, did: report,
        data_dir=str(env["data_dir"]),
    )
    assert parsed.outcome == rcv.TERUG_NAAR_PLAN
    lines = _lines(env)
    assert len(lines) == 1
    assert lines[0]["role"] == "tiebreaker"
    assert lines[0]["model"] == model
    assert lines[0]["run_id"].startswith("vnx-dev:review_cap_tiebreaker:review-cap-tiebreak-")


# ---------------------------------------------------------------------------
# Acceptance 3 — a codex gate run K2b books stays ONE line
# ---------------------------------------------------------------------------


def test_codex_gate_run_k2b_books_yields_one_line_not_two(env):
    harness_run_cost.record_codex_run(
        harness_run_cost.cli_run(
            provider="codex", model="gpt-6-astra",
            started=datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc),
            ended=datetime(2026, 10, 27, 10, 1, tzinfo=timezone.utc),
            token_usage={"input_tokens": 100, "output_tokens": 10},
        ),
        project_id="vnx-dev", producer="codex_gate", ref=f"pr-{PR + 8}",
        ledger_path=_ledger_path(env),
    )
    assert len(_lines(env)) == 1

    record = {
        "gate": "codex_gate", "pr_id": "", "pr_number": PR + 8, "status": "pass",
        "commit_sha": HEAD, "branch": BRANCH, "contract_hash": "hash-k3",
        "report_path": "/tmp/x.md", "dispatch_id": f"pr-{PR + 8}",
        "blocking_findings": [], "provider": "codex", "model": "gpt-6-astra",
        "duration_seconds": 12.0, "recorded_at": RECORDED_AT,
        # The field gate_runner's K2b path stamps on the record it writes.
        harness_run_cost.COST_BOOKED_FIELD: harness_run_cost.COST_BOOKED_BY_K2B,
    }
    _commit(env, gate="codex_gate", record=record)
    lines = _lines(env)
    assert len(lines) == 1, f"K2b's line was booked twice: {[line['run_id'] for line in lines]}"
    assert lines[0]["provider"] == "codex"


# ---------------------------------------------------------------------------
# Acceptance 5 (K3 round 2) — the three codex reproductions
# ---------------------------------------------------------------------------


def test_a_reprocessed_outcome_books_no_second_attempt_line(env):
    """Codex r2 finding 1: committing a timed-out dispatch and then its reprocessed
    outcome booked attempts ``:1`` AND ``:2`` for one execution. ``--reprocess``
    reads a report an earlier run wrote and calls no model, so the first line stays
    the only line."""
    dispatch_id = f"glm-gate-pr{PR + 10}-1"
    _commit(env, status="unavailable", record=_record(
        "unavailable", pr_number=PR + 10, reason="timeout", dispatch_id=dispatch_id,
    ))
    first = _lines(env)
    assert [line["run_id"] for line in first] == [f"vnx-dev:{GATE}:{dispatch_id}:1"]

    _commit(env, record=_record(
        pr_number=PR + 10, dispatch_id=dispatch_id,
        evidence_source=gate_status.EVIDENCE_SOURCE_REPROCESSED,
    ))
    lines = _lines(env)
    assert [line["run_id"] for line in lines] == [
        f"vnx-dev:{GATE}:{dispatch_id}:1"
    ], f"reprocessing booked another run: {[line['run_id'] for line in lines]}"


def _codex_payload(pr_number: int) -> dict:
    return {
        "gate": "codex_gate", "status": "requested", "branch": BRANCH,
        "pr_number": pr_number, "review_mode": "per_pr", "risk_class": "medium",
        "changed_files": ["scripts/lib/gate_outcome.py"], "prompt": "Review this diff",
        "report_path": "",
    }


def test_codex_attempt_ending_before_k2b_books_one_confirmed_zero(env, monkeypatch):
    """Codex r2 finding 3a: a codex gate that returns ``not_executable`` before
    ``_book_gate_run_cost`` (a missing binary) booked ZERO lines. It must book
    exactly one confirmed zero — and only one: K2b never ran for it."""
    monkeypatch.setattr("shutil.which", lambda binary: None)
    runner = gate_runner.GateRunner(state_dir=env["state_dir"], reports_dir=env["reports_dir"])
    pr_number = PR + 11
    result = runner.run(
        gate="codex_gate", request_payload=_codex_payload(pr_number),
        pr_number=pr_number, pr_id=f"pr-{pr_number}",
    )

    assert (result["status"], result["reason"]) == ("not_executable", "provider_not_installed")
    lines = _lines(env)
    assert len(lines) == 1, f"expected one confirmed zero, got {[line['run_id'] for line in lines]}"
    line = lines[0]
    assert (line["provider"], line["model"], line["role"]) == ("codex", "gpt-6-astra", "review")
    assert line["cost_source"] == "unavailable:not_executable"
    assert list(line["tokens"].values()) == [0] * 5


def test_codex_attempt_that_reaches_k2b_books_no_k3_line(env, monkeypatch, tmp_path):
    """Codex r2 finding 3b: a codex attempt that DOES reach K2b's booking path gets
    K2b's line only. K3 must not add a second line for the same run."""
    monkeypatch.setattr("shutil.which", lambda binary: "/usr/bin/codex")
    monkeypatch.setattr(gate_runner, "create_gate_worktree", lambda **kwargs: tmp_path)
    monkeypatch.setattr(gate_runner, "remove_gate_worktree", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        gate_runner.GateRunner, "_run_with_stall_detection",
        lambda self, **kwargs: {
            "status": "failed", "reason": "timeout", "reason_detail": "deadline exceeded",
            "stdout": "", "stderr": "", "duration_seconds": 30.0,
            "partial_output_lines": 0, "runner_pid": 4242,
        },
    )
    runner = gate_runner.GateRunner(state_dir=env["state_dir"], reports_dir=env["reports_dir"])
    pr_number = PR + 12
    runner.run(
        gate="codex_gate", request_payload=_codex_payload(pr_number),
        pr_number=pr_number, pr_id=f"pr-{pr_number}",
    )

    lines = _lines(env)
    assert len(lines) == 1, f"K2b's line was booked twice: {[line['run_id'] for line in lines]}"
    assert lines[0]["provider"] == "codex"
    assert lines[0]["run_id"].endswith(":1")


def test_codex_completed_run_keeps_k2b_line_through_the_artifact_writer(env, monkeypatch, tmp_path):
    """The success writer (``materialize_artifacts``) must carry K2b's owner marker
    onto the record it commits. If it did not, every completed codex run would
    book its second line at the outcome commit."""
    monkeypatch.setattr("shutil.which", lambda binary: "/usr/bin/codex")
    monkeypatch.setenv("VNX_CODEX_HEADLESS_ENABLED", "1")
    monkeypatch.setattr(gate_runner, "create_gate_worktree", lambda **kwargs: tmp_path)
    monkeypatch.setattr(gate_runner, "remove_gate_worktree", lambda *args, **kwargs: None)
    stream = (
        Path(__file__).resolve().parent / "fixtures" / "gate_verdict"
        / "pr-1869-codex_gate-bare-verdict.ndjson"
    ).read_text(encoding="utf-8")
    monkeypatch.setattr(
        gate_runner.GateRunner, "_run_with_stall_detection",
        lambda self, **kwargs: {
            "status": "completed", "stdout": stream, "stderr": "",
            "duration_seconds": 12.0, "partial_output_lines": len(stream.splitlines()),
            "exit_code": 0,
        },
    )
    runner = gate_runner.GateRunner(state_dir=env["state_dir"], reports_dir=env["reports_dir"])
    pr_number = PR + 13
    payload = _codex_payload(pr_number)
    payload["prompt"] = "Review this diff for correctness"
    payload["report_path"] = str(env["reports_dir"] / "codex-k3-report.md")

    result = runner.run(
        gate="codex_gate", request_payload=payload, pr_number=pr_number, pr_id=f"pr-{pr_number}",
    )

    assert result["status"] == "completed"
    assert result[harness_run_cost.COST_BOOKED_FIELD] == harness_run_cost.COST_BOOKED_BY_K2B
    lines = _lines(env)
    assert len(lines) == 1, f"a completed codex run booked {[line['run_id'] for line in lines]}"
    assert lines[0]["provider"] == "codex"


def test_lane_identity_logs_the_resolver_failure_and_falls_back(env, monkeypatch, caplog):
    """Codex r2 finding 2: a resolver that raises inside ``_lane_identity`` was
    swallowed silently. The fallback stays (fail-open), and it is now observable."""

    def broken_resolver(provider, model):
        raise OSError("model registry unreadable")

    monkeypatch.setattr(harness_run_cost, "lane_model", broken_resolver)
    with caplog.at_level(logging.WARNING, logger="plan_gate_tiebreaker"):
        identity = pgt._lane_identity("deepseek-harness", "deepseek-v4-pro")

    assert identity == "deepseek-v4-pro"
    warnings = [
        record for record in caplog.records
        if record.levelno == logging.WARNING and "lane identity" in record.getMessage()
    ]
    assert len(warnings) == 1, [record.getMessage() for record in caplog.records]
    message = warnings[0].getMessage()
    assert "deepseek-harness" in message
    assert "deepseek-v4-pro" in message
    assert "model registry unreadable" in message
    assert warnings[0].exc_info is not None


# ---------------------------------------------------------------------------
# Acceptance 4 — a ledger OSError leaves the outcome, receipt and request intact
# ---------------------------------------------------------------------------


def _receipts(env) -> list:
    out = []
    for name in ("t0_receipts.ndjson", "gate_events.ndjson"):
        path = env["state_dir"] / name
        if path.exists():
            out.extend(
                record for record in (
                    json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
                )
                if record.get("event_type") == "review_gate_result"
            )
    return out


def test_ledger_oserror_leaves_outcome_receipt_and_request_intact_and_logs_one_warning(
    env, monkeypatch, caplog,
):
    payload = _request(env)
    from gate_outcome import GateRequest

    request = GateRequest(env["requests_dir"], payload, {"completed_at": RECORDED_AT})

    def broken(*args, **kwargs):
        raise OSError(errno.EACCES, "ledger is not writable")

    monkeypatch.setattr(cost_ledger, "record_attempt_once", broken)
    with caplog.at_level(logging.WARNING, logger="harness_run_cost"):
        record, written = _commit(env, record=_record(pr_number=PR + 9), request=request)

    assert written is True
    assert record["status"] == "pass"
    slot = env["results_dir"] / f"pr-{PR + 9}-{GATE}.json"
    assert json.loads(slot.read_text(encoding="utf-8"))["status"] == "pass"
    assert len(_receipts(env)) == 1
    request_on_disk = json.loads(
        (env["requests_dir"] / f"pr-{PR + 9}-{GATE}.json").read_text(encoding="utf-8")
    )
    assert request_on_disk["status"] == "completed"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "not booked" in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert _lines(env) == []


# ---------------------------------------------------------------------------
# Acceptance 6 (K3 round 4, OI-2444) — one line per EXECUTION, not per slot write
#
# The identity a gate attempt is booked under is the pair that names its run:
# the record's own ``dispatch_id`` — the gate run's identity, and for a harness
# lane ``<gate>-gate-pr<pr>-<timestamp>``, unique per run — plus the run's
# ``attempt_seq``. A write that re-commits the record already on the slot ends
# that same run, so it books that run's attempt and the booking finds its line
# and writes nothing; a separate attempt books its own, whether or not the slot
# accepted its record.
# ---------------------------------------------------------------------------


def test_the_same_unavailable_record_committed_twice_books_one_line(env):
    """Codex r4 blocker (OI-2444), the reproduction: an ``unavailable``/timeout
    record is no repeat of the slot (it owes no receipt), so committing it twice
    landed as attempts ``:1`` and ``:2`` and booked two cost lines for one
    dispatch. One run, one line."""
    record = _record(
        "unavailable", pr_number=PR + 20, reason="timeout",
        dispatch_id=f"glm-gate-pr{PR + 20}-1",
    )
    _commit(env, status="unavailable", record=record)
    first = _lines(env)
    assert [line["run_id"] for line in first] == [f"vnx-dev:{GATE}:{record['dispatch_id']}:1"]

    _commit(env, status="unavailable", record=dict(record))
    lines = _lines(env)
    assert [line["run_id"] for line in lines] == [f"vnx-dev:{GATE}:{record['dispatch_id']}:1"], (
        f"the same record booked a second run: {[line['run_id'] for line in lines]}"
    )
    slot = json.loads((env["results_dir"] / f"pr-{PR + 20}-{GATE}.json").read_text(encoding="utf-8"))
    assert slot["attempt_seq"] == 2, "the slot still counts writes; the ledger counts runs"


def test_a_repeat_of_a_decided_record_books_one_line(env):
    """The same rule for a decided record, where the slot itself calls the write a
    repeat (ADR-046 step 6): re-committing it is the same run and books nothing."""
    record = _record(pr_number=PR + 21, dispatch_id=f"glm-gate-pr{PR + 21}-1")
    _commit(env, record=record)
    _commit(env, record=_record(pr_number=PR + 21, dispatch_id=f"glm-gate-pr{PR + 21}-1", recorded_at=None))
    lines = _lines(env)
    assert len(lines) == 1, f"a repeated verdict booked {[line['run_id'] for line in lines]}"


def test_a_refused_overwrite_of_a_separate_attempt_books_its_own_line(env):
    """Codex r4 blocker, second half (OI-2444): a separate attempt that really ran,
    whose record the guard refuses to put on a decided slot, booked NOTHING —
    the booking followed the slot write. It books its own line."""
    decided = _record(pr_number=PR + 22, dispatch_id=f"glm-gate-pr{PR + 22}-1")
    _commit(env, record=decided)
    outage = _record(
        "unavailable", pr_number=PR + 22, reason="timeout",
        dispatch_id=f"glm-gate-pr{PR + 22}-2", recorded_at="2026-10-27T10:09:00Z",
    )
    on_disk, written = _commit(env, status="unavailable", record=outage)
    assert written is False, "the guard must still refuse the downgrade (OI-1469/1470)"
    assert on_disk["status"] == "pass", "the decided verdict stays on the slot"

    lines = _lines(env)
    assert [line["run_id"] for line in lines] == [
        f"vnx-dev:{GATE}:{decided['dispatch_id']}:1",
        f"vnx-dev:{GATE}:{outage['dispatch_id']}:1",
    ], f"the refused attempt's run booked no line: {[line['run_id'] for line in lines]}"
    assert lines[1]["cost_source"] == "unavailable:timeout"
    assert lines[1]["usage_completeness"] == "unknown:timeout"


def test_two_runs_of_one_slot_book_one_line_each(env):
    """The other side of the identity: two runs are two lines, never
    deduplicated — a different dispatch-id names a different run, also when the
    slot reads the second verdict as a repeat of the first (ADR-046 step 6 is
    about the slot; the ledger counts runs, and this run called a model)."""
    for attempt in (1, 2):
        _commit(env, record=_record(
            pr_number=PR + 23, dispatch_id=f"glm-gate-pr{PR + 23}-{attempt}",
            recorded_at=f"2026-10-27T10:0{attempt}:00Z",
        ))
    lines = _lines(env)
    assert [line["run_id"] for line in lines] == [
        f"vnx-dev:{GATE}:glm-gate-pr{PR + 23}-1:1",
        f"vnx-dev:{GATE}:glm-gate-pr{PR + 23}-2:1",
    ]


def test_an_admitted_retry_after_a_refused_attempt_is_not_swallowed(env):
    """A refused attempt books its own number, and a later run of the same ref must
    not land on it: the ledger numbers each NEW execution at the ref's next free
    attempt, so the retry's line is written rather than read as already booked."""
    dispatch_id = f"20261028-builder-{PR + 24}"
    _commit(env, record=_record(pr_number=PR + 24, dispatch_id=dispatch_id))
    _commit(env, status="unavailable", record=_record(
        "unavailable", pr_number=PR + 24, reason="timeout",
        dispatch_id=dispatch_id, recorded_at="2026-10-27T10:09:00Z",
    ))
    retry = _record(
        pr_number=PR + 24, dispatch_id=dispatch_id, recorded_at="2026-10-27T10:12:00Z",
        blocking_findings=[{"message": "a second look found another defect"}],
    )
    _commit(env, record=retry)
    ids = [line["run_id"] for line in _lines(env)]
    assert ids == [
        f"vnx-dev:{GATE}:{dispatch_id}:1",
        f"vnx-dev:{GATE}:{dispatch_id}:2",
        f"vnx-dev:{GATE}:{dispatch_id}:3",
    ], ids


def test_the_same_refused_record_committed_twice_books_one_line(env):
    """Point 2, on the branch where the slot has no attempt to hand out: the first
    commit of the outage is refused the decided slot and books its own line, and
    the identical record committed again is the SAME run — one line, not two."""
    decided = _record(pr_number=PR + 26, dispatch_id=f"glm-gate-pr{PR + 26}-1")
    _commit(env, record=decided)
    outage = _record(
        "unavailable", pr_number=PR + 26, reason="timeout",
        dispatch_id=f"glm-gate-pr{PR + 26}-2", recorded_at="2026-10-27T10:09:00Z",
    )
    for _ in range(2):
        _, written = _commit(env, status="unavailable", record=dict(outage))
        assert written is False, "the guard must still refuse the downgrade (OI-1469/1470)"
    lines = _lines(env)
    assert [line["run_id"] for line in lines] == [
        f"vnx-dev:{GATE}:{decided['dispatch_id']}:1",
        f"vnx-dev:{GATE}:{outage['dispatch_id']}:1",
    ], f"the same refused run booked twice: {[line['run_id'] for line in lines]}"


def test_an_attempt_k2b_booked_books_no_k3_line_even_when_the_slot_refuses_it(env):
    """Point 4: the K2b exclusion is read off the record on EVERY path, the refused
    one included — K2b's line is this attempt's only line."""
    decided = _record(
        pr_number=PR + 25, dispatch_id=f"codex-gate-pr{PR + 25}-1",
        provider="codex", model="gpt-6-astra",
    )
    _commit(env, gate="codex_gate", record=decided)
    codex_record = {
        "gate": "codex_gate", "pr_id": "", "pr_number": PR + 25, "status": "unavailable",
        "reason": "timeout", "commit_sha": HEAD, "branch": BRANCH, "contract_hash": "hash-k3",
        "report_path": "", "recorded_at": "2026-10-27T10:09:00Z",
        "provider": "codex", "model": "gpt-6-astra",
        harness_run_cost.COST_BOOKED_FIELD: harness_run_cost.COST_BOOKED_BY_K2B,
    }
    _, written = _commit(env, gate="codex_gate", status="unavailable", record=codex_record)
    assert written is False
    lines = _lines(env)
    assert [line["run_id"] for line in lines] == [f"vnx-dev:codex_gate:{decided['dispatch_id']}:1"]


def test_the_tiebreaker_books_the_tenant_of_the_store_it_writes_into(tmp_path, monkeypatch):
    """Point 5: ``review_cap_tiebreaker`` supplies no project_id, and the fallback
    read the ambient ``VNX_PROJECT_ID`` — so a line written into tenant-b's ledger
    was stamped ``vnx-dev``. The store decides (ADR-007)."""
    data_dir = tmp_path / ".vnx-data" / "tenant-b"
    (data_dir / "state").mkdir(parents=True)
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")

    answer = json.dumps({"outcome": rcv.TERUG_NAAR_PLAN, "rationale": "no convergence"})
    report = f"Decision.\n```{rct.FENCE}\n{answer}\n```\n"
    rct.ask(
        "the brief", [rcv.TERUG_NAAR_PLAN, rcv.MERGEN_MET_OI],
        dispatcher=lambda provider, model, instruction, did: report,
        data_dir=str(data_dir),
    )

    ledger = data_dir / "events" / "provider_costs.ndjson"
    lines = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [(line["project_id"], line["run_id"]) for line in lines] == [
        ("tenant-b", lines[0]["run_id"]),
    ]
    assert lines[0]["run_id"].startswith("tenant-b:review_cap_tiebreaker:")
