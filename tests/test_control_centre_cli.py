"""Tests for scripts/control_centre_cli.py (Wave 5 PR-5.5).

Validates:
- Registry loading and project enumeration
- dispatch command writes correct pending file
- kill command calls T0LifecycleManager.kill with lease token
- intel command calls IntelligenceAggregator.recommend_cross_project
- each command emits an audit event via StateAggregator.submit
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch, call

import pytest
import yaml

# Make repo root importable.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from scripts.control_centre_cli import (
    _CC_AUDIT_PROJECT,
    _build_intel_db_paths,
    _coord_db_path,
    _find_project,
    _get_active_lease,
    _load_registry,
    _project_vnx_data,
    _resolve_placeholders,
    _token_digest,
    _validate_project_id,
    build_parser,
    cmd_aggregate,
    cmd_dispatch,
    cmd_heartbeat,
    cmd_intel,
    cmd_kill,
    cmd_reap,
    cmd_status,
    main,
)
from scripts.lib.vnx_ids import PROJECT_ID_RE


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _write_registry(tmp_path: Path, projects: List[Dict[str, Any]]) -> Path:
    """Write a registry YAML to tmp_path and return its path."""
    registry_path = tmp_path / "projects.yaml"
    registry_path.write_text(
        yaml.dump({"projects": projects}), encoding="utf-8"
    )
    return registry_path


def _make_project(tmp_path: Path, project_id: str) -> Dict[str, Any]:
    """Create a minimal project directory structure with coord_db."""
    root = tmp_path / project_id
    root.mkdir(parents=True, exist_ok=True)
    coord_dir = root / ".vnx-data" / "state"
    coord_dir.mkdir(parents=True, exist_ok=True)
    return {
        "id": project_id,
        "root": str(root),
        "coord_db": ".vnx-data/state/runtime_coordination.db",
        "intel_db": ".vnx-data/state/quality_intelligence.db",
    }


def _seed_coord_db(coord_db: Path, project_id: str, lease_token: str, pid: int = 9999) -> None:
    """Create minimal terminal_leases table with one active T0 row."""
    coord_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(coord_db))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS terminal_leases (
            terminal_id         TEXT NOT NULL,
            project_id          TEXT NOT NULL DEFAULT '',
            state               TEXT NOT NULL DEFAULT 'released',
            generation          INTEGER NOT NULL DEFAULT 1,
            lease_token         TEXT,
            leased_at           TEXT,
            last_heartbeat_at   TEXT,
            released_at         TEXT,
            metadata_json       TEXT
        );
    """)
    meta = json.dumps({
        "pid": pid,
        "lifecycle_state": "RUNNING",
        "project_root": str(coord_db.parent.parent.parent),
        "lease_token": lease_token,
    })
    conn.execute(
        "INSERT INTO terminal_leases "
        "(terminal_id, project_id, state, generation, lease_token, "
        " leased_at, last_heartbeat_at, metadata_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "T0",
            project_id,
            "leased",
            1,
            lease_token,
            "2026-05-16T08:00:00.000+00:00",
            "2026-05-16T08:00:30.000+00:00",
            meta,
        ),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# test_status_lists_all_projects_from_registry
# ---------------------------------------------------------------------------


def test_status_lists_all_projects_from_registry(tmp_path: Path, capsys) -> None:
    """Status command returns one row per project in the registry (2 projects)."""
    proj_a = _make_project(tmp_path, "alpha-proj")
    proj_b = _make_project(tmp_path, "beta-proj")
    registry_path = _write_registry(tmp_path, [proj_a, proj_b])

    mock_agg = MagicMock()

    with (
        patch("scripts.control_centre_cli._make_aggregator", return_value=mock_agg),
        patch("scripts.control_centre_cli._repo_vnx_data", return_value=tmp_path / ".vnx-data"),
    ):
        parser = build_parser()
        args = parser.parse_args(["--registry", str(registry_path), "status"])
        rc = cmd_status(args)

    assert rc == 0
    out = capsys.readouterr().out
    assert "alpha-proj" in out
    assert "beta-proj" in out
    # Two project rows — check header + 2 data rows present
    lines = [l for l in out.splitlines() if l.strip() and not l.startswith("-")]
    assert len(lines) >= 3  # header + 2 project rows

    # Audit event was submitted
    mock_agg.submit.assert_called_once()
    submitted_update = mock_agg.submit.call_args[0][0]
    assert submitted_update.project_id == _CC_AUDIT_PROJECT
    assert submitted_update.event_type == "cc.status.requested"
    assert submitted_update.payload["project_count"] == 2


