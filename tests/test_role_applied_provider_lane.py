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
  6. (round-3 gate fix) only the RESOLVED, highest-priority source's content may
     evidence role application — terminal fallback content must NOT stamp
     role_applied=True when an existing agents/<role>/CLAUDE.md is absent from
     the final prompt (RED on round-2 code).
  7. the genuine positive case still returns True after the round-3 fix — the
     probe is not made stricter until it never says yes (OI-893).
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
from role_application import _validate_slug, verify_role_applied

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
    # _govern moved to envelope_govern.py (dispatch-monolith-split, PR-4 of 6) —
    # its internal calls to _archive_dispatch_events/_clear_dispatch_events now
    # resolve against envelope_govern's own globals, not dispatch_envelope's.
    # These patches MUST bind (asserted below): if they didn't, the REAL
    # _clear_dispatch_events would truncate the live event stream for
    # spec.terminal_id under whatever VNX_DATA_DIR is ambient — run this test
    # file only with VNX_DATA_DIR pointed at tmp_path/a throwaway dir, never
    # against the real store.
    with (
        patch("envelope_govern._archive_dispatch_events", return_value=(None, True)) as mock_archive,
        patch("envelope_govern._clear_dispatch_events") as mock_clear,
        patch("provider_costs.emit_provider_cost"),
        patch("phantom_guard.record_phantom_if_any"),
        patch("phantom_guard.record_guard_error"),
    ):
        dispatch_envelope._govern(spec, result, start, end)
    mock_archive.assert_called_once_with(spec.terminal_id, spec.dispatch_id)
    mock_clear.assert_called_once_with(spec.terminal_id, spec.dispatch_id)
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


# ---------------------------------------------------------------------------
# 6. round-3 gate fix: only the RESOLVED (highest-priority) source may evidence
#    role application — a lower-priority tier's presence must not stamp True
# ---------------------------------------------------------------------------


def test_role_applied_false_when_resolved_source_absent_but_lower_tier_present():
    """agents/quality-engineer/CLAUDE.md EXISTS but is absent from the final
    prompt, while terminal-fallback (T1/CLAUDE.md) content IS present.
    role_applied must be False.

    RED on dispatch-20260801-w10 (round-2 code): the first pass accepted content
    from ANY candidate, so the terminal fallback stamped role_applied=True even
    though the role's own source never reached the worker — the exact
    false-positive the round-3 gate flagged.
    """
    terminal_body = (REPO_ROOT / ".claude" / "terminals" / "T1" / "CLAUDE.md").read_text()
    agents_body = (REPO_ROOT / "agents" / "quality-engineer" / "CLAUDE.md").read_text()
    # Sanity: the two bodies are distinct, otherwise the test is vacuous.
    assert "Quality Engineer Agent" in agents_body
    assert "Backend Developer Agent" in terminal_body

    final_prompt = f"{terminal_body}\n\n---\n\nimplement the change"

    verdict = verify_role_applied(final_prompt, "T1", "quality-engineer")

    assert verdict.role_applied is False
    assert verdict.tier == "agents"
    assert verdict.reason is not None
    assert "absent from the final prompt" in verdict.reason
    assert "terminal" in verdict.reason  # the lower-tier presence is recorded


def test_role_applied_true_when_resolved_source_present():
    """The genuine positive case still holds after the round-3 fix: when the
    resolved (highest-priority) source's content IS in the final prompt,
    role_applied must be True — the fix must not make the probe never say yes.
    """
    agents_body = (REPO_ROOT / "agents" / "quality-engineer" / "CLAUDE.md").read_text()
    final_prompt = f"{agents_body}\n\n---\n\nimplement the change"

    verdict = verify_role_applied(final_prompt, "T1", "quality-engineer")

    assert verdict.role_applied is True
    assert verdict.tier == "agents"
    assert verdict.reason is None


