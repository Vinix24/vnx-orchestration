"""SP-7: the AGENTS.md chain budget in scripts/fleet_role_drift.py (ADR-058 section 8).

The budget is a reference metric. The CLI tests also pin that the exit code is the
one it was without the section.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import fleet_role_drift as frd

CANON = "# T0\n\nYou are T0.\n"


def _repo(root: Path) -> Path:
    t0 = root / ".claude" / "terminals" / "T0"
    t0.mkdir(parents=True)
    (t0 / "role-orchestrator.md").write_text(CANON)
    (t0 / "CLAUDE.md").write_text("@role-orchestrator.md\n")
    return root


def _put(root: Path, rel: str, size: int) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x" * size)


def _budget(root: Path) -> dict:
    """The budget section as the measurement reports it for the project."""
    return frd.measure_project(root, CANON)["budget"]


def _run(root: Path, tmp_path: Path, monkeypatch, capsys) -> tuple[int, dict]:
    canon = tmp_path / "canon.md"
    canon.write_text(CANON)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("VNX_HOME", str(tmp_path / "vnxhome"))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path / "state"))
    code = frd.main(["--canon", str(canon), "--project-dir", str(root), "--json"])
    report = json.loads(capsys.readouterr().out)
    return code, report["projects"][root.name]["budget"]


def test_33kb_agents_md_is_exceeded_with_all_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = _repo(tmp_path / "proj")
    _put(root, ".claude/terminals/T0/AGENTS.md", 33000)
    b = _budget(root)
    assert b["status"] == "exceeded"
    assert b["per_file_max"] == 33000
    assert b["aggregate_bytes"] == 33000
    assert b["cap"] == 32768
    assert b["cap_source"] == "kimi-cli 1.46.0 CHANGELOG.md:177"
    assert b["claim"] == "reference_metric_only"
    assert {"path": ".claude/terminals/T0/AGENTS.md", "bytes": 33000, "present": True} in b["files"]


def test_aggregate_of_32264_is_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = _repo(tmp_path / "proj")
    _put(root, "AGENTS.md", 1582)
    _put(root, ".claude/terminals/T0/AGENTS.md", 30682)
    b = _budget(root)
    assert b["aggregate_bytes"] == 32264
    assert b["per_file_max"] == 30682
    assert b["status"] == "ok"


def test_aggregate_over_cap_with_each_file_under_is_exceeded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = _repo(tmp_path / "proj")
    _put(root, "AGENTS.md", 20000)
    _put(root, ".claude/terminals/T0/AGENTS.md", 20000)
    b = _budget(root)
    assert b["per_file_max"] == 20000
    assert b["status"] == "exceeded"


def test_exactly_at_cap_is_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = _repo(tmp_path / "proj")
    _put(root, "AGENTS.md", 32768)
    assert _budget(root)["status"] == "ok"


def test_no_file_is_unmeasured_never_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = _repo(tmp_path / "proj")
    b = _budget(root)
    assert b["status"] == "unmeasured"
    assert b["aggregate_bytes"] == 0
    assert all(f["present"] is False and f["bytes"] is None for f in b["files"])


def test_kimi_level_is_counted_at_every_level(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = _repo(tmp_path / "proj")
    _put(root, ".kimi/AGENTS.md", 100)
    _put(root, ".claude/.kimi/AGENTS.md", 200)
    _put(root, ".claude/terminals/T0/.kimi/AGENTS.md", 33000)
    b = _budget(root)
    present = {f["path"]: f["bytes"] for f in b["files"] if f["present"]}
    assert present == {
        ".kimi/AGENTS.md": 100,
        ".claude/.kimi/AGENTS.md": 200,
        ".claude/terminals/T0/.kimi/AGENTS.md": 33000,
    }
    assert b["aggregate_bytes"] == 33300
    assert b["status"] == "exceeded"


def test_unreadable_file_is_unmeasured_not_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = _repo(tmp_path / "proj")
    _put(root, "AGENTS.md", 10)
    unreadable = root / ".claude" / "AGENTS.md"
    unreadable.write_bytes(b"y" * 5)
    unreadable.chmod(0)
    try:
        b = _budget(root)
    finally:
        unreadable.chmod(0o644)
    if b["status"] == "ok":
        pytest.skip("running as a user that can read mode-000 files")
    assert b["status"] == "unmeasured"


@pytest.mark.parametrize("size", [None, 1000, 33000])
def test_budget_never_changes_the_exit_code(tmp_path, monkeypatch, capsys, size):
    root = _repo(tmp_path / "proj")
    base_code, _ = _run(root, tmp_path, monkeypatch, capsys)
    if size is not None:
        _put(root, ".claude/terminals/T0/AGENTS.md", size)
    code, budget = _run(root, tmp_path, monkeypatch, capsys)
    assert code == base_code
    assert budget["status"] == {None: "unmeasured", 1000: "ok", 33000: "exceeded"}[size]


def test_text_render_names_the_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = _repo(tmp_path / "proj")
    line = frd._render_budget(_budget(root))
    assert "budget" in line and "UNMEASURED" in line
