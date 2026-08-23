#!/usr/bin/env python3
"""Tests for api_subsystems — the subsystem cockpit HTTP handler (framework-status-audit-and-cockpit
PR-4).

Covers GET /api/operator/subsystems: rowset shape (union of CONFIG_REGISTRY_SUBSYSTEMS + the
canonical-flag-per-subsystem view of CONFIG_REGISTRY), health attachment from health_beacon
(VNX_DATA_DIR root, not VNX_STATE_DIR), and the 503 fail-open path when the registry is unavailable.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))
sys.path.insert(0, str(REPO / "dashboard"))

import config_registry as cr  # noqa: E402
import api_subsystems as api_sub  # noqa: E402
from health_beacon import HealthBeacon  # noqa: E402

PID = "vnx-dev"


def test_build_rows_returns_at_least_10_subsystems():
    rows = api_sub.build_rows(cr, PID)
    assert len(rows) >= 10
    subsystems = {r["subsystem"] for r in rows}
    assert "governance-enforcement-stack" in subsystems
    assert "phantom_guard" in subsystems  # flag-less kernel subsystem


def test_build_rows_no_duplicate_subsystems():
    rows = api_sub.build_rows(cr, PID)
    subsystems = [r["subsystem"] for r in rows]
    assert len(subsystems) == len(set(subsystems))


def test_build_rows_flag_backed_row_has_effective_value():
    # OI-1385: VNX_CI_GATE_REQUIRED (5 production read-sites) is now canonical for
    # governance-enforcement-stack, not VNX_GOVERNANCE_ENFORCED (0 read-sites, see
    # config_registry.py's read_site_wired=False). effective_value no longer comes from the
    # canonical flag's own registry value either -- it reads .vnx/governance_enforcement.yaml
    # directly (see test_governance_row_effective_value_reads_real_yaml_source for the isolated,
    # non-live-file version of this assertion).
    rows = api_sub.build_rows(cr, PID)
    governance = next(r for r in rows if r["subsystem"] == "governance-enforcement-stack")
    assert governance["flag"] == "VNX_CI_GATE_REQUIRED"
    assert governance["status"] == "ACTIVATE"
    assert governance["effective_value"] != "0", (
        "must not read the static registry bool -- see governance_enforcement_effective_value()"
    )


def test_build_rows_flag_less_row_has_no_flag():
    rows = api_sub.build_rows(cr, PID)
    phantom = next(r for r in rows if r["subsystem"] == "phantom_guard")
    assert phantom["flag"] is None
    assert phantom["status"] == "LIVE"
    assert phantom["effective_value"] is None


def test_attach_health_unknown_when_no_beacon(tmp_path):
    rows = [{"subsystem": "phantom_guard"}]
    api_sub._attach_health(rows, tmp_path)
    assert rows[0]["health"] == "unknown"
    assert rows[0]["last_signal"] == ""


def test_attach_health_reads_beacon(tmp_path):
    beacon = HealthBeacon(tmp_path, "phantom_guard", expected_interval_seconds=None)
    beacon.heartbeat_strict(status="ok", details={"signal": "zero duplicates"})
    rows = [{"subsystem": "phantom_guard"}]
    api_sub._attach_health(rows, tmp_path)
    assert rows[0]["health"] == "ok"
    assert rows[0]["last_signal"]


def test_attach_health_fail_beacon(tmp_path):
    beacon = HealthBeacon(tmp_path, "intelligence-self-learning-loop", expected_interval_seconds=None)
    beacon.heartbeat_strict(status="fail", details={"signal": "98% ignore rate"})
    rows = [{"subsystem": "intelligence-self-learning-loop"}]
    api_sub._attach_health(rows, tmp_path)
    assert rows[0]["health"] == "fail"


def test_operator_get_subsystems_returns_200_with_health(monkeypatch, tmp_path):
    beacon = HealthBeacon(tmp_path, "phantom_guard", expected_interval_seconds=None)
    beacon.heartbeat_strict(status="ok")
    monkeypatch.setattr(api_sub, "_resolve_data_dir", lambda: tmp_path)

    body, status = api_sub.operator_get_subsystems({}, project_id=PID)

    assert status == 200
    assert body["project_id"] == PID
    assert len(body["subsystems"]) >= 10
    phantom = next(r for r in body["subsystems"] if r["subsystem"] == "phantom_guard")
    assert phantom["health"] == "ok"
    governance = next(r for r in body["subsystems"] if r["subsystem"] == "governance-enforcement-stack")
    assert governance["health"] == "unknown"  # no beacon written for it


def test_operator_get_subsystems_unavailable_returns_503(monkeypatch):
    monkeypatch.setattr(api_sub, "_REGISTRY_AVAILABLE", False)
    body, status = api_sub.operator_get_subsystems({}, project_id=PID)
    assert status == 503
    assert body["subsystems"] == []
    assert "error" in body


# ---------------------------------------------------------------------------
# OI-1385: governance_enforcement_effective_value() — yaml-sourced, never the static bool
# ---------------------------------------------------------------------------


def test_governance_enforcement_effective_value_reads_mode_and_max_level(tmp_path):
    yaml_path = tmp_path / "governance_enforcement.yaml"
    yaml_path.write_text(
        "mode: standard\n"
        "checks:\n"
        "  a:\n"
        "    level: 1\n"
        "  b:\n"
        "    level: 3\n",
        encoding="utf-8",
    )
    assert api_sub.governance_enforcement_effective_value(yaml_path) == "standard:3"


def test_governance_enforcement_effective_value_off_mode(tmp_path):
    yaml_path = tmp_path / "governance_enforcement.yaml"
    yaml_path.write_text("mode: off\nchecks:\n  a:\n    level: 0\n", encoding="utf-8")
    assert api_sub.governance_enforcement_effective_value(yaml_path) == "off:0"


def test_governance_enforcement_effective_value_missing_file_is_unknown_not_zero(tmp_path):
    # PRD requirement (framework-status-audit-and-cockpit_PRD.md:89): an unreadable source must
    # never silently read as "off" -- that is the exact drift class this dispatch fixes.
    missing = tmp_path / "does-not-exist.yaml"
    assert api_sub.governance_enforcement_effective_value(missing) == "unknown"


def test_governance_enforcement_effective_value_malformed_file_is_unknown_not_zero(tmp_path):
    bad = tmp_path / "governance_enforcement.yaml"
    bad.write_text(":::not valid yaml:::\n  - [unterminated", encoding="utf-8")
    assert api_sub.governance_enforcement_effective_value(bad) == "unknown"


def test_build_rows_governance_row_uses_the_wired_yaml_path_constant(monkeypatch, tmp_path):
    """Integration: build_rows() reads through the module-level path constant, not a hardcoded
    path -- monkeypatching it changes the row's effective_value, proving the wiring is live."""
    yaml_path = tmp_path / "governance_enforcement.yaml"
    yaml_path.write_text("mode: strict\nchecks:\n  a:\n    level: 2\n", encoding="utf-8")
    monkeypatch.setattr(api_sub, "_GOVERNANCE_ENFORCEMENT_YAML", yaml_path)

    rows = api_sub.build_rows(cr, PID)

    governance = next(r for r in rows if r["subsystem"] == "governance-enforcement-stack")
    assert governance["effective_value"] == "strict:2"


