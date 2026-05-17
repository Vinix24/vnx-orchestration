"""wiring_gate.py — Dead-code detection gate for PR diffs.

AST-parses new public definitions (functions, classes) added in a PR diff,
then greps the codebase for callers. Definitions with zero callers outside
their own file (excluding tests) are flagged as unwired dead code.

Skip-list: ${VNX_STATE_DIR}/wiring_skip.yaml supports library-exports,
decorator-registry entries, __all__ re-exports, and CLI dispatch-dicts.

Env: VNX_WIRING_GATE_REQUIRED (default "0" = shadow/advisory, "1" = blocking).
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml


class WiringGateError(Exception):
    pass


@dataclass(frozen=True)
class UnwiredSymbol:
    name: str
    file: str
    line: int
    kind: str  # "function" or "class"


@dataclass
class WiringGateResult:
    status: str  # "pass", "fail", "advisory", "error"
    unwired: List[UnwiredSymbol] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    total_checked: int = 0
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "unwired": [
                {"name": s.name, "file": s.file, "line": s.line, "kind": s.kind}
                for s in self.unwired
            ],
            "skipped": self.skipped,
            "total_checked": self.total_checked,
            "summary": self.summary,
        }


def _load_skip_list(state_dir: Optional[Path] = None) -> set:
    if state_dir is None:
        state_dir = Path(os.environ.get("VNX_STATE_DIR", ".vnx-data/state"))
    skip_path = state_dir / "wiring_skip.yaml"
    if not skip_path.exists():
        return set()
    try:
        data = yaml.safe_load(skip_path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return set()
    if not isinstance(data, dict):
        return set()
    symbols = set()
    for key in ("library_exports", "decorator_registry", "all_reexports", "cli_dispatch"):
        entries = data.get(key, [])
        if isinstance(entries, list):
            symbols.update(str(e) for e in entries)
    return symbols


def _get_pr_diff_files(pr_number: int) -> str:
    try:
        proc = subprocess.run(
            ["gh", "pr", "diff", str(pr_number), "--name-only"],
            capture_output=True, text=True, timeout=15, check=True,
        )
    except subprocess.TimeoutExpired as e:
        raise WiringGateError(f"gh pr diff --name-only timed out for PR #{pr_number}") from e
    except (subprocess.CalledProcessError, OSError) as e:
        raise WiringGateError(f"gh pr diff --name-only failed for PR #{pr_number}: {e}") from e
    return proc.stdout


def _get_pr_diff(pr_number: int) -> str:
    try:
        proc = subprocess.run(
            ["gh", "pr", "diff", str(pr_number)],
            capture_output=True, text=True, timeout=30, check=True,
        )
    except subprocess.TimeoutExpired as e:
        raise WiringGateError(f"gh pr diff timed out for PR #{pr_number}") from e
    except (subprocess.CalledProcessError, OSError) as e:
        raise WiringGateError(f"gh pr diff failed for PR #{pr_number}: {e}") from e
    return proc.stdout


def _extract_added_python_files(diff_names: str) -> List[str]:
    return [
        f.strip() for f in diff_names.splitlines()
        if f.strip().endswith(".py")
    ]


_ADDED_DEF_RE = re.compile(r"^\+(?:async\s+)?(?:def|class)\s+([A-Za-z]\w*)")


def _extract_added_source_per_file(diff_text: str) -> dict[str, tuple[str, dict[int, int]]]:
    """Extract full added source per .py file from a unified diff.

    Returns {filepath: (source_text, {source_line: diff_line})} where source_text
    is the reconstructed new-file content from added lines, and the mapping allows
    translating AST lineno back to the actual file line in the PR.
    """
    files: dict[str, tuple[list[str], dict[int, int]]] = {}
    current_file: Optional[str] = None
    line_in_new = 0
    hunk_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

    for line in diff_text.splitlines():
        if line.startswith("diff --git"):
            match = re.search(r" b/(.+)$", line)
            current_file = match.group(1) if match else None
            line_in_new = 0
            continue
        if line.startswith("@@"):
            m = hunk_re.match(line)
            if m:
                line_in_new = int(m.group(1)) - 1
            continue
        if current_file and current_file.endswith(".py"):
            if line.startswith("+") and not line.startswith("+++"):
                line_in_new += 1
                if current_file not in files:
                    files[current_file] = ([], {})
                src_lines, line_map = files[current_file]
                src_lines.append(line[1:])  # strip leading '+'
                line_map[len(src_lines)] = line_in_new
            elif line.startswith("-"):
                pass
            else:
                line_in_new += 1

    return {fp: ("\n".join(lines), lmap) for fp, (lines, lmap) in files.items()}


def _extract_defs_via_ast(source: str, filepath: str, line_map: dict[int, int]) -> List[dict]:
    """Parse source with AST and extract public top-level function/class defs."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    defs = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                diff_line = line_map.get(node.lineno, node.lineno)
                defs.append({
                    "name": node.name,
                    "file": filepath,
                    "line": diff_line,
                    "kind": "function",
                })
        elif isinstance(node, ast.ClassDef):
            if not node.name.startswith("_"):
                diff_line = line_map.get(node.lineno, node.lineno)
                defs.append({
                    "name": node.name,
                    "file": filepath,
                    "line": diff_line,
                    "kind": "class",
                })
    return defs


