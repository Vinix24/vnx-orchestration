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
- a seat with no distinct reviewer left is booked unfillable by name, with the reviewers tried;
- round 2 (codex FAIL on #2073): a decided verdict, PASS or FAIL, is never handed on, and a
  seat only moves forward from the reviewer whose own record says it ran out; an entry's
  own ``seat`` beats an earlier round's ``takeover_path`` when verification credits it.

Everything runs the real request and execute code on a tmp store. The provider edge is the
only thing replaced (``execute_gate`` for the run, ``gh`` for the head); nothing starts a
provider and nothing is written outside ``tmp_path``.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest

import review_stack_support  # noqa: F401  (puts scripts/ and scripts/lib on sys.path)
from review_stack_support import BRANCH, HEAD_SHA, PR, build_project_store, isolate_config_registry
from review_seat_support import open_pr_in_ledger

_FRESH = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
# Two hours back: past the one-hour lane-exhaustion TTL (OI-1569), so the record no longer holds.
_EXPIRED = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")

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
    def test_codex_exhausted_gives_codex_seat_to_kimi_and_kimi_seat_to_claude(self, env):
        """Red on the base: kimi was dispatched for codex's seat and nothing for kimi's own."""
        _write_result(env, "codex_gate", _CODEX_LIMIT_RECORD)

        result = _request(None)

        assert _gates(result) == ["kimi_gate", "claude_gate"]
        codex_seat, kimi_seat = result["requested"]
        assert (codex_seat["seat"], codex_seat["status"]) == ("codex_gate", "requested")
        assert (kimi_seat["seat"], kimi_seat["status"]) == ("kimi_gate", "requested")
        assert [hop["gate"] for hop in codex_seat["takeover_path"]] == ["codex_gate"]
        assert kimi_seat["takeover_path"] == [{
            "gate": "kimi_gate", "reason": "reads_another_seat", "status": "held",
            "detail": "kimi_gate already reads seat codex_gate in this request",
        }]
        assert "kimi_gate reads another seat" in kimi_seat["failure_reason"]
        assert [r["gate"] for r in env["receipts"]] == ["kimi_gate", "claude_gate"]

    def test_every_subscription_seat_exhausted_on_the_default_chain_leaves_the_second_seat_chain_exhausted(self, env):
        """R4 (operator decision 2026-10-17): glm_gate is not in the default chain. Since R7
        (2026-10-22) claude_gate stands in it too, so with codex, kimi and claude all out deepseek
        reads one seat and the other ends as chain_exhausted, never a silent re-dispatch."""
        _write_result(env, "codex_gate", _CODEX_LIMIT_RECORD)
        _write_result(env, "kimi_gate", _KIMI_WEEKLY_LIMIT_RECORD)
        _write_result(env, "claude_gate", {**_KIMI_WEEKLY_LIMIT_RECORD, "gate": "claude_gate"})

        result = _request(None)

        assert _gates(result) == ["deepseek_gate", "kimi_gate"]
        assert [p["seat"] for p in result["requested"]] == ["codex_gate", "kimi_gate"]
        second = result["requested"][1]
        assert second["status"] == "chain_exhausted"
        assert "glm_gate" not in [p["gate"] for p in result["requested"]]
        assert not (env["requests_dir"] / f"pr-{PR}-glm_gate.json").exists()

    def test_both_subscription_seats_exhausted_on_the_default_chain_give_claude_and_deepseek(self, env):
        """R7 (2026-10-22): with codex and kimi both out, claude_gate reads codex's seat and
        deepseek_gate, the next distinct reviewer, reads kimi's."""
        _write_result(env, "codex_gate", _CODEX_LIMIT_RECORD)
        _write_result(env, "kimi_gate", _KIMI_WEEKLY_LIMIT_RECORD)

        result = _request(None)

        assert _gates(result) == ["claude_gate", "deepseek_gate"]
        assert [p["seat"] for p in result["requested"]] == ["codex_gate", "kimi_gate"]
        assert "glm_gate" not in _gates(result)

    def test_both_subscription_seats_exhausted_give_deepseek_and_glm(self, env, monkeypatch):
        monkeypatch.setenv("VNX_OVERRIDE_REVIEW_GATE_TAKEOVER_CHAIN", "codex_gate,kimi_gate,deepseek_gate,glm_gate")
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
        kimi, claude = _request_record(env, "kimi_gate"), _request_record(env, "claude_gate")
        assert (kimi["seat"], kimi["review_seats"]) == ("codex_gate", expected)
        assert (claude["seat"], claude["review_seats"]) == ("kimi_gate", expected)
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
    def test_kimi_off_its_seat_goes_to_claude_and_kimi_is_never_asked(self, env):
        _set_switches(kimi=False)

        result = _request(None)

        assert _gates(result) == ["codex_gate", "claude_gate"]
        claude = result["requested"][1]
        assert claude["seat"] == "kimi_gate"
        assert [(h["gate"], h["reason"]) for h in claude["takeover_path"]] == [("kimi_gate", "provider_switched_off")]
        assert not (env["requests_dir"] / f"pr-{PR}-kimi_gate.json").exists()

    def test_codex_exhausted_and_kimi_off_both_seats_move_and_kimi_fills_neither(self, env, monkeypatch):
        monkeypatch.setenv("VNX_OVERRIDE_REVIEW_GATE_TAKEOVER_CHAIN", "codex_gate,kimi_gate,deepseek_gate,glm_gate")
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
    to its result record, as a real run would, and returned. Records which gates ran.
    ``during[gate]`` runs while that gate reviews: time passing for the rest of the store."""
    import review_gate_manager

    monkeypatch.setenv("VNX_CODEX_HEADLESS_ENABLED", "1")
    outcomes: Dict[str, Dict[str, Any]] = {}
    during: Dict[str, Callable[[], None]] = {}
    ran: List[str] = []

    def execute_gate(self, *, gate, pr_number, pr_id=""):
        ran.append(gate)
        if gate in during:
            during[gate]()
        record = outcomes.get(gate) or _pass_record(env, gate)
        _write_result(env, gate, record)
        return record

    monkeypatch.setattr(review_gate_manager.ReviewGateManager, "execute_gate", execute_gate)
    return {"outcomes": outcomes, "during": during, "ran": ran}


def _request_and_execute(review_stack):
    return _manager().request_and_execute(
        pr_number=PR, branch=BRANCH, review_stack=review_stack, risk_class="medium",
        changed_files=["scripts/lib/example.py"], mode="per_pr", dispatch_id="20261016-rg-r5a-seat-resolver",
    )


class TestAReviewerThatRunsOutDuringTheRequest:
    def test_kimi_exhausted_mid_request_hands_its_seat_to_claude_in_the_same_call(self, env, runs):
        """Red on the base: the call ran kimi, booked lane_exhausted and stopped; only a second
        identical request gave the seat to deepseek (claude_gate since R7) (#2066 21:09Z and 03:55Z, #2072 00:07Z and 01:37Z)."""
        runs["outcomes"]["kimi_gate"] = _KIMI_WEEKLY_LIMIT_RECORD

        result = _request_and_execute(["kimi_gate"])

        assert [g["gate"] for g in result["gates"]] == ["kimi_gate", "claude_gate"]
        assert runs["ran"] == ["kimi_gate", "claude_gate"]
        kimi, claude = result["gates"]
        assert kimi["superseded_by"] == "claude_gate" and kimi["passed"] is False
        assert claude["passed"] is True and "superseded_by" not in claude
        assert result["has_required_failure"] is False, "the seat's final reviewer passed"
        record = _request_record(env, "claude_gate")
        assert record["seat"] == "kimi_gate"
        assert [(h["gate"], h["reason"]) for h in record["takeover_path"]] == [("kimi_gate", "lane_exhausted")]

    def test_the_handed_on_seat_skips_reviewers_other_seats_hold(self, env, runs, monkeypatch):
        monkeypatch.setenv("VNX_OVERRIDE_REVIEW_GATE_TAKEOVER_CHAIN", "codex_gate,kimi_gate,deepseek_gate,glm_gate")
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
        assert entries["kimi_gate"]["gate"] == "claude_gate"
        assert verify_report(result, pr_number=PR, seats=["kimi_gate"], state_dir=env["state_dir"]).ok


# ---------------------------------------------------------------------------
# Round 2, codex BLOCKING on #2073: a decided verdict is never handed on
# ---------------------------------------------------------------------------

def _fail_record(env, gate: str) -> Dict[str, Any]:
    """A review rejection as review gates book it: ``completed`` plus a blocking finding."""
    record = _pass_record(env, gate)
    record.update(status="completed", blocking_findings=[
        {"severity": "error", "file": "scripts/lib/example.py", "summary": "the seat rejects the change"},
    ])
    return record


def _codex_limit_expires_while(env, runs, reviewer: str) -> None:
    """codex is at its limit when the round walks codex's seat; while ``reviewer`` reads that
    seat, the hour passes and codex's exhaustion record expires."""
    _write_result(env, "codex_gate", _CODEX_LIMIT_RECORD)
    runs["during"][reviewer] = lambda: _write_result(
        env, "codex_gate", {**_CODEX_LIMIT_RECORD, "recorded_at": _EXPIRED},
    )


class TestADecidedVerdictIsNeverHandedOn:
    def test_a_substitute_fail_survives_the_expiry_of_the_exhaustion_it_replaced(self, env, runs):
        """Codex's reproduction. Red on 7f70286e: the walk after the round went back to codex,
        superseded kimi's FAIL with codex's PASS and the call reported no failure."""
        _codex_limit_expires_while(env, runs, "kimi_gate")
        runs["outcomes"]["kimi_gate"] = _fail_record(env, "kimi_gate")

        result = _request_and_execute(["codex_gate"])

        assert result["has_required_failure"] is True, "kimi's FAIL is the answer for codex's seat"
        kimi = result["gates"][0]
        assert "superseded_by" not in kimi, "a decided FAIL is the seat's answer, never handed on"
        assert runs["ran"] == ["kimi_gate"], "codex is not asked again for the seat kimi decided"
        assert [g["gate"] for g in result["gates"]] == ["kimi_gate"]
        assert (kimi["gate"], kimi["seat"], kimi["passed"]) == ("kimi_gate", "codex_gate", False)
        assert not (env["requests_dir"] / f"pr-{PR}-codex_gate.json").exists()

    def test_a_substitute_pass_keeps_its_seat_too(self, env, runs):
        _codex_limit_expires_while(env, runs, "kimi_gate")

        result = _request_and_execute(["codex_gate"])

        kimi = result["gates"][0]
        assert "superseded_by" not in kimi, "a decided PASS is the seat's answer, never handed on"
        assert runs["ran"] == ["kimi_gate"]
        assert [g["gate"] for g in result["gates"]] == ["kimi_gate"]
        assert (kimi["gate"], kimi["passed"]) == ("kimi_gate", True)
        assert result["has_required_failure"] is False

    def test_neither_seat_moves_when_both_reviewers_decided(self, env, runs):
        """The default two-seat stack: kimi reads codex's seat and rejects, claude reads kimi's (R7)
        and passes. Red on 7f70286e: codex took its seat back and kimi took its own seat from
        its reader, so four gates ran for two seats."""
        _codex_limit_expires_while(env, runs, "kimi_gate")
        runs["outcomes"]["kimi_gate"] = _fail_record(env, "kimi_gate")

        result = _request_and_execute(None)

        assert [g.get("superseded_by") for g in result["gates"]] == [None, None]
        assert runs["ran"] == ["kimi_gate", "claude_gate"]
        assert [(g["gate"], g["seat"]) for g in result["gates"]] == [
            ("kimi_gate", "codex_gate"), ("claude_gate", "kimi_gate"),
        ]
        assert result["has_required_failure"] is True

    def test_an_abstaining_reviewer_keeps_its_seat(self, env, runs):
        """No verdict, but no exhaustion either: only the reviewer's own run-out record moves a
        seat, and an abstention is not one. The call fails on the missing verdict."""
        _codex_limit_expires_while(env, runs, "kimi_gate")
        runs["outcomes"]["kimi_gate"] = {
            **_KIMI_WEEKLY_LIMIT_RECORD, "reason": "no_verdict",
            "reason_detail": "the model answered without a verdict block",
        }

        result = _request_and_execute(["codex_gate"])

        assert [g["gate"] for g in result["gates"]] == ["kimi_gate"]
        assert "superseded_by" not in result["gates"][0]
        assert runs["ran"] == ["kimi_gate"]
        assert result["has_required_failure"] is True

    def test_a_substitute_that_runs_out_hands_the_seat_forward_never_back(self, env, runs):
        """kimi read codex's seat and ran out; codex's own exhaustion expired meanwhile. The seat
        walks on from kimi to claude (R7). Red on 7f70286e: the walk restarted at codex."""
        _codex_limit_expires_while(env, runs, "kimi_gate")
        runs["outcomes"]["kimi_gate"] = _KIMI_WEEKLY_LIMIT_RECORD

        result = _request_and_execute(["codex_gate"])

        assert [g["gate"] for g in result["gates"]] == ["kimi_gate", "claude_gate"]
        assert runs["ran"] == ["kimi_gate", "claude_gate"]
        assert result["gates"][0]["superseded_by"] == "claude_gate"
        assert result["has_required_failure"] is False
        record = _request_record(env, "claude_gate")
        assert record["seat"] == "codex_gate"
        assert [(h["gate"], h["reason"]) for h in record["takeover_path"]] == [
            ("codex_gate", "exit_nonzero"), ("kimi_gate", "lane_exhausted"),
        ], "the successor's path names every reviewer the seat passed, starting at the seat"

    def test_an_own_seat_reviewer_that_runs_out_still_goes_to_the_next_link(self, env, runs):
        """Round 1's case (d) under the round-2 rule: the confirmed exhaustion still moves."""
        runs["outcomes"]["kimi_gate"] = _KIMI_WEEKLY_LIMIT_RECORD

        result = _request_and_execute(["kimi_gate"])

        assert [g["gate"] for g in result["gates"]] == ["kimi_gate", "claude_gate"]
        assert result["gates"][0]["superseded_by"] == "claude_gate"
        assert result["has_required_failure"] is False


# ---------------------------------------------------------------------------
# R1c, OI-2181 (2): the advance path passes the round ledger like the first request
# ---------------------------------------------------------------------------

def _book_codex_seat_line(env, *, head: str, reviewer: str, verdict: str, blockers: int) -> None:
    """A codex-seat verdict line in the store's round ledger, as the result writer books it."""
    with (env["state_dir"] / "review_gates" / "rounds.ndjson").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "project_id": "", "pr_number": PR, "head_sha": head, "seat": "codex_gate", "reviewer": reviewer,
            "takeover_from": "codex_gate" if reviewer != "codex_gate" else "", "verdict": verdict,
            "blocking_count": blockers, "report_path": f"/r/20261018-HEADLESS-{reviewer}-pr-{PR}-{head[:2]}.md",
            "dispatch_id": "d-1", "recorded_at": "2026-10-18T09:00:00Z", "branch": BRANCH, "source": "gate",
        }) + "\n")


