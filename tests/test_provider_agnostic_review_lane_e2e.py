"""End to end: a ``kimi_gate`` obligation the door declares is fulfilled by the
obligation runner, with complete evidence (track ``provider-agnostic-review-lane``, P7).

Three real parts run together, only the provider edge is faked:

1. the door: ``_resolve_gate_via_router`` + ``_register_gate_obligation`` on a
   project whose review stack puts kimi in the primary seat;
2. the obligation runner: ``fulfill_obligation`` through its real launch guard,
   evidence decision and booking;
3. the review-gate manager and GateRunner: the real harness-lane path, with the
   governed dispatcher replaced by a fake lane that returns a report.

No kimi process, no network, no ``gh``. The expected gate is read from the stack
the test configures, never typed as the answer to a question the door should
decide (the same rule as ``tests/test_gate_obligation_follows_review_stack.py``).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

import review_stack_support  # noqa: F401  (puts scripts/ and scripts/lib on sys.path)
from review_stack_support import BRANCH, HEAD_SHA, PR, build_project_store, isolate_config_registry

import gate_obligation_runner as runner
from dispatch_cli import _register_gate_obligation, _resolve_gate_via_router
from dispatch_spec import DispatchSpec, ValidatedSpec
from gate_obligations import STATUS_FULFILLED, STATUS_NOT_EXECUTABLE, STATUS_PENDING
from gate_status import has_complete_evidence, is_pass

KIMI_STACK = "kimi_gate,glm_gate"
DISPATCH_ID = "dlv-p7-kimi-e2e"
OWNER_REPO = "example-owner/example-repo"

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


@pytest.fixture(autouse=True)
def _clean_config_registry(monkeypatch):
    yield from isolate_config_registry(monkeypatch)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_OVERRIDE_DEFAULT_REVIEW_STACK", KIMI_STACK)
    monkeypatch.setenv("VNX_OVERRIDE_CI_GATE_REQUIRED", "0")
    return build_project_store(tmp_path, monkeypatch)


@pytest.fixture
def lane(env, monkeypatch) -> List[Dict[str, Any]]:
    """The governed dispatcher and ``gh pr diff`` replaced; returns the lane's calls."""
    import gate_runner

    calls: List[Dict[str, Any]] = []

    def factory(data_dir, timeout_seconds, *, role="plan-reviewer", base_ref=None,
                total_deadline_seconds=None):
        def dispatch(provider, model, instruction, dispatch_id):
            calls.append({"provider": provider, "model": model, "dispatch_id": dispatch_id})
            return _PASS_REPORT
        return dispatch

    monkeypatch.setattr("plan_gate_panel._make_default_dispatcher", factory)
    monkeypatch.setattr(gate_runner, "fetch_branch", lambda *a, **kw: None)
    monkeypatch.setattr(
        gate_runner.GateRunner, "_fetch_gh_pr_diff", staticmethod(lambda _pr: _DIFF),
    )
    return calls


@pytest.fixture
def runner_edges(env, monkeypatch) -> None:
    """Hermetic runner: the PR resolves, the head is fixed, no ``gh``, no git."""
    resolution = runner.PrResolution(
        runner.RESOLUTION_RESOLVED, pr_number=PR, owner_repo=OWNER_REPO,
    )
    monkeypatch.setattr(runner, "resolve_pr_number", lambda *a, **k: resolution)
    monkeypatch.setattr(runner, "_get_pr_head_sha_for_gate", lambda pr_number: HEAD_SHA)
    monkeypatch.setattr(runner, "_pr_head_sha_for_gate", lambda pr_number, snapshot=None: HEAD_SHA)
    monkeypatch.setattr(runner, "_branch_from_github", lambda pr, owner_repo, snapshot=None: BRANCH)
    monkeypatch.setattr(runner, "_resolve_github_owner_repo", lambda state_dir: OWNER_REPO)
    monkeypatch.setattr(runner, "_pr_state_from_github", lambda *a, **k: "OPEN")
    import review_gate_manager
    monkeypatch.setattr(review_gate_manager, "_compute_changed_files", lambda branch: ["scripts/lib/example.py"])


def _door_declares(state_dir: Path, dispatch_id: str = DISPATCH_ID) -> Path:
    """Run the door's own two steps for a silent spec; return the obligation path."""
    spec = DispatchSpec(
        schema_version=1, project_id="vnx-dev", dispatch_id=dispatch_id,
        staging_id=dispatch_id, instruction_file=Path("instruction.md"),
        role="quality-engineer", target_slot="T1", gate="", dispatch_paths=(),
    )
    validated = ValidatedSpec(
        spec=spec, instruction_text="werk", normalized_paths=spec.dispatch_paths,
        instruction_sha256="0" * 64,
    )
    vspec, _reason = _resolve_gate_via_router(validated)
    assert _register_gate_obligation(vspec.spec, state_dir=state_dir) is None
    path = state_dir / "review_gates" / "obligations" / f"{dispatch_id}.json"
    assert path.exists(), f"the door declared no obligation at {path}"
    return path


