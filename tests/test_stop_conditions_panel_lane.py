"""OI-1800: panel-lane attempts do not count toward provider_exhausted for a build or gate dispatch."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import dispatch_cli
import stop_conditions
from stop_conditions import (
    PANEL_LANE_DISPATCH_PREFIXES,
    CheckStatus,
    check_provider_exhausted,
    is_panel_lane_dispatch,
    run_all_checks,
)

_PANEL_IDS = (
    "plan-gate-trk-codex-1a2b3c4d",
    "plan-tiebreak-trk-5e6f7a8b",
    "panel-architecture-contrarian",
)


def _codex_dead(dispatch_id: str, ts: str) -> dict:
    return {
        "event_type": "task_complete", "provider": "codex", "status": "failed",
        "failure_class": "credit_exhausted", "dispatch_id": dispatch_id, "timestamp": ts,
    }


def _write(path: Path, records: list) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


@pytest.fixture
def panel_only_ledger(tmp_path) -> Path:
    records = [_codex_dead(did, f"2026-11-14T0{i}:00:00Z") for i, did in enumerate(_PANEL_IDS)]
    return _write(tmp_path / "t0_receipts.ndjson", records)


def test_panel_dispatch_still_counts_panel_attempts(panel_only_ledger):
    result = check_provider_exhausted(panel_only_ledger)
    assert result.status == CheckStatus.TRIGGERED


def test_build_dispatch_leaves_panel_attempts_out(panel_only_ledger):
    result = check_provider_exhausted(panel_only_ledger, exclude_panel_lane=True)
    assert result.status != CheckStatus.TRIGGERED
    assert result.evidence["panel_lane_excluded"] == {"codex": 3}


def test_build_lane_exhaustion_still_triggers_when_panel_excluded(tmp_path):
    records = [_codex_dead(f"dlv-build-{i}", f"2026-11-14T0{i}:00:00Z") for i in range(3)]
    records.append(_codex_dead("plan-gate-trk-codex-99", "2026-11-14T05:00:00Z"))
    result = check_provider_exhausted(_write(tmp_path / "r.ndjson", records), exclude_panel_lane=True)
    assert result.status == CheckStatus.TRIGGERED
    assert result.evidence["panel_lane_excluded"] == {"codex": 1}


def test_panel_success_no_longer_breaks_build_streak(tmp_path):
    records = [_codex_dead("dlv-a", "2026-11-14T01:00:00Z"), _codex_dead("dlv-b", "2026-11-14T02:00:00Z")]
    records.append({**_codex_dead("panel-sweep-verify", "2026-11-14T03:00:00Z"), "status": "success"})
    records.append(_codex_dead("dlv-c", "2026-11-14T04:00:00Z"))
    path = _write(tmp_path / "r.ndjson", records)
    assert check_provider_exhausted(path, exclude_panel_lane=True).status == CheckStatus.TRIGGERED


def test_run_all_checks_threads_the_flag(tmp_path, panel_only_ledger, monkeypatch):
    monkeypatch.setattr(stop_conditions, "check_main_ci_red", lambda *a, **k: stop_conditions._clear("main_ci_red", "x"))
    monkeypatch.setattr(stop_conditions, "check_gh_auth_dead", lambda *a, **k: stop_conditions._clear("gh_auth_dead", "x"))
    state = panel_only_ledger.parent
    by_id = {r.check_id: r for r in run_all_checks(state_dir=state, project_root=tmp_path, write_halt=False)}
    assert by_id["provider_exhausted"].status == CheckStatus.TRIGGERED
    by_id = {
        r.check_id: r
        for r in run_all_checks(state_dir=state, project_root=tmp_path, write_halt=False, exclude_panel_lane=True)
    }
    assert by_id["provider_exhausted"].status != CheckStatus.TRIGGERED


@pytest.mark.parametrize("value", [None, "", "   ", 7, ["panel-x"], "dlv-panel-x", "Plan-gate-x", "kimi-gate-x"])
def test_non_panel_ids_are_not_panel_lane(value):
    assert is_panel_lane_dispatch(value) is False


@pytest.mark.parametrize("did", _PANEL_IDS)
def test_panel_ids_are_panel_lane(did):
    assert is_panel_lane_dispatch(did) is True


def test_record_without_dispatch_id_stays_counted(tmp_path):
    records = [{k: v for k, v in _codex_dead("x", f"2026-11-14T0{i}:00:00Z").items() if k != "dispatch_id"} for i in range(3)]
    result = check_provider_exhausted(_write(tmp_path / "r.ndjson", records), exclude_panel_lane=True)
    assert result.status == CheckStatus.TRIGGERED
    assert result.evidence["panel_lane_excluded"] == {}


# Every site that mints a panel-lane dispatch id. In the panel modules every
# minted id must carry a known prefix; planning_cli.py also mints build ids
# (dlv-), so only its plan-* ids are checked there.
_MINTING_FILES = {
    "scripts/lib/deliberation_panel.py": "",
    "scripts/lib/plan_gate_panel.py": "",
    "scripts/lib/plan_gate_tiebreaker.py": "",
    "scripts/lib/panel_leg_cost.py": "",
    "scripts/planning_cli.py": "plan",
}
_MINT_RE = re.compile(r'(?:did\s*=\s*|dispatch_id\s*=\s*|return\s+|^\s*[a-z_]+_prompt,\s*)f"([a-z][a-z-]*?-)\{', re.M)


def test_every_minting_site_uses_a_known_prefix():
    minted = []
    for rel, only in _MINTING_FILES.items():
        text = (REPO / rel).read_text(encoding="utf-8")
        minted += [(rel, m.group(1)) for m in _MINT_RE.finditer(text) if m.group(1).startswith(only)]
    assert len(minted) >= 9, minted
    unknown = [(rel, p) for rel, p in minted if not p.startswith(PANEL_LANE_DISPATCH_PREFIXES)]
    assert not unknown, f"panel-lane id prefix not in PANEL_LANE_DISPATCH_PREFIXES: {unknown}"


def _fake_results(excluded):
    exhausted = stop_conditions._clear("provider_exhausted", "x", per_provider={}, panel_lane_excluded=excluded)
    return [exhausted]


def test_door_prints_one_exclusion_line(tmp_path, monkeypatch, capsys):
    seen = {}

    def fake_run(**kwargs):
        seen.update(kwargs)
        return _fake_results({"codex": 3})

    monkeypatch.setattr(stop_conditions, "run_all_checks", fake_run)
    assert dispatch_cli._check_stop_conditions_verdict(state_dir=tmp_path, dispatch_id="dlv-build-1") is None
    assert seen["exclude_panel_lane"] is True
    lines = [line for line in capsys.readouterr().err.splitlines() if "panel-lane" in line]
    assert len(lines) == 1 and "left out 3 panel-lane attempt(s) (codex=3)" in lines[0]


def test_door_counts_panel_attempts_for_a_panel_dispatch(tmp_path, monkeypatch, capsys):
    seen = {}

    def fake_run(**kwargs):
        seen.update(kwargs)
        return _fake_results({})

    monkeypatch.setattr(stop_conditions, "run_all_checks", fake_run)
    dispatch_cli._check_stop_conditions_verdict(state_dir=tmp_path, dispatch_id="plan-gate-trk-codex-1")
    assert seen["exclude_panel_lane"] is False
    assert "panel-lane" not in capsys.readouterr().err


@pytest.mark.parametrize("dispatch_id,blocked", [("dlv-codex-build-1", False), ("plan-gate-trk-codex-2", True)])
def test_door_end_to_end_codex_build_vs_panel(panel_only_ledger, monkeypatch, dispatch_id, blocked):
    monkeypatch.setattr(stop_conditions, "check_main_ci_red", lambda *a, **k: stop_conditions._clear("main_ci_red", "x"))
    monkeypatch.setattr(stop_conditions, "check_gh_auth_dead", lambda *a, **k: stop_conditions._clear("gh_auth_dead", "x"))
    verdict = dispatch_cli._check_stop_conditions_verdict(
        state_dir=panel_only_ledger.parent, dry_run=True, dispatch_id=dispatch_id,
        relevant_spec_providers={"codex"}, relevant_gates={"codex_gate"},
    )
    assert (verdict is not None and verdict.severity == "blocking") is blocked