def test_real_governance_enforcement_yaml_is_currently_standard_hard_mandatory():
    """Pins the OI-1385 dispatch's own measured ground truth (23-08): the committed
    .vnx/governance_enforcement.yaml is mode: standard with gate_before_next_feature and
    ci_green_required both at level 3 (hard_mandatory). If this ever drifts, re-verify the
    dispatch report's blast-radius claims before trusting them."""
    real_yaml = api_sub._GOVERNANCE_ENFORCEMENT_YAML
    assert real_yaml.exists(), f"expected {real_yaml} to exist"
    import yaml as _yaml
    raw = _yaml.safe_load(real_yaml.read_text(encoding="utf-8"))
    assert raw.get("mode") == "standard"
    checks = raw.get("checks", {})
    assert checks["gate_before_next_feature"]["level"] == 3
    assert checks["ci_green_required"]["level"] == 3


# ---------------------------------------------------------------------------
# OI-1385: the cockpit-vs-reality contradiction control
# ---------------------------------------------------------------------------
#
# "Bouw een controle die ROOD geeft wanneer de cockpit een subsysteem als geparkeerd of uit
# toont terwijl de echte afdwinging mandatory is." The control composes two independent reads
# (the real enforcement source, the cockpit row) and flags the case where they disagree.


def _yaml_is_blocking(config_path) -> bool:
    """True when a governance_enforcement.yaml-shaped config has at least one check at
    level>=2 (soft_mandatory or hard_mandatory)."""
    import governance_enforcer as ge
    enforcer = ge.GovernanceEnforcer()
    enforcer.load_config(config_path)
    return enforcer.effective_summary()["max_level"] >= 2