def _extract_defs_via_regex(diff_text: str) -> List[dict]:
    """Fallback regex-based extraction when AST parse fails."""
    defs = []
    current_file = None
    line_in_new = 0
    hunk_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

    for line in diff_text.splitlines():
        if line.startswith("diff --git"):
            match = re.search(r" b/(.+)$", line)
            current_file = match.group(1) if match else None
            line_in_new = 0
            continue
        if line.startswith("@@"):
            m = hunk_re.match(line)
            if m:
                line_in_new = int(m.group(1)) - 1
            continue
        if current_file and current_file.endswith(".py"):
            if line.startswith("+") and not line.startswith("+++"):
                line_in_new += 1
                m = _ADDED_DEF_RE.match(line)
                if m:
                    name = m.group(1)
                    if not name.startswith("_"):
                        kind = "class" if line[1:].strip().startswith("class") else "function"
                        defs.append({
                            "name": name,
                            "file": current_file,
                            "line": line_in_new,
                            "kind": kind,
                        })
            elif line.startswith("-"):
                pass
            else:
                line_in_new += 1
    return defs


def _extract_new_public_defs(diff_text: str) -> List[dict]:
    """Extract new public definitions from diff. Uses AST as primary, regex as fallback."""
    file_sources = _extract_added_source_per_file(diff_text)

    all_defs: List[dict] = []
    ast_failed_files: set[str] = set()

    for filepath, (source, line_map) in file_sources.items():
        ast_defs = _extract_defs_via_ast(source, filepath, line_map)
        if ast_defs:
            all_defs.extend(ast_defs)
        else:
            ast_failed_files.add(filepath)

    if ast_failed_files:
        regex_defs = _extract_defs_via_regex(diff_text)
        for d in regex_defs:
            if d["file"] in ast_failed_files:
                all_defs.append(d)

    return all_defs


def _grep_callers(symbol_name: str, def_file: str) -> Optional[int]:
    """Count non-test callers of symbol outside its definition file.

    Returns None on grep failure (unknown state) — callers must treat
    None as blocking rather than assuming the symbol is wired.
    """
    try:
        proc = subprocess.run(
            [
                "grep", "-r", "--include=*.py",
                "-l", symbol_name, ".",
            ],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None

    if proc.returncode not in (0, 1):
        return None

    callers = 0
    for hit_file in proc.stdout.splitlines():
        hit_file = hit_file.strip().lstrip("./")
        if hit_file == def_file:
            continue
        if hit_file.startswith("tests/") or "/tests/" in hit_file or hit_file.startswith("test_"):
            continue
        callers += 1
    return callers


def check_pr_wiring(pr_number: int, *, state_dir: Optional[Path] = None) -> WiringGateResult:
    """Check a PR for unwired (dead) public definitions.

    Returns WiringGateResult with status:
      - "pass": all new public defs have callers
      - "fail": unwired defs found AND VNX_WIRING_GATE_REQUIRED=1
      - "advisory": unwired defs found but gate is in shadow mode
      - "error": subprocess failure fetching diff (gate cannot determine wiring)

    Raises WiringGateError if the diff cannot be fetched — the gate runner
    must catch this and mark the gate outcome as FAILED, never PASS with 0 checks.
    """
    skip_list = _load_skip_list(state_dir)
    required = os.environ.get("VNX_WIRING_GATE_REQUIRED", "0") == "1"

    diff_text = _get_pr_diff(pr_number)
    if not diff_text:
        return WiringGateResult(
            status="pass", total_checked=0,
            summary="Empty PR diff (no changes)",
        )

    new_defs = _extract_new_public_defs(diff_text)
    if not new_defs:
        return WiringGateResult(
            status="pass", total_checked=0,
            summary="No new public definitions in PR diff",
        )

    unwired: List[UnwiredSymbol] = []
    skipped: List[str] = []
    grep_failures: List[str] = []

    for d in new_defs:
        if d["name"] in skip_list:
            skipped.append(d["name"])
            continue
        caller_count = _grep_callers(d["name"], d["file"])
        if caller_count is None:
            grep_failures.append(d["name"])
            unwired.append(UnwiredSymbol(
                name=d["name"],
                file=d["file"],
                line=d["line"],
                kind=d["kind"],
            ))
        elif caller_count == 0:
            unwired.append(UnwiredSymbol(
                name=d["name"],
                file=d["file"],
                line=d["line"],
                kind=d["kind"],
            ))

    total_checked = len(new_defs) - len(skipped)

    if grep_failures:
        status = "fail"
        names = ", ".join(grep_failures[:5])
        tail = f" (+{len(grep_failures) - 5} more)" if len(grep_failures) > 5 else ""
        summary = f"grep failed for {len(grep_failures)} symbol(s) (treated as blocking): {names}{tail}"
        return WiringGateResult(
            status=status,
            unwired=unwired,
            skipped=skipped,
            total_checked=total_checked,
            summary=summary,
        )

    if not unwired:
        return WiringGateResult(
            status="pass",
            unwired=[],
            skipped=skipped,
            total_checked=total_checked,
            summary=f"All {total_checked} new public definitions are wired",
        )

    status = "fail" if required else "advisory"
    names = ", ".join(s.name for s in unwired[:5])
    tail = f" (+{len(unwired) - 5} more)" if len(unwired) > 5 else ""
    summary = f"{len(unwired)} unwired symbol(s): {names}{tail}"

    return WiringGateResult(
        status=status,
        unwired=unwired,
        skipped=skipped,
        total_checked=total_checked,
        summary=summary,
    )