# ---------------------------------------------------------------------------
# test_dispatch_forwards_to_project_t0
# ---------------------------------------------------------------------------


def test_dispatch_forwards_to_project_t0(tmp_path: Path) -> None:
    """Dispatch command writes dispatch.json + instruction.md to project pending dir."""
    proj = _make_project(tmp_path, "vnx-dev")
    registry_path = _write_registry(tmp_path, [proj])

    mock_agg = MagicMock()

    with (
        patch("scripts.control_centre_cli._make_aggregator", return_value=mock_agg),
        patch("scripts.control_centre_cli._repo_vnx_data", return_value=tmp_path / ".vnx-data"),
    ):
        parser = build_parser()
        args = parser.parse_args([
            "--registry", str(registry_path),
            "dispatch",
            "--project", "vnx-dev",
            "--task", "Refactor authentication module",
        ])
        rc = cmd_dispatch(args)

    assert rc == 0

    project_root = Path(proj["root"])
    pending_dir = project_root / ".vnx-data" / "dispatches" / "pending"
    assert pending_dir.exists(), "pending/ directory not created"

    dispatch_dirs = list(pending_dir.iterdir())
    assert len(dispatch_dirs) == 1, "Expected exactly one dispatch directory"

    dispatch_json = dispatch_dirs[0] / "dispatch.json"
    instruction_md = dispatch_dirs[0] / "instruction.md"
    assert dispatch_json.exists(), "dispatch.json not written"
    assert instruction_md.exists(), "instruction.md not written"

    payload = json.loads(dispatch_json.read_text())
    assert payload["project_id"] == "vnx-dev"
    assert payload["terminal_id"] == "T0"
    assert payload["source"] == "control-centre"
    import re
    assert re.match(r"^cc-\d{8}-\d{6}-\d{6}-", payload["dispatch_id"]), (
        f"dispatch_id missing microsecond component: {payload['dispatch_id']!r}"
    )

    assert instruction_md.read_text(encoding="utf-8") == "Refactor authentication module"

    # Audit event emitted with correct project scope
    mock_agg.submit.assert_called_once()
    update = mock_agg.submit.call_args[0][0]
    assert update.project_id == "vnx-dev"
    assert update.event_type == "cc.dispatch.forwarded"


# ---------------------------------------------------------------------------
# test_kill_calls_t0_lifecycle_manager
# ---------------------------------------------------------------------------


def test_kill_calls_t0_lifecycle_manager(tmp_path: Path) -> None:
    """Kill command fetches active lease token and calls T0LifecycleManager.kill."""
    proj = _make_project(tmp_path, "sales-copilot")
    lease_token = "abc123-lease-token"
    coord_db = Path(proj["root"]) / ".vnx-data" / "state" / "runtime_coordination.db"
    _seed_coord_db(coord_db, "sales-copilot", lease_token, pid=12345)
    registry_path = _write_registry(tmp_path, [proj])

    mock_kill_result = MagicMock()
    mock_kill_result.signaled = True
    mock_kill_result.verified_dead = True
    mock_kill_result.lease_released = True
    mock_kill_result.escalated_to_sigkill = False
    mock_kill_result.duration_ms = 250
    mock_kill_result.error = None

    mock_mgr = MagicMock()
    mock_mgr.kill.return_value = mock_kill_result

    mock_agg = MagicMock()

    with (
        patch("scripts.control_centre_cli.T0LifecycleManager", return_value=mock_mgr),
        patch("scripts.control_centre_cli._make_aggregator", return_value=mock_agg),
        patch("scripts.control_centre_cli._repo_vnx_data", return_value=tmp_path / ".vnx-data"),
    ):
        parser = build_parser()
        args = parser.parse_args([
            "--registry", str(registry_path),
            "kill",
            "--project", "sales-copilot",
        ])
        rc = cmd_kill(args)

    assert rc == 0
    mock_mgr.kill.assert_called_once_with(
        "sales-copilot",
        lease_token=lease_token,
        source="control-centre",
    )
    # Audit event submitted
    mock_agg.submit.assert_called_once()
    update = mock_agg.submit.call_args[0][0]
    assert update.project_id == "sales-copilot"
    assert update.event_type == "cc.kill.requested"
    assert "token" not in update.payload, "raw lease token must not appear in audit payload"
    assert update.payload["token_digest"] == _token_digest(lease_token)


