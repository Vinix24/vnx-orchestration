"""tests/test_skill_applied_receipt.py — RS-3 (ADR-048 §3): the receipt proves
the skill reached the worker.

ADR-048 §3 fixes the contract in four rows:

    no ``skill``            -> all three fields absent
    body literally present  -> skill_applied=True + absolute SKILL.md path
    resolution failed       -> skill_applied=False + ``resolution_failed: <why>``
    reviewer role           -> skill_applied=False + ``reviewer_bound``

The verdict is a measurement on the final prompt (``skill_application.
verify_skill_applied``), never a flag the injection seam sets: the SKILL.md is
read at delivery time and its body (frontmatter stripped) must occur literally
in the prompt the lane handed the worker.

On the pre-change code (origin/main before RS-3) no receipt carries
``skill_applied`` at all — the two receipt tests below are the red run.
"""

from __future__ import annotations

import inspect
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import dispatch_envelope  # noqa: E402
import provider_dispatch  # noqa: E402
from dispatch_envelope import EnvelopeSpec, _AdapterResult  # noqa: E402
from envelope_types import EnvelopeSpec as _EnvelopeSpec  # noqa: E402,F401

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL = "refactor-safe"
SKILL_MD = REPO_ROOT / "skills" / SKILL / "SKILL.md"

_SKILL_KEYS = ("skill_applied", "skill_source_path", "skill_unapplied_reason")


def _skill_body() -> str:
    from skill_prefix import _strip_frontmatter

    return _strip_frontmatter(SKILL_MD.read_text(encoding="utf-8"))


def _verify(prompt, metadata, role, project_root=None):
    from skill_application import verify_skill_applied

    if project_root is None:
        return verify_skill_applied(prompt, metadata, role)
    return verify_skill_applied(prompt, metadata, role, project_root)


def _tmp_project_with_skill(tmp_path: Path, body: str = "STEP-ONE-MARKER\nSTEP-TWO-MARKER\n") -> Path:
    """A throwaway project root holding ``skills/<SKILL>/SKILL.md``."""
    root = tmp_path / "project"
    folder = root / "skills" / SKILL
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {SKILL}\n---\n\n{body}", encoding="utf-8"
    )
    return root


def _last_receipt(state_dir: Path) -> dict:
    path = state_dir / "t0_receipts.ndjson"
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return json.loads(lines[-1])


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# The four contract rows (verify_skill_applied)
# ---------------------------------------------------------------------------


def test_no_skill_returns_none_and_stamps_nothing():
    """A spec without ``skill`` is not a failed delivery — there is no verdict."""
    assert _verify("some prompt", {}, "backend-developer") is None
    assert _verify("some prompt", {"skill": None}, "backend-developer") is None
    assert _verify("some prompt", {"skill": "   "}, "backend-developer") is None


def test_resolved_skill_body_in_prompt_is_true_with_absolute_path(tmp_path):
    root = _tmp_project_with_skill(tmp_path)
    prompt = f"# SKILL: {SKILL}\n\nSTEP-ONE-MARKER\nSTEP-TWO-MARKER\n\n# USER INSTRUCTION:\n\ndo it"

    verdict = _verify(prompt, {"skill": SKILL}, "backend-developer", root)

    assert verdict is not None
    assert verdict.skill_applied is True
    assert verdict.skill_source_path == str((root / "skills" / SKILL / "SKILL.md").resolve())
    assert verdict.skill_unapplied_reason is None


def test_reviewer_with_a_skill_never_claims_arrival(tmp_path):
    """Row 4 — checked FIRST, even when the body would match the prompt."""
    root = _tmp_project_with_skill(tmp_path)
    prompt = "STEP-ONE-MARKER\nSTEP-TWO-MARKER\n"

    for role in ("review-gate", "plan-reviewer"):
        verdict = _verify(prompt, {"skill": SKILL}, role, root)
        assert verdict is not None
        assert verdict.skill_applied is False
        assert verdict.skill_source_path is None
        assert verdict.skill_unapplied_reason == "reviewer_bound"


