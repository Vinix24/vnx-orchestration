"""OI-2066: a harness-lane timeout is booked as a timeout, through the real runner path.

Measured 2026-10-15 on PR #2058 (head ae1e193e, +2288/-207): kimi_gate and
deepseek_gate both stopped at 900.0 s and both records read
``status=unavailable, reason=lane_exhausted`` — a spent quota.

What happened: the governed dispatcher raised ``subprocess.TimeoutExpired``.
``gate_runner._run_harness_lane_path`` booked it as
``harness_lane_dispatch_error`` with ``reason_detail=str(exc)``. That string is
the whole argv, including the ``--instruction`` value that carries the diff
(163,891 chars; the result file was 513 KB). ``harness_lane_dispatch_error``
is cause-agnostic, so ``gate_recorder.is_quota_refusal_text`` scanned that
text and found ``insufficient balance`` and ``reached your usage limit for
this billing cycle`` — two test fixtures inside the diff under review.

These tests drive ``GateRunner.run`` and the standalone gates end to end with
the dispatcher (or the subprocess under it) patched; nothing starts a real
kimi, glm, deepseek, codex or claude. Module-level imports use only names that
exist before the fix, so on the old code the regression tests fail on their
assertions, not on an ImportError.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import config_registry
import config_runtime
import gate_runner
import glm_gate
import kimi_gate
import plan_gate_panel
import vnx_paths
from gate_request_handler import GateRequestHandlerMixin
from gate_runner import GateRunner

# Hardcoded, not imported: these names must resolve on the old code too.
_CONFIG_KEYS = (
    "VNX_KIMI_GATE_TIMEOUT_SECONDS", "VNX_GLM_GATE_TIMEOUT_SECONDS",
    "VNX_DEEPSEEK_GATE_TIMEOUT_SECONDS", "VNX_KIMI_GATE_MAX_DIFF_CHARS",
    "VNX_GLM_GATE_MAX_DIFF_CHARS", "VNX_DEEPSEEK_GATE_MAX_DIFF_CHARS",
)

QUOTA_PHRASE_A = "insufficient balance"
QUOTA_PHRASE_B = "You've reached your usage limit for this billing cycle"
FILLER_MARKER = "oi2066-filler-line"
PR2058_DIFF_CHARS = 163_891


def _pr2058_shaped_diff(total_chars: int = PR2058_DIFF_CHARS) -> str:
    """A diff of exactly *total_chars* (post-strip, the size diff_coverage
    records) whose tail quotes the two provider phrases as TEST FIXTURES, at
    roughly the offsets measured on 15-10 (114,487 and 157,129)."""
    head = (
        "diff --git a/tests/test_quota.py b/tests/test_quota.py\n"
        "--- a/tests/test_quota.py\n+++ b/tests/test_quota.py\n@@ -1,1 +1,4000 @@\n"
    )
    fixture_a = f"+    REFUSAL_402 = 'API Error: 402 {QUOTA_PHRASE_A}'\n"
    fixture_b = f'+    REFUSAL_CYCLE = "{QUOTA_PHRASE_B}"\n'
    line = f"+    {FILLER_MARKER} = 1\n"
    body_a = line * ((114_000 - len(head)) // len(line))
    body_b = line * ((157_000 - 114_000) // len(line))
    text = head + body_a + fixture_a + body_b + fixture_b
    pad = total_chars - len(text.strip()) - 2
    assert pad > 0
    text += "+" + "#" * pad + "\n"
    assert len(text.strip()) == total_chars
    return text


@pytest.fixture
def isolated_config(monkeypatch):
    """Hold the timeout and diff-cap resolvers to their registry defaults: no
    env value, no override brake, no real project_config store."""
    for key in _CONFIG_KEYS:
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(f"VNX_OVERRIDE_{config_registry._bare(key)}", raising=False)
    monkeypatch.delenv("VNX_TOTAL_DEADLINE", raising=False)
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    monkeypatch.setattr(vnx_paths, "resolve_paths", lambda: {})
    config_runtime._wired_for.clear()
    config_registry.set_db_resolver(None)
    config_registry.set_default_project_id(None)
    yield
    config_runtime._wired_for.clear()
    config_registry.set_db_resolver(None)
    config_registry.set_default_project_id(None)


@pytest.fixture
def gate_dirs(tmp_path, monkeypatch, isolated_config):
    state = tmp_path / "state"
    (state / "review_gates" / "requests").mkdir(parents=True)
    (state / "review_gates" / "results").mkdir(parents=True)
    reports = tmp_path / "unified_reports"
    reports.mkdir()
    monkeypatch.setenv("VNX_STATE_DIR", str(state))
    # OI-1887: the harness lane fetches origin/<branch> first; the payload's
    # branch exists on no remote, so keep the run hermetic.
    monkeypatch.setattr(gate_runner, "fetch_branch", lambda *a, **kw: None)
    monkeypatch.delenv("VNX_KIMI_GATE_MODEL", raising=False)
    monkeypatch.delenv("VNX_GLM_GATE_MODEL", raising=False)
    return {"state": state, "reports": reports, "results": state / "review_gates" / "results"}


def _payload(gate: str, pr_number: int, reports: Path) -> dict:
    # No "prompt": the production shape, so the runner builds the prompt from
    # the diff and records its size (diff_coverage).
    return {
        "gate": gate,
        "status": "requested",
        "branch": "feature/oi2066",
        "pr_number": pr_number,
        "commit_sha": "ae1e193e",
        "requested_at": "2026-10-15T09:00:00Z",
        "report_path": str(reports / f"{gate}-pr{pr_number}.md"),
    }


def _recording_factory(behaviour):
    """A ``_make_default_dispatcher`` double with the real signature. It
    records what the runner handed it; *behaviour* is called with
    ``(call, timeout_seconds)`` and returns the report text or raises."""
    calls = []

    def factory(data_dir, timeout_seconds, *, role="plan-reviewer", base_ref=None,
                total_deadline_seconds=None):
        def dispatch(provider, model, instruction, dispatch_id):
            call = {
                "timeout_seconds": timeout_seconds,
                "total_deadline_seconds": total_deadline_seconds,
                "provider": provider,
                "instruction": instruction,
                "dispatch_id": dispatch_id,
            }
            calls.append(call)
            return behaviour(call, timeout_seconds)
        return dispatch

    return factory, calls


def _provider_dispatch_argv(call: dict) -> list:
    return [
        sys.executable, str(plan_gate_panel.PROVIDER_DISPATCH),
        "--provider", call["provider"], "--terminal-id", "plan-gate",
        "--dispatch-id", call["dispatch_id"], "--role", "review-gate",
        "--instruction", call["instruction"], "--no-auto-commit",
    ]


def _run(gate_dirs, monkeypatch, gate, behaviour, *, diff, pr_number=2058):
    factory, calls = _recording_factory(behaviour)
    monkeypatch.setattr("plan_gate_panel._make_default_dispatcher", factory)
    monkeypatch.setattr(GateRunner, "_fetch_gh_pr_diff", staticmethod(lambda _pr: diff))
    runner = GateRunner(state_dir=gate_dirs["state"], reports_dir=gate_dirs["reports"])
    result = runner.run(
        gate=gate, request_payload=_payload(gate, pr_number, gate_dirs["reports"]),
        pr_number=pr_number,
    )
    on_disk = json.loads(
        (gate_dirs["results"] / f"pr-{pr_number}-{gate}.json").read_text(encoding="utf-8")
    )
    return result, on_disk, calls


def _assert_no_diff_text(text: str) -> None:
    assert QUOTA_PHRASE_A not in text
    assert "usage limit" not in text
    assert FILLER_MARKER not in text


# ---------------------------------------------------------------------------
# 1. The 15-10 regression: an outer TimeoutExpired is a timeout, not a quota
# ---------------------------------------------------------------------------


class TestDispatcherTimeoutIsBookedAsTimeout:
    def test_timeout_expired_with_the_diff_in_its_argv_books_timeout(self, gate_dirs, monkeypatch):
        diff = _pr2058_shaped_diff()

        def raise_timeout(call, timeout_seconds):
            raise subprocess.TimeoutExpired(_provider_dispatch_argv(call), timeout_seconds)

        result, on_disk, calls = _run(gate_dirs, monkeypatch, "kimi_gate", raise_timeout, diff=diff)

        # The argv really carried the diff, quota fixtures included.
        assert QUOTA_PHRASE_A in calls[0]["instruction"]
        assert QUOTA_PHRASE_B in calls[0]["instruction"]

        for record in (result, on_disk):
            assert record["reason"] == "timeout"
            assert record["status"] == "unavailable"
            assert "reason_before_classification" not in record
            assert len(record["reason_detail"]) < 2000
            for field in ("reason_detail", "summary", "failure_reason"):
                _assert_no_diff_text(record[field])
                assert "--instruction" not in record[field]
            assert record["timeout_seconds"] == 1500

        assert result["reason_detail"] == (
            "harness lane kimi_gate exceeded 1500 s (diff 163,891 chars): TimeoutExpired: "
            "provider_dispatch.py timed out after 1560 s, the outer timeout (60 s above "
            "the lane deadline)"
        )
        # 15-10 wrote a 513 KB result file.
        result_file = gate_dirs["results"] / "pr-2058-kimi_gate.json"
        assert result_file.stat().st_size < 10_000

    def test_the_dispatcher_got_the_resolved_deadline_and_the_margin(self, gate_dirs, monkeypatch):
        def raise_timeout(call, timeout_seconds):
            raise subprocess.TimeoutExpired(_provider_dispatch_argv(call), timeout_seconds)

        _result, _on_disk, calls = _run(
            gate_dirs, monkeypatch, "glm_gate", raise_timeout, diff=_pr2058_shaped_diff(),
        )
        assert calls[0]["total_deadline_seconds"] == 1500
        assert calls[0]["timeout_seconds"] == 1560

    def test_a_timeout_record_does_not_hand_the_seat_to_the_next_gate(self, gate_dirs, monkeypatch):
        """Takeover stays as it is: a ``timeout`` record reads as
        ``no_response``, so the chain does not take over from it."""
        def raise_timeout(call, timeout_seconds):
            raise subprocess.TimeoutExpired(_provider_dispatch_argv(call), timeout_seconds)

        _result, on_disk, _calls = _run(
            gate_dirs, monkeypatch, "kimi_gate", raise_timeout, diff=_pr2058_shaped_diff(),
        )
        assert GateRequestHandlerMixin._classify_review_seat_failure(None, on_disk) == "no_response"

    def test_another_dispatcher_failure_stays_a_dispatch_error_without_argv(
        self, gate_dirs, monkeypatch,
    ):
        def raise_runtime(call, _timeout_seconds):
            raise RuntimeError(
                f"provider_dispatch failed for argv {_provider_dispatch_argv(call)!r} and "
                f"prompt {call['instruction']}"
            )

        result, on_disk, _calls = _run(
            gate_dirs, monkeypatch, "kimi_gate", raise_runtime, diff=_pr2058_shaped_diff(),
        )
        for record in (result, on_disk):
            assert record["reason"] == "harness_lane_dispatch_error"
            assert record["reason_detail"].startswith("RuntimeError: provider_dispatch failed")
            # The prompt is replaced by its length, wherever the message put it.
            assert record["reason_detail"].count("<prompt: ") == 2
            assert len(record["reason_detail"]) <= 2000
            for field in ("reason_detail", "summary", "failure_reason"):
                _assert_no_diff_text(record[field])
            assert record["timeout_seconds"] == 1500


# ---------------------------------------------------------------------------
# 2. The lane's own deadline: the dispatcher returns the lane's report, and
#    the report SAYS the deadline stopped the run (round 2: never inferred
#    from duration_seconds)
# ---------------------------------------------------------------------------


def _lane_report(
    *, exit_code, duration, failure_reason=None, body="(partial output)\n",
    deadline_exceeded=None, deadline_seconds=None,
):
    """A governed lane report. ``deadline_exceeded``/``deadline_seconds`` are
    the pair provider_dispatch stamps when the lane's own deadline killed the
    run; left None they are absent, as on every other report."""
    lines = [
        "---",
        "dispatch_id: glm-gate-pr2058-1792000000",
        "provider: claude",
        f"exit_code: {exit_code}",
        f"duration_seconds: {duration}",
    ]
    if failure_reason:
        lines.append(f"failure_reason: '{failure_reason}'")
    if deadline_exceeded is not None:
        lines.append(f"deadline_exceeded: {deadline_exceeded}")
    if deadline_seconds is not None:
        lines.append(f"deadline_seconds: {deadline_seconds}")
    lines.append("---")
    return "\n".join(lines) + "\n\n# Dispatch glm-gate-pr2058-1792000000\n\n## Response\n\n" + body


_SMALL_DIFF = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"

# The quota refusal DeepSeek answers with, as the lane records it.
_DEEPSEEK_402 = f"[quota_or_auth] provider=deepseek msg=API Error: 402 {QUOTA_PHRASE_A.title()}"


class TestLaneDeadlineIsBookedAsTimeout:
    def test_a_report_stopped_by_the_lane_deadline_books_timeout(self, gate_dirs, monkeypatch):
        """kimi's shape: the drainer killed the run at its total deadline and
        said so (deadline_exceeded + deadline_seconds); its 'total deadline Ns
        exceeded' also became the report's failure_reason. Before OI-2066 this
        booked harness_lane_no_model_response."""
        report = _lane_report(
            exit_code=1, duration=900.4, failure_reason="total deadline 900s exceeded",
            deadline_exceeded="true", deadline_seconds=900.0,
        )
        result, on_disk, _calls = _run(
            gate_dirs, monkeypatch, "kimi_gate", lambda *_: report, diff=_SMALL_DIFF,
        )
        for record in (result, on_disk):
            assert record["reason"] == "timeout"
            assert record["status"] == "unavailable"
            assert record["timeout_seconds"] == 900
        assert result["reason_detail"] == (
            f"harness lane kimi_gate exceeded 900 s (diff {len(_SMALL_DIFF.strip())} chars): "
            "the lane reported its own deadline of 900 s stopped the run; it ran 900 s "
            "(report exit_code=1)"
        )

    def test_a_partial_answer_quoting_quota_text_is_still_a_timeout(self, gate_dirs, monkeypatch):
        """The claude-harness shape: killed at the deadline with partial text,
        a non-zero exit_code, no failure_reason, and the adapter's deadline
        pair. Before OI-2066 this booked harness_lane_exit_nonzero with the
        body's tail in reason_detail — and a body quoting the diff's quota
        fixtures re-booked it as a spent quota."""
        body = f"Reviewing tests/test_quota.py: REFUSAL_402 = 'API Error: 402 {QUOTA_PHRASE_A}'\n"
        report = _lane_report(
            exit_code=-15, duration=1500.2, body=body,
            deadline_exceeded="true", deadline_seconds=1500.0,
        )
        result, _on_disk, _calls = _run(
            gate_dirs, monkeypatch, "deepseek_gate", lambda *_: report,
            diff=_pr2058_shaped_diff(),
        )
        assert result["reason"] == "timeout"
        assert "reason_before_classification" not in result
        _assert_no_diff_text(result["reason_detail"])
        assert result["timeout_seconds"] == 1500

    def test_the_lane_field_decides_even_when_the_run_was_shorter_than_the_runner_deadline(
        self, gate_dirs, monkeypatch,
    ):
        """The lane's own word, not the runner's arithmetic: a lane whose
        deadline was set lower (VNX_KIMI_TIMEOUT) says it fired at 600 s while
        the runner applied 900 s. The old duration rule read that as a quick
        failure."""
        report = _lane_report(
            exit_code=1, duration=601.0, failure_reason="total deadline 600s exceeded",
            deadline_exceeded="true", deadline_seconds=600.0,
        )
        result, _on_disk, _calls = _run(
            gate_dirs, monkeypatch, "kimi_gate", lambda *_: report, diff=_SMALL_DIFF,
        )
        assert result["reason"] == "timeout"
        assert "deadline of 600 s" in result["reason_detail"]
        assert result["timeout_seconds"] == 900


class TestDurationIsNotADeadlineSignal:
    """codex_gate on 595c3e8e: ``failed and lasted >= deadline`` mixed up the
    dispatch duration with the lane timer. DeepSeek's ``duration_seconds``
    includes prompt and worktree setup, its timer starts after delivery: a
    quota failure after 20 s of setup and 890 s of work 'lasted' the 900 s
    deadline, was booked ``timeout``, lost its quota diagnosis and read as
    ``no_response`` in the takeover chain. The quick-failure and long-clean-run
    cases at the end of this class predate round 2 and keep their booking."""

    def test_a_quota_refusal_that_outlasted_the_deadline_stays_a_quota_refusal(
        self, gate_dirs, monkeypatch,
    ):
        report = _lane_report(exit_code=1, duration=910.0, failure_reason=_DEEPSEEK_402)
        result, on_disk, _calls = _run(
            gate_dirs, monkeypatch, "deepseek_gate", lambda *_: report, diff=_SMALL_DIFF,
        )
        for record in (result, on_disk):
            assert record["reason"] == "lane_exhausted"
            assert record["status"] == "unavailable"
            assert record["reason_before_classification"] == "harness_lane_no_model_response"
            assert record["timeout_seconds"] == 900
        assert GateRequestHandlerMixin._classify_review_seat_failure(None, on_disk) == (
            "lane_exhausted"
        )

    def test_a_402_body_that_outlasted_the_deadline_stays_a_quota_refusal(
        self, gate_dirs, monkeypatch,
    ):
        """OI-1753's shape (exit_code 1, no failure_reason, the 402 in the
        body), after a long setup: the provider answered, the deadline never
        fired."""
        report = _lane_report(
            exit_code=1, duration=905.5, body=f"API Error: 402 {QUOTA_PHRASE_A.title()}\n",
        )
        result, _on_disk, _calls = _run(
            gate_dirs, monkeypatch, "deepseek_gate", lambda *_: report, diff=_SMALL_DIFF,
        )
        assert result["reason"] == "lane_exhausted"
        assert result["reason_before_classification"] == "harness_lane_exit_nonzero"

    @pytest.mark.parametrize("value", ["false", "'true'", "1"], ids=["false", "string", "int"])
    def test_only_a_true_flag_is_the_signal(self, gate_dirs, monkeypatch, value):
        """``deadline_exceeded`` must be YAML ``true``: false, a quoted string
        or a number keeps the old booking."""
        report = _lane_report(
            exit_code=1, duration=910.0, failure_reason=_DEEPSEEK_402,
            deadline_exceeded=value, deadline_seconds=900.0,
        )
        result, _on_disk, _calls = _run(
            gate_dirs, monkeypatch, "deepseek_gate", lambda *_: report, diff=_SMALL_DIFF,
        )
        assert result["reason"] == "lane_exhausted"

    def test_a_quick_spawn_failure_keeps_its_own_reason(self, gate_dirs, monkeypatch):
        """OI-1748 is unchanged: a failure the lane does not call a deadline
        kill is not a timeout."""
        report = _lane_report(
            exit_code=1, duration=4.2,
            failure_reason="[quota_or_auth] provider=kimi msg=Error code: 403",
        )
        result, _on_disk, _calls = _run(
            gate_dirs, monkeypatch, "kimi_gate", lambda *_: report, diff=_SMALL_DIFF,
        )
        assert result["reason"] != "timeout"
        assert result["status"] == "unavailable"

    def test_a_quick_nonzero_exit_keeps_its_own_reason(self, gate_dirs, monkeypatch):
        """OI-1753 is unchanged too."""
        report = _lane_report(exit_code=1, duration=31.0, body="API Error: 500\n")
        result, _on_disk, _calls = _run(
            gate_dirs, monkeypatch, "kimi_gate", lambda *_: report, diff=_SMALL_DIFF,
        )
        assert result["reason"] == "harness_lane_exit_nonzero"

    def test_a_clean_report_is_never_a_timeout(self, gate_dirs, monkeypatch):
        body = (
            "No blocking findings.\n\n```json\n"
            '{"verdict": "pass", "findings": [], "residual_risk": null}\n```\n'
        )
        report = _lane_report(exit_code=0, duration=899.9, body=body)
        result, on_disk, _calls = _run(
            gate_dirs, monkeypatch, "kimi_gate", lambda *_: report, diff=_SMALL_DIFF,
        )
        assert result["status"] == "completed", result.get("reason_detail")
        # The success record carries the applied deadline too.
        assert result["timeout_seconds"] == 900
        assert on_disk["timeout_seconds"] == 900


# ---------------------------------------------------------------------------
# 3. The child env: the real _make_default_dispatcher under the runner
# ---------------------------------------------------------------------------


_PASS_REPORT = (
    "---\ndispatch_id: x\nexit_code: 0\nduration_seconds: 412.0\n---\n\n"
    "Reviewed.\n\n```json\n"
    '{"verdict": "pass", "findings": [], "residual_risk": null}\n```\n'
)


class TestChildEnvironment:
    def test_the_child_carries_the_lane_deadline_and_the_runner_env_is_untouched(
        self, gate_dirs, monkeypatch,
    ):
        real_run = subprocess.run
        seen = []

        def fake_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and len(cmd) > 1 and cmd[1] == str(plan_gate_panel.PROVIDER_DISPATCH):
                env = kwargs["env"]
                seen.append({"env": dict(env), "timeout": kwargs.get("timeout")})
                dispatch_id = cmd[cmd.index("--dispatch-id") + 1]
                report = Path(env["VNX_DATA_DIR"]) / "unified_reports" / f"{dispatch_id}.md"
                report.write_text(_PASS_REPORT, encoding="utf-8")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(plan_gate_panel.subprocess, "run", fake_run)
        monkeypatch.setattr(
            GateRunner, "_fetch_gh_pr_diff", staticmethod(lambda _pr: _pr2058_shaped_diff()),
        )
        environ_before = dict(os.environ)

        runner = GateRunner(state_dir=gate_dirs["state"], reports_dir=gate_dirs["reports"])
        result = runner.run(
            gate="kimi_gate", request_payload=_payload("kimi_gate", 2058, gate_dirs["reports"]),
            pr_number=2058,
        )

        assert len(seen) == 1
        assert seen[0]["env"].get("VNX_TOTAL_DEADLINE") == "1500"
        assert seen[0]["timeout"] == 1560
        assert "VNX_TOTAL_DEADLINE" not in os.environ
        assert dict(os.environ) == environ_before
        assert result["status"] == "completed", result.get("reason_detail")
        assert result["timeout_seconds"] == 1500


class TestPanelsAreUnchanged:
    """The plan-gate panel and the deliberation panel call the same factory
    without the new parameter: their child env and timeout stay as before."""

    @pytest.mark.parametrize("role", ["plan-reviewer", "deliberation-panelist"])
    def test_without_the_parameter_the_env_is_the_old_env(self, tmp_path, monkeypatch, role):
        monkeypatch.delenv("VNX_TOTAL_DEADLINE", raising=False)
        (tmp_path / "unified_reports").mkdir()
        seen = []

        def fake_run(cmd, *args, **kwargs):
            seen.append({"env": dict(kwargs["env"]), "timeout": kwargs.get("timeout")})
            dispatch_id = cmd[cmd.index("--dispatch-id") + 1]
            (tmp_path / "unified_reports" / f"{dispatch_id}.md").write_text("ok", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(plan_gate_panel.subprocess, "run", fake_run)
        dispatcher = plan_gate_panel._make_default_dispatcher(str(tmp_path), 900, role=role)
        assert dispatcher("kimi", "kimi-k3", "review this plan", "plan-gate-oi2066-1") == "ok"

        expected = dict(os.environ)
        expected["VNX_DATA_DIR"] = str(plan_gate_panel._resolve_data_dir(str(tmp_path)))
        expected["VNX_DATA_DIR_EXPLICIT"] = "1"
        assert seen[0]["env"] == expected
        assert "VNX_TOTAL_DEADLINE" not in seen[0]["env"]
        assert seen[0]["timeout"] == 900


# ---------------------------------------------------------------------------
# 4. The standalone gates: same resolver, same argv-free failure text
# ---------------------------------------------------------------------------


def _standalone(module, tmp_path, monkeypatch, dispatcher, extra_args=()):
    diff_file = tmp_path / "x.diff"
    diff_file.write_text(_pr2058_shaped_diff(), encoding="utf-8")
    data_dir = tmp_path / "data"
    captured = {}

    def factory(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return dispatcher

    monkeypatch.setattr(module, "_make_default_dispatcher", factory)
    gate = module.__name__
    module.main(["--pr", "0", "--diff-file", str(diff_file), "--data-dir", str(data_dir), *extra_args])
    out = data_dir / "state" / "review_gates" / "results" / f"pr-0-{gate}.json"
    record = json.loads(out.read_text(encoding="utf-8"))
    kwargs = captured["kwargs"]
    timeout = kwargs.get("timeout_seconds", captured["args"][1] if len(captured["args"]) > 1 else None)
    return record, timeout, kwargs.get("total_deadline_seconds")


def _standalone_timeout(call_args):
    provider, model, instruction, dispatch_id = call_args
    raise subprocess.TimeoutExpired(
        [sys.executable, str(plan_gate_panel.PROVIDER_DISPATCH), "--provider", provider,
         "--dispatch-id", dispatch_id, "--instruction", instruction],
        960,
    )


@pytest.mark.parametrize("module", [kimi_gate, glm_gate], ids=["kimi_gate", "glm_gate"])
class TestStandaloneGates:
    def test_the_deadline_is_resolved_for_the_diff(self, module, tmp_path, monkeypatch, isolated_config):
        record, timeout, deadline = _standalone(
            module, tmp_path, monkeypatch, lambda *a: _PASS_REPORT,
        )
        assert (timeout, deadline) == (1560, 1500)
        assert record["status"] == "pass"

    def test_an_explicit_timeout_still_wins(self, module, tmp_path, monkeypatch, isolated_config):
        _record, timeout, deadline = _standalone(
            module, tmp_path, monkeypatch, lambda *a: _PASS_REPORT, extra_args=("--timeout", "1200"),
        )
        assert (timeout, deadline) == (1260, 1200)

    def test_a_timeout_never_puts_the_argv_in_the_record(
        self, module, tmp_path, monkeypatch, isolated_config, capsys,
    ):
        record, _timeout, _deadline = _standalone(
            module, tmp_path, monkeypatch, lambda *a: _standalone_timeout(a),
        )
        assert record["status"] == "unavailable"
        for field in ("residual_risk", "summary", "reason_detail", "failure_reason"):
            if field in record:
                _assert_no_diff_text(str(record[field]))
        err = capsys.readouterr().err
        _assert_no_diff_text(err)
        assert "TimeoutExpired: provider_dispatch.py timed out after 960 s" in err
