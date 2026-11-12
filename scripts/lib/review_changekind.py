"""Changekind of a follow-up head against its parent head (ADR-064 besluit 2 and 3, eisen 1 to 3).

Proof is git facts only (own diff, patch-id, interdiff, seat input set, security-path
table), never ``contract_hash``. Whatever cannot be proven is ``REWORK``/``cannot_prove``.
"""
from __future__ import annotations

import ast
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Set

from gate_reanchor import (
    MODULE_LEVEL,
    WHOLE_MODULE,
    ImportGraph,
    ReanchorError,
    Symbol,
    build_import_graph,
    reachable_modules,
)
from review_findings import STATUS_UNVERIFIABLE
from smart_router import plan_scope_rule_version, security_path_kind

NO_CODE_CHANGE = "NO_CODE_CHANGE"
TRIVIAL_REBASE = "TRIVIAL_REBASE"
REWORK = "REWORK"

REASON_CANNOT_PROVE = "cannot_prove"
REASON_SECURITY_PATH = "security_path"
REASON_INPUT_SET = "input_set_touched"
REASON_REBASE_REWORK = "rebase_with_rework"
REASON_ANCHOR = "anchor_unverifiable"

#: ``git patch-id`` prints nothing for an empty diff; an empty own diff has this id.
EMPTY_PATCH_ID = "0" * 40
ANCHOR_PRESENT = "present"
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

Runner = Callable[..., str]


class ChangeKindError(RuntimeError):
    """A git fact needed for the classification could not be established."""