# ---------------------------------------------------------------------------
# test_intel_aggregates_cross_project_recommendations
# ---------------------------------------------------------------------------


def test_intel_aggregates_cross_project_recommendations(tmp_path: Path) -> None:
    """Intel command calls IntelligenceAggregator.recommend_cross_project and reports results."""
    proj_a = _make_project(tmp_path, "target-proj")
    proj_b = _make_project(tmp_path, "source-proj")
    registry_path = _write_registry(tmp_path, [proj_a, proj_b])

    from scripts.lib.intelligence_aggregator import CrossProjectRecommendation

    mock_recs = [
        CrossProjectRecommendation(
            source_project="source-proj",
            target_project="target-proj",
            pattern_id="gp-abc123",
            rationale="Pattern 'atomic writes' proven in 'source-proj' (5 uses, conf=0.85)",
            confidence=0.595,
        )
    ]

    mock_ia = MagicMock()
    mock_ia.recommend_cross_project.return_value = mock_recs
    mock_agg = MagicMock()

    with (
        patch("scripts.control_centre_cli.IntelligenceAggregator", return_value=mock_ia),
        patch("scripts.control_centre_cli._make_aggregator", return_value=mock_agg),
        patch("scripts.control_centre_cli._repo_vnx_data", return_value=tmp_path / ".vnx-data"),
    ):
        parser = build_parser()
        args = parser.parse_args([
            "--registry", str(registry_path),
            "intel",
            "--project", "target-proj",
        ])
        rc = cmd_intel(args)

    assert rc == 0
    mock_ia.recommend_cross_project.assert_called_once_with("target-proj")

    # Audit event submitted for the target project
    mock_agg.submit.assert_called_once()
    update = mock_agg.submit.call_args[0][0]
    assert update.project_id == "target-proj"
    assert update.event_type == "cc.intel.requested"
    assert update.payload["recommendation_count"] == 1


# ---------------------------------------------------------------------------
# test_each_command_emits_audit_event
# ---------------------------------------------------------------------------


def test_each_command_emits_audit_event(tmp_path: Path) -> None:
    """Every command calls state_aggregator.submit at least once."""
    proj = _make_project(tmp_path, "audit-proj")
    registry_path = _write_registry(tmp_path, [proj])

    commands_and_extra_args = [
        (["status"], {}),
        (["dispatch", "--project", "audit-proj", "--task", "do something"], {}),
        (["reap"], {}),
        (["aggregate"], {}),
    ]

    for cmd_args, _extra in commands_and_extra_args:
        mock_agg = MagicMock()

        with (
            patch("scripts.control_centre_cli._make_aggregator", return_value=mock_agg),
            patch("scripts.control_centre_cli._repo_vnx_data", return_value=tmp_path / ".vnx-data"),
            patch("scripts.control_centre_cli.T0LifecycleManager"),
            patch("scripts.control_centre_cli.IntelligenceAggregator"),
        ):
            full_args = ["--registry", str(registry_path)] + cmd_args
            rc = main(full_args)

        assert rc == 0, f"Command {cmd_args} returned non-zero: {rc}"
        assert mock_agg.submit.call_count >= 1, (
            f"Command {cmd_args} did not emit an audit event"
        )


