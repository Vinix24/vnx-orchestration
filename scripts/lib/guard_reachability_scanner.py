#!/usr/bin/env python3
"""guard_reachability_scanner.py — AST scanner for field-presence guards.

Golf-4 (2026-09-05) surfaced eight unrelated defects sharing one shape: a
guard condition (an ``if`` that gates a raise/return/skip or a gate decision)
reads a field, column, or spec attribute that in practice is never filled.
The code is correct. The branch it protects is unreachable. Nothing fails, so
nothing alerts — three of the eight cases were already written down as a
"Caveat" or "accepted, intentional no-op" and that is exactly what kept them
invisible: a documented limitation reads as an accepted boundary, not as a
defect with a measurable consequence.

This module is the STATIC half of the detector: given Python source, find
every ``if`` whose test expression probes a named field via ``dict.get(str)``,
``obj[str]``, or ``obj.attr`` (attribute access is only counted when the
attribute name is a field of some ``@dataclass`` discovered in the scanned
tree(s) — see :func:`collect_dataclass_fields` — otherwise every ``self.foo``
in the repo would match). The MEASURE half
(``guard_reachability_store.py``) answers whether that field is ever actually
filled in a real store; only the combination tells you whether a guard is
reachable in practice — this module alone only tells you a guard EXISTS.

Deliberately out of scope (documented, not silently dropped): SQL literals
passed to ``cursor.execute()`` that reference a column only inside the query
string are not parsed here. A generic SQL-column extractor was drafted during
golf-4 and cut for precision: this codebase's own ``_has_col(conn, table,
col)`` helper (12 call sites) already guards most such reads defensively, and
conflating a defensive existence-check with the unreachable-guard bug class
it was written to prevent produces exactly the false-positive noise this
detector must not add. Tracked as follow-up scope, not fixed here.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

DEFAULT_SCAN_DIRS: Tuple[str, ...] = ("scripts/lib", "scripts")


@dataclass(frozen=True)
class GuardedFieldRef:
    """One field-presence probe found inside an ``if`` test expression."""

    file: str
    lineno: int
    field: str
    access_kind: str  # "dict_get" | "subscript" | "attribute"
    container: str
    enclosing_function: Optional[str]
    test_source: str


def _has_dataclass_decorator(node: ast.ClassDef) -> bool:
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Name) and target.id == "dataclass":
            return True
        if isinstance(target, ast.Attribute) and target.attr == "dataclass":
            return True
    return False


def collect_dataclass_fields(tree: ast.AST) -> Set[str]:
    """Field names of every ``@dataclass``-decorated class in ``tree``.

    Used to qualify ``obj.attr`` guard probes: a bare attribute name is far
    too noisy to treat as a "field" on its own (every ``self.foo`` and
    ``logger.info`` reference is an ``ast.Attribute``), but a name that is
    also a declared dataclass field — the exact shape ``DispatchSpec.track_id``
    took in the OI-1632 case — is a genuine candidate.
    """
    fields: Set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or not _has_dataclass_decorator(node):
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                fields.add(stmt.target.id)
    return fields


def _slice_value(node: ast.Subscript) -> Optional[ast.AST]:
    sl = node.slice
    # Python <3.9 wrapped simple subscripts in ast.Index; 3.9+ (this repo
    # targets 3.12) exposes the expression directly. Handle both defensively.
    if sl.__class__.__name__ == "Index":  # pragma: no cover - py<3.9 shape
        return getattr(sl, "value", None)
    return sl


def _is_string_constant(node: Optional[ast.AST]) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _safe_unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return "<unparseable>"


def _iter_field_probes(
    test: ast.AST, known_attr_fields: Set[str],
) -> Iterator[Tuple[ast.AST, str, str, str]]:
    """Yield (probe_node, field_name, access_kind, container_repr) for every
    field-presence probe anywhere inside ``test`` (``ast.walk`` already
    descends through ``not``/``and``/``or``/comparison wrappers, so
    ``if not spec.get("x"):`` and ``if spec.get("x") is None:`` both match
    without extra handling).
    """
    for node in ast.walk(test):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and _is_string_constant(node.args[0])
        ):
            yield node, node.args[0].value, "dict_get", _safe_unparse(node.func.value)
            continue
        if isinstance(node, ast.Subscript):
            sv = _slice_value(node)
            if _is_string_constant(sv):
                yield node, sv.value, "subscript", _safe_unparse(node.value)
            continue
        if isinstance(node, ast.Attribute) and node.attr in known_attr_fields:
            yield node, node.attr, "attribute", _safe_unparse(node.value)


_SCOPE_BOUNDARY_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _direct_statements(node: ast.AST) -> Iterator[ast.stmt]:
    """Statements reachable by normal control flow inside ONE function scope.

    Descends into ``if``/``for``/``while``/``try``/``with`` bodies (an
    assignment made in a sibling branch is still visible later in the same
    function) but never into a nested function/class/lambda — those open
    their own local scope, and a same-named local there must not be confused
    with the enclosing function's variable.
    """
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
        stmts: List[ast.stmt] = list(node.body)
    elif isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith)):
        stmts = list(node.body) + list(getattr(node, "orelse", []))
    elif isinstance(node, ast.Try):
        stmts = list(node.body) + list(node.orelse) + list(node.finalbody)
        for handler in node.handlers:
            stmts += list(handler.body)
    else:
        stmts = []
    for stmt in stmts:
        yield stmt
        if not isinstance(stmt, _SCOPE_BOUNDARY_TYPES):
            yield from _direct_statements(stmt)


def _local_field_assignments(
    func_node: ast.AST, known_attr_fields: Set[str],
) -> Dict[str, List[Tuple[str, str, str]]]:
    """Map ``var_name -> [(field, kind, container), ...]`` for every simple
    local assignment in ``func_node`` whose right-hand side contains a field
    probe — the ``track_id = (spec.track_id or "").strip()`` shape from the
    OI-1632 calibration case, where the guard later tests the bare local
    name (``if track_id:``), not the attribute access itself.
    """
    mapping: Dict[str, List[Tuple[str, str, str]]] = {}
    for stmt in _direct_statements(func_node):
        target: Optional[str] = None
        value: Optional[ast.AST] = None
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            target, value = stmt.targets[0].id, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
            target, value = stmt.target.id, stmt.value
        if target is None or value is None:
            continue
        for _node, field, kind, container in _iter_field_probes(value, known_attr_fields):
            mapping.setdefault(target, []).append((field, kind, container))
    return mapping


def find_guarded_field_refs(
    source: str,
    filename: str,
    known_attr_fields: Set[str] = frozenset(),
) -> List[GuardedFieldRef]:
    """Every field-presence guard in ``source``, keyed by (line, field, kind).

    Matches two shapes:
      1. the field probe sits directly in the ``if`` test
         (``if spec.get("track_id"):``);
      2. the probe sits in an assignment, and the ``if`` test later gates on
         the bare local name (``track_id = (spec.track_id or "").strip()``
         ... ``if track_id:``) — the ACTUAL shape of the OI-1632 calibration
         case. Resolved per-function via :func:`_local_field_assignments`,
         never across function boundaries.

    Scoped to the guarding ``if`` itself, not what its branches do — the
    condition is the guard; what the branches do with it varies too much
    across the eight known cases to use as a reliable shape signal (requiring
    e.g. a ``raise``/``return`` in the body false-negatives on the
    calibration case: ``_check_track_link_verdict`` returns a Verdict on
    every branch, guarded or not).
    """
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError:
        return []

    refs: List[GuardedFieldRef] = []
    seen: Set[Tuple[int, str, str]] = set()
    func_stack: List[str] = []
    scope_stack: List[Dict[str, List[Tuple[str, str, str]]]] = []

    def _emit(lineno: int, field: str, kind: str, container: str, test_source: str) -> None:
        key = (lineno, field, kind)
        if key in seen:
            return
        seen.add(key)
        refs.append(
            GuardedFieldRef(
                file=filename,
                lineno=lineno,
                field=field,
                access_kind=kind,
                container=container,
                enclosing_function=func_stack[-1] if func_stack else None,
                test_source=test_source,
            )
        )

    class _Visitor(ast.NodeVisitor):
        def _enter_function(self, node: ast.AST) -> None:
            func_stack.append(getattr(node, "name", "<lambda>"))
            scope_stack.append(_local_field_assignments(node, known_attr_fields))
            self.generic_visit(node)
            scope_stack.pop()
            func_stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
            self._enter_function(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
            self._enter_function(node)

        def visit_If(self, node: ast.If) -> None:  # noqa: N802
            test_source = _safe_unparse(node.test)
            for probe_node, field, kind, container in _iter_field_probes(
                node.test, known_attr_fields,
            ):
                _emit(getattr(probe_node, "lineno", node.lineno), field, kind, container, test_source)

            scope = scope_stack[-1] if scope_stack else {}
            resolved_names: Set[str] = set()
            for name_node in ast.walk(node.test):
                if not isinstance(name_node, ast.Name) or name_node.id not in scope:
                    continue
                if name_node.id in resolved_names:
                    continue
                resolved_names.add(name_node.id)
                for field, kind, container in scope[name_node.id]:
                    _emit(
                        name_node.lineno, field, kind,
                        f"{container} (via local {name_node.id!r})", test_source,
                    )
            self.generic_visit(node)

    _Visitor().visit(tree)
    return refs


def iter_scan_files(root: Path, scan_dirs: Tuple[str, ...] = DEFAULT_SCAN_DIRS) -> List[Path]:
    files: List[Path] = []
    for d in scan_dirs:
        base = root / d
        if not base.is_dir():
            continue
        # "scripts" itself is scanned shallow (top-level entry points only);
        # "scripts/lib" is scanned recursively (it has its own subpackages,
        # e.g. scripts/lib/append_receipt_internals/, scripts/lib/providers/).
        candidates = base.rglob("*.py") if d.endswith("lib") else base.glob("*.py")
        files.extend(sorted(candidates))
    return files


def scan_repo_guarded_fields(
    root: Path, scan_dirs: Tuple[str, ...] = DEFAULT_SCAN_DIRS,
) -> List[GuardedFieldRef]:
    """Two-pass repo scan: collect dataclass fields fleet-wide first (a field
    like ``DispatchSpec.track_id`` is declared in ``dispatch_spec.py`` but
    read as a guard in ``dispatch_cli.py`` — a single-file pass would miss
    the attribute-guard shape entirely), then scan every file for guards
    qualified against that merged field set.
    """
    files = iter_scan_files(root, scan_dirs)
    sources: Dict[Path, str] = {}
    known_fields: Set[str] = set()
    for f in files:
        try:
            src = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        sources[f] = src
        try:
            tree = ast.parse(src, filename=str(f))
        except SyntaxError:
            continue
        known_fields |= collect_dataclass_fields(tree)

    refs: List[GuardedFieldRef] = []
    for f, src in sources.items():
        rel = f.relative_to(root).as_posix()
        refs.extend(find_guarded_field_refs(src, rel, known_fields))
    return refs
