"""Review stack: how the subscription-first order runs.

The order itself (billing classification, the guard, the obligation the door
declares) is pinned in ``tests/test_review_stack_subscription_first.py``. Here the
order is exercised end to end, against the real code with only the provider edge
stubbed (the governed dispatcher, ``gh``). Nothing starts a kimi, codex, glm or
deepseek process.

1. ``vnx gate --only kimi_gate`` (``request_and_execute``) runs kimi through the
   governed lane and books a record with the same evidence a codex pass carries.
2. The merge door accepts that kimi pass, declared as kimi_gate and declared as
   codex_gate (kimi took the codex seat).
3. With codex at its limit the default stack goes to kimi, exactly once, before
   any API-credit gate is asked; only when kimi is exhausted too does glm read.
4. The dedupe counts only the requests that went out.

The shared project-store setup is in ``tests/review_stack_support.py``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import review_stack_support  # noqa: F401  (puts scripts/ and scripts/lib on sys.path)
from review_stack_support import BRANCH, HEAD_SHA, PR, build_project_store, isolate_config_registry

import gate_recorder
from gate_recorder import GATE_PROVIDERS

_PROVIDER_BINARIES = frozenset({"kimi", "codex", "claude", "gemini", "glm", "deepseek"})

_FRESH_RECORDED_AT = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

_DIFF = (
    "diff --git a/scripts/lib/example.py b/scripts/lib/example.py\n"
    "--- a/scripts/lib/example.py\n"
    "+++ b/scripts/lib/example.py\n"
    "@@ -1,2 +1,3 @@\n"
    " def example():\n"
    "-    return 1\n"
    "+    return 2\n"
    "+\n"
)

_PASS_REPORT = (
    "Reviewed the diff.\nRan the tests.\nNo blocking findings.\n\n"
    "```json\n"
    '{"verdict": "pass", "findings": [], "residual_risk": null}\n'
    "```\n"
)

_CODEX_LIMIT_RECORD = {
    "gate": "codex_gate",
    "pr_id": str(PR),
    "pr_number": PR,
    "status": "unavailable",
    "reason": "exit_nonzero",
    "reason_detail": (
        "Subprocess exited with code 1: You've hit your usage limit. Upgrade "
        "to Pro (https://chatgpt.com/explore/pro), visit "
        "https://chatgpt.com/codex/settings/usage to purchase more credits or "
        "try again at 8:53 PM."
    ),
    "contract_hash": "",
    "report_path": "",
    "blocking_findings": [],
    "advisory_findings": [],
    "required_reruns": ["codex_gate"],
    "residual_risk": "Gate exit_nonzero. Re-run required.",
    "commit_sha": HEAD_SHA,
    "recorded_at": _FRESH_RECORDED_AT,
}

_KIMI_LIMIT_RECORD = {
    "gate": "kimi_gate",
    "pr_id": str(PR),
    "pr_number": PR,
    "status": "unavailable",
    "reason": "dispatch_error",
    "residual_risk": (
        "governed kimi dispatch failed: Error code: 403 - {'error': "
        "{'message': 'Your account has been suspended, please contact us "
        "via api-feedback@moonshot.cn', 'type': 'access_terminated_error'}}"
    ),
    "contract_hash": "",
    "report_path": "",
    "blocking_findings": [],
    "advisory_findings": [],
    "commit_sha": HEAD_SHA,
    "recorded_at": _FRESH_RECORDED_AT,
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_config_registry(monkeypatch):
    yield from isolate_config_registry(monkeypatch)


@pytest.fixture
def env(tmp_path, monkeypatch):
    return build_project_store(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def _no_provider_process(monkeypatch):
    """A leaking test that spawns a real reviewer has happened before: any attempt
    to start a provider CLI fails the test, whatever a test stubs or forgets to."""
    import gate_runner

    real_popen = gate_runner.subprocess.Popen

    def guarded_popen(args, *a, **kw):
        argv = list(args) if isinstance(args, (list, tuple)) else [args]
        if Path(str(argv[0])).name in _PROVIDER_BINARIES:
            raise AssertionError(
                f"a review gate must run through the governed lane, never start {argv[0]!r}"
            )
        return real_popen(args, *a, **kw)

    monkeypatch.setattr(gate_runner.subprocess, "Popen", guarded_popen)


@pytest.fixture
def lane(env, monkeypatch):
    """The governed dispatcher and ``gh pr diff`` replaced. Returns the list of
    provider calls the lane received."""
    import gate_runner

    calls: list = []

    def factory(data_dir, timeout_seconds, *, role="plan-reviewer"):
        def dispatch(provider, model, instruction, dispatch_id):
            calls.append({"provider": provider, "model": model, "dispatch_id": dispatch_id, "role": role})
            return _PASS_REPORT
        return dispatch

    monkeypatch.setattr("plan_gate_panel._make_default_dispatcher", factory)
    monkeypatch.setattr(
        gate_runner.GateRunner, "_fetch_gh_pr_diff", staticmethod(lambda _pr: _DIFF),
    )
    return calls


def _manager():
    import review_gate_manager as rgm
    return rgm.ReviewGateManager()


def _write_result(env, gate: str, payload: dict) -> Path:
    path = env["results_dir"] / f"pr-{PR}-{gate}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _request(manager, review_stack):
    return manager.request_reviews(
        pr_number=PR, branch=BRANCH, review_stack=review_stack, risk_class="medium",
        changed_files=["scripts/lib/example.py"], mode="per_pr",
        dispatch_id="20260926-review-stack-subscription-first",
    )


# ---------------------------------------------------------------------------
# 3. kimi runs through `vnx gate` (request_and_execute), not a side script
# ---------------------------------------------------------------------------

class TestKimiThroughTheGatePath:
    """``vnx gate <pr> --only kimi_gate`` is ``review_gate_manager.py
    request-and-execute --review-stack kimi_gate``. It must book a real kimi verdict,
    never ``not_executable`` / ``gate_not_subprocess_routable`` (the record of a
    gate registered as a script runner, which kimi_gate no longer is)."""

    def _run(self, review_stack, mode="final"):
        return _manager().request_and_execute(
            pr_number=PR, branch=BRANCH, review_stack=review_stack, risk_class="medium",
            changed_files=["scripts/lib/example.py"], mode=mode,
            dispatch_id="20260926-review-stack-subscription-first",
        )

    def test_kimi_gate_is_a_harness_lane_gate_not_a_script_runner(self):
        kind, provider = GATE_PROVIDERS["kimi_gate"]
        assert kind == gate_recorder.GATE_PROVIDER_HARNESS_LANE
        assert provider == "kimi"

    def test_only_kimi_gate_executes_kimi_through_the_governed_lane(self, env, lane):
        result = self._run(["kimi_gate"])

        assert [g["gate"] for g in result["gates"]] == ["kimi_gate"]
        gate = result["gates"][0]
        assert gate["execution_status"] == "completed", gate["detail"].get("reason_detail")
        assert gate["passed"] is True
        assert result["has_required_failure"] is False
        assert len(lane) == 1 and lane[0]["provider"] == "kimi" and lane[0]["role"] == "review-gate"

    def test_the_kimi_record_carries_the_evidence_a_codex_pass_carries(self, env, lane):
        from gate_status import has_complete_evidence, is_pass

        self._run(["kimi_gate"])
        record = json.loads((env["results_dir"] / f"pr-{PR}-kimi_gate.json").read_text(encoding="utf-8"))

        assert record["status"] == "completed"
        assert record.get("reason") != "gate_not_subprocess_routable"
        assert record["status"] != "not_executable"
        assert record["commit_sha"] == HEAD_SHA, "a verdict is about ONE commit: the PR head"
        assert record["contract_hash"], "empty contract_hash never satisfies the merge door"
        assert record["report_path"] and Path(record["report_path"]).is_file()
        assert record["blocking_findings"] == []
        assert record["provider"] == "kimi"
        assert record["dispatch_id"].startswith("kimi-gate-pr"), "signed with its OWN identity, never the builder's"
        assert has_complete_evidence(record)
        assert is_pass(record)[0]

    def test_a_rejecting_verdict_is_booked_as_a_rejection_not_a_pass(self, env, monkeypatch):
        import gate_runner

        fail_report = (
            "Reviewed the diff.\n\n```json\n"
            '{"verdict": "fail", "findings": [{"severity": "error", "message": "return value changed", '
            '"file_path": "scripts/lib/example.py", "line": 2}], "residual_risk": null}\n```\n'
        )
        monkeypatch.setattr(
            "plan_gate_panel._make_default_dispatcher",
            lambda *a, **kw: (lambda provider, model, instruction, dispatch_id: fail_report),
        )
        monkeypatch.setattr(gate_runner.GateRunner, "_fetch_gh_pr_diff", staticmethod(lambda _pr: _DIFF))

        result = self._run(["kimi_gate"])

        gate = result["gates"][0]
        assert gate["passed"] is False
        assert result["has_required_failure"] is True
        record = json.loads((env["results_dir"] / f"pr-{PR}-kimi_gate.json").read_text(encoding="utf-8"))
        assert record["blocking_findings"], "the blocking finding must reach the record the door reads"


# ---------------------------------------------------------------------------
# 4. The merge door accepts a kimi pass like a codex pass
# ---------------------------------------------------------------------------

class TestMergeDoorAcceptsKimi:
    DISPATCH = "20260926-review-stack-subscription-first"

    def _door(self, env, monkeypatch, declared_gate):
        import gate_obligations
        import pr_merge

        gate_obligations.register_obligation(
            env["state_dir"], dispatch_id=self.DISPATCH, gate=declared_gate,
            pr_number=PR, pr_id=str(PR), branch=BRANCH,
        )
        monkeypatch.setattr(pr_merge, "_query_pr", lambda _n: {"headRefName": BRANCH, "headRefOid": HEAD_SHA})
        monkeypatch.setattr(pr_merge, "ensure_env", lambda *a, **k: {"VNX_STATE_DIR": str(env["state_dir"])})
        monkeypatch.setattr(pr_merge, "_resolve_override_reason", lambda _r: None)
        gate, _pr_data = pr_merge._run_review_gate(PR)
        return gate

    def test_a_kimi_pass_opens_the_door_when_kimi_is_the_declared_gate(self, env, lane, monkeypatch):
        _manager().request_and_execute(
            pr_number=PR, branch=BRANCH, review_stack=["kimi_gate"], risk_class="medium",
            changed_files=["scripts/lib/example.py"], mode="final", dispatch_id=self.DISPATCH,
        )

        gate = self._door(env, monkeypatch, "kimi_gate")

        assert gate["verdict"] == "GO", gate["message"]
        assert gate["overridden"] is False

    def test_a_kimi_pass_opens_the_door_for_a_codex_obligation_when_kimi_took_the_seat(
        self, env, lane, monkeypatch,
    ):
        _write_result(env, "codex_gate", _CODEX_LIMIT_RECORD)
        _manager().request_and_execute(
            pr_number=PR, branch=BRANCH, review_stack=["codex_gate"], risk_class="medium",
            changed_files=["scripts/lib/example.py"], mode="final", dispatch_id=self.DISPATCH,
        )
        record = json.loads((env["results_dir"] / f"pr-{PR}-kimi_gate.json").read_text(encoding="utf-8"))
        assert record["takeover_from"] == "codex_gate", "kimi must carry the takeover it was booked under"

        gate = self._door(env, monkeypatch, "codex_gate")

        assert gate["verdict"] == "GO", gate["message"]
        assert gate.get("evidence_gate") == "kimi_gate"

    def test_the_door_refuses_a_kimi_rejection_exactly_like_a_codex_rejection(self, env, monkeypatch):
        import gate_runner

        fail_report = (
            "```json\n"
            '{"verdict": "fail", "findings": [{"severity": "error", "message": "breaks callers", '
            '"file_path": "scripts/lib/example.py", "line": 2}], "residual_risk": null}\n```\n'
        )
        monkeypatch.setattr(
            "plan_gate_panel._make_default_dispatcher",
            lambda *a, **kw: (lambda provider, model, instruction, dispatch_id: fail_report),
        )
        monkeypatch.setattr(gate_runner.GateRunner, "_fetch_gh_pr_diff", staticmethod(lambda _pr: _DIFF))
        _manager().request_and_execute(
            pr_number=PR, branch=BRANCH, review_stack=["kimi_gate"], risk_class="medium",
            changed_files=["scripts/lib/example.py"], mode="final", dispatch_id=self.DISPATCH,
        )

        assert self._door(env, monkeypatch, "kimi_gate")["verdict"] == "NO-GO"


# ---------------------------------------------------------------------------
# 5. Codex at its limit: kimi reads next, exactly once, before any API-credit gate
# ---------------------------------------------------------------------------

@pytest.fixture
def default_stack(env, monkeypatch):
    """The stack a project gets when it never set one, with the gh-driven ci_gate
    left out so no test path reaches for ``gh``; codex counted as installed."""
    import review_gate_manager as rgm
    import gate_request_handler
    import profile_gate_resolver

    monkeypatch.setenv("VNX_CI_GATE_REQUIRED", "0")
    stack = rgm._build_default_review_stack()
    assert stack == ["codex_gate", "kimi_gate"]
    monkeypatch.setattr(rgm, "DEFAULT_REVIEW_STACK", stack)
    monkeypatch.setattr(profile_gate_resolver, "resolve_gate_stack", lambda _files: None)
    monkeypatch.setattr(gate_request_handler.GateRequestHandlerMixin, "_codex_headless_available", lambda self: True)
    return stack


def _requested_gates(result) -> list:
    return [seat["gate"] for seat in result["requested"]]


class TestCodexAtItsLimitGoesToKimi:
    def test_healthy_codex_reads_beside_kimi_and_no_api_gate_is_asked(self, env, default_stack):
        result = _request(_manager(), None)

        assert _requested_gates(result) == ["codex_gate", "kimi_gate"]
        for gate in ("glm_gate", "deepseek_gate"):
            assert not (env["requests_dir"] / f"pr-{PR}-{gate}.json").exists(), (
                f"{gate} is a fallback; nobody asked for it while both subscription seats work"
            )

    def test_codex_at_its_limit_hands_the_seat_to_kimi_not_to_glm(self, env, default_stack):
        _write_result(env, "codex_gate", _CODEX_LIMIT_RECORD)

        result = _request(_manager(), None)

        assert _requested_gates(result) == ["kimi_gate"], (
            "codex is exhausted: kimi takes its seat, and kimi's own standing seat must not "
            f"request kimi a second time. requested={_requested_gates(result)}"
        )
        seat = result["requested"][0]
        assert seat["takeover_from"] == "codex_gate"
        assert "usage limit" in seat["failure_reason"].lower()
        for gate in ("glm_gate", "deepseek_gate"):
            assert not (env["requests_dir"] / f"pr-{PR}-{gate}.json").exists()

    def test_the_takeover_annotation_survives_on_the_kimi_request_record(self, env, default_stack):
        _write_result(env, "codex_gate", _CODEX_LIMIT_RECORD)

        _request(_manager(), None)

        on_disk = json.loads((env["requests_dir"] / f"pr-{PR}-kimi_gate.json").read_text(encoding="utf-8"))
        assert on_disk["takeover"] is True
        assert on_disk["takeover_from"] == "codex_gate"
        assert [hop["gate"] for hop in on_disk["takeover_path"]] == ["codex_gate"]

    def test_kimi_runs_once_when_codex_is_at_its_limit(self, env, lane, default_stack):
        _write_result(env, "codex_gate", _CODEX_LIMIT_RECORD)

        result = _manager().request_and_execute(
            pr_number=PR, branch=BRANCH, review_stack=None, risk_class="medium",
            changed_files=["scripts/lib/example.py"], mode="final",
            dispatch_id="20260926-review-stack-subscription-first",
        )

        assert [g["gate"] for g in result["gates"]] == ["kimi_gate"]
        assert result["gates"][0]["passed"] is True
        assert [call["provider"] for call in lane] == ["kimi"], (
            f"kimi is a real, minutes-long provider call: it ran {len(lane)} times"
        )

    def test_glm_reads_only_when_codex_and_kimi_are_both_at_their_limit(self, env, default_stack):
        _write_result(env, "codex_gate", _CODEX_LIMIT_RECORD)
        _write_result(env, "kimi_gate", _KIMI_LIMIT_RECORD)

        result = _request(_manager(), None)

        assert _requested_gates(result) == ["glm_gate"], (
            f"both subscription seats are exhausted: glm reads, once. requested={_requested_gates(result)}"
        )
        seat = result["requested"][0]
        assert [hop["gate"] for hop in seat["takeover_path"]] == ["codex_gate", "kimi_gate"]
        assert not (env["requests_dir"] / f"pr-{PR}-deepseek_gate.json").exists()

    def test_a_stack_that_names_the_same_gate_twice_requests_it_once(self, env, default_stack):
        result = _request(_manager(), ["kimi_gate", "kimi_gate"])

        assert _requested_gates(result) == ["kimi_gate"]

    def test_a_chain_walked_to_a_gate_outside_the_stack_is_still_requested(self, env, default_stack):
        """The dedupe removes only a repeat. A takeover to a gate no earlier seat asked for
        is a new request and must still go out."""
        _write_result(env, "codex_gate", _CODEX_LIMIT_RECORD)

        result = _request(_manager(), ["codex_gate"])

        assert _requested_gates(result) == ["kimi_gate"]
        assert result["requested"][0]["status"] == "requested"


# ---------------------------------------------------------------------------
# 6. One request per gate counts only the requests that went out
# ---------------------------------------------------------------------------

class TestOneRequestPerGateCountsOnlyRequestsThatWentOut:
    """The dedupe is against requests that were DISPATCHED. A request the gate itself
    refused (binary missing, not configured) asked no reader anything, so a later seat that
    resolves to the same gate must still try it instead of being skipped behind a refusal."""

    @pytest.fixture
    def kimi_requests(self, env, monkeypatch):
        """The real ``_request_kimi``, counted, with the takeover chain off so a seat
        resolves to exactly the gate it names. Only availability is stubbed."""
        import gate_request_handler

        monkeypatch.setenv("VNX_CI_GATE_REQUIRED", "0")
        monkeypatch.setenv("VNX_REVIEW_GATE_TAKEOVER_CHAIN", "")
        mixin = gate_request_handler.GateRequestHandlerMixin
        real_request_kimi = mixin._request_kimi
        calls: list = []

        def counting(self, *args, **kwargs):
            calls.append(args[0])
            return real_request_kimi(self, *args, **kwargs)

        monkeypatch.setattr(mixin, "_request_kimi", counting)
        return calls

    @staticmethod
    def _kimi_available(monkeypatch, available: bool) -> None:
        import gate_request_handler
        monkeypatch.setattr(
            gate_request_handler.GateRequestHandlerMixin, "_kimi_gate_available", lambda self: available,
        )

    def test_a_repeat_of_an_accepted_request_is_skipped(self, kimi_requests, monkeypatch):
        self._kimi_available(monkeypatch, True)

        result = _request(_manager(), ["kimi_gate", "kimi_gate"])

        assert len(kimi_requests) == 1
        assert [(seat["gate"], seat["status"]) for seat in result["requested"]] == [("kimi_gate", "requested")]

    def test_a_repeat_of_a_refused_request_is_attempted(self, kimi_requests, monkeypatch):
        self._kimi_available(monkeypatch, False)

        result = _request(_manager(), ["kimi_gate", "kimi_gate"])

        assert len(kimi_requests) == 2, (
            "the first kimi request was refused at request time (not_executable): it asked no "
            f"reader anything, so the second seat must try kimi itself. requests={len(kimi_requests)}"
        )
        assert [seat["status"] for seat in result["requested"]] == ["not_executable", "not_executable"]

    @staticmethod
    def _kimi_stub(monkeypatch, statuses: list) -> list:
        """``_request_kimi`` answering with one status per call and writing nothing, so the
        second seat's chain walk sees no kimi record of its own."""
        import gate_request_handler

        calls: list = []

        def stub(self, pr_number, branch, risk_class, changed_files, mode, dispatch_id=""):
            calls.append(len(calls))
            return {"gate": "kimi_gate", "status": statuses[len(calls) - 1], "pr_number": pr_number}

        monkeypatch.setattr(gate_request_handler.GateRequestHandlerMixin, "_request_kimi", stub)
        return calls

    def test_a_seat_taken_over_to_a_gate_whose_earlier_request_was_refused_is_requested(
        self, env, default_stack, monkeypatch,
    ):
        """codex is at its limit, so the codex seat resolves to kimi. kimi's own seat, earlier in
        the same round, was refused at request time: the takeover must still be attempted."""
        _write_result(env, "codex_gate", _CODEX_LIMIT_RECORD)
        calls = self._kimi_stub(monkeypatch, ["not_executable", "requested"])

        result = _request(_manager(), ["kimi_gate", "codex_gate"])

        assert len(calls) == 2
        assert [seat["status"] for seat in result["requested"]] == ["not_executable", "requested"]
        assert result["requested"][1]["takeover_from"] == "codex_gate"

    def test_a_seat_taken_over_to_a_gate_whose_earlier_request_was_accepted_is_skipped(
        self, env, default_stack, monkeypatch,
    ):
        _write_result(env, "codex_gate", _CODEX_LIMIT_RECORD)
        calls = self._kimi_stub(monkeypatch, ["requested", "requested"])

        result = _request(_manager(), ["kimi_gate", "codex_gate"])

        assert len(calls) == 1
        assert _requested_gates(result) == ["kimi_gate"]

    @pytest.mark.parametrize("status", [
        "not_executable", "unavailable", "blocked", "not_configured", "chain_exhausted",
    ])
    def test_a_refused_status_never_counts_as_requested(self, status):
        from gate_request_handler import _gates_with_accepted_request

        assert _gates_with_accepted_request([{"gate": "kimi_gate", "status": status}]) == []

    @pytest.mark.parametrize("status", [
        "requested", "queued", "configured_dry_run", "pass", "fail", "advisory",
    ])
    def test_any_other_status_counts_as_requested(self, status):
        from gate_request_handler import _gates_with_accepted_request

        assert _gates_with_accepted_request([{"gate": "kimi_gate", "status": status}]) == ["kimi_gate"]

    def test_a_payload_without_a_status_counts_as_requested_and_keeps_its_gate(self):
        from gate_request_handler import _gates_with_accepted_request

        payloads = [
            {"gate": "codex_gate", "status": "not_executable"},
            {"gate": "kimi_gate"},
            {"gate": "glm_gate", "status": "requested"},
        ]

        assert _gates_with_accepted_request(payloads) == ["kimi_gate", "glm_gate"]
