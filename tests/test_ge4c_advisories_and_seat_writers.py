"""GE-4c (dlv-a4644be8f2ee): the three kimi advisories of #2252, and the seat request writers' slot lock.

Advisory (a): a refused ci_gate write left its request on ``executing``, so
every retry re-ran the gate into the same refusal. The attempt now ends
``failed``/``write_refused`` and the next call runs nothing.
Advisory (b): ``_finish_request`` changed the caller's payload before the write
and only logged an ``OSError``. It copies, and a failed write reaches the
caller as ``RequestWriteFailed``.
Advisory (c): ``REQUEST_STATUS_OF`` maps ``unavailable``, which
``execute_gate``'s own short-circuit list did not know. One shared constant
now decides both sides.

The last group pins the slot lock under every request writer GE-4c-2 wired:
the three write sites of ``_request_wiring_gate`` and the kimi, glm and
harness-seat request writers. Nothing here mocks the writer under test.

GE-7 (dlv-20e9ed688893) changed what the three wiring_gate groups write, on
purpose: the gate is decided at request time, so it now books its outcome
through ``commit_gate_outcome``. The request record carries the request's own
lifecycle word where it used to carry the gate's verdict word, and each group
also asserts the result record. The returned payload keeps the verdict word.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import types
from pathlib import Path
from unittest import mock

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import gate_obligations
import gate_recorder
import review_stack_support
import wiring_gate
from atomic_io import slot_lock
from gate_executor import GateExecutorMixin
from gate_request_handler import GateRequestHandlerMixin

PR, HEAD_A, BRANCH, MODE, RISK = 4408, "a" * 40, "feat/ge-4c", "standard", "high"


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = review_stack_support.build_project_store(tmp_path, monkeypatch)
    # Every request writer imports ``_utc_now`` from the manager; the real one drags in everything.
    monkeypatch.setitem(sys.modules, "review_gate_manager",
                        types.SimpleNamespace(_utc_now=lambda: "2026-10-27T10:00:00Z"))
    return store


class _Writers(GateExecutorMixin):
    def __init__(self, env):
        self.requests_dir, self.results_dir = env["requests_dir"], env["results_dir"]
        self.state_dir, self.reports_dir = env["state_dir"], env["reports_dir"]

    def _request_path(self, gate, pr_number):
        return self.requests_dir / f"pr-{pr_number}-{gate}.json"

    def _result_path(self, gate, pr_number):
        return self.results_dir / f"pr-{pr_number}-{gate}.json"

    def _load_request_payload(self, gate, pr_number):
        path = self._request_path(gate, pr_number)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def _load_contract_request_payload(self, gate, pr_id):
        return {}


class _Requests(GateRequestHandlerMixin):
    def __init__(self, env):
        self.requests_dir, self.results_dir = env["requests_dir"], env["results_dir"]
        self.state_dir = env["state_dir"]

    def _request_path(self, gate, pr_number):
        return self.requests_dir / f"pr-{pr_number}-{gate}.json"

    def _build_report_path(self, *, gate, requested_at, pr_number=None, pr_id=""):
        return f"/tmp/ge4c-{gate}-{pr_number or pr_id}.md"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _request_slot(env, gate: str = "ci_gate") -> Path:
    return env["requests_dir"] / f"pr-{PR}-{gate}.json"


def _result_slot(env, gate: str = "ci_gate") -> Path:
    return env["results_dir"] / f"pr-{PR}-{gate}.json"


def _persist(env, gate: str = "ci_gate", status: str = "requested") -> dict:
    request = {"gate": gate, "status": status, "pr_number": PR, "commit_sha": HEAD_A,
               "branch": BRANCH, "contract_hash": "ch"}
    gate_recorder.persist_request(env["requests_dir"], gate, request, pr_number=PR, pr_id="")
    return request


def _gh_result(bucket: str = "pass"):
    return mock.Mock(returncode=0, stdout=json.dumps(
        [{"name": "ci/tests", "bucket": bucket, "state": "SUCCESS"}]), stderr="")


def _execute(env, gate: str = "ci_gate"):
    """Run ``execute_gate`` with gh faked at the process boundary; returns (payload, subprocess mock)."""
    writers = _Writers(env)
    with mock.patch("gate_executor.shutil.which", return_value="/usr/bin/gh"), \
            mock.patch("gate_executor.subprocess") as sub:
        sub.TimeoutExpired, sub.run.side_effect = subprocess.TimeoutExpired, lambda *a, **k: _gh_result()
        return writers.execute_gate(gate=gate, pr_number=PR), sub


# ---------------------------------------------------------------------------
# (a) The refused write is bounded: the attempt ends, the retry runs nothing
# ---------------------------------------------------------------------------


def test_a_refused_ci_gate_write_ends_the_attempt_and_the_retry_runs_nothing(env):
    _persist(env)
    _result_slot(env).write_text('{"status": "pa', encoding="utf-8")

    first, sub = _execute(env)

    assert first["write_refused"] is True
    assert sub.run.call_count >= 1, "the first call really ran the gate"
    ended = _json(_request_slot(env))
    assert (ended["status"], ended["failure_reason"]) == ("failed", "write_refused")

    second, sub = _execute(env)

    assert sub.run.call_count == 0, "the retry re-runs nothing: the attempt is over"
    assert (second["status"], second["failure_reason"]) == ("failed", "write_refused")
    assert _result_slot(env).read_text(encoding="utf-8") == '{"status": "pa'


def test_a_refused_write_leaves_a_terminal_request_so_a_real_retry_is_a_new_request(env):
    _persist(env)
    _result_slot(env).write_text("not json at all", encoding="utf-8")

    _execute(env)

    ended = _json(_request_slot(env))
    assert ended["status"] in {"failed", "completed", "not_executable", "unavailable"}
    assert "completed_at" not in ended, "a refused run was never completed"


# ---------------------------------------------------------------------------
# (b) The payload is copied, and a failed request write reaches the caller
# ---------------------------------------------------------------------------


def _outcome(status: str = "pass"):
    from gate_outcome import GateOutcome

    record = {"gate": "ci_gate", "pr_id": "", "pr_number": PR, "status": status, "commit_sha": HEAD_A,
              "contract_hash": "ch", "report_path": "/tmp/ci.md", "blocking_findings": []}
    return GateOutcome("ci_gate", "pass" if status == "pass" else "unavailable", "", PR, record)


def test_a_failed_request_write_raises_and_leaves_the_callers_payload_alone(env, monkeypatch):
    from gate_outcome import GateRequest, RequestWriteFailed, commit_gate_outcome

    request = _persist(env, status="executing")
    real = gate_recorder.persist_request

    def refuse_the_end(requests_dir, gate, payload, *, pr_number, pr_id):
        if payload.get("status") == "completed":
            raise OSError("the request slot is on a read-only mount")
        return real(requests_dir, gate, payload, pr_number=pr_number, pr_id=pr_id)

    monkeypatch.setattr(gate_recorder, "persist_request", refuse_the_end)

    with pytest.raises(RequestWriteFailed) as raised:
        commit_gate_outcome(_outcome(), results_dir=env["results_dir"],
                            request=GateRequest(env["requests_dir"], request))

    assert raised.value.status == "completed"
    assert request["status"] == "executing", "the caller's payload is not mutated before the write lands"
    assert _json(_request_slot(env))["status"] == "executing", "memory and disk still agree"
    assert _json(_result_slot(env))["status"] == "pass", "the record landed first and stands"


def test_the_request_end_is_written_to_a_copy(env, monkeypatch):
    from gate_outcome import GateRequest, commit_gate_outcome

    request = _persist(env, status="executing")
    written = []
    real = gate_recorder.persist_request

    def record_the_write(requests_dir, gate, payload, *, pr_number, pr_id):
        written.append(dict(payload))
        assert request["status"] == "executing", "the caller's dict is still untouched while writing"
        return real(requests_dir, gate, payload, pr_number=pr_number, pr_id=pr_id)

    monkeypatch.setattr(gate_recorder, "persist_request", record_the_write)

    commit_gate_outcome(_outcome(), results_dir=env["results_dir"],
                        request=GateRequest(env["requests_dir"], request, {"completed_at": "2026-10-27T10:00:00Z"}))

    assert written[-1]["status"] == "completed"
    assert request["status"] == "completed" and request["completed_at"] == "2026-10-27T10:00:00Z"


# ---------------------------------------------------------------------------
# (c) One shared list: an ``unavailable`` request is over
# ---------------------------------------------------------------------------


def test_every_request_status_the_mapping_produces_is_terminal():
    from gate_status import REQUEST_TERMINAL_STATUSES
    from gate_outcome import REQUEST_STATUS_OF

    assert set(REQUEST_STATUS_OF.values()) <= REQUEST_TERMINAL_STATUSES
    assert "unavailable" in REQUEST_TERMINAL_STATUSES


def test_a_request_ended_unavailable_by_the_commit_is_never_re_run(env, monkeypatch):
    from gate_outcome import GateRequest, commit_gate_outcome

    request = _persist(env, status="executing")
    commit_gate_outcome(_outcome("unavailable"), results_dir=env["results_dir"],
                        request=GateRequest(env["requests_dir"], request))
    assert _json(_request_slot(env))["status"] == "unavailable"

    writers = _Writers(env)
    monkeypatch.setattr(_Writers, "_execute_ci_gate",
                        lambda self, **kwargs: pytest.fail("execute_gate re-ran a finished attempt"))

    assert writers.execute_gate(gate="ci_gate", pr_number=PR)["status"] == "unavailable"


def test_a_request_ended_completed_is_not_re_run_either(env, monkeypatch):
    _persist(env, status="completed")
    monkeypatch.setattr(_Writers, "_execute_ci_gate",
                        lambda self, **kwargs: pytest.fail("execute_gate re-ran a finished attempt"))

    assert _Writers(env).execute_gate(gate="ci_gate", pr_number=PR)["status"] == "completed"


# ---------------------------------------------------------------------------
# The request writers: every write goes through the request slot's lock
# ---------------------------------------------------------------------------


def _write_under_lock(env, gate: str, write):
    """Write through ``write`` in a thread while the request slot is held; nothing lands until released."""
    path, started, outcome = _request_slot(env, gate), threading.Event(), {}

    def run():
        started.set()
        try:
            outcome["payload"] = write()
        except BaseException as exc:  # vnx-silent-except: re-raised below, after the thread joined
            outcome["error"] = exc

    with slot_lock(path):
        thread = threading.Thread(target=run)
        thread.start()
        assert started.wait(5)
        thread.join(0.3)
        assert thread.is_alive() and not path.exists(), "the request write waits for the slot lock"
    thread.join(10)
    assert not thread.is_alive() and "error" not in outcome, outcome.get("error")
    return outcome["payload"]


_WIRING_RESULT = wiring_gate.WiringGateResult(
    status="pass", total_checked=12, summary="12 symbols checked, all wired",
)


def test_the_wiring_gate_result_write_takes_the_slot_lock(env, monkeypatch):
    monkeypatch.setattr(wiring_gate, "check_pr_wiring", lambda pr_number: _WIRING_RESULT)

    payload = _write_under_lock(env, "wiring_gate", lambda: _Requests(env)._request_wiring_gate(PR, BRANCH))

    assert (payload["status"], payload["total_checked"]) == ("pass", 12)
    on_disk = _json(_request_slot(env, "wiring_gate"))
    # The request's own lifecycle word since GE-7; the record keeps "pass".
    assert (on_disk["status"], on_disk["gate"]) == ("completed", "wiring_gate")
    assert _json(_result_slot(env, "wiring_gate"))["status"] == "pass"


def test_the_wiring_gate_error_write_takes_the_slot_lock(env, monkeypatch):
    def explode(pr_number):
        raise wiring_gate.WiringGateError("ast-grep exited 3")

    monkeypatch.setattr(wiring_gate, "check_pr_wiring", explode)

    payload = _write_under_lock(env, "wiring_gate", lambda: _Requests(env)._request_wiring_gate(PR, BRANCH))

    assert (payload["status"], payload["blocking_count"]) == ("fail", 1)
    assert "ast-grep exited 3" in payload["summary"]
    assert _json(_request_slot(env, "wiring_gate"))["status"] == "completed"
    assert _json(_result_slot(env, "wiring_gate"))["status"] == "fail"


def test_the_wiring_gate_unavailable_write_takes_the_slot_lock(env, monkeypatch):
    monkeypatch.setattr(_Requests, "_wiring_gate_available", lambda self: False)

    payload = _write_under_lock(env, "wiring_gate", lambda: _Requests(env)._request_wiring_gate(PR, BRANCH))

    assert (payload["status"], payload["reason"]) == ("not_executable", "provider_not_installed")
    assert _json(_request_slot(env, "wiring_gate"))["status"] == "not_executable"
    assert _json(_result_slot(env, "wiring_gate"))["status"] == "not_executable"


@pytest.mark.parametrize("writer,gate", [("_request_kimi", "kimi_gate"), ("_request_glm", "glm_gate")])
def test_the_kimi_and_glm_request_writes_take_the_slot_lock(env, monkeypatch, writer, gate):
    import gate_request_handler

    monkeypatch.setattr(gate_request_handler, "gate_is_available", lambda *a, **k: True)

    def write():
        return getattr(_Requests(env), writer)(PR, BRANCH, RISK, ["scripts/a.py"], MODE)

    payload = _write_under_lock(env, gate, write)

    assert (payload["status"], payload["gate"]) == ("requested", gate)
    on_disk = _json(_request_slot(env, gate))
    assert (on_disk["status"], on_disk["commit_sha"]) == ("requested", review_stack_support.HEAD_SHA)


def test_the_harness_seat_request_write_takes_the_slot_lock(env):
    def write():
        return _Requests(env)._request_harness_seat(
            "deepseek_gate", "deepseek", True, pr_number=PR, branch=BRANCH, risk_class=RISK,
            changed_files=["scripts/a.py"], mode=MODE, dispatch_id="",
        )

    payload = _write_under_lock(env, "deepseek_gate", write)

    assert (payload["status"], payload["provider"]) == ("requested", "deepseek")
    assert _json(_request_slot(env, "deepseek_gate"))["status"] == "requested"