# ---------------------------------------------------------------------------
# test_status_with_active_lease_shows_running_state
# ---------------------------------------------------------------------------


def test_status_with_active_lease_shows_running_state(tmp_path: Path, capsys) -> None:
    """Status shows RUNNING state when an active T0 lease exists in coord_db."""
    proj = _make_project(tmp_path, "running-proj")
    coord_db = Path(proj["root"]) / ".vnx-data" / "state" / "runtime_coordination.db"
    _seed_coord_db(coord_db, "running-proj", "my-token-xyz", pid=42000)
    registry_path = _write_registry(tmp_path, [proj])

    mock_agg = MagicMock()

    with (
        patch("scripts.control_centre_cli._make_aggregator", return_value=mock_agg),
        patch("scripts.control_centre_cli._repo_vnx_data", return_value=tmp_path / ".vnx-data"),
    ):
        parser = build_parser()
        args = parser.parse_args(["--registry", str(registry_path), "status"])
        rc = cmd_status(args)

    assert rc == 0
    out = capsys.readouterr().out
    assert "running-proj" in out
    assert "RUNNING" in out
    assert "42000" in out


# ---------------------------------------------------------------------------
# test_dispatch_unknown_project_returns_error
# ---------------------------------------------------------------------------


def test_dispatch_unknown_project_returns_error(tmp_path: Path) -> None:
    """Dispatch to an unknown project returns exit code 1."""
    proj = _make_project(tmp_path, "known-proj")
    registry_path = _write_registry(tmp_path, [proj])

    mock_agg = MagicMock()

    with (
        patch("scripts.control_centre_cli._make_aggregator", return_value=mock_agg),
        patch("scripts.control_centre_cli._repo_vnx_data", return_value=tmp_path / ".vnx-data"),
    ):
        parser = build_parser()
        args = parser.parse_args([
            "--registry", str(registry_path),
            "dispatch",
            "--project", "does-not-exist",
            "--task", "irrelevant",
        ])
        rc = cmd_dispatch(args)

    assert rc == 1
    mock_agg.submit.assert_not_called()


# ---------------------------------------------------------------------------
# Path-resolver regression tests (Legacy path gate CI fix)
# ---------------------------------------------------------------------------


def test_project_vnx_data_default_uses_resolver(tmp_path: Path) -> None:
    """_project_vnx_data without coord_db returns root/.vnx-data via resolver."""
    project = {"id": "x", "root": str(tmp_path)}
    result = _project_vnx_data(project)
    assert result == (tmp_path / ".vnx-data").resolve()


def test_project_vnx_data_explicit_coord_db(tmp_path: Path) -> None:
    """_project_vnx_data with explicit coord_db uses first path component."""
    project = {"id": "x", "root": str(tmp_path), "coord_db": "mydata/state/coord.db"}
    result = _project_vnx_data(project)
    assert result == tmp_path / "mydata"


def test_coord_db_path_default_uses_resolver(tmp_path: Path) -> None:
    """_coord_db_path without coord_db key builds path from resolve_state_dir."""
    project = {"id": "x", "root": str(tmp_path)}
    result = _coord_db_path(project)
    assert result == (tmp_path / ".vnx-data" / "state").resolve() / "runtime_coordination.db"


def test_coord_db_path_explicit(tmp_path: Path) -> None:
    """_coord_db_path with explicit coord_db uses it verbatim."""
    project = {"id": "x", "root": str(tmp_path), "coord_db": "custom/coord.db"}
    assert _coord_db_path(project) == tmp_path / "custom" / "coord.db"


def test_build_intel_db_paths_default_uses_resolver(tmp_path: Path) -> None:
    """_build_intel_db_paths without intel_db builds path from resolve_state_dir."""
    registry = [{"id": "x", "root": str(tmp_path)}]
    result = _build_intel_db_paths(registry)
    expected = (tmp_path / ".vnx-data" / "state").resolve() / "quality_intelligence.db"
    assert result["x"] == expected


