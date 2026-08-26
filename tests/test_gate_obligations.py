"""test_gate_obligations.py — OI-876/OI-881: a declared gate must leave evidence.

Root cause under test: the dispatch spec carries ``gate=<name>`` all the way
into the staged bundle, but after ``dispatch_cli.load_spec`` reads the field
NOTHING consumes it — between 2026-07-28 and 2026-07-31 at least nine
dispatches declared ``gate=codex_gate`` and ten PRs merged with zero
request records and zero result records. A dispatch whose declared gate
never ran was indistinguishable from one whose gate ran.

The fix has three parts, each pinned by tests here:

  1. The door registers a gate obligation per accepted dispatch with a
     declared gate (``dispatch_cli._register_gate_obligation`` →
     ``gate_obligations.register_obligation``). RED on origin/main: no code
     there reads ``spec.gate`` after ``load_spec``, so no obligation exists.
  2. ``scripts/gate_obligation_runner.py`` fulfils obligations: request +
     result records via review_gate_manager, or a LOUD not_executable
     outcome (both records, registered) when the gate cannot run.
  3. ``producer_freshness.scan_gate_obligations`` groups obligations PER
     GATE KEY: a key whose oldest pending declaration exceeds cadence is
     stale — a live sibling gate can no longer hide a dead one (OI-881).

Tests run against throwaway dirs under tmp_path — never the live store.
"""

from __future__ import annotations

