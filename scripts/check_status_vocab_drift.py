#!/usr/bin/env python3
"""CI drift check: the receipt completion-status vocabulary has one source.

``event_outcome_semantics._STATUS_VOCABULARY`` is the single generated source
for the completion-outcome status vocabulary. Any OTHER module that hard-codes
its own module-level set/frozenset of status literals drawn entirely from the
canonical ``FAILURE_STATUSES | SUCCESS_STATUSES`` is a drifting copy and fails
this check.

Why AST, not import: reading source files means the check runs in CI without
executing repo code, and it catches a copy the moment it is written (before a
new canonical literal is even added and drift becomes observable at runtime).

A module-level name is in scope when it contains ``STATUS`` and is assigned a
set/frozenset literal with at least two string elements, all of which are in
the canonical outcome set. A single-literal constant or a name without
``STATUS`` is not a collection worth flagging; a set that contains even one
non-canonical literal is a DIFFERENT vocabulary (e.g. the review-verdict
``pass``/``fail`` set), not a copy of this one.

Exit 0 when clean, 1 when a drifting copy is found. Importable for tests via
``find_hardcoded_outcome_vocab(repo_root)``.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List

_SCRIPTS_DIR = Path(__file__).resolve().parent
_SCRIPTS_LIB = _SCRIPTS_DIR / "lib"
sys.path.insert(0, str(_SCRIPTS_LIB))

from event_outcome_semantics import FAILURE_STATUSES, SUCCESS_STATUSES  # noqa: E402

_CANONICAL = FAILURE_STATUSES | SUCCESS_STATUSES

# Known constants whose own module-level STATUS set is a legitimately-distinct
# vocabulary, not a copy of the completion-outcome vocabulary. Keyed by
# (basename, name) — not by whole file — so a future drifting copy added to
# one of these files is still caught. Each reason names the semantic the
# constant actually serves, so the allow-list documents itself rather than
# silently swallowing a real copy.
_ALLOWLIST = {
    # ADR-035 §3.1 review-verdict completion-claim vocabulary: "is a success
    # status" for gate verdicts, a narrower claim than the completion-outcome
    # vocabulary (it deliberately omits "ok"). Not an outcome classification.
    ("receipt_verdict.py", "SUCCESS_STATUSES"):
        "ADR-035 review-verdict completion-claim vocabulary, omits 'ok'",
    # Shares receipt_verdict's completion-claim set; phantom_guard uses it to
    # decide whether a completion claim is even eligible for scrutiny.
    ("phantom_guard.py", "COMPLETION_STATUSES"):
        "mirrors receipt_verdict completion-claim vocabulary",
    # _AUTHORITATIVE_STATUSES = {done, failed} is the receipt-dedup authority
    # tier (authored > synthesized), not an outcome classification.
    ("dispatch_govern.py", "_AUTHORITATIVE_STATUSES"):
        "receipt-dedup authority tier (done/failed)",
}


@dataclass
class Violation:
    path: str
    line: int
    name: str
    literals: List[str]


def _iter_module_level_assignments(tree: ast.Module):
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    yield target.id, node.value, node.lineno
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            yield node.target.id, node.value, node.lineno


def _string_elements(node) -> List[str]:
    """Return string literal elements of a set/frozenset literal, else []."""
    inner = None
    if isinstance(node, ast.Set):
        inner = node.elts
    elif (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "frozenset"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Set)
    ):
        inner = node.args[0].elts
    if inner is None:
        return []
    literals: List[str] = []
    for elt in inner:
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
            literals.append(elt.value)
    return literals


def find_hardcoded_outcome_vocab(repo_root: Path) -> List[Violation]:
    """Scan scripts/ for hard-coded copies of the completion-outcome vocabulary."""
    violations: List[Violation] = []
    scripts_root = repo_root / "scripts"
    if not scripts_root.is_dir():
        return violations

    for py_file in sorted(scripts_root.rglob("*.py")):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue

        for name, value, lineno in _iter_module_level_assignments(tree):
            if "STATUS" not in name.upper():
                continue
            if (py_file.name, name) in _ALLOWLIST:
                continue
            literals = _string_elements(value)
            if len(literals) < 2:
                continue
            if all(lit in _CANONICAL for lit in literals):
                violations.append(
                    Violation(
                        path=str(py_file.relative_to(repo_root)),
                        line=lineno,
                        name=name,
                        literals=sorted(literals),
                    )
                )
    return violations


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    violations = find_hardcoded_outcome_vocab(repo_root)
    if not violations:
        print("check_status_vocab_drift: clean — no hard-coded outcome vocab copies")
        return 0

    print(
        "check_status_vocab_drift: FAILED — hard-coded copies of the completion-"
        "outcome vocabulary found. Import event_outcome_semantics instead."
    )
    for v in violations:
        print(f"  {v.path}:{v.line} {v.name} = {sorted(v.literals)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
