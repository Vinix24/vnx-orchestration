"""tests/test_oi1634_escalation_track_id.py — OI-1634: the escalation dispatch
never receives the rejected attempt's track_id.

Root cause, measured 2026-09-05: ``dispatch_bridge.stage_escalation_bundle``
already HAS a ``track_id`` parameter (OI-1632, PR #1774) with an explicit
comment that it "never guesses one from rejected_dispatch_id" — the caller
must supply it. That is designed behavior, not an omission. The defect sits
one level up: the SOLE production caller, ``dispatch_cli._maybe_stage_escalation``
(the real call at dispatch_cli.py:2590), never passed ``track_id`` at all. An
escalation continues the SAME dispatch chain as the rejected attempt, so it
should carry that attempt's track — instead it silently dropped it on every
climb.

This test drives the REAL caller (``dispatch_cli._maybe_stage_escalation``),
never a mock of ``stage_escalation_bundle``, and reads ``track_id`` back from
the staged ``dispatch-spec.json`` on disk — the same bytes the door -> receipt
chain copies. It also asserts the staged escalation bundle genuinely exists
(not an accidental empty-glob false negative) before trusting its contents.

RED on the pre-fix caller: the staged escalation spec's ``track_id`` is None
even though the rejected dispatch's own spec carried ``"trk-oi1634"``. GREEN
once the caller forwards ``spec.track_id`` through to ``stage_escalation_bundle``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import dispatch_cli  # noqa: E402

_REJECTED_ID = "20260905-090000-rejected-oi1634"
_TRACK_ID = "trk-oi1634"


def _force_kimi(monkeypatch, present: bool = True):
    """Deterministic kimi CLI presence (kimi-via-cli-only gate) — mirrors the
    helper in tests/test_dispatch_bridge_escalation.py so the real
    ``resolve_tier_route`` walk behaves the same regardless of the machine
    this test runs on."""
    import shutil as _shutil

    monkeypatch.setattr(
        _shutil,
        "which",
        (lambda name: "/usr/local/bin/kimi") if present else (lambda name: None),
    )


def _run_maybe_stage_escalation(tmp_path, monkeypatch, *, track_id):
    _force_kimi(monkeypatch, present=True)

    plan = SimpleNamespace(
        dispatch_id=_REJECTED_ID,
        provider="claude",
        tier_from="tier-low",
        tier_to=None,
        task_class=None,
    )
    result = SimpleNamespace(
        status="failure",
        error="upstream 500 internal error",
        completion_text="",
        returncode=1,
    )
    spec = SimpleNamespace(
        role="dev",
        target_slot="T1",
        project_id="p1",
        gate="",
        deadline_seconds=3600,
        base_ref="origin/main",
        track_id=track_id,
    )
    vspec = SimpleNamespace(
        spec=spec,
        instruction_text="original instruction text for the rejected attempt",
        normalized_paths=(),
    )
    state_dir = tmp_path / "state"
    data_dir = tmp_path

    dispatch_cli._maybe_stage_escalation(
        plan, result, vspec=vspec, state_dir=state_dir, data_dir=data_dir,
    )

    pending = data_dir / "dispatches" / "pending"
    staged = sorted(pending.glob("*/dispatch-spec.json")) if pending.exists() else []

    # Nul is eerst een meetfout: prove the search itself is sound (a bundle
    # DOES land under pending/ for this real call) before drawing any
    # conclusion from its contents. A missing bundle here would mean the
    # escalation was refused entirely (e.g. classify_failure_safe/escalate_tier
    # changed under us) — a different bug than a dropped track_id, and this
    # assertion is what would catch that distinction instead of silently
    # reading an empty list as "track_id is None".
    assert len(staged) == 1, (
        f"expected exactly one staged escalation bundle under {pending}, found "
        f"{len(staged)}: {staged} — the escalation call itself did not land, "
        "which is a different failure than a dropped track_id"
    )
    return json.loads(staged[0].read_text(encoding="utf-8"))


def test_escalation_from_a_tracked_dispatch_keeps_its_track(tmp_path, monkeypatch):
    """The real call at dispatch_cli.py:2590: a rejected dispatch whose own
    spec carries track_id="trk-oi1634" must hand that track to its escalation
    followup — the chain continues, the track does not reset to None."""
    payload = _run_maybe_stage_escalation(tmp_path, monkeypatch, track_id=_TRACK_ID)

    assert payload["track_id"] == _TRACK_ID


def test_escalation_from_an_untracked_dispatch_stays_untracked(tmp_path, monkeypatch):
    """Companion negative case: a rejected dispatch with no track must not
    fabricate one on escalation — the absence itself stays a countable,
    honest None rather than stage_escalation_bundle silently guessing."""
    payload = _run_maybe_stage_escalation(tmp_path, monkeypatch, track_id=None)

    assert payload["track_id"] is None