def test_skill_that_stops_resolving_at_delivery_is_a_resolution_failure(tmp_path):
    """Row 3 — the folder is gone between staging and delivery (read at delivery)."""
    root = _tmp_project_with_skill(tmp_path)
    (root / "skills" / SKILL / "SKILL.md").unlink()

    verdict = _verify("STEP-ONE-MARKER\nSTEP-TWO-MARKER\n", {"skill": SKILL}, "backend-developer", root)

    assert verdict is not None
    assert verdict.skill_applied is False
    assert verdict.skill_source_path is None
    assert verdict.skill_unapplied_reason.startswith("resolution_failed:")


def test_invalid_slug_is_a_resolution_failure_not_a_path_escape(tmp_path):
    root = _tmp_project_with_skill(tmp_path)
    verdict = _verify("anything", {"skill": "../skills/" + SKILL}, "backend-developer", root)

    assert verdict is not None
    assert verdict.skill_applied is False
    assert verdict.skill_unapplied_reason.startswith("resolution_failed:")


def test_resolved_but_body_absent_is_a_resolution_failure(tmp_path):
    """Row 3 — a bare ``# SKILL:`` header (the seam's shape) proves nothing."""
    root = _tmp_project_with_skill(tmp_path)
    prompt = f"# SKILL: {SKILL}\n\n## Aanvullende resources\n\n# USER INSTRUCTION:\n\nrefactor"

    verdict = _verify(prompt, {"skill": SKILL}, "backend-developer", root)

    assert verdict.skill_applied is False
    assert verdict.skill_source_path is None
    assert verdict.skill_unapplied_reason == "resolution_failed: body_not_in_prompt"


def test_body_check_is_literal_not_normalised(tmp_path):
    """Frontmatter alone, or half the body, is not arrival."""
    root = _tmp_project_with_skill(tmp_path)

    frontmatter_only = _verify(
        f"---\nname: {SKILL}\n---\n\n", {"skill": SKILL}, "backend-developer", root,
    )
    half_body = _verify("STEP-ONE-MARKER\n", {"skill": SKILL}, "backend-developer", root)

    assert frontmatter_only.skill_applied is False
    assert frontmatter_only.skill_unapplied_reason == "resolution_failed: body_not_in_prompt"
    assert half_body.skill_applied is False
    assert half_body.skill_unapplied_reason == "resolution_failed: body_not_in_prompt"


# ---------------------------------------------------------------------------
# Envelope lane: the receipt carries the verdict
# ---------------------------------------------------------------------------


def _run_envelope_govern(tmp_path, *, instruction, role="backend-developer", skill=None):
    state = tmp_path / "state"
    data = tmp_path / "data"
    state.mkdir(parents=True)
    data.mkdir(parents=True)
    spec = EnvelopeSpec(
        dispatch_id="rs3-envelope",
        terminal_id="T1",
        provider="claude",
        model="sonnet",
        instruction=instruction,
        role=role,
        pr_id=None,
        state_dir=state,
        data_dir=data,
        skill=skill,
    )
    result = _AdapterResult(returncode=0, completion_text="all good", status="success")
    start = end = _now()
    with (
        patch("envelope_govern._archive_dispatch_events", return_value=(None, True)),
        patch("envelope_govern._clear_dispatch_events"),
        patch("provider_costs.emit_provider_cost"),
        patch("phantom_guard.record_phantom_if_any"),
        patch("phantom_guard.record_guard_error"),
    ):
        dispatch_envelope._govern(spec, result, start, end)
    return _last_receipt(state)


def test_envelope_receipt_stamps_skill_applied_true(tmp_path):
    """A spec with skill=refactor-safe and the SKILL.md body in the final prompt."""
    body = _skill_body()
    receipt = _run_envelope_govern(
        tmp_path, instruction=f"{body}\n\n---\n\nimplement the change", skill=SKILL,
    )

    assert receipt.get("skill_applied") is True, (
        "the envelope-lane receipt must prove the skill reached the worker, "
        f"got {receipt.get('skill_applied')!r}"
    )
    assert receipt.get("skill_source_path") == str(SKILL_MD.resolve())
    assert receipt.get("skill_unapplied_reason") is None


