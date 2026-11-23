"""The pure plan-table parser (ADR-096, B8/B9): no store, no writes."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
for _p in (_ROOT / "scripts" / "lib", _ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import plan_table  # noqa: E402

def _write(root: Path, relative: str, body: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path

@pytest.fixture
def locked():
    """Make a plan file unreadable, and readable again after the test."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root reads a chmod 000 file, so no candidate read can fail")
    paths: list[Path] = []

    def make(path: Path) -> Path:
        path.chmod(0o000)
        paths.append(path)
        return path

    yield make
    for path in paths:
        path.chmod(0o600)

def test_find_plan_one_none_several_and_a_path_with_a_space(tmp_path):
    one = _write(tmp_path, "claudedocs/specs/with space/plan.md", "Track `feat-x`\n")
    assert plan_table.find_plan("feat-x", tmp_path) == (one, "")
    assert plan_table.find_plan("missing", tmp_path) == (None, "no plan")
    two = _write(tmp_path, "claudedocs/specs/other/plan-v2.md", "Track `feat-x`\n")
    assert plan_table.find_plan("feat-x", tmp_path) == (
        None, "several plans: " + ", ".join(str(p) for p in sorted([one, two])))
    _write(tmp_path, "claudedocs/specs/late/plan.md", "\n" * 12 + "Track `late`\n")
    assert plan_table.find_plan("late", tmp_path) == (None, "no plan")
    assert plan_table.find_plan("feat-x", tmp_path / "claudedocs") == (None, plan_table.find_plan(
        "feat-x", tmp_path)[1])

def test_find_plan_skips_archive_and_plan_gate(tmp_path):
    real = _write(tmp_path, "claudedocs/specs/x/plan.md", "Track `feat-y`\n")
    _write(tmp_path, "claudedocs/archive/plan.md", "Track `feat-y`\n")
    _write(tmp_path, "claudedocs/archive/2026-10/plan-deep.md", "Track `feat-y`\n")
    _write(tmp_path, "claudedocs/specs/x/plan-gate-old.md", "Track `feat-y`\n")
    assert plan_table.find_plan("feat-y", tmp_path) == (real, "")
    only = tmp_path / "only"
    _write(only, "claudedocs/archive/plan.md", "Track `gone`\n")
    _write(only, "claudedocs/x/plan-gate-one.md", "Track `gone`\n")
    assert plan_table.find_plan("gone", only) == (None, "no plan")

def test_find_plan_names_an_unreadable_candidate_beside_one_readable_match(tmp_path, locked):
    _write(tmp_path, "claudedocs/specs/ok/plan.md", "Track `feat-z`\n")
    blocked = locked(_write(tmp_path, "claudedocs/specs/locked/plan.md", "Track `feat-z`\n"))
    found, reason = plan_table.find_plan("feat-z", tmp_path)
    assert found is None
    assert reason.startswith("unreadable plan ")
    assert str(blocked) in reason and "PermissionError" in reason

def test_find_plan_never_reports_no_plan_when_a_candidate_is_unreadable(tmp_path, locked):
    blocked = locked(_write(tmp_path, "claudedocs/specs/locked/plan.md", "Track `feat-q`\n"))
    _write(tmp_path, "claudedocs/specs/other/plan-notes.md", "unrelated\n")
    found, reason = plan_table.find_plan("feat-q", tmp_path)
    assert found is None
    assert reason != "no plan" and reason.startswith("unreadable plan ")
    assert str(blocked) in reason and "PermissionError" in reason

def test_find_plan_names_every_unreadable_candidate_and_beats_several_matches(tmp_path, locked):
    _write(tmp_path, "claudedocs/a/plan.md", "Track `feat-w`\n")
    _write(tmp_path, "claudedocs/b/plan.md", "Track `feat-w`\n")
    first = locked(_write(tmp_path, "claudedocs/c/plan.md", "Track `feat-w`\n"))
    second = locked(_write(tmp_path, "claudedocs/d/plan.md", "Track `nothing`\n"))
    found, reason = plan_table.find_plan("feat-w", tmp_path)
    assert found is None
    assert reason.startswith(f"unreadable plan {first}: ")
    assert f", unreadable plan {second}: " in reason
    assert "several plans" not in reason

@pytest.mark.parametrize("header", ["hangt af van", "Hangt af van", "depends on", "Depends On",
                                    "Na", "na", "after", "After"])