def _capped_ledger(env) -> str:
    """The PR is opened and its codex seat sits at B3.1 after one FAIL(3) on an earlier head."""
    open_pr_in_ledger(env["state_dir"], PR)
    _book_codex_seat_line(env, head="a" * 40, reviewer="codex_gate", verdict="fail", blockers=3)
    return "b3_cap"


def _history_on_disk(env) -> str:
    """The PR is not opened and an earlier headless report of the codex seat is on disk."""
    headless = env["reports_dir"] / "headless"
    headless.mkdir(parents=True, exist_ok=True)
    (headless / f"20261017-090000-HEADLESS-codex_gate-pr-{PR}-c0ffee.md").write_text("FAIL\n", encoding="utf-8")
    return "round_history_missing"


def _unreadable_ledger(env) -> str:
    """A line of the ledger does not parse and names no PR, so it touches every PR."""
    (env["state_dir"] / "review_gates" / "rounds.ndjson").write_text("not json at all\n", encoding="utf-8")
    return "round_ledger_unreadable"


_DURING_THE_RUN = {
    # A parallel takeover in the codex seat booked its PASS on this head while codex ran.
    "verdict_on_head": lambda env: _book_codex_seat_line(env, head=HEAD_SHA, reviewer="kimi_gate", verdict="pass", blockers=0),
    # A slow review of the previous head finished with FAIL(3) while codex ran: B3.1 now holds.
    "b3_cap": lambda env: _book_codex_seat_line(env, head="b" * 40, reviewer="codex_gate", verdict="fail", blockers=3),
    # A writer died in the middle of its record, which names no PR.
    "round_ledger_unreadable": lambda env: _unreadable_ledger(env),
}