import json
import sys
import time
import types
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import gate_obligation_runner as runner
import producer_freshness
from dispatch_cli import run_dispatch
from gate_obligations import (
    NO_GATE_KEY,
    REASON_NO_PR_BRANCH_GONE,
    STATUS_FAILED,
    STATUS_FULFILLED,
    STATUS_NOT_EXECUTABLE,
    STATUS_PENDING,
    STATUS_RETIRED,
    STATUS_UNRESOLVABLE,
    TERMINAL_STATUSES,
    check_gate_requirement_mismatch,
    obligation_path,
    pr_number_from_pr_id,
    register_obligation,
    update_obligation,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_state_dir(tmp_path: Path) -> Path:
    state_dir = tmp_path / "vnx-data" / "state"
    (state_dir / "review_gates" / "requests").mkdir(parents=True, exist_ok=True)
    (state_dir / "review_gates" / "results").mkdir(parents=True, exist_ok=True)
    return state_dir


def _make_bundle(
    tmp_path: Path,
    *,
    staging_id: str,
    dispatch_id: str,
    gate: str,
    dispatch_paths: list[str] | None = None,
    role: str = "backend-developer",
):
    """A promoted-style staged bundle (spec + instruction inside the bundle dir)."""
    data_dir = tmp_path / "vnx-data"
    bundle_dir = data_dir / "dispatches" / "pending" / staging_id
    bundle_dir.mkdir(parents=True, exist_ok=True)
    instruction = bundle_dir / "instruction.md"
    instruction.write_text("Do something useful.", encoding="utf-8")
    spec = {
        "schema_version": 1,
        "project_id": "vnx-dev",
        "dispatch_id": dispatch_id,
        "staging_id": staging_id,
        "instruction_file": str(instruction),
        "role": role,
        "target_slot": "T0",
        "gate": gate,
        "dispatch_paths": [{"path": p} for p in (dispatch_paths or [])],
        "provider": "claude",
        "deadline_seconds": 3600,
        "isolation": "worktree",
        # A2 (2026-08-26): these are door tests (gate obligation registration) — they
        # don't exercise lane behavior, and they run in a tmp_path that is NOT a real
        # git repo. Since claude_headless became the default lane, an unpinned claude
        # spec now hits dispatch_envelope.run_envelope_headless_plan's
        # create_dispatch_worktree, which correctly hard-aborts on a non-git cwd (the
        # PR #1416 isolation guarantee — never soften that). Pin to the tmux lane
        # explicitly via the opt-out these tests actually need.
        "force_tmux": True,
        "force_tmux_reason": "door test asserts gate obligation state, not lane behavior; tmp_path is not a real git repo",
    }
    spec_file = bundle_dir / "dispatch-spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    return data_dir, spec_file


def _read_obligation(state_dir: Path, dispatch_id: str) -> dict:
    return json.loads(obligation_path(state_dir, dispatch_id).read_text(encoding="utf-8"))


class _FakeManager:
    """Stands in for ReviewGateManager: writes the request+result records the
    real manager's request_and_execute produces, without running any gate.

    ``result_status``/``result_reason`` let a test control what the gate
    RESULT record reports (e.g. ``status="not_executable", reason=
    "provider_disabled"`` for a parked gate, or ``status="running"`` for an
    in-flight CI check) — OI-1384 fixtures for the runner's temporary-vs-
    permanent-refusal distinction.
    """

    def __init__(
        self, state_dir: Path, *, boom: bool = False,
        result_status: str = "completed", result_reason: str | None = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.boom = boom
        self.result_status = result_status
        self.result_reason = result_reason
        self.calls = []

    def _request_path(self, gate: str, pr_number: int) -> Path:
        return self.state_dir / "review_gates" / "requests" / f"pr-{pr_number}-{gate}.json"

    def _result_path(self, gate: str, pr_number: int) -> Path:
        return self.state_dir / "review_gates" / "results" / f"pr-{pr_number}-{gate}.json"

    def request_and_execute(self, *, pr_number, branch, review_stack, risk_class,
                            changed_files, mode, dispatch_id=""):
        self.calls.append(
            {"pr_number": pr_number, "branch": branch, "review_stack": list(review_stack)}
        )
        if self.boom:
            raise RuntimeError("codex CLI exploded")
        for gate in review_stack:
            self._request_path(gate, pr_number).write_text(
                json.dumps({"gate": gate, "pr_number": pr_number, "status": "completed"}),
                encoding="utf-8",
            )
            result_payload = {"gate": gate, "pr_number": pr_number, "status": self.result_status}
            if self.result_reason is not None:
                result_payload["reason"] = self.result_reason
            self._result_path(gate, pr_number).write_text(
                json.dumps(result_payload),
                encoding="utf-8",
            )
        return {"pr_number": pr_number, "branch": branch, "gates": [], "has_required_failure": False}


def _patch_manager(monkeypatch, manager: "_FakeManager") -> None:
    """Hermetic patch: no git, no gh, no real gate — only the fake manager."""
    monkeypatch.setattr(runner, "_build_manager", lambda state_dir: manager)
    monkeypatch.setattr(runner, "_branch_from_github", lambda pr, owner_repo: None)
    monkeypatch.setattr(runner, "_resolve_github_owner_repo", lambda state_dir: "Vinix24/vnx-orchestration")
    fake_rgm = types.ModuleType("review_gate_manager")
    fake_rgm._compute_changed_files = lambda branch: ["scripts/lib/foo.py"]
    monkeypatch.setitem(sys.modules, "review_gate_manager", fake_rgm)


# ---------------------------------------------------------------------------
# 1. The door registers an obligation for a declared gate (RED on main).
# ---------------------------------------------------------------------------


def test_door_registers_obligation_for_declared_gate(tmp_path, monkeypatch):
    data_dir, spec_file = _make_bundle(
        tmp_path,
        staging_id="20260731-staging-oi876-gate",
        dispatch_id="20260731-oi876-declared-gate",
        gate="codex_gate",
    )
    _make_state_dir(tmp_path)
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    with patch("dispatch_cli._execute_claude", return_value=0):
        rc = run_dispatch(spec_file)
    assert rc == 0

    path = obligation_path(data_dir / "state", "20260731-oi876-declared-gate")
    assert path.exists(), (
        "a dispatch whose spec declares gate=codex_gate must leave a registered "
        "gate obligation — on origin/main nothing reads spec.gate after load_spec"
    )
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["gate"] == "codex_gate"
    assert record["status"] == STATUS_PENDING
    assert record["dispatch_id"] == "20260731-oi876-declared-gate"


def test_door_stamps_gate_requirement_resolution_on_registration(tmp_path, monkeypatch):
    """OI-1462: the door (the eiser) must snapshot its OWN resolution of
    VNX_CI_GATE_REQUIRED into the obligation record at registration time, so
    a fulfiller running later in a different environment can be checked
    against it (gate_obligations.check_gate_requirement_mismatch)."""
    import config_runtime

    data_dir, spec_file = _make_bundle(
        tmp_path,
        staging_id="20260826-staging-oi1462-resolution",
        dispatch_id="20260826-oi1462-resolution",
        gate="ci_gate",
    )
    _make_state_dir(tmp_path)
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    real_get_bool = config_runtime.get_bool
    monkeypatch.setattr(
        config_runtime, "get_bool",
        lambda key: True if key == "VNX_CI_GATE_REQUIRED" else real_get_bool(key),
    )

    with patch("dispatch_cli._execute_claude", return_value=0):
        rc = run_dispatch(spec_file)
    assert rc == 0

    record = _read_obligation(data_dir / "state", "20260826-oi1462-resolution")
    assert record["gate_requirement_resolution"] == {
        "status": "captured", "flags": {"VNX_CI_GATE_REQUIRED": True}, "error": None,
    }


def test_door_stamps_capture_failure_as_a_distinct_state_not_none(tmp_path, monkeypatch, caplog):
    """OI-1462 residu (Codex-gate finding on this exact obligation mechanism):
    when the door's OWN flag-resolution snapshot fails, that must land in the
    record as an explicit ``status="failed"`` -- a THIRD state, never
    collapsed into the same ``None`` a caller-never-attempted record would
    carry. Also: the failure must be logged loudly (with the dispatch_id),
    never swallowed silently."""
    import logging

    import config_runtime

    data_dir, spec_file = _make_bundle(
        tmp_path,
        staging_id="20260826-staging-oi1462-capture-fail",
        dispatch_id="20260826-oi1462-capture-fail",
        gate="ci_gate",
    )
    _make_state_dir(tmp_path)
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    real_get_bool = config_runtime.get_bool

    def _boom(key):
        if key == "VNX_CI_GATE_REQUIRED":
            raise RuntimeError("config store unreachable")
        return real_get_bool(key)

    monkeypatch.setattr(config_runtime, "get_bool", _boom)

    with patch("dispatch_cli._execute_claude", return_value=0), \
         caplog.at_level(logging.WARNING, logger="dispatch_cli"):
        rc = run_dispatch(spec_file)
    assert rc == 0

    record = _read_obligation(data_dir / "state", "20260826-oi1462-capture-fail")
    resolution = record["gate_requirement_resolution"]
    assert resolution["status"] == "failed", (
        f"a broken flag-read must be recorded as failed, not silently as "
        f"None (indistinguishable from 'never attempted'): {resolution}"
    )
    assert resolution["flags"] is None
    assert "config store unreachable" in resolution["error"]
    assert any(
        "20260826-oi1462-capture-fail" in rec.message and "gate_requirement_resolution" in rec.message
        for rec in caplog.records
    ), "the capture failure must be logged loudly, with the dispatch_id, not swallowed silently"


def test_door_without_declared_gate_derives_obligation(tmp_path, monkeypatch):
    """Punt 7 (gate-weight-by-variant): a silent spec now derives a gate.

    Before the router derived the gate, a dispatch with no ``gate`` field left
    no obligation (that was the OLD assertion this test used to make). Now the
    router fills the silent spec from its governance variant. Empty paths and
    no task_class land on the safe 'code' middle -> codex_gate, so the door
    registers the derived gate's obligation. "Silent" no longer means
    "unreviewed"; it means "derived from the change's risk class".
    """
    data_dir, spec_file = _make_bundle(
        tmp_path,
        staging_id="20260731-staging-oi876-nogate",
        dispatch_id="20260731-oi876-no-gate",
        gate="",
    )
    _make_state_dir(tmp_path)
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    with patch("dispatch_cli._execute_claude", return_value=0):
        rc = run_dispatch(spec_file)
    assert rc == 0
    path = obligation_path(data_dir / "state", "20260731-oi876-no-gate")
    assert path.exists(), (
        "a silent spec now derives a gate from its governance variant and must "
        "leave a registered obligation, not run unreviewed"
    )
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["gate"] == "codex_gate"
    assert record["status"] == STATUS_PENDING
    assert record["dispatch_id"] == "20260731-oi876-no-gate"


def test_explicit_gate_wins_over_derived_obligation(tmp_path, monkeypatch):
    """An explicit gate on the spec wins over the router's derived gate.

    A core path (scripts/lib/dispatch_cli.py) would derive codex_gate, but a
    spec that declares wiring_gate keeps wiring_gate. The router fills in, it
    never overrides (worker-provider-free-choice, pin_semantics=default).
    """
    data_dir, spec_file = _make_bundle(
        tmp_path,
        staging_id="20260731-staging-oi876-explicit",
        dispatch_id="20260731-oi876-explicit-wins",
        gate="wiring_gate",
        dispatch_paths=["scripts/lib/dispatch_cli.py"],
    )
    _make_state_dir(tmp_path)
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    with patch("dispatch_cli._execute_claude", return_value=0):
        rc = run_dispatch(spec_file)
    assert rc == 0
    path = obligation_path(data_dir / "state", "20260731-oi876-explicit-wins")
    assert path.exists()
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["gate"] == "wiring_gate", (
        "an explicit gate on the spec must win over the router's derived "
        "codex_gate for a core path"
    )
    assert record["status"] == STATUS_PENDING


def test_register_obligation_never_resets_fulfilled(tmp_path):
    state_dir = _make_state_dir(tmp_path)
    path = register_obligation(
        state_dir, dispatch_id="d-1", gate="codex_gate", project_id="vnx-dev"
    )
    assert path is not None
    update_obligation(path, status=STATUS_FULFILLED, resolved_at="2026-07-31T00:00:00Z")

    again = register_obligation(
        state_dir, dispatch_id="d-1", gate="codex_gate", project_id="vnx-dev"
    )
    assert again == path
    assert _read_obligation(state_dir, "d-1")["status"] == STATUS_FULFILLED, (
        "a retry/fix-forward must never reset a fulfilled obligation to pending"
    )


# ---------------------------------------------------------------------------
# OI-1462: gate_requirement_resolution stamp + cross-process mismatch check.
# ---------------------------------------------------------------------------


def test_register_obligation_stamps_gate_requirement_resolution(tmp_path):
    state_dir = _make_state_dir(tmp_path)
    resolution = {"status": "captured", "flags": {"VNX_CI_GATE_REQUIRED": True}, "error": None}
    path = register_obligation(
        state_dir, dispatch_id="d-resolution", gate="ci_gate", project_id="vnx-dev",
        gate_requirement_resolution=resolution,
    )
    record = _read_obligation(state_dir, "d-resolution")
    assert record["gate_requirement_resolution"] == resolution


def test_register_obligation_without_resolution_defaults_to_none(tmp_path):
    """A caller that never captures a resolution (or an old codepath) must
    stamp None, never fabricate a value -- absent is UNKNOWN, not "off"."""
    state_dir = _make_state_dir(tmp_path)
    register_obligation(state_dir, dispatch_id="d-no-resolution", gate="codex_gate")
    record = _read_obligation(state_dir, "d-no-resolution")
    assert record["gate_requirement_resolution"] is None


# ---------------------------------------------------------------------------
# check_gate_requirement_mismatch: three input buckets, three distinct
# answers -- (a) never attempted, (b) attempted and FAILED, (c) captured a
# real value. Collapsing (a) and (b) into the same None was itself an
# OI-1462-shaped bug (a Codex-gate finding on this very mechanism): a broken
# flag-read at the writer must never look identical to "nobody ever asked".
# ---------------------------------------------------------------------------


def test_check_gate_requirement_mismatch_flags_divergence():
    """Bucket (c): both sides captured a real value, and they differ."""
    record = {
        "gate_requirement_resolution": {
            "status": "captured", "flags": {"VNX_CI_GATE_REQUIRED": True}, "error": None,
        },
    }
    mismatch = check_gate_requirement_mismatch(
        record, flag="VNX_CI_GATE_REQUIRED", reader_value=False,
    )
    assert mismatch == {
        "flag": "VNX_CI_GATE_REQUIRED",
        "kind": "value_mismatch",
        "writer_value": True,
        "reader_value": False,
        "detected_at": mismatch["detected_at"],
    }


def test_check_gate_requirement_mismatch_agrees_when_values_match():
    """Bucket (c), agreement case: no finding."""
    record = {
        "gate_requirement_resolution": {
            "status": "captured", "flags": {"VNX_CI_GATE_REQUIRED": True}, "error": None,
        },
    }
    assert check_gate_requirement_mismatch(
        record, flag="VNX_CI_GATE_REQUIRED", reader_value=True,
    ) is None


def test_check_gate_requirement_mismatch_absent_resolution_is_unknown_not_mismatch():
    """Bucket (a): a record predating this field, one whose resolution never
    captured this flag, or a resolution with no recognised status must read
    as UNKNOWN -- never as a manufactured mismatch and never as a false
    agreement."""
    assert check_gate_requirement_mismatch(
        {}, flag="VNX_CI_GATE_REQUIRED", reader_value=True,
    ) is None
    assert check_gate_requirement_mismatch(
        {"gate_requirement_resolution": {
            "status": "captured", "flags": {"OTHER_FLAG": True}, "error": None,
        }},
        flag="VNX_CI_GATE_REQUIRED", reader_value=True,
    ) is None
    assert check_gate_requirement_mismatch(
        {"gate_requirement_resolution": {"status": "unrecognised_future_status"}},
        flag="VNX_CI_GATE_REQUIRED", reader_value=True,
    ) is None


def test_check_gate_requirement_mismatch_writer_capture_failed_is_a_distinct_loud_finding():
    """Bucket (b) -- THE point of the three-bucket fix: when the writer's OWN
    flag-read broke (status="failed"), that must NOT read as bucket (a)
    (nothing to see) -- the function must return a real, distinct finding
    that says something concrete: capture failed, for this reason, so the
    obligation's requirement was never reliably established. This is what an
    earlier version of this fix got wrong by collapsing "failed" into None."""
    record = {
        "gate_requirement_resolution": {
            "status": "failed", "flags": None, "error": "RuntimeError: config store unreachable",
        },
    }
    finding = check_gate_requirement_mismatch(
        record, flag="VNX_CI_GATE_REQUIRED", reader_value=True,
    )
    assert finding is not None, (
        "a writer capture FAILURE must produce a real finding, not the same "
        "None a never-attempted resolution would produce"
    )
    assert finding["kind"] == "writer_capture_failed"
    assert finding["flag"] == "VNX_CI_GATE_REQUIRED"
    assert "config store unreachable" in finding["writer_error"]
    # Distinguishable from bucket (a) by construction: bucket (a) always
    # returns bare None, never a dict.
    assert finding != check_gate_requirement_mismatch(
        {}, flag="VNX_CI_GATE_REQUIRED", reader_value=True,
    )


def test_pr_number_from_pr_id_shapes():
    assert pr_number_from_pr_id("879") == 879
    assert pr_number_from_pr_id("PR-879") == 879
    assert pr_number_from_pr_id("#879") == 879
    assert pr_number_from_pr_id(None) is None
    assert pr_number_from_pr_id("") is None
    assert pr_number_from_pr_id("pr-4d") is None  # contract slug, not a GitHub PR


# ---------------------------------------------------------------------------
# 2. The runner fulfils obligations — or fails LOUD, never silent.
# ---------------------------------------------------------------------------


def test_runner_fulfills_obligation_with_request_and_result_records(tmp_path, monkeypatch):
    state_dir = _make_state_dir(tmp_path)
    register_obligation(
        state_dir, dispatch_id="20260731-oi876-fulfill", gate="codex_gate",
        project_id="vnx-dev", pr_number=1253,
    )
    manager = _FakeManager(state_dir)
    _patch_manager(monkeypatch, manager)

    summary = runner.run(state_dir)

    assert summary["pending_after"] == 0
    record = _read_obligation(state_dir, "20260731-oi876-fulfill")
    assert record["status"] == STATUS_FULFILLED
    assert record["pr_number"] == 1253
    # Exactly the declared gate ran — not the default stack.
    assert manager.calls[0]["review_stack"] == ["codex_gate"]
    # The datapath proof: both records exist on disk at the recorded paths.
    assert record["request_path"].endswith("review_gates/requests/pr-1253-codex_gate.json")
    assert record["result_path"].endswith("review_gates/results/pr-1253-codex_gate.json")
    assert Path(record["request_path"]).exists()
    assert Path(record["result_path"]).exists()


def test_runner_gate_failure_is_loud_and_registered(tmp_path, monkeypatch):
    """A gate that cannot run must produce not_executable request+result
    records — a loud, registered outcome, not silence."""
    state_dir = _make_state_dir(tmp_path)
    register_obligation(
        state_dir, dispatch_id="20260731-oi876-broken-gate", gate="codex_gate",
        project_id="vnx-dev", pr_number=1254,
    )
    _patch_manager(monkeypatch, _FakeManager(state_dir, boom=True))

    summary = runner.run(state_dir)

    assert summary["pending_after"] == 0
    record = _read_obligation(state_dir, "20260731-oi876-broken-gate")
    assert record["status"] == STATUS_NOT_EXECUTABLE
    assert record["reason"] == "runner_error"

    request_file = state_dir / "review_gates" / "requests" / "pr-1254-codex_gate.json"
    result_file = state_dir / "review_gates" / "results" / "pr-1254-codex_gate.json"
    assert request_file.exists(), "loud failure must still write the request record"
    assert result_file.exists(), "loud failure must still write the result record"
    assert json.loads(request_file.read_text())["status"] == "not_executable"
    assert json.loads(result_file.read_text())["status"] == "not_executable"
    # And the skip-rationale audit trail carries the same loud signal.
    audit = state_dir / "gate_execution_audit.ndjson"
    assert audit.exists()
    assert "gate_skip_rationale" in audit.read_text(encoding="utf-8")


def test_runner_leaves_pending_when_no_pr_yet(tmp_path, monkeypatch):
    """Repo resolves and gh works, but no PR exists yet — a genuine WAIT, not a
    fault. The obligation stays pending (OI-1253 fix-forward: this must not be
    confused with the `unresolvable` env-wrong state).

    The dispatch's head branch still exists (a still-running dispatch), so
    OI-1388's dead-branch retirement path must NOT fire here — control case
    for that new behavior, exercised below in the retirement tests."""
    state_dir = _make_state_dir(tmp_path)
    register_obligation(
        state_dir, dispatch_id="20260731-oi876-no-pr", gate="codex_gate",
        project_id="vnx-dev",
    )
    monkeypatch.setattr(runner, "_pr_from_dispatch_metadata", lambda sd, did: None)
    monkeypatch.setattr(runner, "_resolve_github_owner_repo", lambda sd: "Vinix24/vnx-orchestration")
    monkeypatch.setattr(runner, "_pr_from_github", lambda did, owner_repo: None)
    monkeypatch.setattr(runner, "_branch_exists_on_github", lambda did, owner_repo: True)

    summary = runner.run(state_dir)

    assert summary["pending_after"] == 1
    assert summary["unresolvable_after"] == 0
    record = _read_obligation(state_dir, "20260731-oi876-no-pr")
    assert record["status"] == STATUS_PENDING
    assert record["attempts"] == 1


def test_runner_leaves_pending_when_branch_existence_unknown(tmp_path, monkeypatch):
    """gh could not determine whether the branch still exists (outage, rate
    limit, timeout) — ambiguous evidence must never retire an obligation.
    Stays pending, exactly like the "branch confirmed to exist" case."""
    state_dir = _make_state_dir(tmp_path)
    register_obligation(
        state_dir, dispatch_id="20260731-oi1388-branch-unknown", gate="codex_gate",
        project_id="vnx-dev",
    )
    monkeypatch.setattr(runner, "_pr_from_dispatch_metadata", lambda sd, did: None)
    monkeypatch.setattr(runner, "_resolve_github_owner_repo", lambda sd: "Vinix24/vnx-orchestration")
    monkeypatch.setattr(runner, "_pr_from_github", lambda did, owner_repo: None)
    monkeypatch.setattr(runner, "_branch_exists_on_github", lambda did, owner_repo: None)

    summary = runner.run(state_dir)

    assert summary["pending_after"] == 1
    record = _read_obligation(state_dir, "20260731-oi1388-branch-unknown")
    assert record["status"] == STATUS_PENDING


# ---------------------------------------------------------------------------
# 2d. OI-1388: a dispatch that dies without ever producing a PR must not stay
# pending forever — nothing closes that obligation today. RED on unfixed
# main: with no dead-branch check, resolve_pr_number returns AWAITING and the
# obligation lands STATUS_PENDING regardless of whether the branch is gone.
# ---------------------------------------------------------------------------


def test_runner_retires_obligation_when_dispatch_died_without_pr(tmp_path, monkeypatch):
    """A dispatch that never produced a PR, whose head branch no longer
    exists on origin, is DEAD — nothing will ever gate it. The obligation
    must land in a terminal state, never stay pending forever."""
    state_dir = _make_state_dir(tmp_path)
    register_obligation(
        state_dir, dispatch_id="20260801-oi1388-dead-no-pr", gate="codex_gate",
        project_id="vnx-dev",
    )
    monkeypatch.setattr(runner, "_pr_from_dispatch_metadata", lambda sd, did: None)
    monkeypatch.setattr(runner, "_resolve_github_owner_repo", lambda sd: "Vinix24/vnx-orchestration")
    monkeypatch.setattr(runner, "_pr_from_github", lambda did, owner_repo: None)
    monkeypatch.setattr(runner, "_branch_exists_on_github", lambda did, owner_repo: False)

    summary = runner.run(state_dir)

    # State assertion, split from the reason assertions below — a composite
    # check would silently pass even if the wrong field carried the evidence.
    record = _read_obligation(state_dir, "20260801-oi1388-dead-no-pr")
    assert summary["pending_after"] == 0
    assert record["status"] == STATUS_RETIRED
    assert record["status"] != STATUS_FULFILLED, (
        "a dead dispatch with no PR was never reviewed — it must never read as fulfilled"
    )
    assert record["status"] in TERMINAL_STATUSES, "the runner must never retry a dead-branch retirement"

    assert record["reason"] == REASON_NO_PR_BRANCH_GONE
    assert record["reason"], "the reason field is required, never empty"

    assert record["reason_detail"], "the reason_detail field is required, never empty"
    assert "20260801-oi1388-dead-no-pr" in record["reason_detail"]
    assert "dispatch/20260801-oi1388-dead-no-pr" in record["reason_detail"]


def test_runner_never_refires_a_retired_obligation(tmp_path, monkeypatch):
    """A retired obligation is terminal: a later run must never touch it
    again, exactly like an already-fulfilled one."""
    state_dir = _make_state_dir(tmp_path)
    path = register_obligation(
        state_dir, dispatch_id="20260801-oi1388-retired-stays", gate="codex_gate",
        project_id="vnx-dev",
    )
    update_obligation(
        path, status=STATUS_RETIRED, reason=REASON_NO_PR_BRANCH_GONE,
        reason_detail="already retired", resolved_at="2026-08-01T00:00:00Z",
    )
    monkeypatch.setattr(runner, "_pr_from_dispatch_metadata", lambda sd, did: None)
    monkeypatch.setattr(runner, "_resolve_github_owner_repo", lambda sd: "Vinix24/vnx-orchestration")
    monkeypatch.setattr(runner, "_pr_from_github", lambda did, owner_repo: (_ for _ in ()).throw(
        AssertionError("a terminal obligation must never be re-resolved")
    ))

    summary = runner.run(state_dir)

    assert summary["pending_after"] == 0
    record = _read_obligation(state_dir, "20260801-oi1388-retired-stays")
    assert record["status"] == STATUS_RETIRED
    assert record["reason_detail"] == "already retired", "a retired obligation must never be rewritten"


def test_retired_status_never_counts_as_fulfilled_in_a_status_tally(tmp_path):
    """Control case: any report that tallies obligations by status must be
    able to tell `retired` apart from `fulfilled` — a naive status-count
    (the shape any consumer report would take) must not conflate the two."""
    state_dir = _make_state_dir(tmp_path)
    fulfilled_path = register_obligation(
        state_dir, dispatch_id="d-fulfilled", gate="codex_gate", project_id="vnx-dev",
    )
    update_obligation(fulfilled_path, status=STATUS_FULFILLED, resolved_at="2026-08-01T00:00:00Z")
    retired_path = register_obligation(
        state_dir, dispatch_id="d-retired", gate="codex_gate", project_id="vnx-dev",
    )
    update_obligation(
        retired_path, status=STATUS_RETIRED, reason=REASON_NO_PR_BRANCH_GONE,
        reason_detail="dead", resolved_at="2026-08-01T00:00:00Z",
    )

    import gate_obligations as go

    tally = {}
    for _path, record in go.iter_obligations(state_dir):
        tally[record["status"]] = tally.get(record["status"], 0) + 1

    assert tally.get(STATUS_FULFILLED) == 1, "the fulfilled obligation must count as reviewed"
    assert tally.get(STATUS_RETIRED) == 1, "the retired obligation must count separately"
    total_terminal = sum(tally.get(s, 0) for s in TERMINAL_STATUSES)
    assert total_terminal == 2
    assert tally[STATUS_FULFILLED] < total_terminal, (
        "'reviewed' (fulfilled) must not be reported as the full terminal count "
        "— a retired obligation inflating it would be exactly the OI-1400 shape"
    )


def test_runner_marks_unresolvable_when_repo_unattributable(tmp_path, monkeypatch):
    """Env wrong (no attributable GitHub owner/repo) is a FAULT, recorded in the
    distinct `unresolvable` status — never left as `pending`, which would read
    as "not yet" forever (OI-1253 fix-forward)."""
    state_dir = _make_state_dir(tmp_path)
    register_obligation(
        state_dir, dispatch_id="20260816-s1fix-no-repo", gate="codex_gate",
        project_id="vnx-dev",
    )
    monkeypatch.setattr(runner, "_pr_from_dispatch_metadata", lambda sd, did: None)
    monkeypatch.setattr(runner, "_resolve_github_owner_repo", lambda sd: None)

    summary = runner.run(state_dir)

    assert summary["pending_after"] == 1
    assert summary["unresolvable_after"] == 1
    record = _read_obligation(state_dir, "20260816-s1fix-no-repo")
    assert record["status"] == STATUS_UNRESOLVABLE
    assert record["reason"] == "unresolvable_repo"
    assert record["reason_detail"], "an unresolvable obligation must carry an actionable reason"


def test_runner_escalates_unresolvable_to_not_executable_after_threshold(tmp_path, monkeypatch):
    """An obligation that stays unresolvable past the bounded term escalates to
    the loud terminal not_executable — it can never wait silently forever."""
    state_dir = _make_state_dir(tmp_path)
    path = register_obligation(
        state_dir, dispatch_id="20260816-s1fix-escalate", gate="codex_gate",
        project_id="vnx-dev",
    )
    update_obligation(path, attempts=runner._UNRESOLVABLE_ESCALATION_ATTEMPTS - 1)
    monkeypatch.setattr(runner, "_pr_from_dispatch_metadata", lambda sd, did: None)
    monkeypatch.setattr(runner, "_resolve_github_owner_repo", lambda sd: None)

    summary = runner.run(state_dir)

    assert summary["pending_after"] == 0, "escalated obligation is terminal, not still open"
    record = _read_obligation(state_dir, "20260816-s1fix-escalate")
    assert record["status"] == STATUS_NOT_EXECUTABLE
    assert record["reason"] == "unresolvable_timeout"
    assert "VNX_PROJECT_ID" in record["reason_detail"]


def test_runner_never_refires_terminal_obligations(tmp_path, monkeypatch):
    state_dir = _make_state_dir(tmp_path)
    path = register_obligation(
        state_dir, dispatch_id="20260731-oi876-done", gate="codex_gate",
        project_id="vnx-dev", pr_number=1255,
    )
    update_obligation(path, status=STATUS_FULFILLED, resolved_at="2026-07-31T00:00:00Z")
    manager = _FakeManager(state_dir)
    _patch_manager(monkeypatch, manager)

    summary = runner.run(state_dir)

    assert summary["pending_after"] == 0
    assert manager.calls == [], "a fulfilled obligation must never re-fire the gate"


# ---------------------------------------------------------------------------
# 2b. OI-1384: a TEMPORARY refusal must not burn the obligation terminal.
# ---------------------------------------------------------------------------


def test_runner_leaves_pending_on_provider_disabled_result(tmp_path, monkeypatch):
    """reason=provider_disabled means the gate is PARKED by config (e.g.
    VNX_CI_GATE_REQUIRED=0), not that anything is broken — measured live
    against PR #1627, where CI had actually passed but the obligation still
    died as a permanent not_executable. Must stay pending so a later run can
    retry once the gate is unparked."""
    state_dir = _make_state_dir(tmp_path)
    register_obligation(
        state_dir, dispatch_id="20260821-oi1384-parked", gate="ci_gate",
        project_id="vnx-dev", pr_number=1627,
    )
    manager = _FakeManager(state_dir, result_status="not_executable", result_reason="provider_disabled")
    _patch_manager(monkeypatch, manager)

    summary = runner.run(state_dir)

    assert summary["pending_after"] == 1
    record = _read_obligation(state_dir, "20260821-oi1384-parked")
    assert record["status"] == STATUS_PENDING
    assert record["attempts"] == 1
    assert record["reason"] == "gate_parked"
    assert "parked" in record["reason_detail"]
    assert "not broken" in record["reason_detail"]


def test_runner_leaves_pending_on_running_result(tmp_path, monkeypatch):
    """status=running means the CI check is still in flight — a "not yet",
    not a verdict. Must stay pending, not be marked fulfilled or terminal."""
    state_dir = _make_state_dir(tmp_path)
    register_obligation(
        state_dir, dispatch_id="20260821-oi1384-running", gate="ci_gate",
        project_id="vnx-dev", pr_number=1628,
    )
    manager = _FakeManager(state_dir, result_status="running")
    _patch_manager(monkeypatch, manager)

    summary = runner.run(state_dir)

    assert summary["pending_after"] == 1
    record = _read_obligation(state_dir, "20260821-oi1384-running")
    assert record["status"] == STATUS_PENDING
    assert record["attempts"] == 1
    assert record["reason"] == "gate_run_in_progress"


def test_runner_still_terminates_a_genuinely_permanent_refusal(tmp_path, monkeypatch):
    """The fix narrows what burns the obligation — it must not widen what
    stays pending. A gate that is not_executable for a reason outside the
    explicit temporary set (here: the gate provider is not configured at
    all, distinct from merely "not installed" or "disabled by a flag") is a
    real, permanent refusal and must still terminate on the first attempt.

    ``provider_not_installed`` used to be exactly this case, but OI-1400
    residu moved it into the temporary set (see
    ``tests/test_gate_obligation_runner.py::
    TestProviderNotInstalledIsTemporary``) — a missing binary can be
    installed later, same as a config flag can be flipped.
    ``provider_not_configured`` stays outside the set on purpose so this
    test keeps covering a genuinely permanent refusal."""
    state_dir = _make_state_dir(tmp_path)
    register_obligation(
        state_dir, dispatch_id="20260821-oi1384-real-refusal", gate="codex_gate",
        project_id="vnx-dev", pr_number=1629,
    )
    manager = _FakeManager(state_dir, result_status="not_executable", result_reason="provider_not_configured")
    _patch_manager(monkeypatch, manager)

    summary = runner.run(state_dir)

    assert summary["pending_after"] == 0
    record = _read_obligation(state_dir, "20260821-oi1384-real-refusal")
    assert record["status"] == STATUS_NOT_EXECUTABLE
    assert record["attempts"] == 1


def test_runner_escalates_temporary_refusal_to_not_executable_after_threshold(tmp_path, monkeypatch):
    """A temporary refusal that recurs forever must still eventually escalate
    loudly — same shape as the `unresolvable` PR-resolution escalation path —
    just not on the first attempt."""
    state_dir = _make_state_dir(tmp_path)
    path = register_obligation(
        state_dir, dispatch_id="20260821-oi1384-escalate", gate="ci_gate",
        project_id="vnx-dev", pr_number=1630,
    )
    update_obligation(path, attempts=runner._TEMPORARY_REFUSAL_ESCALATION_ATTEMPTS - 1)
    manager = _FakeManager(state_dir, result_status="not_executable", result_reason="provider_disabled")
    _patch_manager(monkeypatch, manager)

    summary = runner.run(state_dir)

    assert summary["pending_after"] == 0
    record = _read_obligation(state_dir, "20260821-oi1384-escalate")
    assert record["status"] == STATUS_NOT_EXECUTABLE
    assert record["reason"] == "gate_parked_timeout"
    assert record["attempts"] == runner._TEMPORARY_REFUSAL_ESCALATION_ATTEMPTS


# ---------------------------------------------------------------------------
# 2c. OI-1400: an unknown/unavailable gate status must never fall through
# to "fulfilled" — that silent default is exactly how a consumer project's
# PR #966/#967 booked vervuld off a not_executable/provider-not-installed and
# an unavailable/worktree-checkout-failed result, both with empty
# contract_hash and empty report_path.
# ---------------------------------------------------------------------------


def test_runner_leaves_pending_on_unavailable_result(tmp_path, monkeypatch):
    """status=unavailable (gate_status.UNAVAILABLE_STATES) means the provider
    produced no verdict at all — outage, quota, timeout, or a failed setup
    step such as a worktree checkout (measured live: a consumer project's
    PR #967 booked vervuld off exactly this shape). Must stay pending with a
    reason that names the outage, not "gate_parked" (that name is reserved
    for a config-disabled gate) and never "fulfilled"."""
    state_dir = _make_state_dir(tmp_path)
    register_obligation(
        state_dir, dispatch_id="20260821-oi1400-unavailable", gate="codex_gate",
        project_id="vnx-dev", pr_number=967,
    )
    manager = _FakeManager(
        state_dir, result_status="unavailable", result_reason="worktree_checkout_failed",
    )
    _patch_manager(monkeypatch, manager)

    summary = runner.run(state_dir)

    assert summary["pending_after"] == 1
    record = _read_obligation(state_dir, "20260821-oi1400-unavailable")
    assert record["status"] == STATUS_PENDING
    assert record["attempts"] == 1
    assert record["reason"] == "gate_run_unavailable"
    assert "unavailable" in record["reason_detail"]
    assert "worktree_checkout_failed" in record["reason_detail"]


def test_runner_escalates_unavailable_to_not_executable_after_threshold(tmp_path, monkeypatch):
    """Same bounded-retry-then-escalate shape as the other temporary
    refusals: an unavailable result that recurs forever must still
    eventually alarm someone, just not on the first attempt."""
    state_dir = _make_state_dir(tmp_path)
    path = register_obligation(
        state_dir, dispatch_id="20260821-oi1400-unavailable-escalate", gate="codex_gate",
        project_id="vnx-dev", pr_number=968,
    )
    update_obligation(path, attempts=runner._TEMPORARY_REFUSAL_ESCALATION_ATTEMPTS - 1)
    manager = _FakeManager(state_dir, result_status="unavailable", result_reason="timeout")
    _patch_manager(monkeypatch, manager)

    summary = runner.run(state_dir)

    assert summary["pending_after"] == 0
    record = _read_obligation(state_dir, "20260821-oi1400-unavailable-escalate")
    assert record["status"] == STATUS_NOT_EXECUTABLE
    assert record["reason"] == "gate_run_unavailable_timeout"
    assert record["attempts"] == runner._TEMPORARY_REFUSAL_ESCALATION_ATTEMPTS


def test_runner_does_not_fulfill_on_unknown_result_status(tmp_path, monkeypatch):
    """A gate result status this runner has never seen before must NOT fall
    through to `fulfilled` — that silent default is the exact bug behind
    OI-1400. It must land somewhere safe and visible (pending, loud reason)
    instead."""
    state_dir = _make_state_dir(tmp_path)
    register_obligation(
        state_dir, dispatch_id="20260821-oi1400-unknown-status", gate="codex_gate",
        project_id="vnx-dev", pr_number=969,
    )
    manager = _FakeManager(state_dir, result_status="quantum_flux_undecided")
    _patch_manager(monkeypatch, manager)

    summary = runner.run(state_dir)

    record = _read_obligation(state_dir, "20260821-oi1400-unknown-status")
    assert record["status"] != STATUS_FULFILLED, (
        "an unrecognised result status must never silently land as fulfilled"
    )
    assert summary["pending_after"] == 1
    assert record["status"] == STATUS_PENDING
    assert record["attempts"] == 1
    assert record["reason"] == "gate_status_unknown"
    assert "quantum_flux_undecided" in record["reason_detail"]


def test_runner_fulfills_a_normal_passing_gate_result(tmp_path, monkeypatch):
    """The doorval inversion must not touch the ordinary success path: a gate
    that actually ran and decided PASS still lands fulfilled.

    ``status="pass"`` is the real verdict value a genuine passing gate run
    writes — confirmed via
    ``grep -n '"status": verdict' scripts/lib/gate_executor.py``
    (ci_gate's ``_ci_gate_summary``/``verdict`` produce exactly ``"pass"``,
    ``"fail"``, or ``"running"``) and cross-checked against the canonical
    vocabulary in ``scripts/lib/gate_status.py`` (``PASS_STATES = {"approve",
    "completed", "pass", "passed"}``), which is what this fix imports as
    ``_GATE_RESULT_PASS_STATES`` to decide the fulfilled path."""
    state_dir = _make_state_dir(tmp_path)
    register_obligation(
        state_dir, dispatch_id="20260821-oi1400-normal-pass", gate="ci_gate",
        project_id="vnx-dev", pr_number=970,
    )
    manager = _FakeManager(state_dir, result_status="pass")
    _patch_manager(monkeypatch, manager)

    summary = runner.run(state_dir)

    assert summary["pending_after"] == 0
    record = _read_obligation(state_dir, "20260821-oi1400-normal-pass")
    assert record["status"] == STATUS_FULFILLED
    assert record["attempts"] == 1


def test_runner_known_terminal_statuses_stay_unchanged(tmp_path, monkeypatch):
    """The three obligation-level terminal statuses (failed, fulfilled,
    not_executable) must keep resolving to themselves — the doorval
    inversion narrows the fallback, it must not touch the already-correct
    literal mappings."""
    for status, pr_number in (
        (STATUS_FAILED, 971),
        (STATUS_FULFILLED, 972),
        (STATUS_NOT_EXECUTABLE, 973),
    ):
        state_dir = _make_state_dir(tmp_path / f"terminal-{status}")
        dispatch_id = f"20260821-oi1400-terminal-{status}"
        register_obligation(
            state_dir, dispatch_id=dispatch_id, gate="codex_gate",
            project_id="vnx-dev", pr_number=pr_number,
        )
        manager = _FakeManager(state_dir, result_status=status)
        _patch_manager(monkeypatch, manager)

        summary = runner.run(state_dir)

        assert summary["pending_after"] == 0
        record = _read_obligation(state_dir, dispatch_id)
        assert record["status"] == status, f"result status {status!r} must stay terminal as itself"


# ---------------------------------------------------------------------------
# 3. The freshness scanner: per sleutel, declaration checked against result.
# ---------------------------------------------------------------------------


def _write_obligation(state_dir: Path, dispatch_id: str, gate: str, **fields) -> None:
    register_obligation(state_dir, dispatch_id=dispatch_id, gate=gate)
    if fields:
        update_obligation(obligation_path(state_dir, dispatch_id), **fields)


def test_scan_groups_per_gate_key_not_per_directory(tmp_path):
    """OI-881: a directory can look alive because ONE key still writes. The
    scanner must expose each gate separately."""
    state_dir = _make_state_dir(tmp_path)
    old = "2026-07-06T15:33:32Z"      # 25 days of silence shape
    recent = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _write_obligation(state_dir, "d-old", "gemini_review", declared_at=old)
    _write_obligation(
        state_dir, "d-fresh", "codex_gate",
        status=STATUS_FULFILLED, resolved_at=recent,
    )

    spec = {"path": str(state_dir / "review_gates" / "obligations")}
    seen = producer_freshness.scan_gate_obligations(spec, now=time.time())

    assert set(seen) == {"gemini_review", "codex_gate"}
    # codex_gate resolved just now; gemini_review's oldest pending is 25 days old.
    assert time.time() - seen["codex_gate"] < 3600
    assert time.time() - seen["gemini_review"] > 20 * 86400


def test_evaluate_flags_declaration_without_result(tmp_path):
    """The OI-876 shape: a declared gate that produced no result within
    cadence is a stale finding for THAT gate key — with demand evidence."""
    state_dir = _make_state_dir(tmp_path)
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3 * 86400))
    _write_obligation(state_dir, "d-1", "codex_gate", declared_at=old)
    # A second gate fulfilled recently must NOT mask the stale one.
    _write_obligation(
        state_dir, "d-2", "kimi_gate",
        status=STATUS_FULFILLED,
        resolved_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )

    spec = {
        "name": "review_gate_obligations",
        "type": "gate_obligations",
        "path": str(state_dir / "review_gates" / "obligations"),
        "cadence_seconds": 86400,
    }
    section = producer_freshness.evaluate_producer(spec, now=time.time())

    assert section["status"] == "stale"
    stale_keys = {f["key"] for f in section["findings"]}
    assert stale_keys == {"codex_gate"}, (
        "only the gate with an unfulfilled declaration may be flagged — "
        "the fulfilled sibling must stay green (per sleutel, not per tabel)"
    )
    finding = section["findings"][0]
    assert finding["kind"] == "stale"
    assert finding["silence_seconds"] >= 3 * 86400


