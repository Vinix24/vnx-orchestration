"""Golf schoon R31: characterisation of ``run_envelope_headless_plan``.

Pins the observable behaviour before the extract-method cut: return value,
the error texts and log lines, the kwargs handed to the worktree, spawn,
phantom-guard, push/PR and govern seams, and the order of those side effects.
The seams are module-level names, patched with recorders; the state dir is a
real temporary dir and no external process (``claude``, ``git``) runs.
"""
from __future__ import annotations

import hashlib
import logging
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import dispatch_envelope as de  # noqa: E402
from dispatch_internal import issue_permit  # noqa: E402
from dispatch_plan import ExecutionPlan  # noqa: E402
from dispatch_spec import Isolation, Provider  # noqa: E402

DID = "r31-headless"


def _plan(tmp_path: Path, *, sha=None, lane="claude_headless") -> ExecutionPlan:
    inst = tmp_path / "instruction.md"
    inst.write_text("# Do\nthe work.", encoding="utf-8")
    digest = hashlib.sha256(inst.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    return ExecutionPlan(
        dispatch_id=DID, project_id="vnx-dev", provider=Provider.CLAUDE, model="opus",
        lane=lane, adapter="claude_subprocess", target_id="ephemeral",
        billing="subscription", serialization_class="claude-tmux",
        isolation=Isolation.WORKTREE, require_worktree=True, seed_materialize=False,
        instruction_delivery="file_ref", report_contract="required", warmup="n/a",
        deadline_seconds=1200, base_ref="origin/feature", dispatch_paths=(),
        instruction_file=inst, route_reason="D1", parent_dispatch="parent-1",
        instruction_sha256=digest if sha is None else sha,
    )


class _Trace:
    """Recorders for every seam the lane touches, in call order."""

    def __init__(self, tmp_path: Path):
        self.calls: list = []
        self.tmp = tmp_path
        self.worker = de._AdapterResult(returncode=0, completion_text="done",
                                        status="success", model="opus", session_id="s1")
        self.after_push = de._AdapterResult(returncode=0, completion_text="pushed", status="success")
        self.run_exc = self.push_exc = self.wt_exc = self.diff_exc = None

    def rec(self, name, result=None, exc_attr=None):
        def _fn(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            exc = getattr(self, exc_attr) if exc_attr else None
            if exc is not None:
                raise exc
            return result(*args, **kwargs) if callable(result) else result
        return _fn

    def names(self):
        return [c[0] for c in self.calls]

    def call(self, name):
        return next(c for c in self.calls if c[0] == name)

    def patches(self):
        govern = self.rec("govern", (self.tmp / "r.md", self.tmp / "rc.ndjson"))
        return [
            patch.object(de, "_prepare", self.rec("prepare", lambda s: "ENRICHED " + s.instruction)),
            patch.object(de, "_stamp_headless_start", self.rec("stamp")),
            patch.object(de, "_record_integrity", self.rec("integrity", {"ok": True})),
            patch("dispatch_worktree_isolation.resolve_consumer_project_root",
                  self.rec("root", self.tmp / "consumer")),
            patch("dispatch_worktree_isolation.create_dispatch_worktree",
                  self.rec("create_wt", self.tmp / "wt", "wt_exc")),
            patch("dispatch_worktree_isolation.remove_dispatch_worktree", self.rec("remove_wt")),
            patch("pr_enforcement._get_remote_head", self.rec("remote_head", "abc123")),
            patch.object(de.ClaudeSubprocessAdapter, "run",
                         lambda _self, *a, **k: self.rec("run", self.worker, "run_exc")(*a, **k)),
            patch.object(de, "_resolve_phantom_diff", self.rec("phantom", "DIFF", "diff_exc")),
            patch.object(de, "_enforce_push_pr", self.rec("push_pr", self.after_push, "push_exc")),
            patch.object(de, "_govern", govern),
            patch.object(de, "finalize_exit", self.rec("finalize_exit")),
            patch.object(de, "finalize_lane_exit", self.rec("finalize_lane_exit")),
            patch.object(de, "_archive_late_dispatch_events", self.rec("archive")),
        ]


def _run(tmp_path, trace, plan=None, **kwargs):
    plan = plan or _plan(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    with __import__("contextlib").ExitStack() as stack:
        for p in trace.patches():
            stack.enter_context(p)
        if not plan.instruction_sha256:  # require_permit refuses it first; reach the lane's own check
            stack.enter_context(patch("dispatch_internal.require_permit", lambda *_: None))
        permit = issue_permit(plan) if plan.instruction_sha256 else None
        return de.run_envelope_headless_plan(plan, permit, state_dir=state_dir,
                                             data_dir=tmp_path / "data", **kwargs)


def test_success_order_kwargs_and_result(tmp_path):
    trace = _Trace(tmp_path)
    res = _run(tmp_path, trace, role="backend-developer", base_sha=" deadbeef ")
    assert trace.names() == ["prepare", "stamp", "integrity", "root", "create_wt", "remote_head",
                             "run", "phantom", "push_pr", "govern", "finalize_lane_exit",
                             "remove_wt", "archive"]
    assert trace.call("create_wt")[1:] == ((DID,), {"project_root": tmp_path / "consumer",
                                                    "base_ref": "deadbeef"})
    assert trace.call("remote_head")[2] == {"branch": f"dispatch/{DID}", "repo_root": tmp_path / "consumer"}
    spec = trace.call("run")[1][0]
    assert (spec.instruction, spec.provider, spec.role, spec.parent_dispatch, spec.deadline_seconds) == (
        "ENRICHED # Do\nthe work.", "claude", "backend-developer", "parent-1", 1200)
    assert trace.call("run")[2]["cwd"] == tmp_path / "wt"
    assert trace.call("phantom")[1:] == ((DID,), {"base_ref": "origin/feature", "wt_path": tmp_path / "wt",
                                                  "repo": tmp_path / "consumer"})
    push = trace.call("push_pr")[2]
    assert (push["branch"], push["base_ref"], push["target_remote_head"], push["skip_pr"],
            push["provider"], push["terminal"], push["result"]) == (
        f"dispatch/{DID}", "origin/feature", "abc123", False, "claude", "ephemeral", trace.worker)
    assert push["receipts_file"] == de._receipts_file_for(tmp_path / "state")
    gargs, gkw = trace.call("govern")[1:]
    assert gargs[0] is spec and gargs[1] is trace.after_push
    assert gkw == {"phantom_diff": "DIFF", "integrity": {"ok": True}, "base_ref": "origin/feature",
                   "lifecycle_v3": False}
    assert (res.status, res.returncode, res.completion_text, res.error) == ("success", 0, "pushed", None)
    assert trace.call("finalize_lane_exit")[2]["status"] == "success"
    assert trace.call("remove_wt")[1:] == ((DID,), {"project_root": tmp_path / "consumer",
                                                    "terminal_id": "ephemeral"})


def test_wrong_lane_raises_value_error(tmp_path):
    with pytest.raises(ValueError, match=r"handles lane='claude_headless' only; got lane='provider'"):
        _run(tmp_path, _Trace(tmp_path), plan=_plan(tmp_path, lane="provider"))


@pytest.mark.parametrize("sha, needle", [
    ("", "plan.instruction_sha256 is not a valid 64-hex digest (got ''); refusing to deliver (fail-closed)"),
    ("0" * 64, "instruction file mutated after permit: sha256 mismatch (expected 000000000000…, got "),
])
def test_hash_refusals_before_any_worktree(tmp_path, sha, needle):
    trace = _Trace(tmp_path)
    res = _run(tmp_path, trace, plan=_plan(tmp_path, sha=sha))
    assert trace.names() == ["finalize_exit"]
    assert trace.call("finalize_exit")[2]["reason"] == de.REASON_PRE_SPAWN_ERROR
    assert (res.status, res.returncode, res.report_path, res.completion_text) == ("failure", 1, None, "")
    assert needle in res.error


def test_worktree_failure_governs_a_refusal(tmp_path, caplog):
    trace = _Trace(tmp_path)
    trace.wt_exc = RuntimeError("no git")
    with caplog.at_level(logging.ERROR, logger=de.logger.name):
        res = _run(tmp_path, trace)
    expected = (f"isolation required but worktree creation failed for {DID}: no git"
                " — aborting; no shared-checkout fallback")
    assert trace.names() == ["prepare", "stamp", "integrity", "root", "create_wt", "govern", "finalize_exit"]
    assert f"run_envelope_headless_plan: {expected}" in caplog.text
    failed = trace.call("govern")[1][1]
    assert (failed.status, failed.error, failed.spawn_refused) == ("failure", expected,
                                                                   "worktree_creation_failed")
    assert trace.call("govern")[2] == {"integrity": {"ok": True}, "lifecycle_v3": False}
    assert (res.status, res.error, res.report_path) == ("failure", expected, tmp_path / "r.md")


def test_closeout_raise_keeps_worker_output_and_diff(tmp_path, caplog):
    trace = _Trace(tmp_path)
    trace.push_exc = OSError("push broke")
    with caplog.at_level(logging.ERROR, logger=de.logger.name):
        res = _run(tmp_path, trace)
    assert (f"envelope: headless work block raised dispatch={DID} — building failure "
            "result so _govern still emits a report+receipt (OI-1743)") in caplog.text
    governed = trace.call("govern")[1][1]
    assert (governed.status, governed.returncode, governed.completion_text, governed.session_id,
            governed.model) == ("failure", 0, "done", "s1", "opus")
    assert governed.error == (f"headless dispatch close-out raised: {OSError('push broke')!r} — "
                              "worker status was 'success' before the failure (OI-1743)")
    assert trace.call("govern")[2]["phantom_diff"] == "DIFF"
    assert res.status == "failure"


def test_spawn_raise_and_failed_worker_and_diff_error(tmp_path):
    trace = _Trace(tmp_path)
    trace.run_exc = RuntimeError("spawn")
    _run(tmp_path, trace)
    governed = trace.call("govern")[1][1]
    assert (governed.returncode, governed.completion_text, governed.token_usage) == (1, "", {})
    assert governed.error == ("headless dispatch raised before any worker result: "
                              "RuntimeError('spawn') (OI-1743)")
    assert trace.call("govern")[2]["phantom_diff"] is None

    trace = _Trace(tmp_path)
    trace.worker = de._AdapterResult(returncode=2, completion_text="", status="failure", error="bad")
    trace.diff_exc = RuntimeError("diff")
    res = _run(tmp_path, trace)
    assert "push_pr" not in trace.names()
    assert trace.call("govern")[2]["phantom_diff"] is None
    assert (res.status, res.error) == ("failure", "bad")


def test_base_exception_still_closes_the_exit(tmp_path):
    trace = _Trace(tmp_path)
    trace.run_exc = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        _run(tmp_path, trace)
    assert trace.names()[-4:] == ["run", "finalize_lane_exit", "remove_wt", "archive"]
    assert trace.call("finalize_lane_exit")[2]["status"] == "failure"
