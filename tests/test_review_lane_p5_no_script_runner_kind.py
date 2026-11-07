"""Review-lane P5 (ADR-046 §3, Amendment 1): the dead ``script_runner`` adapter
kind is gone. The registry knows exactly three kinds, the recorder has no
constant for a fourth, and the gate modules have no branch for it."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
sys.path.insert(0, str(ROOT / "scripts"))

import gate_recorder as _rec

_FILES = (
    ROOT / "scripts" / "lib" / "gate_recorder.py",
    ROOT / "scripts" / "gate_runner.py",
    ROOT / "scripts" / "lib" / "gate_result_parser.py",
)


def test_registry_kinds_are_exactly_the_three_adr_046_kinds():
    kinds = {kind for kind, _name in _rec.GATE_PROVIDERS.values()}
    assert kinds == {"path_binary", "harness_lane", "deterministic"}


def test_recorder_has_no_script_runner_constant():
    assert not hasattr(_rec, "GATE_PROVIDER_SCRIPT_RUNNER")
    assert "script_runner" not in {
        value for name, value in vars(_rec).items()
        if name.startswith("GATE_PROVIDER_") and isinstance(value, str)
    }


def test_no_gate_module_names_the_script_runner_kind():
    for path in _FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        literals = {
            n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        }
        assert "GATE_PROVIDER_SCRIPT_RUNNER" not in names, path.name
        assert "script_runner" not in literals, path.name
