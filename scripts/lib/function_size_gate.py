#!/usr/bin/env python3
"""Function-size gate for critical VNX scripts."""

from __future__ import annotations

import ast
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# Per-PR ratchet: a function may not cross this many executable lines.
FUNCTION_SIZE_LIMIT = 70
RATCHET_SCOPE_DIRS = ("scripts/", "vnx_cli/", "dashboard/")

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


# ---------------------------------------------------------------------------
# Per-PR ratchet: executable-line counting and base/head comparison
# ---------------------------------------------------------------------------


class GitError(RuntimeError):
    """A git invocation failed; never to be read as "no violations"."""


@dataclass(frozen=True)
class MeasuredFunction:
    qualname: str
    occurrence: int
    lineno: int
    executable_lines: int

    @property
    def identity(self) -> Tuple[str, int]:
        return (self.qualname, self.occurrence)


@dataclass(frozen=True)
class RatchetViolation:
    path: str
    lineno: int
    qualname: str
    base_lines: Optional[int]
    head_lines: Optional[int]
    kind: str

    def render(self) -> str:
        if self.kind == "unparseable":
            return f"{self.path}:{self.lineno} {self.qualname}: unparseable at head [unparseable]"
        base = "new" if self.base_lines is None else str(self.base_lines)
        return (
            f"{self.path}:{self.lineno} {self.qualname}: base {base} -> head "
            f"{self.head_lines} (limit {FUNCTION_SIZE_LIMIT}) [{self.kind}]"
        )


def _docstring_span(node: ast.AST) -> range:
    body = getattr(node, "body", None) or []
    first = body[0] if body else None
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
        and first.end_lineno is not None
    ):
        return range(first.lineno, first.end_lineno + 1)
    return range(0)


def count_executable_lines(source_lines: Sequence[str], node: ast.AST) -> int:
    """Lines in the node span that are not blank, comment-only or own docstring."""
    doc = _docstring_span(node)
    count = 0
    for number in range(node.lineno, node.end_lineno + 1):
        stripped = source_lines[number - 1].strip()
        if not stripped or stripped.startswith("#") or number in doc:
            continue
        count += 1
    return count


def _collect(node: ast.AST, prefix: str, lines: Sequence[str], out: List[Tuple[str, int, int]]) -> None:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qualname = f"{prefix}{child.name}"
            out.append((qualname, child.lineno, count_executable_lines(lines, child)))
            _collect(child, f"{qualname}.<locals>.", lines, out)
        elif isinstance(child, ast.ClassDef):
            _collect(child, f"{prefix}{child.name}.", lines, out)
        else:
            _collect(child, prefix, lines, out)


def measure_source(source: str, filename: str = "<source>") -> List[MeasuredFunction]:
    """Measure every function in source. Raises SyntaxError when it does not parse."""
    tree = ast.parse(source, filename=filename)
    raw: List[Tuple[str, int, int]] = []
    _collect(tree, "", source.splitlines(), raw)
    seen: Dict[str, int] = {}
    measured: List[MeasuredFunction] = []
    for qualname, lineno, count in raw:
        occurrence = seen.get(qualname, 0)
        seen[qualname] = occurrence + 1
        measured.append(MeasuredFunction(qualname, occurrence, lineno, count))
    return measured


def _git(repo: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
        )
    except OSError as exc:
        raise GitError(f"cannot run git: {exc}") from exc
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def _in_scope(path: str) -> bool:
    parts = path.split("/")
    return (
        path.endswith(".py")
        and path.startswith(RATCHET_SCOPE_DIRS)
        and "tests" not in parts
        and "spikes" not in parts
    )


def changed_files(repo: Path, merge_base: str, head: str) -> List[Tuple[str, Optional[str]]]:
    """(head_path, base_path) pairs for in-scope files that exist at head."""
    raw = _git(repo, "diff", "--name-status", "-M", "-z", merge_base, head)
    tokens = raw.split("\0")
    pairs: List[Tuple[str, Optional[str]]] = []
    index = 0
    while index < len(tokens) and tokens[index]:
        status = tokens[index]
        if status.startswith("R"):
            old, new = tokens[index + 1], tokens[index + 2]
            index += 3
            if _in_scope(new):
                pairs.append((new, old))
            continue
        path = tokens[index + 1]
        index += 2
        if status.startswith("D") or not _in_scope(path):
            continue
        pairs.append((path, None if status.startswith("A") else path))
    return pairs


def _measure_at(repo: Path, ref: str, path: str) -> Optional[List[MeasuredFunction]]:
    """None when the file does not parse."""
    try:
        return measure_source(_git(repo, "show", f"{ref}:{path}"), filename=path)
    except SyntaxError:
        return None


def _compare_file(
    path: str, head_fns: List[MeasuredFunction], base_fns: List[MeasuredFunction]
) -> List[RatchetViolation]:
    base_by_id = {fn.identity: fn.executable_lines for fn in base_fns}
    found: List[RatchetViolation] = []
    for fn in head_fns:
        if fn.executable_lines <= FUNCTION_SIZE_LIMIT:
            continue
        base = base_by_id.get(fn.identity)
        if base is None or base <= FUNCTION_SIZE_LIMIT:
            kind = "crossed"
        elif fn.executable_lines > base:
            kind = "grew"
        else:
            continue
        found.append(RatchetViolation(path, fn.lineno, fn.qualname, base, fn.executable_lines, kind))
    return found


def check_ratchet(repo: Path, base: str, head: str = "HEAD") -> List[RatchetViolation]:
    """Compare function sizes between the merge base of base/head and head."""
    merge_base = _git(repo, "merge-base", base, head).strip()
    violations: List[RatchetViolation] = []
    for path, old_path in changed_files(repo, merge_base, head):
        head_fns = _measure_at(repo, head, path)
        if head_fns is None:
            violations.append(RatchetViolation(path, 1, "<module>", None, None, "unparseable"))
            continue
        base_fns = (_measure_at(repo, merge_base, old_path) or []) if old_path else []
        violations.extend(_compare_file(path, head_fns, base_fns))
    return violations
