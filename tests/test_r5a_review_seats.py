"""OI-2058, reparatiegolf R5a: one seat resolver, and every seat gets its own reviewer.

Plan: ``claudedocs/specs/reparatiegolf/plan.md`` ("R5 in detail", the R5a parts). What is
pinned here:

- the seat resolver (``scripts/lib/review_seats.py``): ``VNX_DEFAULT_REVIEW_STACK`` read
  fresh, narrowed to review peers; ``ci_gate`` and optional gates are no seat; an unknown or
  retired name is refused by name; a governance profile adds and removes no seat;
- acceptance (a): codex exhausted, kimi available: codex's seat goes to kimi and kimi's seat
  to deepseek, two requests, two distinct gates;
- acceptance (b): the request record carries the requested seat list and its source, so a
  later config change cannot rewrite what the request asked for;
- acceptance (c): a switched-off provider's seat moves along the chain and is never filled;
- acceptance (d): a seat whose reviewer runs out DURING the request goes to the next distinct
  reviewer in the same call (measured 2026-10-15/16 on #2066 and #2072);
- a seat with no distinct reviewer left is booked unfillable by name, with the reviewers tried.

Everything runs the real request and execute code on a tmp store. The provider edge is the
only thing replaced (``execute_gate`` for the run, ``gh`` for the head); nothing starts a
provider and nothing is written outside ``tmp_path``.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

import review_stack_support  # noqa: F401  (puts scripts/ and scripts/lib on sys.path)
from review_stack_support import BRANCH, HEAD_SHA, PR, build_project_store, isolate_config_registry

_FRESH = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

_CODEX_LIMIT_RECORD = {
    "gate": "codex_gate", "pr_id": str(PR), "pr_number": PR, "status": "unavailable",
    "reason": "exit_nonzero",
    "reason_detail": "Subprocess exited with code 1: You've hit your usage limit. Upgrade to Pro.",
    "contract_hash": "", "report_path": "", "blocking_findings": [],
    "residual_risk": "Gate exit_nonzero. Re-run required.",
    "commit_sha": HEAD_SHA, "recorded_at": _FRESH,
}

# The kimi record measured on 2026-10-16 01:37Z (#2072, review_gates/results/pr-2072-kimi_gate.json
# under ~/.vnx-data/vnx-dev/state, read-only), with this test's PR, head and a fresh timestamp.
_KIMI_WEEKLY_LIMIT_RECORD = {
    "gate": "kimi_gate", "pr_id": str(PR), "pr_number": PR, "status": "unavailable",
    "reason": "lane_exhausted",
    "reason_detail": (
        "kimi_gate report carries failure_reason in its frontmatter (spawn layer failed before "
        "the model ran): [quota_or_auth] provider=kimi reason=quota_or_auth msg='Expecting value: "
        "line 1 column 1 (char 0)' raw='Error code: 403 - {'error': {'message': \"You've reached "
        "your weekly (7-day) usag'"
    ),
    "residual_risk": "Gate lane_exhausted. Re-run required.",
    "contract_hash": "", "report_path": "", "blocking_findings": [],
    "commit_sha": HEAD_SHA, "recorded_at": _FRESH,
}


@pytest.fixture(autouse=True)
def _clean_config_registry(monkeypatch):
    yield from isolate_config_registry(monkeypatch)


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = build_project_store(tmp_path, monkeypatch)
    import gate_request_handler
    import review_gate_manager

    monkeypatch.setenv("VNX_OVERRIDE_CI_GATE_REQUIRED", "0")
    monkeypatch.setattr(
        gate_request_handler.GateRequestHandlerMixin, "_codex_headless_available", lambda self: True,
    )
    receipts: List[Dict[str, Any]] = []
    monkeypatch.setattr(
        review_gate_manager, "emit_governance_receipt",
        lambda event, **fields: receipts.append({"event": event, **fields}),
    )
    store["receipts"] = receipts
    return store


def _manager():
    import review_gate_manager
    return review_gate_manager.ReviewGateManager()


def _request(review_stack, *, changed_files=("scripts/lib/example.py",)):
    return _manager().request_reviews(
        pr_number=PR, branch=BRANCH, review_stack=review_stack, risk_class="medium",
        changed_files=list(changed_files), mode="per_pr", dispatch_id="20261016-rg-r5a-seat-resolver",
    )


def _write_result(env, gate: str, payload: Dict[str, Any]) -> None:
    (env["results_dir"] / f"pr-{PR}-{gate}.json").write_text(json.dumps(payload), encoding="utf-8")


def _request_record(env, gate: str) -> Dict[str, Any]:
    return json.loads((env["requests_dir"] / f"pr-{PR}-{gate}.json").read_text(encoding="utf-8"))


def _gates(result) -> List[str]:
    return [payload["gate"] for payload in result["requested"]]


def _set_switches(**families: bool) -> None:
    path = Path(os.environ["VNX_PROVIDER_SWITCH_FILE"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "global": {
        family: {"enabled": enabled, "changed_at": "2026-10-16T08:00:00Z", "changed_by": "op",
                 "reason": "tegoed sparen"}
        for family, enabled in families.items()
    }, "projects": {}}), encoding="utf-8")


# ---------------------------------------------------------------------------
# The seat resolver
# ---------------------------------------------------------------------------

class TestSeatResolver:
    def test_the_config_stack_is_read_fresh_not_from_the_import_time_snapshot(self, env, monkeypatch):
        import review_gate_manager
        from review_seats import SOURCE_CONFIG, resolve_review_seats

        snapshot = list(review_gate_manager.DEFAULT_REVIEW_STACK)
        monkeypatch.setenv("VNX_OVERRIDE_DEFAULT_REVIEW_STACK", "glm_gate,deepseek_gate")

        seats = resolve_review_seats()

        assert seats.seats == ("glm_gate", "deepseek_gate")
        assert seats.source == SOURCE_CONFIG and seats.value == "glm_gate,deepseek_gate"
        assert "glm_gate" not in snapshot, "the import-time snapshot still names the old stack"

    def test_ci_gate_and_optional_gates_are_requested_but_are_no_seat(self, env, monkeypatch):
        from review_seats import resolve_review_seats

        monkeypatch.setenv("VNX_OVERRIDE_DEFAULT_REVIEW_STACK", "codex_gate,claude_github_optional,kimi_gate")
        monkeypatch.setenv("VNX_OVERRIDE_CI_GATE_REQUIRED", "1")

        seats = resolve_review_seats()

        assert seats.seats == ("codex_gate", "kimi_gate")
        assert seats.extra_gates == ("claude_github_optional", "ci_gate")
        assert seats.stack == ("codex_gate", "claude_github_optional", "kimi_gate", "ci_gate")

    def test_wiring_gate_in_an_explicit_stack_is_no_seat(self):
        from review_seats import resolve_review_seats

        seats = resolve_review_seats(explicit_stack=["wiring_gate", "kimi_gate", "kimi_gate"])

        assert seats.seats == ("kimi_gate",)
        assert seats.stack == ("wiring_gate", "kimi_gate"), "a name given twice is one entry"

    @pytest.mark.parametrize("name,said", [
        ("gemini_review", "gemini_review is a retired gate"),
        ("codex_gat", "'codex_gat', an unknown gate"),
    ])
    def test_an_unknown_or_retired_name_is_refused_by_name(self, env, monkeypatch, name, said):
        from review_seats import ReviewSeatError, resolve_review_seats

        with pytest.raises(ReviewSeatError, match=said):
            resolve_review_seats(explicit_stack=["codex_gate", name])
        monkeypatch.setenv("VNX_OVERRIDE_DEFAULT_REVIEW_STACK", f"{name},codex_gate")
        with pytest.raises(ReviewSeatError, match=said):
            resolve_review_seats()

    def test_the_refusal_is_the_config_error_the_door_already_refuses_on(self):
        from dispatch_spec import ReviewGateConfigError
        from review_seats import ReviewSeatError

        assert issubclass(ReviewSeatError, ReviewGateConfigError)

    def test_a_config_stack_with_no_seat_is_refused_an_explicit_one_is_not(self, env, monkeypatch):
        from review_seats import ReviewSeatError, resolve_review_seats

        assert resolve_review_seats(explicit_stack=["ci_gate"]).seats == ()
        monkeypatch.setenv("VNX_OVERRIDE_DEFAULT_REVIEW_STACK", "ci_gate,claude_github_optional")
        with pytest.raises(ReviewSeatError, match="names no review seat"):
            resolve_review_seats()

    def test_the_record_names_seats_source_and_value(self, env, monkeypatch):
        from review_seats import resolve_review_seats

        monkeypatch.setenv("VNX_OVERRIDE_DEFAULT_REVIEW_STACK", "codex_gate,kimi_gate")

        assert resolve_review_seats().to_record() == {
            "seats": ["codex_gate", "kimi_gate"], "extra_gates": [], "source": "config",
            "key": "VNX_DEFAULT_REVIEW_STACK", "value": "codex_gate,kimi_gate", "profile_stack": None,
        }
        assert resolve_review_seats(explicit_stack=["kimi_gate"]).to_record()["key"] == ""


class TestAProfileAddsAndRemovesNoSeat:
    """VNX_PROFILE_SELECTOR=1 used to replace the whole stack with the profile's
    ``required_gates``: a hard-coded codex+kimi pair, or no reviewer at all."""

    @pytest.fixture
    def profiles(self, env, monkeypatch):
        monkeypatch.setenv("VNX_PROFILE_SELECTOR", "1")
        vnx_dir = env["project_root"] / ".vnx"
        vnx_dir.mkdir()
        (vnx_dir / "governance_profiles.yaml").write_text(
            "scopes:\n  \"agents/\": light\n  \"*\": default\n", encoding="utf-8",
        )

    def test_the_scheduler_requests_the_config_seats_when_a_profile_applies(self, env, profiles, monkeypatch):
        """Red on the base: the profile's codex_gate,kimi_gate were requested for a project whose
        stack names glm_gate, so the scheduler and a fresh read of the key disagreed."""
        import config_runtime

        monkeypatch.setenv("VNX_OVERRIDE_DEFAULT_REVIEW_STACK", "glm_gate")
        configured = [g for g in config_runtime.get("VNX_DEFAULT_REVIEW_STACK").split(",") if g]

        result = _request(None)

        requested_reviewers = [g for g in _gates(result) if g != "ci_gate"]
        assert requested_reviewers == configured == ["glm_gate"], (
            f"scheduler requested {requested_reviewers}, the config names {configured}"
        )
        assert "ci_gate" in _gates(result), "the profile still decides the extra gates"

    def test_a_light_profile_removes_no_seat(self, env, profiles, monkeypatch):
        from review_seats import resolve_review_seats

        monkeypatch.setenv("VNX_OVERRIDE_DEFAULT_REVIEW_STACK", "codex_gate,kimi_gate")

        seats = resolve_review_seats(changed_files=["agents/blog-writer/prompt.md"])

        assert seats.seats == ("codex_gate", "kimi_gate")
        assert seats.extra_gates == ("ci_gate",)
        assert seats.profile_stack == ("ci_gate",)

    def test_no_profile_applies_without_the_selector(self, env, monkeypatch):
        from review_seats import resolve_review_seats

        monkeypatch.setenv("VNX_PROFILE_SELECTOR", "0")
        seats = resolve_review_seats(changed_files=["scripts/lib/example.py"])
        assert seats.profile_stack is None


# ---------------------------------------------------------------------------
# Acceptance (a): one reviewer per seat
# ---------------------------------------------------------------------------

class TestEverySeatGetsItsOwnReviewer:
    def test_codex_exhausted_gives_codex_seat_to_kimi_and_kimi_seat_to_deepseek(self, env):
        """Red on the base: kimi was dispatched for codex's seat and nothing for kimi's own."""
        _write_result(env, "codex_gate", _CODEX_LIMIT_RECORD)

        result = _request(None)

        assert _gates(result) == ["kimi_gate", "deepseek_gate"]
        codex_seat, kimi_seat = result["requested"]
        assert (codex_seat["seat"], codex_seat["status"]) == ("codex_gate", "requested")
        assert (kimi_seat["seat"], kimi_seat["status"]) == ("kimi_gate", "requested")
        assert [hop["gate"] for hop in codex_seat["takeover_path"]] == ["codex_gate"]
        assert kimi_seat["takeover_path"] == [{
            "gate": "kimi_gate", "reason": "reads_another_seat", "status": "held",
            "detail": "kimi_gate already reads seat codex_gate in this request",
        }]
        assert "kimi_gate reads another seat" in kimi_seat["failure_reason"]
        assert [r["gate"] for r in env["receipts"]] == ["kimi_gate", "deepseek_gate"]

    def test_both_subscription_seats_exhausted_give_deepseek_and_glm(self, env):
        _write_result(env, "codex_gate", _CODEX_LIMIT_RECORD)
        _write_result(env, "kimi_gate", _KIMI_WEEKLY_LIMIT_RECORD)

        result = _request(None)

        assert _gates(result) == ["deepseek_gate", "glm_gate"]
        assert [p["seat"] for p in result["requested"]] == ["codex_gate", "kimi_gate"]

    def test_healthy_seats_read_themselves(self, env):
        result = _request(None)

        assert _gates(result) == ["codex_gate", "kimi_gate"]
        assert all("takeover" not in p for p in result["requested"])

    def test_a_seat_without_a_distinct_reviewer_is_booked_unfillable_by_name(self, env, monkeypatch):
        """The chain ends at kimi: codex's seat takes kimi, and kimi's seat has nobody left."""
        monkeypatch.setenv("VNX_OVERRIDE_REVIEW_GATE_TAKEOVER_CHAIN", "codex_gate,kimi_gate")
        _write_result(env, "codex_gate", _CODEX_LIMIT_RECORD)

        result = _request(None)

        assert _gates(result) == ["kimi_gate", "kimi_gate"]
        unfillable = result["requested"][1]
        assert unfillable["status"] == "chain_exhausted"
        assert unfillable["reason"] == "no_distinct_reviewer"
        assert (unfillable["seat"], unfillable["reviewers_tried"]) == ("kimi_gate", ["kimi_gate"])
        assert "seat kimi_gate is unfillable" in unfillable["summary"]
        on_disk = json.loads(
            (env["results_dir"] / f"pr-{PR}-kimi_gate-chain-exhausted.json").read_text(encoding="utf-8"),
        )
        assert on_disk["reason"] == "no_distinct_reviewer"
        assert _request_record(env, "kimi_gate")["seat"] == "codex_gate", (
            "kimi's own request record is the codex seat's; the unfillable booking never overwrites it"
        )

    def test_an_unfillable_seat_names_every_reviewer_it_tried(self, env, monkeypatch):
        monkeypatch.setenv("VNX_OVERRIDE_REVIEW_GATE_TAKEOVER_CHAIN", "codex_gate,kimi_gate,deepseek_gate")
        _set_switches(deepseek=False)
        _write_result(env, "codex_gate", _CODEX_LIMIT_RECORD)

        result = _request(None)

        unfillable = result["requested"][1]
        assert unfillable["reason"] == "no_distinct_reviewer"
        assert unfillable["reviewers_tried"] == ["kimi_gate", "deepseek_gate"]
        assert "provider_switched_off" in unfillable["failure_reason"]


# ---------------------------------------------------------------------------
# Acceptance (b): the request record carries what was requested
# ---------------------------------------------------------------------------

class TestTheRequestRecordCarriesTheSeats:
    def test_every_seat_record_names_its_seat_and_the_requested_stack(self, env, monkeypatch):
        monkeypatch.setenv("VNX_OVERRIDE_DEFAULT_REVIEW_STACK", "codex_gate,kimi_gate")
        _write_result(env, "codex_gate", _CODEX_LIMIT_RECORD)

        result = _request(None)

        expected = {
            "seats": ["codex_gate", "kimi_gate"], "extra_gates": [], "source": "config",
            "key": "VNX_DEFAULT_REVIEW_STACK", "value": "codex_gate,kimi_gate", "profile_stack": None,
        }
        assert result["review_seats"] == expected
        kimi, deepseek = _request_record(env, "kimi_gate"), _request_record(env, "deepseek_gate")
        assert (kimi["seat"], kimi["review_seats"]) == ("codex_gate", expected)
        assert (deepseek["seat"], deepseek["review_seats"]) == ("kimi_gate", expected)
        assert kimi["takeover_from"] == "codex_gate", "seat stamp and takeover land in one record"

    def test_a_config_change_after_the_request_leaves_the_requested_stack_on_record(self, env, monkeypatch):
        monkeypatch.setenv("VNX_OVERRIDE_DEFAULT_REVIEW_STACK", "codex_gate,kimi_gate")
        _request(None)

        monkeypatch.setenv("VNX_OVERRIDE_DEFAULT_REVIEW_STACK", "glm_gate")
        from review_seats import resolve_review_seats
        assert resolve_review_seats().seats == ("glm_gate",)

        for gate in ("codex_gate", "kimi_gate"):
            recorded = _request_record(env, gate)["review_seats"]
            assert (recorded["seats"], recorded["value"]) == (["codex_gate", "kimi_gate"], "codex_gate,kimi_gate")

    def test_an_explicit_stack_is_recorded_as_explicit(self, env):
        _request(["kimi_gate"])

        recorded = _request_record(env, "kimi_gate")["review_seats"]
        assert (recorded["source"], recorded["seats"], recorded["key"]) == ("explicit", ["kimi_gate"], "")

    def test_an_extra_gate_fills_no_seat_and_carries_no_seat_stamp(self, env, monkeypatch):
        import gate_request_handler

        monkeypatch.setattr(
            gate_request_handler.GateRequestHandlerMixin, "_request_ci_gate",
            lambda self, pr, branch, risk, files, mode, dispatch_id="": {"gate": "ci_gate", "status": "requested"},
        )
        result = _request(["kimi_gate", "ci_gate"])

        ci = result["requested"][1]
        assert ci["gate"] == "ci_gate" and "seat" not in ci


# ---------------------------------------------------------------------------
# Acceptance (c): a switched-off seat moves along the chain and is never filled
# ---------------------------------------------------------------------------

class TestASwitchedOffSeatIsNeverFilled:
    def test_kimi_off_its_seat_goes_to_deepseek_and_kimi_is_never_asked(self, env):
        _set_switches(kimi=False)

        result = _request(None)

        assert _gates(result) == ["codex_gate", "deepseek_gate"]
        deepseek = result["requested"][1]
        assert deepseek["seat"] == "kimi_gate"
        assert [(h["gate"], h["reason"]) for h in deepseek["takeover_path"]] == [("kimi_gate", "provider_switched_off")]
        assert not (env["requests_dir"] / f"pr-{PR}-kimi_gate.json").exists()

    def test_codex_exhausted_and_kimi_off_both_seats_move_and_kimi_fills_neither(self, env):
        _set_switches(kimi=False)
        _write_result(env, "codex_gate", _CODEX_LIMIT_RECORD)

        result = _request(None)

        assert _gates(result) == ["deepseek_gate", "glm_gate"]
        assert [p["seat"] for p in result["requested"]] == ["codex_gate", "kimi_gate"]
        assert [h["gate"] for h in result["requested"][1]["takeover_path"]] == ["kimi_gate", "deepseek_gate"]
        assert not (env["requests_dir"] / f"pr-{PR}-kimi_gate.json").exists()

    def test_kimi_off_with_no_successor_is_booked_not_executable_never_passed(self, env, monkeypatch):
        monkeypatch.setenv("VNX_OVERRIDE_REVIEW_GATE_TAKEOVER_CHAIN", "codex_gate,kimi_gate")
        _set_switches(kimi=False)

        result = _request(None)

        kimi_seat = result["requested"][1]
        assert (kimi_seat["gate"], kimi_seat["seat"]) == ("kimi_gate", "kimi_gate")
        assert (kimi_seat["status"], kimi_seat["reason"]) == ("not_executable", "provider_switched_off")
        assert _request_record(env, "kimi_gate")["seat"] == "kimi_gate"


# ---------------------------------------------------------------------------
# Acceptance (d): a reviewer that runs out during the request
# ---------------------------------------------------------------------------

def _pass_record(env, gate: str) -> Dict[str, Any]:
    report = env["reports_dir"] / f"{gate}-pr{PR}.md"
    report.write_text("verdict pass\n", encoding="utf-8")
    return {
        "gate": gate, "pr_id": str(PR), "pr_number": PR, "status": "pass", "commit_sha": HEAD_SHA,
        "contract_hash": "0123456789abcdef", "report_path": str(report), "blocking_findings": [],
        "recorded_at": _FRESH,
    }


@pytest.fixture
def runs(env, monkeypatch):
    """``execute_gate`` replaced at the provider edge: each gate's scripted outcome is written
    to its result record, as a real run would, and returned. Records which gates ran."""
    import review_gate_manager

    monkeypatch.setenv("VNX_CODEX_HEADLESS_ENABLED", "1")
    outcomes: Dict[str, Dict[str, Any]] = {}
    ran: List[str] = []

    def execute_gate(self, *, gate, pr_number, pr_id=""):
        ran.append(gate)
        record = outcomes.get(gate) or _pass_record(env, gate)
        _write_result(env, gate, record)
        return record

    monkeypatch.setattr(review_gate_manager.ReviewGateManager, "execute_gate", execute_gate)
    return {"outcomes": outcomes, "ran": ran}


def _request_and_execute(review_stack):
    return _manager().request_and_execute(
        pr_number=PR, branch=BRANCH, review_stack=review_stack, risk_class="medium",
        changed_files=["scripts/lib/example.py"], mode="per_pr", dispatch_id="20261016-rg-r5a-seat-resolver",
    )


class TestAReviewerThatRunsOutDuringTheRequest:
    def test_kimi_exhausted_mid_request_hands_its_seat_to_deepseek_in_the_same_call(self, env, runs):
        """Red on the base: the call ran kimi, booked lane_exhausted and stopped; only a second
        identical request gave the seat to deepseek (#2066 21:09Z and 03:55Z, #2072 00:07Z and 01:37Z)."""
        runs["outcomes"]["kimi_gate"] = _KIMI_WEEKLY_LIMIT_RECORD

        result = _request_and_execute(["kimi_gate"])

        assert [g["gate"] for g in result["gates"]] == ["kimi_gate", "deepseek_gate"]
        assert runs["ran"] == ["kimi_gate", "deepseek_gate"]
        kimi, deepseek = result["gates"]
        assert kimi["superseded_by"] == "deepseek_gate" and kimi["passed"] is False
        assert deepseek["passed"] is True and "superseded_by" not in deepseek
        assert result["has_required_failure"] is False, "the seat's final reviewer passed"
        record = _request_record(env, "deepseek_gate")
        assert record["seat"] == "kimi_gate"
        assert [(h["gate"], h["reason"]) for h in record["takeover_path"]] == [("kimi_gate", "lane_exhausted")]

    def test_the_handed_on_seat_skips_reviewers_other_seats_hold(self, env, runs):
        runs["outcomes"]["kimi_gate"] = _KIMI_WEEKLY_LIMIT_RECORD
        _write_result(env, "codex_gate", _CODEX_LIMIT_RECORD)

        result = _request_and_execute(None)

        # Round: codex's seat -> kimi, kimi's seat -> deepseek. kimi runs out, so codex's
        # seat walks on past kimi and past deepseek (kimi's seat holds it) to glm.
        assert [g["gate"] for g in result["gates"]] == ["kimi_gate", "deepseek_gate", "glm_gate"]
        assert result["gates"][0]["superseded_by"] == "glm_gate"
        assert result["has_required_failure"] is False
        assert _request_record(env, "glm_gate")["seat"] == "codex_gate"

    def test_a_reviewer_that_decided_keeps_its_seat(self, env, runs):
        result = _request_and_execute(None)

        assert runs["ran"] == ["codex_gate", "kimi_gate"]
        assert all("superseded_by" not in g for g in result["gates"])

    def test_no_distinct_reviewer_left_keeps_the_exhausted_answer_and_fails_the_call(self, env, runs, monkeypatch):
        monkeypatch.setenv("VNX_OVERRIDE_REVIEW_GATE_TAKEOVER_CHAIN", "kimi_gate")
        runs["outcomes"]["kimi_gate"] = _KIMI_WEEKLY_LIMIT_RECORD

        result = _request_and_execute(["kimi_gate"])

        assert [g["gate"] for g in result["gates"]] == ["kimi_gate"]
        assert result["has_required_failure"] is True
        assert result["review_seats"]["seats"] == ["kimi_gate"]

    def test_the_seat_line_names_the_reviewer_that_finished_the_seat(self, env, runs):
        from gate_enforcement_verify import resolve_seat_entries, verify_report

        runs["outcomes"]["kimi_gate"] = _KIMI_WEEKLY_LIMIT_RECORD
        result = _request_and_execute(["kimi_gate"])

        entries = resolve_seat_entries(result, pr_number=PR, state_dir=env["state_dir"])
        assert entries["kimi_gate"]["gate"] == "deepseek_gate"
        assert verify_report(result, pr_number=PR, seats=["kimi_gate"], state_dir=env["state_dir"]).ok


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------

class TestTheCli:
    def test_a_refused_stack_exits_2_with_the_reason_and_requests_nothing(self, env, capsys):
        import review_gate_manager

        rc = review_gate_manager.main([
            "request-and-execute", "--pr", str(PR), "--branch", BRANCH,
            "--review-stack", "codex_gate,gemini_review", "--changed-files", "scripts/lib/example.py",
        ])

        assert rc == 2
        assert "gemini_review is a retired gate" in capsys.readouterr().err
        assert not list(env["requests_dir"].glob(f"pr-{PR}-*.json"))

    def test_no_review_stack_flag_resolves_the_stack_when_the_request_runs(self, env, monkeypatch):
        import review_gate_manager

        seen: List[Any] = []
        monkeypatch.setattr(
            review_gate_manager.ReviewGateManager, "request_reviews",
            lambda self, **kw: seen.append(kw["review_stack"]) or {"requested": []},
        )

        review_gate_manager.main([
            "request", "--pr", str(PR), "--branch", BRANCH, "--changed-files", "scripts/lib/example.py",
        ])

        assert seen == [None]
