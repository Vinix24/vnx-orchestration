#!/usr/bin/env python3
"""Central-mode path-correctness gate.

Fails when a ``.vnx-data`` / ``ROADMAP.yaml`` path literal is built from a
``__file__``-anchored expression OUTSIDE the canonical resolvers
(``vnx_paths.py`` / ``project_root.py``).

Why this bug class matters
--------------------------
In a CENTRAL install the shared library lives under
``~/.vnx-system/current/scripts/lib/``. Any ``Path(__file__)….parent…/.vnx-data``
walk therefore resolves the KEYSTONE (``~/.vnx-system/versions/<v>/.vnx-data``)
instead of the project's ``~/.vnx-data/<project>`` — writing runtime state into
the read-only install tree and reading state that will never be there. The same
applies to ``ROADMAP.yaml`` resolved two-up from a central state dir. All
data/roadmap paths must route through ``vnx_paths.resolve_*`` /
``project_root.resolve_*``, which are VNX_HOME + project-marker aware. See
#1023 / #1024 and the ``central-mode-path-correctness`` track.

Detection
---------
AST-based, so comments and docstrings never trip it: a violation is a
``.vnx-data`` / ``ROADMAP.yaml`` string constant that sits inside a path-join
(``/`` BinOp) or ``Path(...)`` call whose expression also references
``__file__`` *or* a module-/function-level name that is itself bound to a
``__file__``-anchored expression (``_REPO_ROOT``, ``_THIS_DIR``, ``here``,
``script_dir``, ...). A ``state_dir.parent.parent`` built from a resolved
runtime Path parameter is NOT flagged — only ``__file__``-anchored derivations.

Scope
-----
``scripts/lib/`` — the shared runtime library that runs in BOTH dev and central
contexts. The top-level ``scripts/*.py`` entry-points resolve their own root
and are out of scope for this gate (tracked separately).

Grandfathering
--------------
The two canonical resolvers are exempt (they are *supposed* to derive from
``__file__``). Pre-existing library occurrences that this task did not migrate
are listed in ``GRANDFATHERED`` with a reason. The gate blocks NEW occurrences
and any change to a grandfathered line, so the bug class cannot silently return.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCAN_DIR = "scripts/lib"

DATA_PATH_MARKERS = (".vnx-data", "ROADMAP.yaml")

# The canonical resolvers: these files are the ONE place allowed to derive the
# data/roadmap roots from __file__.
EXEMPT_FILES = frozenset({"vnx_paths.py", "project_root.py"})

# Pre-existing __file__-anchored data-path derivations in scripts/lib that this
# task (central-mode-path-correctness) did not migrate. Keyed by repo-relative
# path -> set of normalized offending expression segments. These are the SAME
# bug class and should be migrated to the canonical resolvers in follow-up work;
# they are grandfathered so the gate can pass on the current tree while blocking
# NEW occurrences. A change to any listed line drops it from the match and trips
# the gate — forcing migration rather than silent perpetuation.
GRANDFATHERED: Dict[str, Set[str]] = {
    # env-first (VNX_DATA_DIR) with a repo-relative _REPO_ROOT default; only the
    # default branch is __file__-anchored.
    "scripts/lib/governance_audit.py": {
        'Path(os.environ.get("VNX_DATA_DIR", str(_REPO_ROOT / ".vnx-data")))',
    },
    "scripts/lib/governance_enforcer.py": {
        'Path(os.environ.get("VNX_DATA_DIR", str(_REPO_ROOT / ".vnx-data")))',
    },
    # _REPO_ROOT / _LIB_DIR co-located-layout fallbacks (env checked upstream).
    "scripts/lib/gate_register_emit.py": {
        '_REPO_ROOT / ".vnx-data" / "state" / "dispatch_register.ndjson"',
    },
    "scripts/lib/dispatch_register.py": {
        '_REPO_ROOT / ".vnx-data" / "state"',
    },
    "scripts/lib/state_rebuild_trigger.py": {
        '_REPO_ROOT / ".vnx-data" / "state"',
    },
    "scripts/lib/pool_worker_runner.py": {
        '_LIB_DIR.parents[1] / ".vnx-data" / "state"',
        '_LIB_DIR.parents[1] / ".vnx-data" / "dispatches"',
    },
    # Direct Path(__file__)… walks (env checked upstream in each caller).
    "scripts/lib/dispatch_parameter_tracker.py": {
        'Path(__file__).resolve().parents[2] / ".vnx-data" / "state"',
    },
    "scripts/lib/llm_decision_router.py": {
        'Path(__file__).resolve().parents[2] / ".vnx-data"',
    },
    "scripts/lib/receipt_classifier.py": {
        'Path(__file__).resolve().parents[2] / ".vnx-data" / "state"',
    },
    "scripts/lib/session_store.py": {
        'Path(__file__).resolve().parent.parent.parent / ".vnx-data" / "state"',
    },
    "scripts/lib/worker_health_monitor.py": {
        'Path(__file__).resolve().parents[2] / ".vnx-data" / "events"',
    },
    # here.parent.parent.parent quality_intelligence.db lookups (env-first,
    # .exists()-gated).
    "scripts/lib/intelligence_dashboard_data.py": {
        'here.parent.parent.parent / ".vnx-data" / "state" / "quality_intelligence.db"',
    },
    "scripts/lib/pattern_extractor.py": {
        'here.parent.parent.parent / ".vnx-data" / "state" / "quality_intelligence.db"',
    },
    "scripts/lib/intelligence_backfill.py": {
        # env-first (VNX_STATE_DIR), .exists()-gated; _PROJECT_ROOT is __file__-anchored.
        '_PROJECT_ROOT / ".vnx-data" / "state" / "quality_intelligence.db"',
    },
    # .is_dir()-gated upward walks that look for an EXISTING .vnx-data; canonical
    # resolution (VNX_DATA_DIR env, or ensure_env()) is tried first in each caller.
    "scripts/lib/subprocess_adapter.py": {
        'search / ".vnx-data"',
    },
    "scripts/lib/headless_dispatch_writer.py": {
        # vnx_paths.ensure_env() is the primary resolver here; this walk is the
        # except-branch last resort.
        'candidate / ".vnx-data"',
    },
}


# ---------------------------------------------------------------------------
# Core detection
# ---------------------------------------------------------------------------


def _file_anchored_names(tree: ast.AST) -> Set[str]:
    """Names bound anywhere to an expression that references ``__file__``.

    Catches module-level and local assignments such as
    ``_REPO_ROOT = Path(__file__).resolve().parents[2]`` or
    ``here = Path(__file__).resolve()``.
    """
    anchored: Set[str] = set()
    for node in ast.walk(tree):
        value = None
        targets: List[ast.expr] = []
        if isinstance(node, ast.Assign):
            value = node.value
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            value = node.value
            targets = [node.target]
        if value is None:
            continue
        if any(isinstance(n, ast.Name) and n.id == "__file__" for n in ast.walk(value)):
            for tgt in targets:
                if isinstance(tgt, ast.Name):
                    anchored.add(tgt.id)
    return anchored


def _references_file_anchor(node: ast.AST, anchored: Set[str]) -> bool:
    """True if the subtree references ``__file__`` or a file-anchored name."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and (sub.id == "__file__" or sub.id in anchored):
            return True
    return False


