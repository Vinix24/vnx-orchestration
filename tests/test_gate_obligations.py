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
    STATUS_FULFILLED,
    STATUS_NOT_EXECUTABLE,
    STATUS_PENDING,
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
        "role": "backend-developer",
        "target_slot": "T0",
        "gate": gate,
        "dispatch_paths": [{"path": p} for p in (dispatch_paths or [])],
        "provider": "claude",
        "deadline_seconds": 3600,
        "isolation": "worktree",
    }
    spec_file = bundle_dir / "dispatch-spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    return data_dir, spec_file


def _read_obligation(state_dir: Path, dispatch_id: str) -> dict:
    return json.loads(obligation_path(state_dir, dispatch_id).read_text(encoding="utf-8"))


class _FakeManager:
    """Stands in for ReviewGateManager: writes the request+result records the
    real manager's request_and_execute produces, without running any gate."""

    def __init__(self, state_dir: Path, *, boom: bool = False) -> None:
        self.state_dir = Path(state_dir)
        self.boom = boom
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
            self._result_path(gate, pr_number).write_text(
                json.dumps({"gate": gate, "pr_number": pr_number, "status": "completed"}),
                encoding="utf-8",
            )
        return {"pr_number": pr_number, "branch": branch, "gates": [], "has_required_failure": False}


def _patch_manager(monkeypatch, manager: "_FakeManager") -> None:
    """Hermetic patch: no git, no gh, no real gate — only the fake manager."""
    monkeypatch.setattr(runner, "_build_manager", lambda state_dir: manager)
    monkeypatch.setattr(runner, "_branch_from_github", lambda pr: None)
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


def test_runner_leaves_pending_when_pr_unresolvable(tmp_path, monkeypatch):
    state_dir = _make_state_dir(tmp_path)
    register_obligation(
        state_dir, dispatch_id="20260731-oi876-no-pr", gate="codex_gate",
        project_id="vnx-dev",
    )
    monkeypatch.setattr(runner, "_pr_from_dispatch_metadata", lambda sd, did: None)
    monkeypatch.setattr(runner, "_pr_from_github", lambda did: None)

    summary = runner.run(state_dir)

    assert summary["pending_after"] == 1
    record = _read_obligation(state_dir, "20260731-oi876-no-pr")
    assert record["status"] == STATUS_PENDING
    assert record["attempts"] == 1


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
    """A read-only dispatch with an empty gate (router failed) still runs and
    records an explicit, countable no-gate obligation — never a silent absence."""
    data_dir, spec_file = _make_bundle(
        tmp_path,
        staging_id="20260816-staging-ro-nogate",
        dispatch_id="20260816-ro-no-gate",
        gate="",
        dispatch_paths=[],
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
