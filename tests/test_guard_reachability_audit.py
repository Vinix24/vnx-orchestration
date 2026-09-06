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

import pytest

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


def test_track_id_violation_surfaces_from_the_staged_spec_not_the_persisted_column(tmp_path):
    """OI-1640 (golf4 ronde 2), the bug this dispatch fixes.

    Uses the REAL registry (unmonkeypatched) and the REAL scanner against
    the REAL repo (``VNX_ROOT``) — only the DATA is synthetic, shaped to
    reproduce the measured split T0 found on 2026-09-06: the persisted
    ``dispatches.track`` sqlite column partially filled (mirrors the live
    122/815), the staged ``dispatch-spec.json`` bundles the door's guard
    (``_check_track_link_verdict`` and ``_persist_track_id``) ACTUALLY read
    at exact zero (mirrors the live 0/1115).

    Before this dispatch's fix, ``FIELD_STORE_MAP`` mapped ``track_id``
    ONLY against the sqlite column — a nonzero-but-partial persisted column
    read as [OK], laundering the real zero-fill guard-source into a clean
    report. This test fails red against that pre-fix registry (the sqlite
    target alone is "ok", no violation) and passes once the registry also
    measures the staged-spec store the guard actually reads.
    """
    data_dir = tmp_path / "data"
    db_path = data_dir / "state" / "runtime_coordination.db"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE dispatches (dispatch_id TEXT, track TEXT)")
    conn.executemany(
        "INSERT INTO dispatches (dispatch_id, track) VALUES (?, ?)",
        [(f"d{i}", "T1" if i < 3 else None) for i in range(20)],
    )
    conn.commit()
    conn.close()

    specs_dir = data_dir / "dispatches" / "pending"
    specs_dir.mkdir(parents=True)
    for i in range(20):
        one = specs_dir / f"s{i}"
        one.mkdir()
        (one / "dispatch-spec.json").write_text(
            json.dumps({"dispatch_id": f"s{i}"}),  # no track_id key at all — 0 of 20
            encoding="utf-8",
        )

    violations, suppressed, ok, unmeasured = audit_cli.build_findings(VNX_ROOT, data_dir)

    track_id_violations = [f for f in violations if f.field == "track_id"]
    assert track_id_violations, (
        "track_id must surface as a violation from the staged-spec store "
        "even though the persisted sqlite column is partially filled — a "
        "nonzero downstream column does not prove the door's guard is "
        "reachable, it only proves _persist_track_id ran"
    )
    assert any(f.target.kind == "json_dir" for f in track_id_violations)
    # the sqlite target's own partial fill must still read as ok, not dropped
    track_id_ok = [f for f in ok if f.field == "track_id"]
    assert any(f.target.kind == "sqlite" for f in track_id_ok)


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
    monkeypatch.setattr(registry, "MIN_MAPPED_FIELDS", 1)
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


def _make_synthetic_repo_two_fields(tmp_path: Path) -> Path:
    """Two independent guarded fields, each its own zero-fill-shaped guard —
    needed to prove one field's ACCEPTED_GAPS suppression does not silently
    launder the other's violation away (golf4r2 bewijs #3)."""
    lib = tmp_path / "scripts" / "lib"
    lib.mkdir(parents=True)
    (lib / "fake_dispatch_cli.py").write_text(
        "from dataclasses import dataclass\n"
        "\n"
        "@dataclass\n"
        "class FakeSpec:\n"
        "    dispatch_id: str\n"
        "    widget_id: str | None = None\n"
        "    gizmo_id: str | None = None\n"
        "\n"
        "def check_widget_link(spec: FakeSpec):\n"
        "    widget_id = (spec.widget_id or '').strip()\n"
        "    if widget_id:\n"
        "        return 'checked'\n"
        "    return 'advisory-skip'\n"
        "\n"
        "def check_gizmo_link(spec: FakeSpec):\n"
        "    gizmo_id = (spec.gizmo_id or '').strip()\n"
        "    if gizmo_id:\n"
        "        return 'checked'\n"
        "    return 'advisory-skip'\n",
        encoding="utf-8",
    )
    return tmp_path


def _make_two_zero_fill_tables(tmp_path: Path, *, n: int) -> Path:
    db_path = tmp_path / "state" / "runtime_coordination.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE widgets (widget_id TEXT, linked TEXT)")
    conn.execute("CREATE TABLE gizmos (gizmo_id TEXT, linked TEXT)")
    conn.executemany(
        "INSERT INTO widgets (widget_id, linked) VALUES (?, NULL)", [(f"w{i}",) for i in range(n)],
    )
    conn.executemany(
        "INSERT INTO gizmos (gizmo_id, linked) VALUES (?, NULL)", [(f"g{i}",) for i in range(n)],
    )
    conn.commit()
    conn.close()
    return db_path


def test_run_live_violation_selftest_passes_when_a_real_violation_exists(tmp_path, monkeypatch):
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

    lines = audit_cli.run_live_violation_selftest(root, data_dir)
    assert any("widget_id" in line for line in lines)


def test_run_live_violation_selftest_survives_partial_suppression(tmp_path, monkeypatch):
    """bewijs #3: gapping ONE of two known-bad fields must not launder the
    whole live-assertion green — the other stays a live violation."""
    root = _make_synthetic_repo_two_fields(tmp_path)
    data_dir = tmp_path / "data"
    _make_two_zero_fill_tables(data_dir, n=25)

    widget_mapping = FieldMapping(
        field="widget_id",
        targets=(
            StoreTarget(
                kind="sqlite", db_relpath="state/runtime_coordination.db",
                table="widgets", column="linked", note="test",
            ),
        ),
        note="test mapping",
    )
    gizmo_mapping = FieldMapping(
        field="gizmo_id",
        targets=(
            StoreTarget(
                kind="sqlite", db_relpath="state/runtime_coordination.db",
                table="gizmos", column="linked", note="test",
            ),
        ),
        note="test mapping",
    )
    gap = AcceptedGap(
        field="widget_id", reason="suppress only ONE of the two for this test",
        decided_by="test", decided_on="2026-09-06",
    )
    monkeypatch.setattr(audit_cli, "FIELD_STORE_MAP", (widget_mapping, gizmo_mapping))
    monkeypatch.setattr(audit_cli, "ACCEPTED_GAPS", (gap,))

    lines = audit_cli.run_live_violation_selftest(root, data_dir)
    assert any("gizmo_id" in line for line in lines)
    assert not any("widget_id" in line for line in lines)

    rc = audit_cli.main(["--root", str(root), "audit", "--data-dir", str(data_dir)])
    assert rc == 1  # gizmo_id alone keeps the overall audit red


def test_run_live_violation_selftest_fails_loud_when_all_known_violations_suppressed(tmp_path, monkeypatch):
    """'Maak je eigen wachter kapot': suppressing EVERY known violation must
    make the live-assertion itself fail loud, not report a clean audit."""
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
        field="widget_id", reason="suppress the only known violation",
        decided_by="test", decided_on="2026-09-06",
    )
    monkeypatch.setattr(audit_cli, "FIELD_STORE_MAP", (mapping,))
    monkeypatch.setattr(audit_cli, "ACCEPTED_GAPS", (gap,))

    with pytest.raises(audit_cli.SelfTestFailure, match="ZERO violations"):
        audit_cli.run_live_violation_selftest(root, data_dir)