def test_build_intel_db_paths_explicit(tmp_path: Path) -> None:
    """_build_intel_db_paths with explicit intel_db uses it verbatim."""
    registry = [{"id": "x", "root": str(tmp_path), "intel_db": "custom/intel.db"}]
    result = _build_intel_db_paths(registry)
    assert result["x"] == tmp_path / "custom" / "intel.db"


def test_no_hardcoded_legacy_path_in_cli_source() -> None:
    """control_centre_cli.py must not contain literal .vnx-data/state/ string."""
    _REPO_ROOT = Path(__file__).resolve().parent.parent
    src = (_REPO_ROOT / "scripts" / "control_centre_cli.py").read_text()
    assert ".vnx-data/state/" not in src


def test_no_hardcoded_legacy_path_in_yaml_example() -> None:
    """control_centre_projects.yaml.example must not contain literal .vnx-data/state/ string."""
    _REPO_ROOT = Path(__file__).resolve().parent.parent
    content = (_REPO_ROOT / "scripts" / "control_centre_projects.yaml.example").read_text()
    assert ".vnx-data/state/" not in content


# ---------------------------------------------------------------------------
# test_token_digest_properties
# ---------------------------------------------------------------------------


def test_token_digest_is_non_reversible_and_fixed_length() -> None:
    """_token_digest returns a 16-char hex string and is deterministic."""
    token = "super-secret-lease-token-abc"
    digest = _token_digest(token)
    assert len(digest) == 16
    assert all(c in "0123456789abcdef" for c in digest)
    assert _token_digest(token) == _token_digest(token)
    expected = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
    assert digest == expected


def test_token_digest_empty_token_returns_empty() -> None:
    """_token_digest returns empty string for empty input."""
    assert _token_digest("") == ""


def test_heartbeat_audit_uses_token_digest_not_raw_token(tmp_path: Path) -> None:
    """Heartbeat audit payload contains token_digest, not raw lease_token."""
    proj = _make_project(tmp_path, "hb-proj")
    lease_token = "raw-heartbeat-token-xyz"
    coord_db = Path(proj["root"]) / ".vnx-data" / "state" / "runtime_coordination.db"
    _seed_coord_db(coord_db, "hb-proj", lease_token, pid=55000)
    registry_path = _write_registry(tmp_path, [proj])

    mock_mgr = MagicMock()
    mock_mgr.heartbeat.return_value = True
    mock_agg = MagicMock()

    with (
        patch("scripts.control_centre_cli.T0LifecycleManager", return_value=mock_mgr),
        patch("scripts.control_centre_cli._make_aggregator", return_value=mock_agg),
        patch("scripts.control_centre_cli._repo_vnx_data", return_value=tmp_path / ".vnx-data"),
    ):
        parser = build_parser()
        args = parser.parse_args([
            "--registry", str(registry_path),
            "heartbeat",
            "--project", "hb-proj",
        ])
        rc = cmd_heartbeat(args)

    assert rc == 0
    mock_agg.submit.assert_called_once()
    update = mock_agg.submit.call_args[0][0]
    assert update.event_type == "cc.heartbeat.sent"
    assert "token" not in update.payload, "raw lease token must not appear in heartbeat audit payload"
    assert update.payload["token_digest"] == _token_digest(lease_token)


def test_dispatch_no_tmp_file_leftover(tmp_path: Path) -> None:
    """Atomic write leaves no .tmp file behind after successful dispatch."""
    proj = _make_project(tmp_path, "atomic-proj")
    registry_path = _write_registry(tmp_path, [proj])

    mock_agg = MagicMock()

    with (
        patch("scripts.control_centre_cli._make_aggregator", return_value=mock_agg),
        patch("scripts.control_centre_cli._repo_vnx_data", return_value=tmp_path / ".vnx-data"),
    ):
        parser = build_parser()
        args = parser.parse_args([
            "--registry", str(registry_path),
            "dispatch",
            "--project", "atomic-proj",
            "--task", "Atomic write test task",
        ])
        rc = cmd_dispatch(args)

    assert rc == 0
    project_root = Path(proj["root"])
    pending_dir = project_root / ".vnx-data" / "dispatches" / "pending"
    dispatch_dirs = list(pending_dir.iterdir())
    assert len(dispatch_dirs) == 1
    dispatch_dir = dispatch_dirs[0]
    # instruction.md must exist and contain correct content
    instruction_md = dispatch_dir / "instruction.md"
    assert instruction_md.exists()
    assert instruction_md.read_text(encoding="utf-8") == "Atomic write test task"
    # No .tmp files left behind
    tmp_files = list(dispatch_dir.glob("*.tmp"))
    assert tmp_files == [], f"Leftover .tmp files found: {tmp_files}"


