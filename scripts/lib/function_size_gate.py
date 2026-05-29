#!/usr/bin/env python3
"""Function-size gate for critical VNX scripts."""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Sequence

_SHELL_FUNCTION_RE = re.compile(r"^\s*(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(\))?\s*\{\s*$")


@dataclass(frozen=True)
class FunctionBudget:
    file_path: Path
    function_name: str
    max_lines: int
    language: str


@dataclass(frozen=True)
class FunctionMeasurement:
    name: str
    start_line: int
    end_line: int

    @property
    def length(self) -> int:
        return self.end_line - self.start_line + 1


@dataclass(frozen=True)
class FunctionSizeViolation:
    file_path: Path
    function_name: str
    max_lines: int
    actual_lines: int | None
    reason: str

    def render(self) -> str:
        if self.actual_lines is None:
            return f"{self.file_path}:{self.function_name} -> {self.reason}"
        return (
            f"{self.file_path}:{self.function_name} -> {self.actual_lines} lines "
            f"(max {self.max_lines}) [{self.reason}]"
        )


def load_function_budgets(config_path: Path, scripts_root: Path) -> List[FunctionBudget]:
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    entries = raw.get("budgets", [])
    budgets: List[FunctionBudget] = []

    for entry in entries:
        relative_file = str(entry["file"]).strip()
        language = str(entry["language"]).strip().lower()
        file_path = Path(relative_file)
        if not file_path.is_absolute():
            file_path = scripts_root / file_path
        budget = FunctionBudget(
            file_path=file_path.resolve(),
            function_name=str(entry["function"]).strip(),
            max_lines=int(entry["max_lines"]),
            language=language,
        )
        budgets.append(budget)

    return budgets


def _scan_python_functions(file_path: Path) -> List[FunctionMeasurement]:
    tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
    functions: List[FunctionMeasurement] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.end_lineno is None:
            continue
        functions.append(FunctionMeasurement(node.name, node.lineno, node.end_lineno))
    return functions


def _scan_shell_functions(file_path: Path) -> List[FunctionMeasurement]:
    lines = file_path.read_text(encoding="utf-8").splitlines()
    functions: List[FunctionMeasurement] = []
    index = 0

    while index < len(lines):
        start_match = _SHELL_FUNCTION_RE.match(lines[index])
        if not start_match:
            index += 1
            continue

        function_name = start_match.group(1)
        start_line = index + 1
        depth = lines[index].count("{") - lines[index].count("}")
        cursor = index
        while cursor + 1 < len(lines) and depth > 0:
            cursor += 1
            depth += lines[cursor].count("{")
            depth -= lines[cursor].count("}")

        end_line = cursor + 1
        functions.append(FunctionMeasurement(function_name, start_line, end_line))
        index = cursor + 1

    return functions


def scan_functions_for_budget(budget: FunctionBudget) -> List[FunctionMeasurement]:
    if budget.language == "python":
        return _scan_python_functions(budget.file_path)
    if budget.language == "shell":
        return _scan_shell_functions(budget.file_path)
    raise ValueError(f"Unsupported language '{budget.language}' for {budget.file_path}")


def evaluate_function_budgets(budgets: Sequence[FunctionBudget]) -> List[FunctionSizeViolation]:
    violations: List[FunctionSizeViolation] = []
    cache: Dict[tuple[Path, str], List[FunctionMeasurement]] = {}

    for budget in budgets:
        cache_key = (budget.file_path, budget.language)
        measurements = cache.get(cache_key)
        if measurements is None:
            measurements = scan_functions_for_budget(budget)
            cache[cache_key] = measurements

        matches = [m for m in measurements if m.name == budget.function_name]
        if not matches:
            violations.append(
                FunctionSizeViolation(
                    file_path=budget.file_path,
                    function_name=budget.function_name,
                    max_lines=budget.max_lines,
                    actual_lines=None,
                    reason="function_not_found",
                )
            )
            continue

        if len(matches) > 1:
            violations.append(
                FunctionSizeViolation(
                    file_path=budget.file_path,
                    function_name=budget.function_name,
                    max_lines=budget.max_lines,
                    actual_lines=None,
                    reason="ambiguous_function_name",
                )
            )
            continue

        measurement = matches[0]
        if measurement.length > budget.max_lines:
            violations.append(
                FunctionSizeViolation(
                    file_path=budget.file_path,
                    function_name=budget.function_name,
                    max_lines=budget.max_lines,
                    actual_lines=measurement.length,
                    reason="max_lines_exceeded",
                )
            )

    return violations


def render_violations(violations: Iterable[FunctionSizeViolation]) -> List[str]:
    return [violation.render() for violation in violations]
