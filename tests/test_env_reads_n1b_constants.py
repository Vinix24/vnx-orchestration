"""config-een-oppervlak N1b: 8 deleted names, constants and always-on behaviour.

The deleted names have zero string-literal reads left in scripts/, vnx_cli/ and
dashboard/. With the old value exported, the code behaves as it does with nothing set.
"""
from __future__ import annotations

import ast
import functools
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

DELETED = (
    "VNX_ABANDONED_WINDOW_HOURS",
    "VNX_CONTRACT_INVALID_WINDOW_DAYS",
    "VNX_EVENTS_ARCHIVE_COMPRESS_DAYS",
    "VNX_REVIEW_DELTA_MAX_CHAIN",
    "VNX_EVENT_STREAM_MAX_BYTES",
    "VNX_REPORT_CONTRACT_DIRECTIVE",
    "VNX_INCIDENT_SHADOW",
    "VNX_LEDGER_ACTIVATION_COMMIT",
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
    for name in DELETED:
        monkeypatch.setenv(name, "0")


def test_outcome_window_ignores_the_environment(monkeypatch):
    import dispatch_outcome_classifier as doc

    _export_old_values(monkeypatch)
    assert doc.abandoned_window_hours() == doc.DEFAULT_ABANDONED_WINDOW_HOURS == 24.0


def test_contract_invalid_window_ignores_the_environment(monkeypatch):
    import contract_invalid_window as ciw

    _export_old_values(monkeypatch)
    assert ciw.contract_invalid_window_days() == ciw.DEFAULT_WINDOW_DAYS == 14


def test_event_store_threshold_ignores_the_environment(monkeypatch, tmp_path):
    import event_store

    monkeypatch.setenv("VNX_EVENT_STREAM_MAX_BYTES", "7")
    assert event_store.EventStore(events_dir=tmp_path)._rotation_threshold() == 10 * 1024 * 1024
    assert event_store.EventStore(events_dir=tmp_path, rotation_threshold_bytes=99)._rotation_threshold() == 99


def test_footprint_probe_window_ignores_the_environment(monkeypatch):
    import vnx_data_footprint_probe as probe

    _export_old_values(monkeypatch)
    assert probe.compress_days() == probe.DEFAULT_COMPRESS_DAYS == 14


def test_review_delta_chain_ignores_the_environment(monkeypatch):
    import review_delta_shadow as shadow

    monkeypatch.setenv("VNX_REVIEW_DELTA_MAX_CHAIN", "9")
    assert shadow.max_chain() == shadow.MAX_CHAIN_DEFAULT == 2


def test_report_directive_is_always_appended(monkeypatch):
    import report_body_contract as rbc

    monkeypatch.setenv("VNX_REPORT_CONTRACT_DIRECTIVE", "0")
    assert rbc.with_directive("body", "disp-n1b") != "body"
    assert not hasattr(rbc, "directive_enabled")


def test_incident_shadow_mode_has_no_switch(monkeypatch):
    import incident_log
    import supervisor_shadow

    monkeypatch.setenv("VNX_INCIDENT_SHADOW", "0")
    assert not hasattr(incident_log, "is_shadow_mode")
    assert not hasattr(supervisor_shadow, "is_shadow_mode")


def test_learning_ledger_activation_comes_from_the_argument_only(monkeypatch, tmp_path):
    import learning_ledger_gate as gate

    monkeypatch.setenv("VNX_LEDGER_ACTIVATION_COMMIT", "a" * 40)
    verdict = gate.ledger_verdict(tmp_path, "n1b-project", now=datetime(2026, 11, 20, tzinfo=timezone.utc))
    assert verdict["cohort"] != "a" * 40
