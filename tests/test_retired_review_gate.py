"""gemini_review is a retired gate: refused wherever it could be selected, still readable as history.

Operator decision 2026-09-26: Gemini is no longer a reviewer. One set,
``dispatch_spec.RETIRED_GATE_NAMES``, sits next to the registry of legal names. Every
reader of ``REGISTERED_GATE_NAMES`` refuses the name and says why, a stale project
stack drops it with a warning, a direct request is blocked, it never signs a chain
advancement, and the closure verifier still interprets an existing record.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path, PurePosixPath

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import closure_verifier
import dispatch_bridge
import gate_recorder
import review_gate_manager
import smart_router
from dispatch_spec import (
    REGISTERED_GATE_NAMES,
    RETIRED_GATE_NAMES,
    DispatchPath,
    DispatchSpec,
    Gate,
    PathAccess,
    Reject,
    ReviewGateConfigError,
    retired_gate_hint,
    validate,
)
from gate_request_handler import (
    ReviewGateTakeoverConfigError,
    _parse_review_gate_takeover_chain,
)

RETIRED = "gemini_review"


def _set_stack(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("VNX_OVERRIDE_DEFAULT_REVIEW_STACK", raw)
    monkeypatch.setenv("VNX_OVERRIDE_CI_GATE_REQUIRED", "0")


def test_the_retired_gate_is_not_registered() -> None:
    assert RETIRED in RETIRED_GATE_NAMES
    assert not RETIRED_GATE_NAMES & REGISTERED_GATE_NAMES
    assert RETIRED not in {g.value for g in Gate}


def test_a_retired_gate_is_not_a_runnable_provider() -> None:
    assert not RETIRED_GATE_NAMES & set(gate_recorder.GATE_PROVIDERS)
    assert gate_recorder.resolve_gate_provider(RETIRED) is None


def test_hint_names_a_retired_gate_and_stays_silent_for_a_typo() -> None:
    assert "retired" in retired_gate_hint(RETIRED)
    assert retired_gate_hint("codex_gat") == ""
    assert retired_gate_hint(None) == ""


def test_door_validation_refuses_it_as_retired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instruction = tmp_path / "instruction.md"
    instruction.write_text("Do the work.", encoding="utf-8")
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
    spec = DispatchSpec(
        schema_version=1, project_id="vnx-dev", dispatch_id="20260926-retired-dispatch",
        staging_id="20260926-retired-staging", instruction_file=instruction,
        role="backend-developer", target_slot="T1", gate=RETIRED,
        dispatch_paths=(DispatchPath(PurePosixPath("scripts/lib/foo.py"), PathAccess.WRITE),),
    )
    result = validate(spec, project_id="vnx-dev", repo_root=Path("/fake/repo"))
    assert isinstance(result, Reject)
    assert result.code == "bad-gate"
    assert "retired" in result.reason


def test_staging_bridge_and_takeover_chain_refuse_it_as_retired() -> None:
    with pytest.raises(ValueError, match="retired gate"):
        dispatch_bridge._canonical_gate(RETIRED)
    with pytest.raises(ReviewGateTakeoverConfigError, match="retired gate"):
        _parse_review_gate_takeover_chain(f"codex_gate,{RETIRED}")


def test_a_stack_of_only_the_retired_gate_refuses_and_says_why(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_stack(monkeypatch, RETIRED)
    with pytest.raises(ReviewGateConfigError, match="retired gate"):
        smart_router._primary_review_gate()


def test_a_stale_stack_still_declares_its_real_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_stack(monkeypatch, f"{RETIRED},codex_gate")
    assert smart_router._primary_review_gate() == "codex_gate"


def test_executed_stack_drops_it_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    _set_stack(monkeypatch, f"{RETIRED},codex_gate")
    with caplog.at_level(logging.WARNING, logger="review_gate_manager"):
        stack = review_gate_manager._build_default_review_stack()
    assert stack == ["codex_gate"]
    assert any(
        record.name == "review_gate_manager" and RETIRED in record.getMessage()
        for record in caplog.records
    )


def test_a_direct_request_is_blocked_as_retired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / ".vnx-data"
    (data_dir / "state").mkdir(parents=True)
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_STATE_DIR", str(data_dir / "state"))
    monkeypatch.setenv("VNX_REPORTS_DIR", str(data_dir / "unified_reports"))
    monkeypatch.setenv("VNX_HEADLESS_REPORTS_DIR", str(data_dir / "unified_reports" / "headless"))
    manager = review_gate_manager.ReviewGateManager()

    result = manager._dispatch_one_review(
        RETIRED, 1, "fix/x", "low", ["scripts/foo.py"], "per_pr", "20260926-direct",
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "retired_review_gate"
    assert not list((data_dir / "state" / "review_gates" / "requests").glob(f"*{RETIRED}*"))


def test_the_closure_verifier_reads_history_but_never_lets_it_sign(tmp_path: Path) -> None:
    assert RETIRED in closure_verifier._KNOWN_GATES
    assert RETIRED not in closure_verifier._REVIEW_PEER_GATES
    report = tmp_path / "report.md"
    report.write_text("# report\n", encoding="utf-8")
    record = {
        "gate": RETIRED, "pr_id": "PR-0", "status": "pass", "contract_hash": "abc123",
        "report_path": str(report), "advisory_count": 1, "blocking_count": 0,
    }
    check = closure_verifier._check_single_gate(RETIRED, None, record, tmp_path, "feature/demo")
    assert check.status == "PASS"
    assert "not implemented" not in check.detail