# ---------------------------------------------------------------------------
# 7. path-traversal hardening (OI-932): role/terminal_id slugs must not contain
#    '..' or path separators that would escape the intended directory trees
# ---------------------------------------------------------------------------


def test_candidate_sources_rejects_path_traversal_in_role():
    """role='../../.claude/terminals/T0' must NOT resolve to a file outside
    agents/ / .claude/skills/ / prompts/roles/.

    RED on current (unpatched) code: _candidate_sources pastes the raw role
    string into Path components, so the '..' segments escape the intended
    directories and a CLAUDE.md outside the role tree can be read.
    """
    verdict = verify_role_applied(
        "some prompt body content that does not match anything",
        "T1",
        "../../.claude/terminals/T0",
    )
    assert verdict.role_applied is False
    assert verdict.tier == "none", (
        f"expected tier='none' (validation rejected the slug before tier resolution), "
        f"got tier='{verdict.tier}'"
    )
    assert verdict.reason is not None
    # The reason must mention the validation failure, not a "resolved" tier.
    assert "path" in verdict.reason.lower() or "slug" in verdict.reason.lower() or (
        "invalid" in verdict.reason.lower()
    ), f"reason must mention path/slug validation, got: {verdict.reason}"


def test_candidate_sources_rejects_path_traversal_in_terminal_id():
    """terminal_id='../../agents/quality-engineer' must NOT resolve to a file
    outside .claude/terminals/.

    RED on current (unpatched) code: the terminal tier escapes into agents/.
    """
    verdict = verify_role_applied(
        "some prompt body content that does not match anything",
        "../../agents/quality-engineer",
        "nonexistent-role-xyz",
    )
    assert verdict.role_applied is False
    assert verdict.tier == "none", (
        f"expected tier='none' (validation rejected the slug before tier resolution), "
        f"got tier='{verdict.tier}'"
    )
    assert verdict.reason is not None
    assert "path" in verdict.reason.lower() or "slug" in verdict.reason.lower() or (
        "invalid" in verdict.reason.lower()
    ), f"reason must mention path/slug validation, got: {verdict.reason}"


def test_candidate_sources_accepts_valid_slugs():
    """Valid role/terminal_id slugs (no '..', no path separators) must still work
    and resolve normally — the validation must not reject legitimate values."""
    agents_body = (REPO_ROOT / "agents" / "quality-engineer" / "CLAUDE.md").read_text()
    final_prompt = f"{agents_body}\n\n---\n\nimplement the change"

    verdict = verify_role_applied(final_prompt, "T1", "quality-engineer")

    assert verdict.role_applied is True
    assert verdict.tier == "agents"
    assert verdict.reason is None


# ---------------------------------------------------------------------------
# 8. _validate_slug unit tests — direct edge-case coverage
# ---------------------------------------------------------------------------


class TestValidateSlug:
    """Direct unit tests for the slug validation guard (OI-932)."""

    def test_accepts_hyphenated_role(self):
        _validate_slug("quality-engineer", "role")  # must not raise

    def test_accepts_terminal_id(self):
        _validate_slug("T1", "terminal_id")  # must not raise

    def test_accepts_dotted_slug(self):
        _validate_slug("backend.developer", "role")  # must not raise

    def test_rejects_dot_dot(self):
        import pytest
        with pytest.raises(ValueError, match="path traversal"):
            _validate_slug("../../.claude/terminals/T0", "role")

    def test_rejects_forward_slash(self):
        import pytest
        with pytest.raises(ValueError, match="path separator"):
            _validate_slug("agents/quality-engineer", "role")

    def test_rejects_backslash(self):
        import pytest
        with pytest.raises(ValueError, match="path separator"):
            _validate_slug("agents\\quality-engineer", "role")

    def test_rejects_empty(self):
        import pytest
        with pytest.raises(ValueError, match="must not be empty"):
            _validate_slug("", "role")

    def test_rejects_whitespace_only(self):
        import pytest
        with pytest.raises(ValueError, match="must not be empty"):
            _validate_slug("   ", "role")