# ---------------------------------------------------------------------------
# test_project_id_validation_rejects_path_traversal
# ---------------------------------------------------------------------------


def test_project_id_validation_rejects_path_traversal() -> None:
    """_validate_project_id raises ValueError for path-traversal and unsafe inputs."""
    invalid_inputs = [
        "../../etc",
        "evil/payload",
        "..",
        "",
        "../secret",
        "UPPERCASE",
        "has space",
        "dot.in.name",
        "/absolute",
        "a" * 33,        # exceeds 32-char limit
        "sales_copilot", # underscore not allowed
        "a",             # single char (needs 2-32)
        "1proj",         # must start with a letter
    ]
    for bad in invalid_inputs:
        with pytest.raises(ValueError, match="invalid project id"):
            _validate_project_id(bad)


# ---------------------------------------------------------------------------
# test_project_id_validation_accepts_valid
# ---------------------------------------------------------------------------


def test_project_id_validation_accepts_valid() -> None:
    """_validate_project_id passes for well-formed project IDs."""
    valid_inputs = ["vnx-dev", "sales-copilot", "proj01", "x1", "my-project-123"]
    for good in valid_inputs:
        result = _validate_project_id(good)
        assert result == good


# ---------------------------------------------------------------------------
# test_kill_emits_audit_on_failure
# ---------------------------------------------------------------------------


def test_kill_emits_audit_on_failure(tmp_path: Path) -> None:
    """Kill command emits audit event even when kill operation fails (ADR-005)."""
    proj = _make_project(tmp_path, "kill-fail-proj")
    lease_token = "fail-token-abc"
    coord_db = Path(proj["root"]) / ".vnx-data" / "state" / "runtime_coordination.db"
    _seed_coord_db(coord_db, "kill-fail-proj", lease_token, pid=77777)
    registry_path = _write_registry(tmp_path, [proj])

    mock_kill_result = MagicMock()
    mock_kill_result.signaled = False
    mock_kill_result.verified_dead = False
    mock_kill_result.lease_released = False
    mock_kill_result.escalated_to_sigkill = False
    mock_kill_result.duration_ms = None
    mock_kill_result.error = "SIGTERM failed: process not found"

    mock_mgr = MagicMock()
    mock_mgr.kill.return_value = mock_kill_result
    mock_agg = MagicMock()

    with (
        patch("scripts.control_centre_cli.T0LifecycleManager", return_value=mock_mgr),
        patch("scripts.control_centre_cli._make_aggregator", return_value=mock_agg),
        patch("scripts.control_centre_cli._repo_vnx_data", return_value=tmp_path / ".vnx-data"),
    ):
        parser = build_parser()
        args = parser.parse_args([
            "--registry", str(registry_path),
            "kill",
            "--project", "kill-fail-proj",
        ])
        rc = cmd_kill(args)

    assert rc == 1
    # Audit MUST be emitted even when kill fails
    mock_agg.submit.assert_called_once()
    update = mock_agg.submit.call_args[0][0]
    assert update.event_type == "cc.kill.requested"
    assert update.payload["success"] is False
    assert update.payload["error"] == "SIGTERM failed: process not found"
    assert update.payload["token_digest"] == _token_digest(lease_token)


# ---------------------------------------------------------------------------
# test_track_requires_project_flag
# ---------------------------------------------------------------------------


