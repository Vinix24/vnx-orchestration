#!/usr/bin/env python3
"""Tests for scripts/lib/guard_reachability_scanner.py — the golf-4 (2026-09-05)
unreachable-guard detector's STATIC half.

The calibration test (``test_finds_historical_oi1632_track_id_guard``) is the
"RODE TEST EERST" required by the dispatch: it runs the scanner against the
REAL pre-fix source of ``_check_track_link_verdict`` (fetched via
``git show ee818845^:...``, never retyped) and asserts the guard is found.
Before ``_local_field_assignments``/name-resolution existed, this test failed
(the scanner only looked at the ``if`` test expression directly, and the
real guard is ``if track_id:`` where ``track_id`` is a local assigned from
``spec.track_id`` a few lines earlier) — see the dispatch report's
Verification section for both the RED and GREEN pytest runs.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
LIB_DIR = SCRIPTS_DIR / "lib"

for p in (str(SCRIPTS_DIR), str(LIB_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from guard_reachability_scanner import (  # noqa: E402
    collect_dataclass_fields,
    find_guarded_field_refs,
)


def _fetch_pre_fix_check_track_link_verdict() -> str:
    """The real pre-OI-1632 source, extracted the same way the calibration
    module does — duplicated here (not imported) so this test file exercises
    the scanner in complete isolation from ``guard_reachability_calibration``.
    """
    import ast

    result = subprocess.run(
        ["git", "show", "ee818845^:scripts/lib/dispatch_cli.py"],
        cwd=VNX_ROOT, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    tree = ast.parse(result.stdout)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_check_track_link_verdict":
            seg = ast.get_source_segment(result.stdout, node)
            assert seg
            return seg
    raise AssertionError("_check_track_link_verdict not found in pre-fix dispatch_cli.py")


def test_finds_historical_oi1632_track_id_guard():
    """Calibration case: the REAL, historical, structurally-unreachable guard.

    Pre-#1774, ``spec.track_id`` was always ``None`` for bridge-staged
    dispatches (0 of 681 rows had a track), so ``if track_id:`` here never
    took its blocking branch. The scanner must find this guard.
    """
    source = _fetch_pre_fix_check_track_link_verdict()
    refs = find_guarded_field_refs(source, "dispatch_cli.py", known_attr_fields={"track_id"})
    matches = [r for r in refs if r.field == "track_id"]
    assert matches, (
        "scanner found no guard on 'track_id' in the real pre-fix "
        "_check_track_link_verdict source — this is the calibration case "
        "from OI-1632 (#1774); see the module docstring"
    )
    assert matches[0].access_kind == "attribute"
    assert matches[0].enclosing_function == "_check_track_link_verdict"


def test_direct_dict_get_guard_is_found():
    source = (
        "def handler(payload):\n"
        "    if payload.get('pr_link'):\n"
        "        return 'linked'\n"
        "    return 'unlinked'\n"
    )
    refs = find_guarded_field_refs(source, "x.py")
    assert len(refs) == 1
    ref = refs[0]
    assert ref.field == "pr_link"
    assert ref.access_kind == "dict_get"
    assert ref.container == "payload"
    assert ref.enclosing_function == "handler"


def test_direct_subscript_guard_is_found():
    source = (
        "def handler(row):\n"
        "    if row['track']:\n"
        "        return True\n"
        "    return False\n"
    )
    refs = find_guarded_field_refs(source, "x.py")
    assert len(refs) == 1
    assert refs[0].field == "track"
    assert refs[0].access_kind == "subscript"


def test_negated_and_is_none_forms_are_found():
    source = (
        "def handler(payload):\n"
        "    if not payload.get('x'):\n"
        "        return None\n"
        "    if payload.get('y') is None:\n"
        "        return None\n"
        "    return True\n"
    )
    refs = find_guarded_field_refs(source, "x.py")
    fields = {r.field for r in refs}
    assert fields == {"x", "y"}


def test_attribute_guard_requires_known_dataclass_field():
    """An arbitrary attribute access must not be treated as a field probe —
    only names cross-referenced against a real @dataclass field (otherwise
    every ``logger.info`` / ``self.foo`` in the repo would match)."""
    source = (
        "def handler(spec):\n"
        "    if spec.track_id:\n"
        "        return True\n"
        "    return False\n"
    )
    refs_unqualified = find_guarded_field_refs(source, "x.py", known_attr_fields=set())
    assert refs_unqualified == []

    refs_qualified = find_guarded_field_refs(source, "x.py", known_attr_fields={"track_id"})
    assert len(refs_qualified) == 1
    assert refs_qualified[0].field == "track_id"
    assert refs_qualified[0].access_kind == "attribute"


def test_local_variable_indirection_is_resolved():
    """The exact OI-1632 shape: the probe is in an ASSIGNMENT, the guard
    tests the bare local name."""
    source = (
        "def handler(spec):\n"
        "    track_id = (spec.track_id or '').strip()\n"
        "    if track_id:\n"
        "        return 'checked'\n"
        "    return 'advisory'\n"
    )
    refs = find_guarded_field_refs(source, "x.py", known_attr_fields={"track_id"})
    assert len(refs) == 1
    assert refs[0].field == "track_id"
    assert refs[0].access_kind == "attribute"
    assert "via local 'track_id'" in refs[0].container


def test_local_variable_indirection_scoped_per_function():
    """The same local name in an UNRELATED function must not cross-pollute:
    only a name assigned from a field probe resolves."""
    source = (
        "def other(spec):\n"
        "    track_id = 'unrelated-literal'\n"
        "    if track_id:\n"
        "        return True\n"
        "\n"
        "def handler(spec):\n"
        "    track_id = spec.track_id\n"
        "    if track_id:\n"
        "        return True\n"
    )
    refs = find_guarded_field_refs(source, "x.py", known_attr_fields={"track_id"})
    assert len(refs) == 1
    assert refs[0].enclosing_function == "handler"


def test_unrelated_condition_is_not_flagged():
    source = (
        "def handler(x):\n"
        "    if x > 5:\n"
        "        return True\n"
        "    return False\n"
    )
    assert find_guarded_field_refs(source, "x.py") == []


def test_syntax_error_returns_empty_not_raise():
    assert find_guarded_field_refs("def broken(:\n", "x.py") == []


def test_collect_dataclass_fields_finds_annotated_class_fields():
    source = (
        "from dataclasses import dataclass\n"
        "@dataclass\n"
        "class DispatchSpec:\n"
        "    dispatch_id: str\n"
        "    track_id: str | None = None\n"
        "class NotADataclass:\n"
        "    other_field: str\n"
    )
    import ast

    fields = collect_dataclass_fields(ast.parse(source))
    assert fields == {"dispatch_id", "track_id"}


def test_docstring_mentioning_a_guard_is_not_matched():
    """bewijs #4: the scanner must hit the REAL call, not prose about one.
    A docstring that describes ``if payload.get("ghost_field"):`` in English
    must not register 'ghost_field' as a finding — no such code runs."""
    source = (
        "def handler(payload):\n"
        "    '''Note: earlier drafts used if payload.get(\"ghost_field\"): here.'''\n"
        "    # if payload.get(\"also_ghost\"): -- old approach, removed\n"
        "    if payload.get('real_field'):\n"
        "        return True\n"
        "    return False\n"
    )
    refs = find_guarded_field_refs(source, "x.py")
    fields = {r.field for r in refs}
    assert fields == {"real_field"}


def test_repo_scan_still_finds_live_track_id_guard():
    """Sanity check against the CURRENT (fixed) tree: the guard shape still
    exists post-#1774 (VNX_REQUIRE_DISPATCH_TRACK stays advisory-only), it
    is simply no longer ALWAYS empty. The scan half must still find it —
    only the measure half (guard_reachability_store) can tell fixed from
    broken.
    """
    from guard_reachability_scanner import scan_repo_guarded_fields

    refs = scan_repo_guarded_fields(VNX_ROOT)
    track_refs = [r for r in refs if r.field == "track_id"]
    assert track_refs, "expected at least one live 'track_id' guard in dispatch_cli.py post-fix"
