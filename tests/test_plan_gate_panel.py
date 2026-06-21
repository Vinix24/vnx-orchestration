"""tests/test_plan_gate_panel.py — the PM plan-first gate.

Covers the pure logic (verdict parsing, the panel pass/fail rule, panel
orchestration with an injected dispatcher) and the DB-level blocker lifecycle
(seed -> derived_status=blocked -> resolve -> derived_status unblocked) that the
promote-lock reads. Real model dispatch is out of scope here — the panel takes an
injectable dispatcher so the rule is tested without a live provider.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_MIGRATIONS = Path(__file__).resolve().parent.parent / "schemas" / "migrations"
for p in (str(_LIB), str(_SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

import schema_migration  # noqa: E402
import tracks  # noqa: E402
import track_reconciler  # noqa: E402
import planning_cli  # noqa: E402
import plan_gate_panel as pgp  # noqa: E402


# --------------------------------------------------------------------------
# parse_verdict
# --------------------------------------------------------------------------

def _report(verdict_json: str) -> str:
    return f"# review\n\nsome prose\n\n```{pgp.VERDICT_FENCE}\n{verdict_json}\n```\n"


def test_parse_verdict_clean_pass():
    out = pgp.parse_verdict(_report('{"verdict": "pass", "blocking_findings": [], "rationale": "ok"}'))
    assert out["verdict"] == "pass"
    assert out["parse_error"] is False
    assert out["rationale"] == "ok"


def test_parse_verdict_block_with_findings():
    out = pgp.parse_verdict(_report('{"verdict": "block", "blocking_findings": ["no rollback", "ssrf"], "rationale": "unsafe"}'))
    assert out["verdict"] == "block"
    assert out["blocking_findings"] == ["no rollback", "ssrf"]
    assert out["parse_error"] is False


def test_parse_verdict_missing_block_is_failsafe_revise():
    out = pgp.parse_verdict("# review\n\nlooks fine to me, ship it\n")
    assert out["verdict"] == "revise"
    assert out["parse_error"] is True


def test_parse_verdict_malformed_json_is_failsafe_revise():
    out = pgp.parse_verdict(_report('{verdict: pass,,,}'))
    assert out["verdict"] == "revise"
    assert out["parse_error"] is True


def test_parse_verdict_unknown_verdict_is_failsafe_revise():
    out = pgp.parse_verdict(_report('{"verdict": "approve"}'))
    assert out["verdict"] == "revise"
    assert out["parse_error"] is True


def test_parse_verdict_last_block_wins():
    text = _report('{"verdict": "block"}') + _report('{"verdict": "pass"}')
    assert pgp.parse_verdict(text)["verdict"] == "pass"


def test_parse_verdict_empty_report():
    out = pgp.parse_verdict("")
    assert out["verdict"] == "revise"
    assert out["parse_error"] is True


# --------------------------------------------------------------------------
# apply_panel_rule
# --------------------------------------------------------------------------

def _r(label, verdict, dispatched=True, parse_error=False):
    return pgp.PanelistResult(
        label=label, provider="x", verdict=verdict,
        dispatched=dispatched, parse_error=parse_error,
    )


def test_rule_unanimous_pass():
    d = pgp.apply_panel_rule([_r("a", "pass"), _r("b", "pass"), _r("c", "pass")])
    assert d["decision"] == "PASS"


def test_rule_one_revise_folds_to_pass():
    d = pgp.apply_panel_rule([_r("a", "pass"), _r("b", "pass"), _r("c", "revise")])
    assert d["decision"] == "PASS"
    assert "dissent" in d["rationale"]


def test_rule_two_revise_blocks():
    d = pgp.apply_panel_rule([_r("a", "pass"), _r("b", "revise"), _r("c", "revise")])
    assert d["decision"] == "REVISE"
    assert d["revise_count"] == 2


def test_rule_any_block_revises():
    d = pgp.apply_panel_rule([_r("a", "pass"), _r("b", "pass"), _r("c", "block")])
    assert d["decision"] == "REVISE"
    assert d["block_count"] == 1


def test_rule_infra_fail_cannot_pass():
    # two pass + one panelist that never returned a verdict -> not certifiable
    d = pgp.apply_panel_rule([_r("a", "pass"), _r("b", "pass"), _r("c", "revise", dispatched=False)])
    assert d["decision"] == "REVISE"
    assert "no readable verdict" in d["rationale"]


# --------------------------------------------------------------------------
# run_panel with an injected dispatcher (no live model)
# --------------------------------------------------------------------------

def _fake_dispatcher(verdict_by_provider):
    def _dispatch(provider, model_arg, instruction, dispatch_id):
        return _report(verdict_by_provider[provider])
    return _dispatch


def test_run_panel_all_pass(tmp_path):
    doc = tmp_path / "plan.md"
    doc.write_text("## Problem\n## Approach\n", encoding="utf-8")
    disp = _fake_dispatcher({
        "claude": '{"verdict": "pass"}',
        "kimi": '{"verdict": "pass"}',
        "litellm:zai": '{"verdict": "pass"}',
    })
    out = pgp.run_panel(doc, track_id="feat-x", project_id="p1", dispatcher=disp)
    assert out["decision"] == "PASS"
    assert len(out["panelists"]) == 3


def test_run_panel_one_block_revises(tmp_path):
    doc = tmp_path / "plan.md"
    doc.write_text("## Problem\n", encoding="utf-8")
    disp = _fake_dispatcher({
        "claude": '{"verdict": "pass"}',
        "kimi": '{"verdict": "block", "blocking_findings": ["unsafe"]}',
        "litellm:zai": '{"verdict": "pass"}',
    })
    out = pgp.run_panel(doc, track_id="feat-x", project_id="p1", dispatcher=disp)
    assert out["decision"] == "REVISE"


def test_run_panel_dispatch_exception_is_no_verdict(tmp_path):
    doc = tmp_path / "plan.md"
    doc.write_text("## Problem\n", encoding="utf-8")

    def _disp(provider, model_arg, instruction, dispatch_id):
        if provider == "kimi":
            raise RuntimeError("kimi cli not installed")
        return _report('{"verdict": "pass"}')

    out = pgp.run_panel(doc, track_id="feat-x", project_id="p1", dispatcher=_disp)
    assert out["decision"] == "REVISE"  # cannot pass with a missing voice
    kimi = next(p for p in out["panelists"] if p["label"] == "kimi")
    assert kimi["dispatched"] is False
    assert "kimi cli not installed" in kimi["error"]


# --------------------------------------------------------------------------
# DB lifecycle: seed -> blocked -> resolve -> unblocked
# --------------------------------------------------------------------------

def _bootstrap(tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS dispatches (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "dispatch_id TEXT NOT NULL, project_id TEXT NOT NULL DEFAULT 'vnx-dev', "
        "state TEXT NOT NULL DEFAULT 'queued', terminal_id TEXT, track TEXT, "
        "priority TEXT DEFAULT 'P2', pr_ref TEXT, gate TEXT, "
        "attempt_count INTEGER NOT NULL DEFAULT 0, bundle_path TEXT, "
        "created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
        "updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
        "expires_after TEXT, metadata_json TEXT DEFAULT '{}', "
        "UNIQUE(dispatch_id, project_id))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS coordination_events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "event_id TEXT, event_type TEXT, entity_type TEXT, entity_id TEXT, from_state TEXT, "
        "to_state TEXT, actor TEXT, reason TEXT, metadata_json TEXT, occurred_at TEXT, project_id TEXT)"
    )
    conn.commit()
    for version, filename in [
        (22, "0022_track_layer.sql"),
        (24, "0024_tracks_tenant_scoping.sql"),
        (27, "0027_planning_horizon_and_deliverable_view.sql"),
        (28, "0028_tracks_derived_status.sql"),
        (30, "0030_track_oi_resolved_at.sql"),
    ]:
        sql = (_MIGRATIONS / filename).read_text(encoding="utf-8")
        schema_migration.apply_script_if_below(conn, version, sql)
        conn.commit()
    conn.close()
    return state_dir


def _derived(state_dir: Path, track_id: str, pid: str):
    t = tracks.get_track(state_dir, track_id, pid)
    return t["derived_status"] if t else None


def test_objective_add_auto_seeds_blocker_and_blocks(tmp_path):
    state_dir = _bootstrap(tmp_path)
    import argparse
    args = argparse.Namespace(
        track_id="feat-pg", title="t", goal_state="shipped", horizon="now",
        priority=None, project_id="p1", state_dir=str(state_dir), json=False,
    )
    assert planning_cli.cmd_objective_add(args) == 0
    # born plan-gated: an open OI-PLAN blocker -> derived_status blocked
    assert _derived(state_dir, "feat-pg", "p1") == "blocked"


def test_seed_then_resolve_unblocks(tmp_path):
    state_dir = _bootstrap(tmp_path)
    tracks.create_track(state_dir, "feat-r", "p1", "t", "shipped", phase="queued")
    assert planning_cli._seed_plan_blocker(state_dir, "feat-r", "p1") is True
    assert _derived(state_dir, "feat-r", "p1") == "blocked"
    # plan gate passed -> resolve -> reconciler clears the block
    assert planning_cli._resolve_plan_blocker(state_dir, "feat-r", "p1") is True
    assert _derived(state_dir, "feat-r", "p1") != "blocked"


def test_resolve_when_no_blocker_returns_false(tmp_path):
    state_dir = _bootstrap(tmp_path)
    tracks.create_track(state_dir, "feat-n", "p1", "t", "shipped", phase="queued")
    assert planning_cli._resolve_plan_blocker(state_dir, "feat-n", "p1") is False


def test_seed_is_tenant_scoped(tmp_path):
    state_dir = _bootstrap(tmp_path)
    tracks.create_track(state_dir, "feat-s", "p1", "t", "shipped", phase="queued")
    tracks.create_track(state_dir, "feat-s", "p2", "t", "shipped", phase="queued")
    assert planning_cli._seed_plan_blocker(state_dir, "feat-s", "p1") is True
    # p1 blocked, p2 untouched (ADR-007 tenant isolation)
    assert _derived(state_dir, "feat-s", "p1") == "blocked"
    assert _derived(state_dir, "feat-s", "p2") != "blocked"


# --------------------------------------------------------------------------
# codex-review hardening (2026-06-21): fail-safe + re-seed + report integrity
# --------------------------------------------------------------------------

def test_rule_parse_error_blocks_pass():
    # two clean passes + one panelist whose verdict could not be parsed: a missing
    # signal must not fold into PASS (codex finding 1).
    d = pgp.apply_panel_rule([_r("a", "pass"), _r("b", "pass"), _r("c", "revise", parse_error=True)])
    assert d["decision"] == "REVISE"
    assert "no readable verdict" in d["rationale"]


def test_run_panel_garbled_verdict_blocks_pass(tmp_path):
    doc = tmp_path / "plan.md"
    doc.write_text("## Problem\n", encoding="utf-8")

    def _disp(provider, model_arg, instruction, dispatch_id):
        if provider == "litellm:zai":
            return "# review\n\nlooks good, but no verdict block emitted\n"
        return _report('{"verdict": "pass"}')

    out = pgp.run_panel(doc, track_id="feat-x", project_id="p1", dispatcher=_disp)
    assert out["decision"] == "REVISE"
    glm = next(p for p in out["panelists"] if p["provider"] == "litellm:zai")
    assert glm["parse_error"] is True


def test_read_report_rejects_foreign_path(tmp_path):
    did = "plan-gate-feat-opus-abc"
    good = tmp_path / "unified_reports" / f"{did}.md"
    good.parent.mkdir(parents=True)
    good.write_text("good report", encoding="utf-8")
    other = tmp_path / "other.md"
    other.write_text("WRONG report", encoding="utf-8")

    # a foreign Report: line is ignored; only {dispatch_id}.md is honoured
    assert pgp._read_report(tmp_path, did, f"Report: {other}\nReport: {good}\n") == "good report"
    # foreign-only stderr -> falls back to the deterministic path
    assert pgp._read_report(tmp_path, did, f"Report: {other}\n") == "good report"
    # foreign-only + no deterministic file -> None (never the wrong file)
    assert pgp._read_report(None, did, f"Report: {other}\n") is None


def test_reseed_after_resolve_reblocks(tmp_path):
    state_dir = _bootstrap(tmp_path)
    tracks.create_track(state_dir, "feat-rs", "p1", "t", "shipped", phase="queued")
    assert planning_cli._seed_plan_blocker(state_dir, "feat-rs", "p1") is True
    assert _derived(state_dir, "feat-rs", "p1") == "blocked"
    assert planning_cli._resolve_plan_blocker(state_dir, "feat-rs", "p1") is True
    assert _derived(state_dir, "feat-rs", "p1") != "blocked"
    # mid-flight plan change: re-seeding must re-block the previously-passed track
    assert planning_cli._seed_plan_blocker(state_dir, "feat-rs", "p1") is True
    assert _derived(state_dir, "feat-rs", "p1") == "blocked"


def test_seed_noop_when_resolved_at_absent(tmp_path):
    # pre-0030 schema (derived_status present, resolved_at absent): seeding would
    # create a blocker this gate could never clear, so it must no-op (codex finding 3).
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS dispatches (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "dispatch_id TEXT NOT NULL, project_id TEXT NOT NULL DEFAULT 'vnx-dev', "
        "state TEXT NOT NULL DEFAULT 'queued', terminal_id TEXT, track TEXT, "
        "priority TEXT DEFAULT 'P2', pr_ref TEXT, gate TEXT, "
        "attempt_count INTEGER NOT NULL DEFAULT 0, bundle_path TEXT, "
        "created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
        "updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
        "expires_after TEXT, metadata_json TEXT DEFAULT '{}', "
        "UNIQUE(dispatch_id, project_id))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS coordination_events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "event_id TEXT, event_type TEXT, entity_type TEXT, entity_id TEXT, from_state TEXT, "
        "to_state TEXT, actor TEXT, reason TEXT, metadata_json TEXT, occurred_at TEXT, project_id TEXT)"
    )
    conn.commit()
    for version, filename in [
        (22, "0022_track_layer.sql"),
        (24, "0024_tracks_tenant_scoping.sql"),
        (27, "0027_planning_horizon_and_deliverable_view.sql"),
        (28, "0028_tracks_derived_status.sql"),
    ]:  # NB: 0030 (resolved_at) deliberately NOT applied
        schema_migration.apply_script_if_below(conn, version, (_MIGRATIONS / filename).read_text(encoding="utf-8"))
        conn.commit()
    conn.close()
    tracks.create_track(state_dir, "feat-old", "p1", "t", "shipped", phase="queued")
    assert planning_cli._seed_plan_blocker(state_dir, "feat-old", "p1") is False
    assert _derived(state_dir, "feat-old", "p1") != "blocked"
