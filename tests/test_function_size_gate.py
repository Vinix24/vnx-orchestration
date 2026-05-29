#!/usr/bin/env python3
"""Function-size guardrail tests for AS-05."""

from __future__ import annotations

from pathlib import Path
import sys

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"

_lib = SCRIPTS_DIR / "lib"
if str(_lib) not in sys.path:
    sys.path.insert(0, str(_lib))

from function_size_gate import FunctionBudget, evaluate_function_budgets, load_function_budgets, render_violations


def test_gate_flags_oversized_python_function(tmp_path: Path):
    py_file = tmp_path / "oversized.py"
    py_file.write_text(
        "\n".join(
            [
                "def oversized():",
                "    x = 0",
                "    x += 1",
                "    x += 1",
                "    x += 1",
                "    x += 1",
                "    x += 1",
                "    return x",
            ]
        ),
        encoding="utf-8",
    )
    budget = FunctionBudget(file_path=py_file, function_name="oversized", max_lines=5, language="python")
    violations = evaluate_function_budgets([budget])

    assert len(violations) == 1
    assert violations[0].reason == "max_lines_exceeded"
    assert violations[0].actual_lines is not None
    assert violations[0].actual_lines > budget.max_lines


def test_gate_flags_oversized_shell_function(tmp_path: Path):
    sh_file = tmp_path / "oversized.sh"
    sh_file.write_text(
        "\n".join(
            [
                "my_func() {",
                "  echo one",
                "  echo two",
                "  echo three",
                "  echo four",
                "  echo five",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    budget = FunctionBudget(file_path=sh_file, function_name="my_func", max_lines=4, language="shell")
    violations = evaluate_function_budgets([budget])

    assert len(violations) == 1
    assert violations[0].reason == "max_lines_exceeded"
    assert violations[0].actual_lines is not None
    assert violations[0].actual_lines > budget.max_lines


def test_critical_function_budgets_are_enforced():
    config_path = SCRIPTS_DIR / "function_size_budgets.json"
    budgets = load_function_budgets(config_path, scripts_root=SCRIPTS_DIR)
    violations = evaluate_function_budgets(budgets)

    assert not violations, "\n".join(render_violations(violations))


def test_budget_loader_uses_provided_scripts_root(tmp_path: Path):
    scripts_root = tmp_path / "scripts"
    scripts_root.mkdir(parents=True, exist_ok=True)
    (scripts_root / "sample.py").write_text("def keep_small():\n    return 1\n", encoding="utf-8")

    config = tmp_path / "budgets.json"
    config.write_text(
        '{ "budgets": [ { "file": "sample.py", "language": "python", "function": "keep_small", "max_lines": 5 } ] }',
        encoding="utf-8",
    )

    budgets = load_function_budgets(config, scripts_root=scripts_root)
    assert budgets[0].file_path == (scripts_root / "sample.py").resolve()