def test_evaluate_unreadable_obligation_is_source_unreadable(tmp_path):
    state_dir = _make_state_dir(tmp_path)
    bad = state_dir / "review_gates" / "obligations" / "broken.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{not json", encoding="utf-8")

    spec = {
        "name": "review_gate_obligations",
        "type": "gate_obligations",
        "path": str(bad.parent),
        "cadence_seconds": 86400,
    }
    section = producer_freshness.evaluate_producer(spec, now=time.time())

    assert section["status"] == "error"
    assert section["findings"][0]["kind"] == "source_unreadable"


def test_scan_treats_retired_as_resolved_not_pending(tmp_path):
    """OI-1388: a retired obligation must read as resolved evidence for
    freshness — the same as fulfilled/not_executable/failed — never as a
    still-open declaration that could trip a staleness finding."""
    state_dir = _make_state_dir(tmp_path)
    old_declared = "2026-07-01T00:00:00Z"  # would be stale if still counted pending
    recent_resolved = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _write_obligation(
        state_dir, "d-retired", "codex_gate",
        declared_at=old_declared,
        status=STATUS_RETIRED,
        reason=REASON_NO_PR_BRANCH_GONE,
        reason_detail="dead",
        resolved_at=recent_resolved,
    )

    spec = {"path": str(state_dir / "review_gates" / "obligations")}
    seen = producer_freshness.scan_gate_obligations(spec, now=time.time())

    assert time.time() - seen["codex_gate"] < 3600, (
        "a retired obligation's OLD declared_at must not drive staleness — "
        "its recent resolved_at must be what freshness reads"
    )


