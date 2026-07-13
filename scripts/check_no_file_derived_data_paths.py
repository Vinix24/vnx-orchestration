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
``__file__``). Every occurrence listed in ``GRANDFATHERED`` has been migrated
(central-mode-path-correctness track, centralmode-embedded-sweep dispatch) to
try the canonical ``vnx_paths.resolve_paths()`` resolver FIRST; the listed
``__file__``-anchored expression is what remains as a defensive last-resort
fallback for the rare case where the canonical resolver itself is unavailable
(e.g. import failure). The gate blocks NEW occurrences and any change to a
grandfathered line, so the bug class cannot silently return.
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
    # central-mode-embedded-sweep (2026-07-13): migrated to try vnx_paths.resolve_paths()
    # first; _REPO_ROOT / ".vnx-data" now only fires in the except-Exception
    # last-resort branch (mirrors the dispatch_register.py pattern below).
    "scripts/lib/governance_audit.py": {
        '_REPO_ROOT / ".vnx-data"',
    },
    "scripts/lib/governance_enforcer.py": {
        '_REPO_ROOT / ".vnx-data"',
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
    # :848 reuses the same project_root, guarded by the VNX_DATA_DIR default.
    "scripts/lib/subprocess_adapter.py": {
        'search / ".vnx-data"',
        'Path(project_root) / ".vnx-data"',
    },
    "scripts/lib/headless_dispatch_writer.py": {
        # vnx_paths.ensure_env() is the primary resolver here; this walk is the
        # except-branch last resort.
        'candidate / ".vnx-data"',
    },
    # Surfaced by the helper-return tracing (below). Pre-existing, out of this
    # task's remit; each is env-first / .exists()-gated / repo_root-param-threaded
    # with the __file__ walk only as a fallback. Tracked for follow-up migration.
    "scripts/lib/event_analyzer.py": {
        # .exists()-gated: only returns the derived archive dir if it exists.
        'repo_root / ".vnx-data" / "events" / "archive"',
    },
    "scripts/lib/headless_dispatch_daemon.py": {
        # env-first (VNX_DATA_DIR); _repo_root() is the helper-return fallback.
        '_repo_root() / ".vnx-data"',
    },
    "scripts/lib/tmux_worktree.py": {
        # _resolve_repo_root threads an explicit repo_root first (#1023); the
        # __file__ branch is the no-arg fallback.
        'root / ".vnx-data" / "worktrees" / f"dispatch-{dispatch_id}"',
    },
    # centralmode-embedded-sweep (2026-07-13): the marker constant lives in a
    # module-level _DEFAULT_RELATIVE_PATH, so this join evaded detection until
    # _marker_named_constants() was added. Migrated to try vnx_paths.resolve_paths()
    # first; the git-root walk now only fires in the except-Exception last resort.
    "scripts/lib/strategy/roadmap.py": {
        'root / _DEFAULT_RELATIVE_PATH',
    },
    "scripts/lib/strategy/decisions.py": {
        'root / _DEFAULT_RELATIVE_PATH',
    },
}


# ---------------------------------------------------------------------------
# Core detection
# ---------------------------------------------------------------------------


def _references_file_anchor(node: ast.AST, anchored: Set[str]) -> bool:
    """True if the subtree references ``__file__`` or a file-anchored name.

    A file-anchored name is a variable OR a helper function whose call resolves
    from ``__file__`` (see :func:`_file_anchored_names`). Because a call
    ``_project_root()`` carries the function name as an ``ast.Name`` in
    ``node.func``, this walk catches ``_project_root() / ".vnx-data"`` once
    ``_project_root`` is in ``anchored``.
    """
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and (sub.id == "__file__" or sub.id in anchored):
            return True
    return False


def _file_anchored_names(tree: ast.AST) -> Set[str]:
    """Names that resolve from ``__file__`` — variables AND helper functions.

    Iterated to a fixpoint so the anchor propagates transitively:
      * assignments  — ``_REPO_ROOT = Path(__file__).resolve().parents[2]``,
        ``here = Path(__file__).resolve()``;
      * function defs — a helper whose ``return`` yields a ``__file__``-anchored
        expression (e.g. ``def _project_root(): return Path(__file__)...``), so a
        later ``_project_root() / ".vnx-data"`` join is caught even though the
        ``__file__`` reference is hidden behind the call.
    """
    anchored: Set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                if value is None:
                    continue
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                if _references_file_anchor(value, anchored):
                    for tgt in targets:
                        if isinstance(tgt, ast.Name) and tgt.id not in anchored:
                            anchored.add(tgt.id)
                            changed = True
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in anchored:
                    continue
                for ret in ast.walk(node):
                    if (
                        isinstance(ret, ast.Return)
                        and ret.value is not None
                        and _references_file_anchor(ret.value, anchored)
                    ):
                        anchored.add(node.name)
                        changed = True
                        break
    return anchored


def _has_marker_constant_literal(node: ast.AST) -> bool:
    """True if the subtree contains a literal ``.vnx-data`` / ``ROADMAP.yaml`` string constant."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            if any(marker in sub.value for marker in DATA_PATH_MARKERS):
                return True
    return False


def _marker_named_constants(tree: ast.AST) -> Set[str]:
    """MODULE-LEVEL names whose assigned value is a marker string literal.

    Catches the indirection where the ``.vnx-data``/``ROADMAP.yaml`` literal lives
    in its own module constant (e.g. ``_DEFAULT_RELATIVE_PATH = Path(".vnx-data/x.yaml")``)
    and a LATER expression joins it against a ``__file__``-anchored root
    (``root / _DEFAULT_RELATIVE_PATH``) — the join node itself carries no inline
    string constant, so without this the marker is invisible to
    :func:`_has_data_marker_constant`.

    Deliberately restricted to assignments that are DIRECT children of the
    module body (not nested in a function). A function-local name like
    ``state_dir`` is routinely reassigned per-branch with unrelated values
    across a file; treating it as globally marker-tainted the moment ANY branch
    assigns it a marker literal produces false positives on every other,
    unrelated use of that same name. A module-level ``_UPPER_SNAKE``-style
    constant is assigned once and never rebound, so tainting it is precise.
    """
    names: Set[str] = set()
    if not isinstance(tree, ast.Module):
        return names
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if value is None or not _has_marker_constant_literal(value):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for tgt in targets:
                if isinstance(tgt, ast.Name):
                    names.add(tgt.id)
    return names


def _has_data_marker_constant(node: ast.AST, marker_names: Set[str]) -> bool:
    """True if the subtree contains a ``.vnx-data``/``ROADMAP.yaml`` marker.

    Either a literal string constant, or a reference to a name previously bound
    to one (see :func:`_marker_named_constants`).
    """
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            if any(marker in sub.value for marker in DATA_PATH_MARKERS):
                return True
        if isinstance(sub, ast.Name) and sub.id in marker_names:
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
    marker_names = _marker_named_constants(tree)

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
        if not _has_data_marker_constant(node, marker_names):
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
