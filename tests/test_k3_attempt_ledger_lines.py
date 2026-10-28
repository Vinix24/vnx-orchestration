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
    import harness_run_cost

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
    }
    _commit(env, gate="codex_gate", record=record)
    lines = _lines(env)
    assert len(lines) == 1, f"K2b's line was booked twice: {[line['run_id'] for line in lines]}"
    assert lines[0]["provider"] == "codex"


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

    monkeypatch.setattr(cost_ledger, "record_next_attempt", broken)
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