def test_track_requires_project_flag(tmp_path: Path) -> None:
    """Finding 3: track subcommand must exit with SystemExit when --project is missing."""
    registry_path = _write_registry(tmp_path, [])
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["--registry", str(registry_path), "track", "some-dispatch-id"])

    assert exc_info.value.code != 0, "--project omitted must exit non-zero"


# ---------------------------------------------------------------------------
# OI-1476: project_id regex alignment + yaml placeholder substitution
# ---------------------------------------------------------------------------


def test_project_id_regex_matches_state_aggregator() -> None:
    """CLI _validate_project_id and PROJECT_ID_RE share the same pattern as StateAggregator."""
    from scripts.aggregator.state_aggregator import _PROJECT_ID_RE as _SA_RE

    # Same compiled pattern
    assert PROJECT_ID_RE.pattern == _SA_RE.pattern, (
        f"Regex divergence: CLI={PROJECT_ID_RE.pattern!r} SA={_SA_RE.pattern!r}"
    )

    # Shared accept set
    accept = ["proj", "vnx-dev", "sales-copilot", "ab", "seocrawler-v2", "cc-system"]
    for pid in accept:
        assert PROJECT_ID_RE.match(pid), f"Should accept: {pid!r}"
        assert _validate_project_id(pid) == pid

    # Shared reject set
    reject = [
        "proj/x",       # slash
        "Project",      # uppercase
        "a",            # single char
        "has_under",    # underscore
        "1start",       # starts with digit
        "dot.name",     # dot
        "",             # empty
        "a" * 33,       # too long
    ]
    for pid in reject:
        assert not PROJECT_ID_RE.match(pid or ""), f"Should reject: {pid!r}"
        with pytest.raises(ValueError):
            _validate_project_id(pid)


def test_placeholders_resolved_to_absolute_paths(tmp_path: Path) -> None:
    """_load_registry resolves {root} and {state} to absolute paths."""
    root = tmp_path / "my-proj"
    root.mkdir()
    expected_state = root / ".vnx-data" / "state"

    # Write registry with {state} placeholders (mirrors the .example format)
    registry_path = tmp_path / "projects.yaml"
    registry_path.write_text(
        f"projects:\n"
        f"  - id: my-proj\n"
        f"    root: {root}\n"
        f'    coord_db: "{{state}}/runtime_coordination.db"\n'
        f'    intel_db: "{{state}}/quality_intelligence.db"\n',
        encoding="utf-8",
    )

    projects = _load_registry(registry_path)
    assert len(projects) == 1
    proj = projects[0]

    assert proj["coord_db"] == str(expected_state / "runtime_coordination.db"), (
        f"coord_db not resolved: {proj['coord_db']!r}"
    )
    assert proj["intel_db"] == str(expected_state / "quality_intelligence.db"), (
        f"intel_db not resolved: {proj['intel_db']!r}"
    )
    # Verify absolute — must not contain literal placeholder
    assert "{state}" not in proj["coord_db"]
    assert "{root}" not in proj["coord_db"]


def test_resolve_placeholders_both_tokens(tmp_path: Path) -> None:
    """_resolve_placeholders substitutes {root} and {state} independently."""
    root = tmp_path / "proj"
    assert _resolve_placeholders("{root}/custom.db", root) == str(root / "custom.db")
    assert _resolve_placeholders("{state}/coord.db", root) == str(
        root / ".vnx-data" / "state" / "coord.db"
    )
    # Literal relative paths pass through unchanged
    assert _resolve_placeholders("rel/path/coord.db", root) == "rel/path/coord.db"


def test_yaml_example_uses_placeholder_syntax() -> None:
    """yaml.example uses {state} placeholders, not <state> or hardcoded paths."""
    _REPO_ROOT = Path(__file__).resolve().parent.parent
    content = (_REPO_ROOT / "scripts" / "control_centre_projects.yaml.example").read_text()
    assert "<state>" not in content, "Old <state> placeholder still present"
    assert "{state}" in content, "New {state} placeholder not found"
    assert ".vnx-data/state/" not in content