def test_parse_order_header_variants(tmp_path, header):
    path = _write(tmp_path, "plan.md", f"| # | Levering | {header} |\n|---|---|---|\n| B1 | x | B0 |\n")
    table = plan_table.parse(path)
    assert table.found and table.header_line == 1
    assert [(row.label, row.depends) for row in table.rows] == [("B1", [("B0", "hard")])]

def test_parse_keeps_looking_and_requires_a_label_column(tmp_path):
    path = _write(tmp_path, "plan.md", "\n".join([
        "| # | Levering | Iets |", "|---|---|---|", "| X1 | ignored | nope |", "",
        "| # | Levering | hangt af van |", "|---|---|---|", "| B1 | first | geen |", ""]))
    table = plan_table.parse(path)
    assert [row.label for row in table.rows] == ["B1"] and table.header_line == 5
    no_label = _write(tmp_path, "other.md", "| id | Levering | hangt af van |\n|---|---|---|\n| B1 | x | B1 |\n")
    assert plan_table.parse(no_label).found is False
    assert plan_table.parse(_write(tmp_path, "plain.md", "none\n")).found is False

def test_parse_none_markers_soft_markers_and_unreadable_parts(tmp_path):
    path = _write(tmp_path, "plan.md", "\n".join([
        "| # | Levering | hangt af van |", "|---|---|---|", "| B0 | x | geen |",
        "| B1 | x | none |", "| B2 | x | - |", "| B3 | x |  |",
        "| B4 | x | geen (B1 staat op main) |", "| B5 | x | none (whatever) |",
        "| B6 | x | B1, B4b (soft), dlv-0123456789ab (soft), dlv-fedcba987654 |",
        "| B7 | x | B4b plus zeven dagen |", "| B8 | x | B1, plus zeven dagen |",
        "| B9 | x | dlv-NOTHEX12345 |", ""]))
    rows = {row.label: row for row in plan_table.parse(path).rows}
    assert all(rows[label].depends == [] and rows[label].order_error is None for label in "B0 B1 B2 B3 B4 B5".split())
    assert rows["B6"].depends == [("B1", "hard"), ("B4b", "soft"),
                                  ("dlv-0123456789ab", "soft"), ("dlv-fedcba987654", "hard")]
    assert rows["B7"].depends == [] and "plus zeven dagen" in rows["B7"].order_error
    assert "line 10" in rows["B7"].order_error and rows["B8"].depends == []
    assert "dlv-NOTHEX12345" in rows["B9"].order_error

def test_parse_id_column_paths_line_numbers_and_missing_file(tmp_path):
    path = _write(tmp_path, "plan.md", "\n".join([
        "| # | id | Levering | hangt af van | paden |", "|---|---|---|---|---|",
        "| B1 | `dlv-0123456789ab` | first | geen | `scripts/lib/a.py`, `tests/test_a.py` |",
        "| B2 | B1 | second | geen | `with space.py` |", "| B3 |  | third | geen | /etc/passwd |",
        "| B4 |  | fourth | geen | scripts/../etc/passwd |", "| B5 |  | fifth | geen | - |", ""]))
    rows = plan_table.parse(path).rows
    assert (rows[0].dlv_id, rows[0].paths, rows[0].line) == ("dlv-0123456789ab",
                                                             ["scripts/lib/a.py", "tests/test_a.py"], 3)
    assert rows[1].dlv_id is None and "with space.py" in rows[1].paths_error
    assert "/etc/passwd" in rows[2].paths_error and "scripts/../etc/passwd" in rows[3].paths_error
    assert rows[4].paths is None and rows[4].paths_error is None
    with pytest.raises(FileNotFoundError):
        plan_table.parse(tmp_path / "missing.md")

def test_map_label_title_and_id_helpers():
    deliverables = [("dlv-aaaaaaaaaaaa", "B0: first"), ("dlv-bbbbbbbbbbbb", "B1 second"),
                    ("dlv-cccccccccccc", "B1: again")]
    assert plan_table.map_label("B0", deliverables) == ("dlv-aaaaaaaaaaaa", "")
    assert plan_table.map_label("B1", deliverables) == (None, "2 deliverables titled B1")
    assert plan_table.map_label("B", deliverables) == (None, "no deliverable titled B")
    assert plan_table.map_label("b0", deliverables) == (None, "no deliverable titled b0")
    assert plan_table.title_matches_label("B4b", "B4b: the door")
    assert plan_table.title_matches_label("B4b", "B4b the door")
    assert not plan_table.title_matches_label("B4", "B4b: the door")
    assert plan_table.is_dlv_id("dlv-0123456789ab") and not plan_table.is_dlv_id("dlv-0123456789AB")
    assert not plan_table.is_dlv_id("B1")
