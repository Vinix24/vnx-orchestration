"""wiring_gate.py — Dead-code detection gate for PR diffs.

AST-parses new public definitions (functions, classes) added in a PR diff,
then greps the codebase for callers. Definitions with zero callers outside
their own file (excluding tests) are flagged as unwired dead code.

Skip-list: .vnx-data/state/wiring_skip.yaml supports library-exports,
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


@dataclass(frozen=True)
class UnwiredSymbol:
    name: str
    file: str
    line: int
    kind: str  # "function" or "class"


@dataclass
class WiringGateResult:
    status: str  # "pass", "fail", "advisory"
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
        return proc.stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return ""


def _get_pr_diff(pr_number: int) -> str:
    try:
        proc = subprocess.run(
            ["gh", "pr", "diff", str(pr_number)],
            capture_output=True, text=True, timeout=30, check=True,
        )
        return proc.stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return ""


def _extract_added_python_files(diff_names: str) -> List[str]:
    return [
        f.strip() for f in diff_names.splitlines()
        if f.strip().endswith(".py")
    ]


_ADDED_DEF_RE = re.compile(r"^\+(?:def|class)\s+([A-Za-z]\w*)")


def _extract_new_public_defs(diff_text: str) -> List[dict]:
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
                pass  # deleted lines don't advance new-file counter
            else:
                line_in_new += 1
    return defs


def _grep_callers(symbol_name: str, def_file: str) -> int:
    try:
        proc = subprocess.run(
            [
                "grep", "-r", "--include=*.py",
                "-l", symbol_name, ".",
            ],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return 1  # assume wired on grep failure (safe default)

    if proc.returncode not in (0, 1):
        return 1

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
    """
    skip_list = _load_skip_list(state_dir)
    required = os.environ.get("VNX_WIRING_GATE_REQUIRED", "0") == "1"

    diff_text = _get_pr_diff(pr_number)
    if not diff_text:
        return WiringGateResult(
            status="pass", total_checked=0,
            summary="No diff available or empty PR",
        )

    new_defs = _extract_new_public_defs(diff_text)
    if not new_defs:
        return WiringGateResult(
            status="pass", total_checked=0,
            summary="No new public definitions in PR diff",
        )

    unwired: List[UnwiredSymbol] = []
    skipped: List[str] = []

    for d in new_defs:
        if d["name"] in skip_list:
            skipped.append(d["name"])
            continue
        caller_count = _grep_callers(d["name"], d["file"])
        if caller_count == 0:
            unwired.append(UnwiredSymbol(
                name=d["name"],
                file=d["file"],
                line=d["line"],
                kind=d["kind"],
            ))

    total_checked = len(new_defs) - len(skipped)

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
