"""Changekind of a follow-up head against its parent head (ADR-064 besluit 2 and 3, eisen 1 to 3).

Proof is git trees: ``NO_CODE_CHANGE`` needs ``head^{tree}`` equal to the parent's tree on the same merge-base,
``TRIVIAL_REBASE`` the parent's own diff replayed cleanly (``git merge-tree``) onto the new merge-base into exactly
``head^{tree}``. A patch-id ignores hunk locations and whitespace, so it is on the record only (``--stable``, eisen
1 and 4) and decides nothing; ``contract_hash`` never. Whatever cannot be proven is ``REWORK``/``cannot_prove``.
"""
from __future__ import annotations

import ast
import itertools
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Set

from gate_reanchor import (MODULE_LEVEL, WHOLE_MODULE, ImportGraph, ReanchorError, Symbol,
                           build_import_graph, reachable_modules)
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
_OID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")

Runner = Callable[..., str]


class ChangeKindError(RuntimeError):
    """A git fact needed for the classification could not be established (``returncode``/``stdout`` of a git run)."""
    def __init__(self, message: str, returncode: Optional[int] = None, stdout: str = "") -> None:
        super().__init__(message)
        self.returncode, self.stdout = returncode, stdout


def _run_git(repo: Path, args: Sequence[str], stdin: Optional[str] = None) -> str:
    try:
        proc = subprocess.run(["git", *args], cwd=str(repo), input=stdin,
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ChangeKindError(f"git {' '.join(args)} could not run: {exc}") from exc
    if proc.returncode != 0:
        raise ChangeKindError(f"git {' '.join(args)} failed (rc={proc.returncode}): {(proc.stderr or '').strip()[:200]}",
                              proc.returncode, proc.stdout)
    return proc.stdout


def _oid(text: str, what: str) -> str:
    if not _OID.fullmatch(oid := text.strip()):
        raise ChangeKindError(f"no object id for {what}: {oid[:60]!r}")
    return oid


def _merge_base(repo: Path, base_ref: str, head: str, run: Runner) -> str:
    return _oid(run(repo, ["merge-base", base_ref, head]), f"merge-base of {head}")


def _tree(repo: Path, rev: str, run: Runner) -> str:
    return _oid(run(repo, ["rev-parse", "--verify", f"{rev}^{{tree}}"]), f"{rev}^{{tree}}")


def _names(text: str) -> FrozenSet[str]:  # ``-z`` output: a quoted path would miss every set it is held against
    return frozenset(p for p in text.split("\0") if p)


def own_diff(repo: Path, base_ref: str, head: str, run: Runner = _run_git) -> str:
    """Diff of ``head`` against its own merge-base with ``base_ref``."""
    return run(repo, ["diff", "--no-color", "--no-ext-diff", "--no-renames", _merge_base(repo, base_ref, head, run), head])


def patch_id(repo: Path, base_ref: str, head: str, run: Runner = _run_git) -> str:
    """``git patch-id --stable`` over the own diff (eis 1), for the record only; ``EMPTY_PATCH_ID`` for an empty one."""
    diff = own_diff(repo, base_ref, head, run)
    if not diff.strip():
        return EMPTY_PATCH_ID
    out = run(repo, ["patch-id", "--stable"], stdin=diff).split()
    if not out or len(out[0]) < 40:
        raise ChangeKindError("git patch-id returned no id for a non-empty diff")
    return out[0]


@dataclass(frozen=True)
class Comparison:
    """``head_tree`` against ``tree``: the parent's tree (same merge-base) or its own diff replayed (moved base).
    ``changed_paths``: paths whose blobs differ between the two trees, plus the replay conflicts."""
    merge_base_equal: bool
    base_move_paths: FrozenSet[str]
    tree: str
    head_tree: str
    replay_clean: bool
    changed_paths: FrozenSet[str]


def _replay(repo: Path, mb_old: str, mb_new: str, parent_head: str, run: Runner) -> tuple[str, bool, FrozenSet[str]]:
    """(tree, clean, conflicted paths) of the parent's own diff merged onto ``mb_new``. Only exit 1 is a conflict."""
    try:
        out, clean = run(repo, ["merge-tree", "--write-tree", "-z", "--name-only", "--no-messages",
                                "--merge-base", mb_old, mb_new, parent_head]), True
    except ChangeKindError as exc:
        if exc.returncode != 1:
            raise
        out, clean = exc.stdout, False
    first, _, rest = out.partition("\0")
    conflicts = frozenset(itertools.takewhile(bool, rest.split("\0")))
    if clean == bool(conflicts):  # a clean merge names no path, a conflict at least one
        raise ChangeKindError(f"git merge-tree: clean={clean} with conflicts {sorted(conflicts)[:3]}")
    return _oid(first, "the replayed tree"), clean, conflicts


def compare(repo: Path, base_ref: str, parent_head: str, head: str, run: Runner = _run_git) -> Comparison:
    """The tree facts of ADR-064 eis 2; raises ``ChangeKindError`` when one cannot be established."""
    mb_old, mb_new = (_merge_base(repo, base_ref, h, run) for h in (parent_head, head))
    head_tree, moved, clean, conflicts = _tree(repo, head, run), frozenset(), True, frozenset()
    if mb_old == mb_new:
        tree = _tree(repo, parent_head, run)
    else:
        moved = _names(run(repo, ["diff", "--name-only", "-z", "--no-renames", mb_old, mb_new]))
        tree, clean, conflicts = _replay(repo, mb_old, mb_new, parent_head, run)
    changed = _names(run(repo, ["diff", "--name-only", "-z", "--no-renames", tree, head_tree])) | conflicts
    return Comparison(mb_old == mb_new, moved, tree, head_tree, clean, changed)


@dataclass(frozen=True)
class Interdiff:
    paths: FrozenSet[str] = frozenset()
    symbols: Mapping[str, FrozenSet[Symbol]] = field(default_factory=dict)


def _scope_sources(source: str) -> Dict[str, List[str]]:
    tree = ast.parse(source)
    lines = source.splitlines()
    out: Dict[str, List[str]] = {MODULE_LEVEL: [ast.dump(n) for n in tree.body if not isinstance(n, _SCOPES)]}
    for node in ast.walk(tree):
        if isinstance(node, _SCOPES):
            first = min([node.lineno] + [d.lineno for d in node.decorator_list])  # decorators belong to the scope
            out.setdefault(node.name, []).append("\n".join(lines[first - 1:node.end_lineno]))
    return out


def _changed_symbols(repo: Path, old_tree: str, new_tree: str, path: str, run: Runner) -> FrozenSet[Symbol]:
    module = Path(path).stem
    if not path.endswith(".py"):
        return frozenset()
    try:
        old, new = (_scope_sources(run(repo, ["show", f"{t}:{path}"])) for t in (old_tree, new_tree))
    except (ChangeKindError, SyntaxError, ValueError):
        return frozenset({(module, WHOLE_MODULE)})  # unknown becomes blocking, never invisible
    return frozenset((module, n) for n in set(old) | set(new) if old.get(n) != new.get(n))


def interdiff(repo: Path, base_ref: str, parent_head: str, head: str, run: Runner = _run_git) -> Interdiff:
    """``compare().changed_paths`` (no patch-ids: a relocated edit always shows) with their changed symbols."""
    cmp = compare(repo, base_ref, parent_head, head, run)
    return Interdiff(cmp.changed_paths, {p: _changed_symbols(repo, cmp.tree, cmp.head_tree, p, run)
                                         for p in sorted(cmp.changed_paths)})


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


def decide(merge_base_equal: bool, tree_equal: bool, replay_clean: bool, base_move_paths: Iterable[str],
           input_set: Iterable[str], security_paths: Mapping[str, str]) -> tuple[str, Optional[str]]:
    """Pure rule: (changekind, full_review_reason). Positive evidence only: each tree fact is a real bool;
    ``tree_equal`` holds ``head^{tree}`` against the parent's tree (same base) or the replayed tree (moved base)."""
    facts = (merge_base_equal, tree_equal, replay_clean)
    if not isinstance(security_paths, Mapping) or not all(isinstance(f, bool) for f in facts):
        return REWORK, REASON_CANNOT_PROVE
    if security_paths:
        return REWORK, REASON_SECURITY_PATH
    if merge_base_equal:
        # Same base: a different tree is a real fix commit, a delta may judge it.
        return (NO_CODE_CHANGE, None) if tree_equal else (REWORK, None)
    if not (replay_clean and tree_equal):
        return REWORK, REASON_REBASE_REWORK
    if set(base_move_paths) & set(input_set):
        return REWORK, REASON_INPUT_SET
    return TRIVIAL_REBASE, None


def classify_change(repo: Path, base_ref: str, parent_head: str, head: str,
                    seat_input_set: Iterable[str], run: Runner = _run_git) -> ChangeKindRecord:
    """Classify ``head`` against ``parent_head`` on tree facts; whatever cannot be proven is REWORK/cannot_prove."""
    rule = plan_scope_rule_version()
    try:
        parent_pid, head_pid = patch_id(repo, base_ref, parent_head, run), patch_id(repo, base_ref, head, run)
    except ChangeKindError:
        return ChangeKindRecord(REWORK, None, None, {}, rule, REASON_CANNOT_PROVE)
    try:
        cmp = compare(repo, base_ref, parent_head, head, run)
    except ChangeKindError:
        return ChangeKindRecord(REWORK, parent_pid, head_pid, {}, rule, REASON_CANNOT_PROVE)
    security = {p: k for p in sorted(cmp.changed_paths) if (k := security_path_kind(p)) is not None}
    kind, reason = decide(cmp.merge_base_equal, cmp.tree == cmp.head_tree, cmp.replay_clean,
                          cmp.base_move_paths, seat_input_set, security)
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