def _run_git(repo: Path, args: Sequence[str], stdin: Optional[str] = None) -> str:
    try:
        proc = subprocess.run(["git", *args], cwd=str(repo), input=stdin,
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ChangeKindError(f"git {' '.join(args)} could not run: {exc}") from exc
    if proc.returncode != 0:
        raise ChangeKindError(f"git {' '.join(args)} failed (rc={proc.returncode}): {(proc.stderr or '').strip()[:200]}")
    return proc.stdout


def _merge_base(repo: Path, base_ref: str, head: str, run: Runner) -> str:
    return run(repo, ["merge-base", base_ref, head]).strip()


def own_diff(repo: Path, base_ref: str, head: str, run: Runner = _run_git, path: Optional[str] = None) -> str:
    """Diff of ``head`` against its own merge-base with ``base_ref`` (optionally one path)."""
    args = ["diff", "--no-color", "--no-ext-diff", "--no-renames", _merge_base(repo, base_ref, head, run), head]
    return run(repo, args + (["--", path] if path else []))


def _patch_id_of(diff: str, repo: Path, run: Runner) -> str:
    if not diff.strip():
        return EMPTY_PATCH_ID
    out = run(repo, ["patch-id", "--stable"], stdin=diff).split()
    if not out or len(out[0]) < 40:
        raise ChangeKindError("git patch-id returned no id for a non-empty diff")
    return out[0]


def patch_id(repo: Path, base_ref: str, head: str, run: Runner = _run_git) -> str:
    """``git patch-id --stable`` over the own diff; ``EMPTY_PATCH_ID`` for an empty one."""
    return _patch_id_of(own_diff(repo, base_ref, head, run), repo, run)


@dataclass(frozen=True)
class Interdiff:
    paths: FrozenSet[str] = frozenset()
    symbols: Mapping[str, FrozenSet[Symbol]] = field(default_factory=dict)


def _scope_sources(source: str) -> Dict[str, List[str]]:
    tree = ast.parse(source)
    out: Dict[str, List[str]] = {MODULE_LEVEL: [ast.dump(n) for n in tree.body if not isinstance(n, _SCOPES)]}
    for node in ast.walk(tree):
        if isinstance(node, _SCOPES):
            out.setdefault(node.name, []).append(ast.get_source_segment(source, node) or "")
    return out


def _changed_symbols(repo: Path, parent: str, head: str, path: str, run: Runner) -> FrozenSet[Symbol]:
    module = Path(path).stem
    if not path.endswith(".py"):
        return frozenset()
    try:
        old, new = (_scope_sources(run(repo, ["show", f"{r}:{path}"])) for r in (parent, head))
    except (ChangeKindError, SyntaxError, ValueError):
        return frozenset({(module, WHOLE_MODULE)})  # unknown becomes blocking, never invisible
    return frozenset((module, n) for n in set(old) | set(new) if old.get(n) != new.get(n))


def interdiff(repo: Path, base_ref: str, parent_head: str, head: str, run: Runner = _run_git) -> Interdiff:
    """Paths whose own diff differs between the two heads, and per path the changed symbols."""
    def paths_of(h: str) -> Set[str]:
        mb = _merge_base(repo, base_ref, h, run)
        return {p for p in run(repo, ["diff", "--name-only", "--no-renames", mb, h]).splitlines() if p}

    def pid(h: str, p: str) -> str:
        return _patch_id_of(own_diff(repo, base_ref, h, run, path=p), repo, run)

    changed = frozenset(p for p in paths_of(parent_head) | paths_of(head) if pid(parent_head, p) != pid(head, p))
    return Interdiff(changed, {p: _changed_symbols(repo, parent_head, head, p, run) for p in sorted(changed)})


def seat_input_set(repo: Path, head: str, own_diff_paths: Iterable[str]) -> FrozenSet[str]:
    """The own diff plus the files that import a module of that diff (depth 1)."""
    paths = set(own_diff_paths)
    try:
        graph = build_import_graph(repo, head)
        listing = _run_git(repo, ["ls-tree", "-r", "--name-only", head]).split()
    except ReanchorError as exc:
        raise ChangeKindError(str(exc)) from exc
    if graph.unresolved:
        raise ChangeKindError(f"import edges unknown for {sorted(graph.unresolved)[:5]}")
    reverse: Dict[str, Set[str]] = {}
    for src, deps in graph.edges.items():
        for dep in deps:
            reverse.setdefault(dep, set()).add(src)
    stems = {Path(p).stem for p in paths if p.endswith(".py")}
    reached = reachable_modules(stems, ImportGraph(edges=reverse), depth=1)
    return frozenset(paths | {p for p in listing if p.endswith(".py") and Path(p).stem in reached})


@dataclass(frozen=True)
class ChangeKindRecord:
    changekind: str
    parent_patch_id: Optional[str]
    head_patch_id: Optional[str]
    security_paths: Mapping[str, str]
    plan_scope_rule_version: str
    full_review_reason: Optional[str] = None


def decide(parent_pid: str, head_pid: str, merge_base_equal: bool, base_move_paths: Iterable[str],
           input_set: Iterable[str], security_paths: Mapping[str, str]) -> tuple[str, Optional[str]]:
    """Pure rule: (changekind, full_review_reason). Positive evidence only."""
    if security_paths:
        return REWORK, REASON_SECURITY_PATH
    if parent_pid != head_pid:
        # Same base: a real fix commit, a delta may judge it. Moved base: a rebase hiding rework.
        return REWORK, None if merge_base_equal else REASON_REBASE_REWORK
    if merge_base_equal:
        return NO_CODE_CHANGE, None
    if set(base_move_paths) & set(input_set):
        return REWORK, REASON_INPUT_SET
    return TRIVIAL_REBASE, None


def classify_change(repo: Path, base_ref: str, parent_head: str, head: str,
                    seat_input_set: Iterable[str], run: Runner = _run_git) -> ChangeKindRecord:
    """Classify ``head`` against ``parent_head``; whatever cannot be proven is REWORK/cannot_prove."""
    rule = plan_scope_rule_version()
    try:
        parent_pid, head_pid = patch_id(repo, base_ref, parent_head, run), patch_id(repo, base_ref, head, run)
    except (ChangeKindError, ReanchorError):
        return ChangeKindRecord(REWORK, None, None, {}, rule, REASON_CANNOT_PROVE)
    try:
        mb_old, mb_new = (_merge_base(repo, base_ref, h, run) for h in (parent_head, head))
        moved = [] if mb_old == mb_new else run(repo, ["diff", "--name-only", "--no-renames", mb_old, mb_new]).splitlines()
        changed = interdiff(repo, base_ref, parent_head, head, run).paths
    except (ChangeKindError, ReanchorError):
        return ChangeKindRecord(REWORK, parent_pid, head_pid, {}, rule, REASON_CANNOT_PROVE)
    security = {p: k for p in sorted(changed) if (k := security_path_kind(p)) is not None}
    kind, reason = decide(parent_pid, head_pid, mb_old == mb_new, moved, seat_input_set, security)
    return ChangeKindRecord(kind, parent_pid, head_pid, security, rule, reason)


def _qualified_names(node: ast.AST, prefix: str = "") -> Set[str]:
    names: Set[str] = set()
    for child in ast.iter_child_nodes(node):
        inner = prefix
        if isinstance(child, _SCOPES):
            inner = f"{prefix}{child.name}"
            names.add(inner)
            inner += "."
        names |= _qualified_names(child, inner)
    return names


def anchors_present(repo: Path, head: str, findings: Iterable[Mapping[str, Any]],
                    run: Runner = _run_git) -> Dict[str, Any]:
    """Per open finding (``file_path`` plus ``symbolic_context``): present or ``unverifiable``.

    One unverifiable anchor gives ``full_review_reason: anchor_unverifiable``.
    """
    statuses: List[Dict[str, str]] = []
    for finding in findings:
        path, context = str(finding.get("file_path") or ""), str(finding.get("symbolic_context") or "")
        try:
            source = run(repo, ["show", f"{head}:{path}"]) if path else None
            gone = source is None or bool(context and context not in _qualified_names(ast.parse(source)))
        except (ChangeKindError, SyntaxError, ValueError):
            gone = True
        status = STATUS_UNVERIFIABLE if gone else ANCHOR_PRESENT
        statuses.append({"file_path": path, "symbolic_context": context, "status": status})
    bad = any(s["status"] != ANCHOR_PRESENT for s in statuses)
    return {"anchors": statuses, "full_review_reason": REASON_ANCHOR if bad else None}