def _cockpit_shows_off(row) -> bool:
    """True when a cockpit row reads as parked/off to an operator glancing at it."""
    return row.get("effective_value") in (None, "", "0", "off")


def _contradicts(source_blocking: bool, cockpit_shows_off: bool) -> bool:
    return source_blocking and cockpit_shows_off


def test_contradiction_control_flags_the_pre_fix_shape():
    """Pin the exact OI-1385 shape: the real enforcement source is blocking (mode=standard,
    hard-mandatory checks) while the cockpit shows the pre-fix stale '0' -- this is the
    misreading a 23-08 morning measurement reported to the operator as 'enforcement is off'.
    The control must flag it as a contradiction; run against the REAL live yaml (not a fixture)
    so this documents the actual measured state, not a hypothetical."""
    source_blocking = _yaml_is_blocking(api_sub._GOVERNANCE_ENFORCEMENT_YAML)
    assert source_blocking is True, "expected the real committed yaml to currently be blocking"
    stale_row = {"subsystem": "governance-enforcement-stack", "effective_value": "0"}
    assert _contradicts(source_blocking, _cockpit_shows_off(stale_row)) is True


def test_contradiction_control_clears_when_source_and_cockpit_both_off(tmp_path):
    """Inverse case (required by the dispatch): a source that is genuinely off, with a cockpit
    that also shows off, is NOT a contradiction -- without this case the control could not be
    distinguished from one that is simply always red."""
    off_yaml = tmp_path / "governance_enforcement.yaml"
    off_yaml.write_text("mode: off\nchecks:\n  a:\n    level: 0\n", encoding="utf-8")
    source_blocking = _yaml_is_blocking(off_yaml)
    assert source_blocking is False
    off_row = {"subsystem": "some-other-subsystem", "effective_value": "0"}
    assert _contradicts(source_blocking, _cockpit_shows_off(off_row)) is False


def test_real_cockpit_row_no_longer_contradicts_real_yaml_source():
    """The regression pin for the fix itself (dispatch requirement: 'een test die op de HUIDIGE
    main ROOD is op GEDRAG'). build_rows()'s REAL governance-enforcement-stack row must not
    contradict the REAL governance_enforcement.yaml: RED before this dispatch's fix
    (effective_value was the static '0' bool while the yaml was blocking, confirmed by running
    this exact assertion against the unmodified files -- see the dispatch report); GREEN after
    (effective_value now reads the yaml directly via governance_enforcement_effective_value())."""
    source_blocking = _yaml_is_blocking(api_sub._GOVERNANCE_ENFORCEMENT_YAML)
    rows = api_sub.build_rows(cr, PID)
    row = next(r for r in rows if r["subsystem"] == "governance-enforcement-stack")
    assert not _contradicts(source_blocking, _cockpit_shows_off(row)), (
        f"cockpit row {row!r} contradicts the real enforcement source (blocking={source_blocking})"
    )
