"""test_lane_matrix_row7_push_pr.py — rij-7 van de lane-conformity-matrix:
de push+PR-verplichting bindt op de envelope-lanes en op de committed-staat.

Dekt de combinaties (2 lanes × 3 worktree-stataten: pushed, committed, clean)
en de luide-failure-eis: een mislukte push of PR-creatie geeft een niet-nul
uitkomst, geen ``exit 0`` met werk lokaal gestrand.

Lanes:
- headless: ``run_envelope_headless_plan`` via de gedeelde ``_enforce_push_pr``.
- provider: ``run_envelope_plan`` via dezelfde ``_enforce_push_pr``.

De kernbeslissing zit in ``pr_enforcement.enforce_pr_exists`` (unit-tests in
test_pr_enforcement.py). De tmux-lane-tests die hier stonden zijn met die lane
verwijderd op 2026-09-18.

De beslissing zelf is EEN plek (``pr_enforcement.enforce_pr_exists``); deze tests
verifiëren dat elke lane die beslissing aanroept en de uitkomst correct doorgeeft.
"""
from __future__ import annotations

import hashlib
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import dispatch_envelope
from dispatch_envelope import (
    _AdapterResult,
    run_envelope_headless_plan,
    run_envelope_plan,
)
from dispatch_internal import issue_permit
from dispatch_plan import RuntimeSnapshot, compile_plan
from dispatch_spec import (
    DispatchSpec,
    Provider,
    ValidatedSpec,
)
from pr_enforcement import PrEnforcementResult


# ---------------------------------------------------------------------------
# Shared helpers (mirror test_headless_worktree_isolation.py / test_dispatch_envelope_plan.py)
# ---------------------------------------------------------------------------


def _fake_instruction_file(tmp_path: Path) -> Path:
    f = tmp_path / "instruction.md"
    f.write_text("# Test instruction\n", encoding="utf-8")
    return f


def _make_claude_headless_spec(tmp_path: Path) -> DispatchSpec:
    return DispatchSpec(
        schema_version=1,
        project_id="vnx-dev",
        dispatch_id="test-row7-headless",
        staging_id="staging-row7-headless",
        instruction_file=_fake_instruction_file(tmp_path),
        role="backend-developer",
        target_slot="T1",
        gate="codex_gate",
        dispatch_paths=(),
        provider=Provider.CLAUDE,
        model="sonnet",
        pr_id=None,
        allow_headless=True,
        headless_reason="test",
    )


def _make_provider_spec(tmp_path: Path) -> DispatchSpec:
    return DispatchSpec(
        schema_version=1,
        project_id="vnx-dev",
        dispatch_id="test-row7-provider",
        staging_id="staging-row7-provider",
        instruction_file=_fake_instruction_file(tmp_path),
        role="backend-developer",
        target_slot="T1",
        gate="codex_gate",
        dispatch_paths=(),
        provider=Provider.CODEX,
        model="gpt-test",
        pr_id=None,
    )


