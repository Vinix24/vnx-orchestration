"""config-een-oppervlak N1a: 14 deleted names, constants and always-on behaviour.

The deleted names have zero string-literal reads left in scripts/, vnx_cli/ and
dashboard/. Each one used to move a limit, a path or a switch; with the old value
exported, the code now behaves as it does with nothing set.
"""
from __future__ import annotations

import ast
import functools
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

DELETED = (
    "VNX_DAILY_METRICS_SETTLE_HOURS",
    "VNX_DAILY_METRICS_CATCHUP_DAYS",
    "VNX_DAILY_METRICS_FRESH_HOURS",
    "VNX_DAILY_METRICS_GH_LIMIT",
    "VNX_DAILY_METRICS_GH_MAX_DEPTH",
    "VNX_DAILY_METRICS_SOURCES_DIR",
    "VNX_DAILY_METRICS_COMMANDS_DIR",
    "VNX_CONTENT_BOUNDARY_FILE",
    "VNX_MARKER_READ_TIMEOUT_SECONDS",
    "VNX_AGENT_FOLDERS",
    "VNX_ISOLATED_WORKTREE",
    "VNX_CURRENT_TRACK_ID",
    "VNX_QUALITY_SCAN_ALLOWLIST",
    "VNX_QUALITY_SCAN_ALLOWLIST_FILE",
)


@functools.lru_cache(maxsize=1)
def _literal_reads():
    """{name: [file:line]} for every string literal equal to a DELETED name."""
    hits = {name: [] for name in DELETED}
    for top in ("scripts", "vnx_cli", "dashboard"):
        for path in sorted((ROOT / top).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and node.value in hits:
                    hits[node.value].append(f"{path.relative_to(ROOT)}:{node.lineno}")
        for path in sorted((ROOT / top).rglob("*.sh")):
            text = path.read_text(encoding="utf-8")
            for name in DELETED:
                if "$" + name in text or "${" + name in text:
                    hits[name].append(str(path.relative_to(ROOT)))
    return hits


@pytest.mark.parametrize("name", DELETED)
def test_deleted_name_has_zero_reads(name):
    assert _literal_reads()[name] == []


def _export_old_values(monkeypatch):
    """Every deleted name set to a value that would have moved the old reader."""
    for name in DELETED:
        monkeypatch.setenv(name, "0")


def test_daily_metrics_limits_ignore_the_environment(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "days_n1a", ROOT / "scripts" / "lib" / "daily_metrics" / "days.py")
    days = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(days)
    _export_old_values(monkeypatch)
    assert (days.settle_hours(), days.catchup_days(), days.fresh_hours()) == (2, 14, 30)


def test_content_boundary_path_ignores_the_environment(monkeypatch, tmp_path):
    import content_class

    monkeypatch.setenv("VNX_CONTENT_BOUNDARY_FILE", str(tmp_path / "other.json"))
    assert content_class.default_boundary_path() == Path.home() / ".vnx" / "content_boundary.json"
    assert not hasattr(content_class, "marker_timeout")
    assert content_class.MARKER_TIMEOUT_DEFAULT == 5.0


def test_agent_folder_resolution_has_no_switch(monkeypatch):
    import agent_resolver

    monkeypatch.setenv("VNX_AGENT_FOLDERS", "0")
    assert not hasattr(agent_resolver, "agent_folders_enabled")


def test_quality_scanner_allowlist_comes_from_the_argument_only(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "code_quality_scanner_n1a", ROOT / "scripts" / "code_quality_scanner.py")
    scanner = importlib.util.module_from_spec(spec)
    sys.modules["code_quality_scanner_n1a"] = scanner
    spec.loader.exec_module(scanner)
    allow_file = tmp_path / "allow.txt"
    allow_file.write_text("*.py\n", encoding="utf-8")
    monkeypatch.setenv("VNX_QUALITY_SCAN_ALLOWLIST", "*.py")
    monkeypatch.setenv("VNX_QUALITY_SCAN_ALLOWLIST_FILE", str(allow_file))
    assert not hasattr(scanner, "_load_allowlist_globs")
    assert scanner.QualityScanner([tmp_path], tmp_path / "q.db").allowlist_globs == []
    assert scanner.QualityScanner([tmp_path], tmp_path / "q.db", allowlist_globs=["x"]).allowlist_globs == ["x"]