def _read(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _declared_and_fulfilled(env) -> tuple[Path, Dict[str, Any]]:
    path = _door_declares(env["state_dir"])
    outcome = runner.fulfill_obligation(env["state_dir"], path, _read(path))
    return path, outcome


class TestTheDoorDeclaresAKimiObligation:
    def test_a_kimi_primary_stack_declares_kimi_gate(self, env):
        record = _read(_door_declares(env["state_dir"]))

        assert record["gate"] == "kimi_gate", (
            f"stack {KIMI_STACK!r} puts kimi in the primary seat, the door declared {record['gate']!r}"
        )
        assert record["status"] == STATUS_PENDING
        assert record["dispatch_id"] == DISPATCH_ID

    def test_kimi_behind_codex_is_not_declared_the_door_declares_one_seat(self, env, monkeypatch):
        """Measured on main: a stack with codex first declares codex only, so kimi
        is never an obligation there. This pins the one-obligation-per-dispatch fact."""
        monkeypatch.setenv("VNX_OVERRIDE_DEFAULT_REVIEW_STACK", "codex_gate,kimi_gate")

        record = _read(_door_declares(env["state_dir"]))

        assert record["gate"] == "codex_gate"


class TestTheRunnerFulfilsItWithCompleteEvidence:
    def test_the_obligation_is_fulfilled_by_the_runner(self, env, lane, runner_edges):
        path, outcome = _declared_and_fulfilled(env)

        assert outcome["action"] == STATUS_FULFILLED, outcome
        assert outcome["gate"] == "kimi_gate"
        stored = _read(path)
        assert stored["status"] == STATUS_FULFILLED
        assert stored["attempts"] == 1
        assert [c["provider"] for c in lane] == ["kimi"], "the fake lane was reached exactly once, as kimi"

    def test_the_result_record_carries_the_complete_evidence(self, env, lane, runner_edges):
        _declared_and_fulfilled(env)

        result_path = env["results_dir"] / f"pr-{PR}-kimi_gate.json"
        result = _read(result_path)
        assert result["status"] == "completed"
        assert result["provider"] == "kimi"
        assert result["commit_sha"] == HEAD_SHA, "a verdict is about ONE commit: the PR head"
        assert result["contract_hash"], "an empty contract_hash never satisfies the merge door"
        assert result["report_path"] and Path(result["report_path"]).is_file()
        assert result["blocking_findings"] == []
        assert has_complete_evidence(result)
        assert is_pass(result)[0]

    def test_the_obligation_points_at_that_result(self, env, lane, runner_edges):
        path, outcome = _declared_and_fulfilled(env)

        result_path = env["results_dir"] / f"pr-{PR}-kimi_gate.json"
        assert outcome["result_path"] == str(result_path)
        assert _read(path)["result_path"] == str(result_path)


class TestAGateThatCouldNotRunDoesNotFulfil:
    def test_a_lane_that_fails_leaves_the_obligation_unfulfilled(self, env, lane, runner_edges, monkeypatch):
        def broken_factory(data_dir, timeout_seconds, **kw):
            def dispatch(provider, model, instruction, dispatch_id):
                raise RuntimeError("kimi lane down")
            return dispatch

        monkeypatch.setattr("plan_gate_panel._make_default_dispatcher", broken_factory)

        path, outcome = _declared_and_fulfilled(env)

        assert outcome["action"] != STATUS_FULFILLED, outcome
        assert _read(path)["status"] != STATUS_FULFILLED
        result_path = env["results_dir"] / f"pr-{PR}-kimi_gate.json"
        if result_path.exists():
            assert not has_complete_evidence(_read(result_path))
            assert not is_pass(_read(result_path))[0]

    def test_a_not_executable_record_does_not_fulfil_the_obligation(self, env, lane, runner_edges, monkeypatch):
        """OI-1400: the run leaves a ``not_executable`` result (the historic
        ``gate_not_subprocess_routable`` shape). The obligation mirrors it."""
        import review_gate_manager

        real = review_gate_manager.ReviewGateManager.request_and_execute

        def run_then_leave_not_executable(self, **kw):
            out = real(self, **kw)
            result_file = self._result_path("kimi_gate", kw["pr_number"])
            record = json.loads(result_file.read_text(encoding="utf-8"))
            record.update(status=STATUS_NOT_EXECUTABLE, reason="gate_not_subprocess_routable")
            result_file.write_text(json.dumps(record), encoding="utf-8")
            return out

        monkeypatch.setattr(
            review_gate_manager.ReviewGateManager, "request_and_execute", run_then_leave_not_executable,
        )

        path, outcome = _declared_and_fulfilled(env)

        assert outcome["action"] == STATUS_NOT_EXECUTABLE, outcome
        stored = _read(path)
        assert stored["status"] == STATUS_NOT_EXECUTABLE
        assert stored["status"] != STATUS_FULFILLED
        assert stored.get("fulfilled_by") in (None, "")


@pytest.mark.parametrize("field", ["contract_hash", "report_path"])
@pytest.mark.parametrize("value", ["missing", None, "", "   ", 0, ["x"]])
def test_evidence_with_a_broken_field_stays_incomplete(env, lane, runner_edges, field, value):
    """Fail-closed: a result whose evidence field is missing, None, blank or malformed is not complete."""
    _declared_and_fulfilled(env)
    record = _read(env["results_dir"] / f"pr-{PR}-kimi_gate.json")
    assert has_complete_evidence(record), "precondition: the booked result is complete"

    if value == "missing":
        del record[field]
    else:
        record[field] = value

    assert not has_complete_evidence(record)


@pytest.mark.parametrize("status", [None, "", "unknown_status", "unavailable", "in_flight", 7])
def test_a_result_without_a_decided_status_is_not_complete(env, lane, runner_edges, status):
    _declared_and_fulfilled(env)
    record = _read(env["results_dir"] / f"pr-{PR}-kimi_gate.json")
    record["status"] = status

    assert not has_complete_evidence(record)
    assert not is_pass(record)[0]