def _has_data_marker_constant(node: ast.AST) -> bool:
    """True if the subtree contains a ``.vnx-data`` / ``ROADMAP.yaml`` string constant."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            if any(marker in sub.value for marker in DATA_PATH_MARKERS):
                return True
    return False


def _segment(source: str, node: ast.AST) -> str:
    """Return the normalized source text of an expression node."""
    try:
        seg = ast.get_source_segment(source, node) or ""
    except Exception:
        seg = ""
    return " ".join(seg.split())


def check_source(source: str) -> List[Tuple[int, str]]:
    """Return (lineno, normalized_segment) violations for one file's source."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    anchored = _file_anchored_names(tree)

    violations: List[Tuple[int, str]] = []
    seen: Set[Tuple[int, str]] = set()
    # Candidate path expressions: '/' path-joins and Path(...) calls.
    for node in ast.walk(tree):
        is_join = isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
        is_path_call = (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Path"
        )
        if not (is_join or is_path_call):
            continue
        if not _has_data_marker_constant(node):
            continue
        if not _references_file_anchor(node, anchored):
            continue
        key = (getattr(node, "lineno", 0), _segment(source, node))
        if key in seen:
            continue
        seen.add(key)
        violations.append(key)
    return violations


def _dedup_violations(source: str) -> List[Tuple[int, str]]:
    """Deduplicate nested matches to the fullest offending expression per line.

    ``_REPO_ROOT / ".vnx-data" / "state"`` yields nested BinOps sharing a lineno;
    keep the LONGEST segment so the grandfather key captures the whole path
    (a change to any component then trips the gate rather than being masked).
    """
    raw = check_source(source)
    if not raw:
        return []
    by_line: Dict[int, Tuple[int, str]] = {}
    for lineno, seg in raw:
        cur = by_line.get(lineno)
        if cur is None or len(seg) > len(cur[1]):
            by_line[lineno] = (lineno, seg)
    return sorted(by_line.values())


def scan_dir(root: Path) -> List[Tuple[str, int, str]]:
    """Return all non-grandfathered violations under ``root/scripts/lib``.

    Result tuples are (repo_relative_path, lineno, normalized_segment).
    """
    scan_root = root / SCAN_DIR
    out: List[Tuple[str, int, str]] = []
    if not scan_root.is_dir():
        return out
    for py in sorted(scan_root.rglob("*.py")):
        if py.name in EXEMPT_FILES:
            continue
        rel = py.relative_to(root).as_posix()
        try:
            source = py.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        allowed = GRANDFATHERED.get(rel, set())
        for lineno, seg in _dedup_violations(source):
            if seg in allowed:
                continue
            out.append((rel, lineno, seg))
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: List[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    root = Path(args[0]).resolve() if args else Path.cwd()

    violations = scan_dir(root)
    if not violations:
        print("[central-mode-paths] PASS — no __file__-derived data-path literals.")
        return 0

    print(f"[central-mode-paths] FAIL — {len(violations)} violation(s):\n")
    for rel, lineno, seg in violations:
        print(
            f"::error file={rel},line={lineno}::central-mode path bug: "
            f"'{seg}' derives a .vnx-data/ROADMAP path from __file__.\n"
            f"In a central install this resolves the keystone, not "
            f"~/.vnx-data/<project>. Route through vnx_paths.resolve_data_root()/"
            f"resolve_state_dir() (VNX_HOME + project-marker aware). See #1023.\n"
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
