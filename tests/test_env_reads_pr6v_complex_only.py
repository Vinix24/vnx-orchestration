"""config-een-oppervlak PR-6v part b2: VNX_PLAN_GATE_COMPLEX_ONLY is deleted and read nowhere.

The name has zero string-literal reads (or bash ``$NAME``) left in scripts/, vnx_cli/
and dashboard/, and ``plan_gate_enforcement.complex_only_active`` no longer exists.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import plan_gate_enforcement  # noqa: E402

NAME = "VNX_PLAN_GATE_COMPLEX_ONLY"


def _literal_reads() -> list:
    hits = []
    for top in ("scripts", "vnx_cli", "dashboard"):
        for path in sorted((ROOT / top).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for n in ast.walk(tree):
                if isinstance(n, ast.Constant) and n.value == NAME:
                    hits.append(f"{path.relative_to(ROOT)}:{n.lineno}")
        for path in sorted((ROOT / top).rglob("*.sh")):
            text = path.read_text(encoding="utf-8")
            if "$" + NAME in text or "${" + NAME in text:
                hits.append(str(path.relative_to(ROOT)))
    return hits


def test_deleted_name_has_zero_reads():
    assert _literal_reads() == []


def test_complex_only_active_is_gone():
    assert not hasattr(plan_gate_enforcement, "complex_only_active")