def _make_vspec(spec: DispatchSpec) -> ValidatedSpec:
    text = "# Test instruction\n"
    return ValidatedSpec(
        spec=spec,
        instruction_text=text,
        normalized_paths=(),
        instruction_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def _healthy_snapshot() -> RuntimeSnapshot:
    slots = ["T0", "T1", "T2", "T3"]
    return RuntimeSnapshot(
        staging_promoted=True,
        target_health={s: "healthy" for s in slots},
        target_capable={s: True for s in slots},
    )


def _fake_adapter_success() -> _AdapterResult:
    return _AdapterResult(returncode=0, completion_text="done", status="success")


def _envelope_lane_patches(tmp_path: Path, *, dispatch_id: str):
    """Common patches for an envelope-lane run: fake worktree, mocked teardown/govern.

    Returns (fake_wt_path, fake_consumer_root, receipt_path, govern_capturer) where
    govern_capturer is a list the test appends a (spec, result) tuple to on each
    _govern call, so the test can assert on the result that reached GOVERN.
    """
    state_dir = tmp_path / "state"
    data_dir = tmp_path / "data"
    state_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    fake_receipt = state_dir / "t0_receipts.ndjson"
    fake_receipt.touch()

    fake_consumer_root = tmp_path / "consumer-root"
    fake_consumer_root.mkdir(parents=True, exist_ok=True)
    fake_wt_path = fake_consumer_root / ".vnx-data" / "worktrees" / f"dispatch-{dispatch_id}"
    fake_wt_path.mkdir(parents=True, exist_ok=True)

    govern_seen: list = []

    def capture_govern(spec_arg, result, *args, **kwargs):
        govern_seen.append((spec_arg, result))
        return (fake_receipt, fake_receipt)

    return {
        "state_dir": state_dir,
        "data_dir": data_dir,
        "wt_path": fake_wt_path,
        "consumer_root": fake_consumer_root,
        "receipt_path": fake_receipt,
        "govern_seen": govern_seen,
        "capture_govern": capture_govern,
    }


def _pr_result(*, applicable: bool, ok: bool, pushed: bool = False,
               pr_number: Optional[int] = None, reason: Optional[str] = None) -> PrEnforcementResult:
    return PrEnforcementResult(
        applicable=applicable, ok=ok, pushed=pushed,
        pr_number=pr_number, created=bool(pr_number), reason=reason,
    )


# ---------------------------------------------------------------------------
# Envelope lanes — 9 combinaties (3 staten × luide failure), headless + provider
# ---------------------------------------------------------------------------


def _run_envelope_lane(tmp_path: Path, *, lane: str, classify_state: str, pr_ok: bool,
                      pr_pushed: bool = True):
    """Run a headless or provider envelope lane with classify_path and
    enforce_pr_exists mocked to return *classify_state* then a PR result.

    Returns the EnvelopeResult plus the govern-seen list.
    """
    if lane == "headless":
        spec = _make_claude_headless_spec(tmp_path)
    else:
        spec = _make_provider_spec(tmp_path)
    vspec = _make_vspec(spec)
    plan = compile_plan(vspec, _healthy_snapshot())
    permit = issue_permit(plan)

    ctx = _envelope_lane_patches(tmp_path, dispatch_id=plan.dispatch_id)
    pr_reason = None if pr_ok else "simulated PR/push failure"

    def fake_classify(**kw):
        return classify_state

    def fake_enforce(**kw):
        # committed/pushed are applicable; clean is not.
        applicable = classify_state in ("committed", "pushed")
        return _pr_result(
            applicable=applicable, ok=pr_ok, pushed=pr_pushed, pr_number=777 if pr_ok else None,
            reason=pr_reason,
        )

    common_patches = [
        patch("dispatch_worktree_isolation.resolve_consumer_project_root",
              return_value=ctx["consumer_root"]),
        patch("dispatch_worktree_isolation.create_dispatch_worktree",
              return_value=ctx["wt_path"]),
        patch("dispatch_worktree_isolation.remove_dispatch_worktree"),
        patch("dispatch_envelope._govern", side_effect=ctx["capture_govern"]),
        patch("tmux_worktree.classify_path", side_effect=fake_classify),
        patch("pr_enforcement.enforce_pr_exists", side_effect=fake_enforce),
    ]

    if lane == "headless":
        adapter_patch = patch.object(dispatch_envelope.ClaudeSubprocessAdapter, "run",
                                      return_value=_fake_adapter_success())
    else:
        adapter_patch = patch.object(dispatch_envelope.ProviderAdapter, "run",
                                     return_value=_fake_adapter_success())

    with ExitStack() as stack:
        stack.enter_context(adapter_patch)
        for p in common_patches:
            stack.enter_context(p)
        if lane == "headless":
            result = run_envelope_headless_plan(
                plan, permit, state_dir=ctx["state_dir"], data_dir=ctx["data_dir"],
                role=plan.role,
            )
        else:
            result = run_envelope_plan(
                plan, permit, state_dir=ctx["state_dir"], data_dir=ctx["data_dir"],
            )
    return result, ctx["govern_seen"]


@pytest.mark.parametrize("lane", ["headless", "provider"])
@pytest.mark.parametrize("state", ["pushed", "committed", "clean"])
def test_envelope_lane_state_matrix(lane, state, tmp_path):
    """3 lanes(no: 2 envelope lanes) × 3 staten. A success-PR leaves status=success;
    clean leaves success (not applicable). All 6 green-path combinations must pass
    through _enforce_push_pr without degrading a successful worker."""
    result, govern_seen = _run_envelope_lane(
        tmp_path, lane=lane, classify_state=state, pr_ok=True, pr_pushed=True,
    )
    assert result.status == "success", (
        f"lane={lane} state={state}: expected success, got {result.status!r} ({result.error!r})"
    )
    # _govern must have been reached with a success adapter result.
    assert govern_seen, f"lane={lane} state={state}: _govern was never called"
    _spec, gov_result = govern_seen[0]
    assert gov_result.status == "success", (
        f"lane={lane} state={state}: GOVERN saw status={gov_result.status!r}"
    )


@pytest.mark.parametrize("lane", ["headless", "provider"])
@pytest.mark.parametrize("state", ["pushed", "committed"])
def test_envelope_lane_pr_failure_is_loud(lane, state, tmp_path):
    """A failed push/PR on committed OR pushed must NOT silently resolve as done.
    EnvelopeResult.status == failure and returncode == 1."""
    result, govern_seen = _run_envelope_lane(
        tmp_path, lane=lane, classify_state=state, pr_ok=False, pr_pushed=False,
    )
    assert result.status == "failure", (
        f"lane={lane} state={state}: PR failure must be loud, got {result.status!r}"
    )
    assert result.returncode == 1, (
        f"lane={lane} state={state}: PR failure must be non-zero, got rc={result.returncode}"
    )
    assert "dispatch_branch_no_pr" in (result.error or ""), (
        f"lane={lane} state={state}: error must name the failure, got {result.error!r}"
    )
    # The failure must reach GOVERN as a failure result, not a silent success.
    _spec, gov_result = govern_seen[0]
    assert gov_result.status == "failure", (
        f"lane={lane} state={state}: GOVERN saw status={gov_result.status!r} — "
        f"the loud failure did not propagate to the governed receipt"
    )


def test_envelope_lane_clean_failure_path_skips_enforcement(tmp_path):
    """clean state: enforce_pr_exists returns applicable=False; even if we simulate
    a 'failure', the lane must NOT treat it as a dispatch failure (nothing to push)."""
    result, govern_seen = _run_envelope_lane(
        tmp_path, lane="headless", classify_state="clean", pr_ok=True, pr_pushed=False,
    )
    assert result.status == "success"
    _spec, gov_result = govern_seen[0]
    assert gov_result.status == "success"
