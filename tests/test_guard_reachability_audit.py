#!/usr/bin/env python3
"""Tests for scripts/guard_reachability_audit.py — the CLI that combines the
scanner + store measurement + registry into an actual audit run.

These are integration tests against a SYNTHETIC repo layout (tmp_path), not
the real one — the real repo's own audit output is exercised manually (see
the dispatch report's Verification section for the actual run), because the
real repo currently surfaces genuine unresolved findings (open items, not
bugs in this detector) that would make a hardcoded pytest assertion either
brittle or dishonestly permissive.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
LIB_DIR = SCRIPTS_DIR / "lib"
for p in (str(SCRIPTS_DIR), str(LIB_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import guard_reachability_audit as audit_cli  # noqa: E402
import guard_reachability_registry as registry  # noqa: E402
from guard_reachability_registry import AcceptedGap, FieldMapping, StoreTarget  # noqa: E402


def _make_synthetic_repo(tmp_path: Path) -> Path:
    """A tiny repo with ONE guard shape mirroring OI-1632: a dataclass field
    read via a local-var indirection, gated in an ``if``."""
    lib = tmp_path / "scripts" / "lib"
    lib.mkdir(parents=True)
    (lib / "fake_dispatch_cli.py").write_text(
        "from dataclasses import dataclass\n"
        "\n"
        "@dataclass\n"
        "class FakeSpec:\n"
        "    dispatch_id: str\n"
        "    widget_id: str | None = None\n"
        "\n"
        "def check_widget_link(spec: FakeSpec):\n"
        "    widget_id = (spec.widget_id or '').strip()\n"
        "    if widget_id:\n"
        "        return 'checked'\n"
        "    return 'advisory-skip'\n",
        encoding="utf-8",
    )
    return tmp_path


def _make_zero_fill_db(tmp_path: Path, *, n: int, filled: int) -> Path:
    db_path = tmp_path / "state" / "runtime_coordination.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE widgets (widget_id TEXT, linked TEXT)")
    rows = [(f"w{i}", "X" if i < filled else None) for i in range(n)]
    conn.executemany("INSERT INTO widgets (widget_id, linked) VALUES (?, ?)", rows)
    conn.commit()
    conn.close()
    return db_path


def test_build_findings_flags_zero_fill_as_violation(tmp_path, monkeypatch):
    root = _make_synthetic_repo(tmp_path)
    data_dir = tmp_path / "data"
    _make_zero_fill_db(data_dir, n=25, filled=0)

    mapping = FieldMapping(
        field="widget_id",
        targets=(
            StoreTarget(
                kind="sqlite", db_relpath="state/runtime_coordination.db",
                table="widgets", column="linked", note="test",
            ),
        ),
        note="test mapping",
    )
    monkeypatch.setattr(registry, "FIELD_STORE_MAP", (mapping,))
    monkeypatch.setattr(registry, "ACCEPTED_GAPS", ())
    monkeypatch.setattr(audit_cli, "FIELD_STORE_MAP", (mapping,))
    monkeypatch.setattr(audit_cli, "ACCEPTED_GAPS", ())

    violations, suppressed, ok, unmeasured = audit_cli.build_findings(root, data_dir)
    assert len(violations) == 1
    assert violations[0].field == "widget_id"
    assert suppressed == []
    assert ok == []


def test_build_findings_reports_ok_when_fill_rate_nonzero(tmp_path, monkeypatch):
    root = _make_synthetic_repo(tmp_path)
    data_dir = tmp_path / "data"
    _make_zero_fill_db(data_dir, n=25, filled=5)

    mapping = FieldMapping(
        field="widget_id",
        targets=(
            StoreTarget(
                kind="sqlite", db_relpath="state/runtime_coordination.db",
                table="widgets", column="linked", note="test",
            ),
        ),
        note="test mapping",
    )
    monkeypatch.setattr(audit_cli, "FIELD_STORE_MAP", (mapping,))
    monkeypatch.setattr(audit_cli, "ACCEPTED_GAPS", ())

    violations, suppressed, ok, unmeasured = audit_cli.build_findings(root, data_dir)
    assert violations == []
    assert len(ok) == 1


def test_build_findings_reports_missing_column_as_violation(tmp_path, monkeypatch):
    root = _make_synthetic_repo(tmp_path)
    data_dir = tmp_path / "data"
    _make_zero_fill_db(data_dir, n=10, filled=10)  # column exists, but wrong name below

    mapping = FieldMapping(
        field="widget_id",
        targets=(
            StoreTarget(
                kind="sqlite", db_relpath="state/runtime_coordination.db",
                table="widgets", column="this_column_does_not_exist", note="test",
            ),
        ),
        note="test mapping",
    )
    monkeypatch.setattr(audit_cli, "FIELD_STORE_MAP", (mapping,))
    monkeypatch.setattr(audit_cli, "ACCEPTED_GAPS", ())

    violations, suppressed, ok, unmeasured = audit_cli.build_findings(root, data_dir)
    assert len(violations) == 1
    assert violations[0].rate.exists is False


def test_build_findings_suppresses_with_accepted_gap_but_still_lists_reason(tmp_path, monkeypatch):
    root = _make_synthetic_repo(tmp_path)
    data_dir = tmp_path / "data"
    _make_zero_fill_db(data_dir, n=25, filled=0)

    mapping = FieldMapping(
        field="widget_id",
        targets=(
            StoreTarget(
                kind="sqlite", db_relpath="state/runtime_coordination.db",
                table="widgets", column="linked", note="test",
            ),
        ),
        note="test mapping",
    )
    gap = AcceptedGap(
        field="widget_id", reason="genuinely optional in this synthetic fixture",
        decided_by="test", decided_on="2026-09-05",
    )
    monkeypatch.setattr(audit_cli, "FIELD_STORE_MAP", (mapping,))
    monkeypatch.setattr(audit_cli, "ACCEPTED_GAPS", (gap,))

    violations, suppressed, ok, unmeasured = audit_cli.build_findings(root, data_dir)
    assert violations == []
    assert len(suppressed) == 1
    finding, matched_gap = suppressed[0]
    assert matched_gap.reason == "genuinely optional in this synthetic fixture"


def test_build_findings_puts_unmapped_fields_in_unmeasured_bucket(tmp_path, monkeypatch):
    root = _make_synthetic_repo(tmp_path)
    data_dir = tmp_path / "data"
    monkeypatch.setattr(audit_cli, "FIELD_STORE_MAP", ())
    monkeypatch.setattr(audit_cli, "ACCEPTED_GAPS", ())

    violations, suppressed, ok, unmeasured = audit_cli.build_findings(root, data_dir)
    assert violations == []
    assert "widget_id" in unmeasured


def test_cli_audit_exits_nonzero_on_unsuppressed_violation(tmp_path, monkeypatch, capsys):
    root = _make_synthetic_repo(tmp_path)
    data_dir = tmp_path / "data"
    _make_zero_fill_db(data_dir, n=25, filled=0)

    mapping = FieldMapping(
        field="widget_id",
        targets=(
            StoreTarget(
                kind="sqlite", db_relpath="state/runtime_coordination.db",
                table="widgets", column="linked", note="test",
            ),
        ),
        note="test mapping",
    )
    monkeypatch.setattr(audit_cli, "FIELD_STORE_MAP", (mapping,))
    monkeypatch.setattr(audit_cli, "ACCEPTED_GAPS", ())

    rc = audit_cli.main(["--root", str(root), "audit", "--data-dir", str(data_dir)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "VIOLATION" in out
    assert "widget_id" in out


def test_cli_scan_always_exits_zero(tmp_path, capsys):
    root = _make_synthetic_repo(tmp_path)
    rc = audit_cli.main(["--root", str(root), "scan"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "widget_id" in out


def test_cli_selftest_passes_on_real_repo(capsys):
    rc = audit_cli.main(["--root", str(VNX_ROOT), "selftest"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS" in out
