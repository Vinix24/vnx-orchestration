"""test_role_applied_provider_lane.py — dispatch-20260801-w10.

The provider/envelope lanes previously assembled the final prompt WITHOUT the
role's own CLAUDE.md context — a dispatch staged with ``role=quality-engineer``
received byte-identical context to ``role=system-architect``. This dispatch:

  1. wires the SAME injector the tmux/subprocess lanes use
     (``_inject_skill_context``) into the provider/envelope assembly path, and
  2. adds a deterministic control (``role_application.verify_role_applied``)
     that verifies the resolved role source's content actually reached the
     assembled prompt, then stamps ``role_applied`` / ``role_tier`` /
     ``role_not_applied_reason`` on the receipt.

These tests pin the contract:
  1. provider-lane enrichment with role=X contains agents/X/CLAUDE.md content
     (red on origin/main — the old code never injected the role context).
  2. ``role_applied`` is correctly False when the role does not resolve (a
     control that can only say "yes" is no control — OI-893).
  3. a REAL assembly for two distinct roles produces visibly different context.
  4. roles that have a PromptAssembler prompt (backend-developer) keep using
     that path unchanged — the shared injector is not regressed.
  5. the envelope GOVERN path stamps role_applied / role_tier /
     role_not_applied_reason on the receipt.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import dispatch_envelope
import provider_dispatch
import skill_context
from dispatch_envelope import EnvelopeSpec, _AdapterResult
from role_application import verify_role_applied

REPO_ROOT = Path(__file__).resolve().parent.parent


def _make_args(role, instruction="implement the change", terminal_id="T1"):
    return SimpleNamespace(
        provider="litellm:deepseek",
        dispatch_id="w10-role-applied-test",
        terminal_id=terminal_id,
        instruction=instruction,
        model="deepseek-v4-pro",
        pr_id=None,
        dispatch_paths="",
        role=role,
        no_auto_commit=True,
        max_retries=1,
        gate="",
        deadline_seconds=900,
    )


def _enrich(args, tmp_path):
    """Run _enrich_instruction with state/data dirs redirected to tmp_path and
    intelligence suppressed so the test is deterministic (no central DB)."""
    with (
        patch.object(provider_dispatch, "_resolve_state_dir", return_value=tmp_path),
        patch.object(provider_dispatch, "_resolve_data_dir", return_value=tmp_path),
        patch("subprocess_dispatch._build_intelligence_section", return_value=""),
    ):
        return provider_dispatch._enrich_instruction(args)


# ---------------------------------------------------------------------------
# 1. role=X -> agents/X/CLAUDE.md content reaches the final prompt
# ---------------------------------------------------------------------------


def test_provider_lane_role_context_reaches_final_prompt(tmp_path):
    """A provider-lane dispatch with role='quality-engineer' must receive
    agents/quality-engineer/CLAUDE.md content in its enriched final prompt.

    RED on origin/main: the provider lane previously never called the injector,
    so no role context reached the worker at all.
    """
    args = _make_args("quality-engineer")
    with patch.object(skill_context, "_try_prompt_assembler", return_value=None):
        enriched = _enrich(args, tmp_path)

    agents_md = (REPO_ROOT / "agents" / "quality-engineer" / "CLAUDE.md").read_text()
    assert "Quality Engineer Agent" in enriched, (
        "agents/quality-engineer/CLAUDE.md content must reach the provider-lane final prompt"
    )
    assert "Quality Engineer Agent" in agents_md  # sanity: marker comes from the role file
    # The deterministic control agrees the role was applied, resolved via agents/.
    assert args._role_application.role_applied is True
    assert args._role_application.tier == "agents"


# ---------------------------------------------------------------------------
# 2. role_applied is correctly False when the role does not resolve
# ---------------------------------------------------------------------------


def test_role_applied_false_when_no_role_source(tmp_path):
    """A role with no agents/skills/prompts/terminal source must yield
    role_applied=False with a reason (the control can say "no")."""
    fake_root = tmp_path / "bare_repo"
    fake_root.mkdir()
    (fake_root / ".claude" / "terminals").mkdir(parents=True)  # no terminal file either

    verdict = verify_role_applied(
        "some assembled prompt body", "T1", "nonexistent-role-xyz",
        project_root=fake_root,
    )

    assert verdict.role_applied is False
    assert verdict.tier == "none"
    assert verdict.reason is not None
    assert "no role source resolved" in verdict.reason


def test_role_applied_false_when_resolved_but_content_absent(tmp_path):
    """A role that RESOLVES (agents/ exists) but whose content never made it into
    the prompt must yield role_applied=False with a reason — this is what catches
    a silent role-routing layer that only moves a parameter around."""
    verdict = verify_role_applied(
        "a prompt that does not mention the role at all",
        "T1",
        "quality-engineer",  # agents/quality-engineer/CLAUDE.md exists in this repo
    )

    assert verdict.role_applied is False
    assert verdict.tier == "agents"
    assert verdict.reason is not None
    assert "absent from the final prompt" in verdict.reason


# ---------------------------------------------------------------------------
# 3. real assembly: two distinct roles produce visibly different context
# ---------------------------------------------------------------------------


def test_real_assembly_two_roles_differ(tmp_path):
    """A real assembly for quality-engineer vs system-architect must differ —
    the proof that the role has an effect on the delivered prompt."""
    qe_args = _make_args("quality-engineer")
    sa_args = _make_args("system-architect")

    qe_prompt = _enrich(qe_args, tmp_path)
    sa_prompt = _enrich(sa_args, tmp_path)

    assert "Quality Engineer Agent" in qe_prompt
    assert "System Architect Agent" in sa_prompt
    assert qe_prompt != sa_prompt, (
        "two distinct roles must not receive byte-identical context"
    )
    assert qe_args._role_application.role_applied is True
    assert qe_args._role_application.tier == "agents"
    assert sa_args._role_application.role_applied is True
    assert sa_args._role_application.tier == "agents"


# ---------------------------------------------------------------------------
# 4. shared injector unchanged for PromptAssembler-backed roles
# ---------------------------------------------------------------------------


def test_prompt_assembler_role_still_used(tmp_path):
    """backend-developer has a PromptAssembler prompt (prompts/roles/) — that path
    must keep working unchanged, reported as tier='prompt_assembler'."""
    args = _make_args("backend-developer")

    prompt = _enrich(args, tmp_path)

    assert "Backend Developer" in prompt  # from prompts/roles/backend-developer.md
    assert args._role_application.role_applied is True
    assert args._role_application.tier == "prompt_assembler"


# ---------------------------------------------------------------------------
# 5. receipt stamping via the envelope GOVERN path
# ---------------------------------------------------------------------------


def _run_envelope_govern(tmp_path, *, instruction, role):
    state = tmp_path / "state"
    state.mkdir()
    data = tmp_path / "data"
    (data / "unified_reports").mkdir(parents=True)
    spec = EnvelopeSpec(
        dispatch_id="w10-receipt-stamp",
        terminal_id="T1",
        provider="codex",
        model="gpt-test",
        instruction=instruction,
        role=role,
        pr_id=None,
        state_dir=state,
        data_dir=data,
    )
    result = _AdapterResult(returncode=0, completion_text="all good", status="success")
    start = end = datetime.now(timezone.utc)
    with (
        patch("dispatch_envelope._archive_dispatch_events", return_value=None),
        patch("dispatch_envelope._clear_dispatch_events"),
        patch("provider_costs.emit_provider_cost"),
        patch("phantom_guard.record_phantom_if_any"),
        patch("phantom_guard.record_guard_error"),
    ):
        dispatch_envelope._govern(spec, result, start, end)
    receipt_path = state / "t0_receipts.ndjson"
    assert receipt_path.exists(), "receipt must be emitted"
    lines = [ln for ln in receipt_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    receipt = json.loads(lines[-1])
    assert receipt["dispatch_id"] == "w10-receipt-stamp"
    return receipt


def test_envelope_receipt_stamps_role_applied_true(tmp_path):
    """When the resolved role source IS in the enriched instruction, the receipt
    stamps role_applied=True + role_tier='agents'."""
    role_body = (REPO_ROOT / "agents" / "quality-engineer" / "CLAUDE.md").read_text()
    receipt = _run_envelope_govern(
        tmp_path,
        instruction=f"{role_body}\n\n---\n\nimplement the change",
        role="quality-engineer",
    )
    assert receipt.get("role_applied") is True
    assert receipt.get("role_tier") == "agents"
    assert receipt.get("role_not_applied_reason") is None


def test_envelope_receipt_stamps_role_applied_false(tmp_path):
    """When the resolved role source is absent from the enriched instruction, the
    receipt stamps role_applied=False + role_tier + role_not_applied_reason."""
    receipt = _run_envelope_govern(
        tmp_path,
        instruction="no role context in this body at all",
        role="quality-engineer",
    )
    assert receipt.get("role_applied") is False
    assert receipt.get("role_tier") == "agents"
    assert receipt.get("role_not_applied_reason") is not None