def test_real_config_loads_with_obligations_producer():
    config = Path(__file__).resolve().parent.parent / "configs" / "producer_freshness.yaml"
    registry = producer_freshness.load_registry(config)
    by_name = {p["name"]: p for p in registry}
    assert "review_gate_obligations" in by_name
    assert by_name["review_gate_obligations"]["type"] == "gate_obligations"


# ---------------------------------------------------------------------------
# 4. dispatch-20260816-gate-never-skippable: an empty gate is never silent
# ---------------------------------------------------------------------------


def _make_router_broken(monkeypatch) -> None:
    import smart_router

    def _boom(**kwargs):
        raise RuntimeError("smart-router derivation exploded")

    monkeypatch.setattr(smart_router, "resolve_gate", _boom)


def test_writing_spec_without_gate_is_refused_when_router_fails(tmp_path, monkeypatch, capsys):
    """A writing dispatch whose gate is still empty after derivation is refused.

    The router fills every silent spec from its governance variant, so an empty
    gate after ``_resolve_gate_via_router`` can only mean the derivation failed
    (fail-open). The door must refuse that for a writing dispatch — the empty
    gate is a governance decision that was never made, not a valid "no gate".
    """
    data_dir, spec_file = _make_bundle(
        tmp_path,
        staging_id="20260816-staging-write-nogate",
        dispatch_id="20260816-write-no-gate",
        gate="",
        dispatch_paths=["scripts/lib/dispatch_cli.py"],
    )
    _make_state_dir(tmp_path)
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    _make_router_broken(monkeypatch)

    with patch("dispatch_cli._execute_claude", return_value=0) as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 1
    assert "gate-required" in capsys.readouterr().err
    assert mock_execute.call_count == 0, "a refused dispatch must never reach execution"
    assert not obligation_path(data_dir / "state", "20260816-write-no-gate").exists(), (
        "a refused writing dispatch must not leave a gate obligation — it never ran"
    )


