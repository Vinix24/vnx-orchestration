"""execution_depth is mandatory for every terminal gate write (OI-1618).

A verdict without investigation is no verdict, on every lane. Agentic lanes
(codex_gate/gemini_review) already refuse a zero-action run via
``gate_artifacts.materialize_artifacts`` (OI-1485). Single-shot lanes
(glm_gate/kimi_gate) had no equivalent floor at all: a "pass" against an
empty diff passed every invariant ``record_terminal_result`` checked,
because nothing there asked whether the diff carried anything to review.

``gate_depth.single_shot_depth`` gives the single-shot lanes their own shape
of the same measurement (an empty diff after strip, not a tool-call count),
and ``gate_recorder.record_terminal_result`` now REQUIRES an
``execution_depth`` for every terminal write and reclassifies a degenerate
pass/fail to ``unavailable``/``gate_execution_degenerate`` itself -- the one
place every lane's terminal write passes through, so glm_gate/kimi_gate get
the floor without reimplementing gate_artifacts' check.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import gate_depth
from gate_recorder import record_terminal_result

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "pr-1749-codex_gate.degenerate.json"


def _fixture_record() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1. The mandatory argument -- rood op main: execution_depth did not exist
#    as a parameter at all, so this call succeeded there.
# ---------------------------------------------------------------------------


def test_record_terminal_result_requires_execution_depth(tmp_path):
    payload = {
        "gate": "kimi_gate", "pr_id": "1", "status": "pass",
        "contract_hash": "h", "report_path": "/r.md", "dispatch_id": "kimi-gate-pr1-1",
    }
    with pytest.raises(TypeError):
        # execution_depth deliberately omitted -- that is what this test checks.
        record_terminal_result(
            gate="kimi_gate", pr_id="1", result_path=tmp_path / "pr-1-kimi_gate.json",
            payload=payload,
        )


# ---------------------------------------------------------------------------
# 2. Agentic depth: a pass with zero investigative actions is refused HERE,
#    not only inside gate_artifacts.materialize_artifacts -- rood op main:
#    the recorder booked `pass` unconditionally.
# ---------------------------------------------------------------------------


def test_pass_with_degenerate_agentic_depth_becomes_unavailable(tmp_path):
    depth = gate_depth.ExecutionDepth(
        parsed=True, mode="agentic", investigative_actions=0, agent_messages=1, input_tokens=100,
    )
    assert gate_depth.is_degenerate(depth) is True  # baseline

    payload = {
        "gate": "codex_gate", "pr_id": "42", "status": "pass",
        "contract_hash": "realhash", "report_path": "/tmp/report.md",
        "dispatch_id": "codex-gate-pr42-1", "blocking_findings": [],
    }
    out = tmp_path / "pr-42-codex_gate.json"
    record_terminal_result(
        gate="codex_gate", pr_id="42", result_path=out, payload=payload,
        execution_depth=depth,
    )

    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["status"] == "unavailable"
    assert written["reason"] == "gate_execution_degenerate"
    assert written["contract_hash"] == ""
    assert written["blocking_findings"] == []
    assert written["execution_depth"]["investigative_actions"] == 0
    # payload is mutated in place -- the caller's own dict matches the file.
    assert payload["status"] == "unavailable"


def test_pass_with_real_agentic_depth_stays_pass(tmp_path):
    """Baseline: a real investigation is untouched."""
    depth = gate_depth.ExecutionDepth(
        parsed=True, mode="agentic", investigative_actions=16, shell_calls=16,
        files_read=16, agent_messages=1, input_tokens=239992,
    )
    assert gate_depth.is_degenerate(depth) is False

    payload = {
        "gate": "codex_gate", "pr_id": "43", "status": "pass",
        "contract_hash": "realhash", "report_path": "/tmp/report.md",
        "dispatch_id": "codex-gate-pr43-1", "blocking_findings": [],
    }
    out = tmp_path / "pr-43-codex_gate.json"
    record_terminal_result(
        gate="codex_gate", pr_id="43", result_path=out, payload=payload,
        execution_depth=depth,
    )
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["status"] == "pass"
    assert written["execution_depth"]["investigative_actions"] == 16


# ---------------------------------------------------------------------------
# 3. not_executable is out of scope for the degeneracy check -- it never ran,
#    so "did it investigate" does not apply to it.
# ---------------------------------------------------------------------------


def test_not_executable_is_never_reclassified_by_depth(tmp_path):
    depth = gate_depth.ExecutionDepth(parsed=True, mode="agentic", investigative_actions=0)
    payload = {
        "gate": "kimi_gate", "pr_id": "1", "status": "not_executable",
        "reason": "provider_disabled", "contract_hash": "", "report_path": "",
        "dispatch_id": "kimi-gate-pr1-1",
    }
    out = tmp_path / "pr-1-kimi_gate.json"
    record_terminal_result(
        gate="kimi_gate", pr_id="1", result_path=out, payload=payload,
        execution_depth=depth,
    )
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["status"] == "not_executable"


# ---------------------------------------------------------------------------
# 4. Single-shot depth: degenerate is EXCLUSIVELY an empty diff after strip.
#    Truncation is recorded but never degenerate on its own.
# ---------------------------------------------------------------------------


def test_single_shot_empty_diff_is_degenerate():
    depth = gate_depth.single_shot_depth(0, False)
    assert depth.mode == "single_shot"
    assert gate_depth.is_degenerate(depth) is True


def test_single_shot_truncated_but_nonempty_diff_is_not_degenerate():
    """A 60000-character diff capped at MAX_DIFF_CHARS still handed the model
    real content — truncation is a fact about size, never about content."""
    depth = gate_depth.single_shot_depth(50000, True)
    assert gate_depth.is_degenerate(depth) is False


def test_pass_with_degenerate_single_shot_depth_becomes_unavailable(tmp_path):
    depth = gate_depth.single_shot_depth(0, False)
    payload = {
        "gate": "glm_gate", "pr_id": "99", "status": "pass",
        "contract_hash": "realhash", "report_path": "/tmp/glm-report.md",
        "dispatch_id": "glm-gate-pr99-1", "blocking_findings": [],
    }
    out = tmp_path / "pr-99-glm_gate.json"
    record_terminal_result(
        gate="glm_gate", pr_id="99", result_path=out, payload=payload,
        execution_depth=depth,
    )
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["status"] == "unavailable"
    assert written["reason"] == "gate_execution_degenerate"
    assert written["execution_depth"]["mode"] == "single_shot"
    assert written["execution_depth"]["diff_chars"] == 0


def test_pass_with_truncated_single_shot_diff_stays_pass(tmp_path):
    depth = gate_depth.single_shot_depth(50000, True)
    payload = {
        "gate": "kimi_gate", "pr_id": "100", "status": "pass",
        "contract_hash": "realhash", "report_path": "/tmp/kimi-report.md",
        "dispatch_id": "kimi-gate-pr100-1", "blocking_findings": [],
    }
    out = tmp_path / "pr-100-kimi_gate.json"
    record_terminal_result(
        gate="kimi_gate", pr_id="100", result_path=out, payload=payload,
        execution_depth=depth,
    )
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["status"] == "pass"
    assert written["execution_depth"]["diff_truncated"] is True


# ---------------------------------------------------------------------------
# 5. The real fixture: byte-copied from a genuine on-disk degenerate
#    codex_gate record (measured 2026-09-04, PR #1749). Feeding its own
#    execution_depth back into the recorder with a mocked `pass` verdict must
#    refuse it the same way the original run was refused -- this is the exact
#    shape the fix exists to catch, not a synthetic one.
# ---------------------------------------------------------------------------


def test_fixture_degenerate_depth_refuses_a_mocked_pass(tmp_path):
    fixture = _fixture_record()
    depth = gate_depth.from_dict(fixture["execution_depth"])
    assert depth.mode == "agentic"
    assert gate_depth.is_degenerate(depth) is True

    payload = {
        "gate": "codex_gate", "pr_id": "1749", "status": "pass",
        "contract_hash": "wouldbeahash", "report_path": "/tmp/pr1749-report.md",
        "dispatch_id": "codex-gate-pr1749-mocked", "blocking_findings": [],
    }
    out = tmp_path / "pr-1749-codex_gate.json"
    record_terminal_result(
        gate="codex_gate", pr_id="1749", result_path=out, payload=payload,
        execution_depth=depth,
    )
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["status"] == "unavailable"
    assert written["reason"] == "gate_execution_degenerate"


# ---------------------------------------------------------------------------
# 6. from_dict: round-trips a real record's execution_depth, and degrades
#    gracefully on anything foreign -- used by gate_reanchor_cli.py to carry
#    the ORIGINAL run's depth forward instead of re-measuring one.
# ---------------------------------------------------------------------------


def test_from_dict_round_trips_the_fixture_depth():
    fixture = _fixture_record()
    depth = gate_depth.from_dict(fixture["execution_depth"])
    restored = depth.to_dict()
    assert restored["investigative_actions"] == fixture["execution_depth"]["investigative_actions"]
    assert restored["agent_messages"] == fixture["execution_depth"]["agent_messages"]
    assert restored["input_tokens"] == fixture["execution_depth"]["input_tokens"]


def test_from_dict_round_trips_single_shot_depth():
    original = gate_depth.single_shot_depth(1234, True)
    restored = gate_depth.from_dict(original.to_dict())
    assert restored == original


@pytest.mark.parametrize("bad", [None, "", [], 1, {}])
def test_from_dict_degrades_gracefully_on_absent_or_foreign_data(bad):
    """A record written before OI-1618 (or any non-dict) reconstructs to the
    unmeasured depth -- never treated as degenerate."""
    depth = gate_depth.from_dict(bad)
    assert depth.parsed is False
    assert gate_depth.is_degenerate(depth) is False


# ---------------------------------------------------------------------------
# 7. Kapotmaak-proef: lower the floor to 0 and prove the EXACT fixture shape
#    this suite refuses would then be booked as `pass` -- the refusal above
#    genuinely depends on MIN_INVESTIGATIVE_ACTIONS, not on something else in
#    the wiring around it. Restored automatically by monkeypatch teardown.
# ---------------------------------------------------------------------------


def test_kapotmaak_zero_floor_would_have_accepted_the_degenerate_fixture(tmp_path, monkeypatch):
    fixture = _fixture_record()
    depth = gate_depth.from_dict(fixture["execution_depth"])

    monkeypatch.setattr(gate_depth, "MIN_INVESTIGATIVE_ACTIONS", 0)
    payload = {
        "gate": "codex_gate", "pr_id": "1749", "status": "pass",
        "contract_hash": "wouldbeahash", "report_path": "/tmp/pr1749-report.md",
        "dispatch_id": "codex-gate-pr1749-kapotmaak", "blocking_findings": [],
    }
    out = tmp_path / "pr-1749-codex_gate-kapotmaak.json"
    record_terminal_result(
        gate="codex_gate", pr_id="1749", result_path=out, payload=payload,
        execution_depth=depth,
    )
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["status"] == "pass", (
        "with MIN_INVESTIGATIVE_ACTIONS=0 the degenerate fixture must be "
        "ACCEPTED as pass -- proving the real floor (not something else in "
        "the wiring) is what refuses it in test_fixture_degenerate_depth_"
        "refuses_a_mocked_pass above"
    )