def test_envelope_receipt_omits_all_three_fields_without_a_skill(tmp_path):
    receipt = _run_envelope_govern(
        tmp_path, instruction="no skill in this spec at all", skill=None,
    )
    for key in _SKILL_KEYS:
        assert key not in receipt, f"{key} must be absent when the spec names no skill"


# ---------------------------------------------------------------------------
# Provider lane: the receipt carries the verdict
# ---------------------------------------------------------------------------


def _provider_args(**overrides):
    defaults = dict(
        provider="kimi",
        dispatch_id="rs3-provider",
        terminal_id="T1",
        instruction="Refactor the module.",
        model="kimi-k2",
        pr_id=None,
        dispatch_paths="",
        role="backend-developer",
        no_auto_commit=True,
        max_retries=1,
        gate="",
        deadline_seconds=900,
        skill=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _run_provider_emit(tmp_path, *, role="backend-developer", skill=SKILL):
    state = tmp_path / "state"
    data = tmp_path / "data"
    state.mkdir(parents=True)
    data.mkdir(parents=True)
    args = _provider_args(role=role, skill=skill)
    result = SimpleNamespace(
        completion_text="done",
        token_usage={"input": 1, "output": 1, "cache_hit": 0},
        returncode=0,
        error=None,
        session_id=None,
    )
    with (
        patch.object(provider_dispatch, "_resolve_state_dir", return_value=state),
        patch.object(provider_dispatch, "_resolve_data_dir", return_value=data),
        patch("subprocess_dispatch._build_intelligence_section", return_value=""),
        patch("dispatch_enricher.apply_repo_map_layer", side_effect=lambda instr, meta: instr),
    ):
        provider_dispatch._enrich_instruction(args)
    with (
        patch.object(provider_dispatch, "_resolve_state_dir", return_value=state),
        patch.object(provider_dispatch, "_resolve_data_dir", return_value=data),
        patch.object(provider_dispatch, "_book_dispatch_cost", return_value=(0.0, None, [])),
        patch.object(provider_dispatch, "_record_provider_metadata"),
        patch.object(provider_dispatch, "_maybe_record_provider_lane_cooldown"),
        patch.object(provider_dispatch, "_maybe_record_provider_reachability"),
        patch.object(provider_dispatch, "_build_frontmatter", return_value={}),
        patch.object(provider_dispatch, "_emit_marked_report"),
    ):
        provider_dispatch._emit_governance(
            args, "kimi", "kimi-k2", result, _now(), _now(), "success",
        )
    return _last_receipt(state)


def test_provider_receipt_stamps_skill_applied_true(tmp_path):
    receipt = _run_provider_emit(tmp_path, role="backend-developer", skill=SKILL)

    assert receipt.get("skill_applied") is True, (
        "the provider-lane receipt must prove the skill reached the worker, "
        f"got {receipt.get('skill_applied')!r}"
    )
    assert receipt.get("skill_source_path") == str(SKILL_MD.resolve())
    assert receipt.get("skill_unapplied_reason") is None


def test_provider_receipt_omits_all_three_fields_without_a_skill(tmp_path):
    receipt = _run_provider_emit(tmp_path, role="backend-developer", skill=None)
    for key in _SKILL_KEYS:
        assert key not in receipt, f"{key} must be absent when the spec names no skill"


# ---------------------------------------------------------------------------
# Every delivery route passes the seam — pin it (no lane_without_seam route)
# ---------------------------------------------------------------------------


def test_every_delivery_route_passes_the_skill_seam():
    """Envelope, provider and pool routes all hand ``skill`` to the one seam.

    If a route ever stops doing so, it must stamp
    ``resolution_failed: lane_without_seam`` instead of silently dropping the
    field; this pin makes the drop visible in CI first.
    """
    import subprocess_dispatch_internals.delivery as delivery

    assembly = inspect.getsource(delivery._assemble_instruction)
    assert "_inject_skill_context" in assembly
    assert '"skill": skill' in assembly

    pool_runner = (REPO_ROOT / "scripts" / "lib" / "pool_worker_runner.py").read_text(
        encoding="utf-8"
    )
    assert "--skill" in pool_runner and "skill=skill" in pool_runner

    provider_source = inspect.getsource(provider_dispatch._enrich_instruction)
    assert '"skill"' in provider_source and "_inject_skill_context" in provider_source