def test_read_only_spec_without_gate_records_no_gate_obligation(tmp_path, monkeypatch):
    """A genuinely read-only dispatch (Edit-denied role) with an empty gate
    (router failed) still runs and records an explicit, countable no-gate
    obligation — never a silent absence."""
    data_dir, spec_file = _make_bundle(
        tmp_path,
        staging_id="20260816-staging-ro-nogate",
        dispatch_id="20260816-ro-no-gate",
        gate="",
        dispatch_paths=[],
        role="code-reviewer",
    )
    _make_state_dir(tmp_path)
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    _make_router_broken(monkeypatch)

    with patch("dispatch_cli._execute_claude", return_value=0):
        rc = run_dispatch(spec_file)

    assert rc == 0
    record = _read_obligation(data_dir / "state", "20260816-ro-no-gate")
    assert record["gate"] == NO_GATE_KEY
    assert record["status"] == STATUS_NOT_EXECUTABLE
    assert record["no_gate"] is True


def test_build_role_without_gate_is_refused_even_with_empty_paths(tmp_path, monkeypatch, capsys):
    """A build role (Edit allowed) dispatched with NO dispatch_paths and no
    declared gate is still WRITING: empty dispatch_paths mean "no narrowing"
    (full write scope), not "read-only". The door must refuse it loudly rather
    than treat it as a no-gate read-only dispatch (the 2026-08-16 leak)."""
    data_dir, spec_file = _make_bundle(
        tmp_path,
        staging_id="20260816-staging-build-nogate",
        dispatch_id="20260816-build-no-gate",
        gate="",
        dispatch_paths=[],
        role="backend-developer",
    )
    _make_state_dir(tmp_path)
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    _make_router_broken(monkeypatch)

    with patch("dispatch_cli._execute_claude", return_value=0) as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 1
    assert "gate-required" in capsys.readouterr().err
    assert mock_execute.call_count == 0, "a refused dispatch must never reach execution"
    assert not obligation_path(data_dir / "state", "20260816-build-no-gate").exists(), (
        "a refused writing dispatch must not leave a gate obligation — it never ran"
    )