class TestTheAdvancePathIsRefusedLikeTheFirstRequest:
    @pytest.mark.parametrize("reason", sorted(_DURING_THE_RUN))
    def test_a_ledger_that_refuses_the_seat_by_the_advance_stops_the_successor(self, env, runs, reason):
        """OI-2181 (2). Red on 2b9da5c5: the seat's first request passed the round ledger, codex ran
        and ran out, and the ledger refused the seat by then; the advance requested kimi without
        reading the ledger, and kimi ran."""
        runs["outcomes"]["codex_gate"] = _CODEX_LIMIT_RECORD
        runs["during"]["codex_gate"] = lambda: _DURING_THE_RUN[reason](env)

        result = _request_and_execute(["codex_gate"])

        assert runs["ran"] == ["codex_gate"], "a refused advance spends no provider run"
        assert [g["gate"] for g in result["gates"]] == ["codex_gate"]
        assert "superseded_by" not in result["gates"][0]
        assert result["has_required_failure"] is True
        assert [r["gate"] for r in env["receipts"]] == ["codex_gate"], "a refused advance books nothing"
        assert not (env["requests_dir"] / f"pr-{PR}-kimi_gate.json").exists()

    @pytest.mark.parametrize("ledger", [_capped_ledger, _history_on_disk, _unreadable_ledger],
                             ids=["b3-cap", "round-history-missing", "ledger-unreadable"])
    def test_a_seat_refused_at_its_first_request_is_never_handed_on(self, env, runs, ledger):
        """A pin, green on 2b9da5c5 as well: the refusal is booked ``blocked``, which is a decided
        answer (``decided_verdict`` reads it as ``fail``), so ``_advance_exhausted_seats`` keeps the
        seat even with codex's own fresh limit record on this head."""
        reason = ledger(env)
        _write_result(env, "codex_gate", _CODEX_LIMIT_RECORD)

        result = _request_and_execute(["codex_gate"])

        assert runs["ran"] == [], "a refused advance spends no provider run"
        assert [(g["gate"], g["request_status"]) for g in result["gates"]] == [("codex_gate", "blocked")]
        assert "superseded_by" not in result["gates"][0]
        assert result["has_required_failure"] is True
        assert [r["request"].get("reason") for r in env["receipts"]] == [reason], "the refusal, and nothing else"
        assert not (env["requests_dir"] / f"pr-{PR}-kimi_gate.json").exists()
        assert not (env["requests_dir"] / f"pr-{PR}-codex_gate.json").exists()

    def test_an_override_still_travels_with_an_advanced_seat(self, env, runs):
        """The one advance the cap lets through: ``--override-b3`` on the request goes with the seat."""
        _capped_ledger(env)
        runs["outcomes"]["codex_gate"] = _CODEX_LIMIT_RECORD

        result = _manager().request_and_execute(
            pr_number=PR, branch=BRANCH, review_stack=["codex_gate"], risk_class="medium",
            changed_files=["scripts/lib/example.py"], mode="per_pr", dispatch_id="20261019-rs-r1c-grootboek",
            override_b3="operator: one-line fix after B3.1",
        )

        assert runs["ran"] == ["codex_gate", "kimi_gate"]
        assert result["gates"][0]["superseded_by"] == "kimi_gate"
        assert _request_record(env, "kimi_gate")["override_b3"]["reason"] == "operator: one-line fix after B3.1"


