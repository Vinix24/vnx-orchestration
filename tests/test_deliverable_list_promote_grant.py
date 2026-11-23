"""`vnx deliverable list --json` names per proposed row who can promote it (OI-2851)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
for _p in (_ROOT / "scripts" / "lib", _ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import actor_class  # noqa: E402
import planning_cli  # noqa: E402
import plan_gate_enforcement  # noqa: E402
import tracks as tracks_lib  # noqa: E402

Grant = plan_gate_enforcement.PromotionGrant


def _row(ref, status, track="trk-a", kind="pr"):
    return {
        "deliverable_ref": ref, "output_kind": kind, "track": track,
        "dispatch_count": 1, "derived_status": status, "last_activity": "t",
        "title": ref, "metadata": {},
    }


def _list_json(monkeypatch, capsys, records, *, opt_in, grant_for):
    calls: list[tuple[str, str]] = []

    def _grant(project_id, track_id):
        calls.append((project_id, track_id))
        return grant_for

    monkeypatch.setattr(planning_cli, "_resolve_state_dir", lambda _s: Path("/nonexistent"))
    monkeypatch.setattr(planning_cli, "_fetch_deliverable_records", lambda *a, **k: records)
    monkeypatch.setattr(planning_cli, "_promotion_grant", _grant)
    monkeypatch.setattr(tracks_lib, "track_autonomous_promotion_eligible", lambda *a, **k: opt_in)
    args = argparse.Namespace(state_dir="", project_id="vnx-dev", objective=None, all=False, json=True)
    assert planning_cli.cmd_deliverable_list(args) == 0
    return json.loads(capsys.readouterr().out), calls


def test_unsigned_grant_cannot_promote(monkeypatch, capsys):
    out, _ = _list_json(monkeypatch, capsys, [_row("pr:dlv-1", "proposed")],
                        opt_in=True, grant_for=Grant.PRESENT_UNSIGNED)
    assert out[0]["promote_grant"] == {
        "track_opt_in": True, "grant": "present_unsigned", "t0_can_promote": False,
    }


def test_verified_grant_on_opted_in_track_can_promote(monkeypatch, capsys):
    out, _ = _list_json(monkeypatch, capsys, [_row("pr:dlv-1", "proposed")],
                        opt_in=True, grant_for=Grant.VERIFIED)
    assert out[0]["promote_grant"] == {"track_opt_in": True, "grant": "verified", "t0_can_promote": True}


@pytest.mark.parametrize("grant", list(Grant))
def test_no_opt_in_never_promotes(monkeypatch, capsys, grant):
    out, _ = _list_json(monkeypatch, capsys, [_row("pr:dlv-1", "proposed")],
                        opt_in=False, grant_for=grant)
    assert out[0]["promote_grant"]["t0_can_promote"] is False
    assert out[0]["promote_grant"]["track_opt_in"] is False


@pytest.mark.parametrize("kind", sorted(tracks_lib.OPERATOR_ONLY_OUTPUT_KINDS))
def test_operator_only_kind_never_promotes(monkeypatch, capsys, kind):
    out, _ = _list_json(monkeypatch, capsys, [_row("x:dlv-1", "proposed", kind=kind)],
                        opt_in=True, grant_for=Grant.VERIFIED)
    assert out[0]["promote_grant"]["t0_can_promote"] is False


def test_ready_row_has_no_promote_grant(monkeypatch, capsys):
    out, calls = _list_json(monkeypatch, capsys, [_row("pr:dlv-1", "ready")],
                            opt_in=True, grant_for=Grant.VERIFIED)
    assert "promote_grant" not in out[0]
    assert calls == []


def test_row_without_track_is_absent(monkeypatch, capsys):
    out, calls = _list_json(monkeypatch, capsys, [_row("pr:dlv-1", "proposed", track=None)],
                            opt_in=True, grant_for=Grant.VERIFIED)
    assert out[0]["promote_grant"] == {"track_opt_in": False, "grant": "absent", "t0_can_promote": False}
    assert calls == []


def test_grant_lookup_runs_once_per_track(monkeypatch, capsys):
    rows = [_row("pr:dlv-1", "proposed"), _row("pr:dlv-2", "proposed")]
    out, calls = _list_json(monkeypatch, capsys, rows, opt_in=True, grant_for=Grant.VERIFIED)
    assert len(calls) == 1
    assert [r["promote_grant"]["t0_can_promote"] for r in out] == [True, True]


@pytest.mark.parametrize("opt_in", [True, False])
@pytest.mark.parametrize("grant", list(Grant))
@pytest.mark.parametrize("kind", ["pr", "learning"])
def test_listing_matches_promotion_refusal_for_t0(monkeypatch, opt_in, grant, kind):
    monkeypatch.setattr(actor_class, "derive", lambda: actor_class.T0)
    monkeypatch.setattr(planning_cli, "_promotion_grant", lambda *a: grant)
    monkeypatch.setattr(tracks_lib, "track_autonomous_promotion_eligible", lambda *a, **k: opt_in)
    refusal, _ = planning_cli._promotion_refusal(
        Path("/nonexistent"), "vnx-dev", {"output_kind": kind}, "trk-a",
    )
    view = planning_cli._promote_grant_view(Path("/nonexistent"), "vnx-dev", "trk-a", kind, {})
    assert view["t0_can_promote"] is (refusal is None)