# ---------------------------------------------------------------------------
# Round 2, codex WARNING on #2073: an entry's own seat beats its takeover history
# ---------------------------------------------------------------------------

class TestAnEntrysOwnSeatWins:
    def test_a_standing_pass_with_an_old_codex_takeover_is_credited_to_kimis_own_seat(self, env, runs):
        """Codex's reproduction: kimi is switched off, and its standing PASS on this head still
        carries the codex takeover of the round in which kimi read codex's seat. A request for
        kimi's own seat reports that PASS. Red on 7f70286e: verification credited it to codex's
        seat and reported kimi's seat missing."""
        _set_switches(kimi=False)
        codex_hop = {"gate": "codex_gate", "reason": "exit_nonzero", "status": "unavailable",
                     "detail": "You've hit your usage limit."}
        # R1: the earlier round's request opened the PR in the round ledger; without that, a
        # PASS on disk is history the ledger never counted and every seat is refused.
        open_pr_in_ledger(env["state_dir"], PR)
        _write_result(env, "kimi_gate", {
            **_pass_record(env, "kimi_gate"), "status": "completed", "takeover_path": [codex_hop],
        })
        # The earlier round's request record: kimi read codex's seat then. A stale seat on disk
        # does not decide either; the entry carries the seat THIS request asked kimi for.
        (env["requests_dir"] / f"pr-{PR}-kimi_gate.json").write_text(json.dumps({
            "gate": "kimi_gate", "status": "completed", "pr_number": PR, "seat": "codex_gate",
            "takeover": True, "takeover_from": "codex_gate", "takeover_path": [codex_hop],
        }), encoding="utf-8")

        result = _request_and_execute(["kimi_gate"])

        from gate_enforcement_verify import resolve_seat_entries, verify_report
        outcome = verify_report(result, pr_number=PR, seats=["kimi_gate"], state_dir=env["state_dir"])
        assert outcome.missing == [], "kimi's own seat is answered by kimi's own standing PASS"
        assert outcome.ok
        kimi = result["gates"][0]
        assert resolve_seat_entries(result, pr_number=PR, state_dir=env["state_dir"]) == {"kimi_gate": kimi}
        assert [g["gate"] for g in result["gates"]] == ["kimi_gate"]
        assert (kimi["seat"], kimi["passed"]) == ("kimi_gate", True)
        assert kimi["detail"]["takeover_path"] == [codex_hop], "the history is still on the record"
        assert runs["ran"] == [], "a switched-off seat's standing verdict is reported, never re-run"

    def test_an_entry_without_a_stated_seat_still_falls_back_to_its_takeover(self, env):
        """A record from before R5a names no seat: its walk's first hop is the seat it read."""
        from gate_enforcement_verify import resolve_seat_entries

        report = {"gates": [{"gate": "kimi_gate", "request_status": "requested", "detail": {
            "takeover_path": [{"gate": "codex_gate", "reason": "exit_nonzero", "status": "unavailable"}],
        }}]}

        assert list(resolve_seat_entries(report, pr_number=PR, state_dir=env["state_dir"])) == ["codex_gate"]

    def test_a_stated_seat_that_is_not_a_plain_name_is_refused(self, env):
        from gate_enforcement_verify import VerificationError, resolve_seat_entries

        report = {"gates": [{"gate": "kimi_gate", "seat": "../codex_gate", "request_status": "requested"}]}

        with pytest.raises(VerificationError, match="not a plain identifier"):
            resolve_seat_entries(report, pr_number=PR, state_dir=env["state_dir"])


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
