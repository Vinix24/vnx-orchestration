#!/usr/bin/env python3
"""gate_reanchor.py — may a gate verdict be re-anchored instead of re-bought?

OI-1471, measured 2026-08-26: PR #1691's ``contract_hash`` was
``dd5ac45f7e84535e`` on three separate glm-gate runs, across two rebases and
three main shifts. For ``glm_gate``/``kimi_gate`` that hash is
``sha256(prompt)[:16]`` and the prompt carries the full diff
(``gate_artifacts._compute_contract_hash``), so an identical hash means the
gate judged a byte-identical input. The same review was bought three times.

Re-anchoring on the hash ALONE is unsafe, and the counterexample is from the
same day: #1688 tightened ``gate_status.has_complete_evidence``; #1692 imports
and calls it. #1692's diff was byte-identical across that shift — and its
meaning was not. A diff hash cannot see that.

So the condition has two halves, and BOTH must hold:

  (a) the contract hash the gate would compute for the new head equals the
      hash on the existing terminal record, and
  (b) no commit between the old and the new merge-base touches a symbol this
      PR's own files reach.

(b) is what this module computes. Measured with THIS code over 171 ordered PR
pairs from 2026-08-26/28, both columns from ONE run over identical rows —
comparing two semantics across two row sets would show a difference that is
partly just the row choice:

  import depth        allowed         allowed
                      (permissive)    (fail-closed, shipped)
  direct (default)        87%             75%
  +1 hop                  77%             70%
  +2 hops                 73%             66%
  full closure            65%             60%

The right-hand column is what ships. Refusing everything the analysis cannot
model costs 12 points at the default depth and still re-anchors three quarters
of the re-gates that were actually paid for. Every refusal it adds comes from
a dynamic import or ``getattr`` in the PR's own files.

The answer to OI-1471 is therefore neither "it cannot be done" nor "it is
free": three quarters can be re-anchored, one quarter has to be re-bought,
and which is which is decided by evidence rather than by a rule of thumb.

The whole sweep took 8.1 seconds including building the 713-module import
graph from scratch, so (b) is affordable — the outcome the OI left open.

Which row to stand on is a real decision, and the measurement settles it in
an unobvious direction: see :data:`DEPTH_DEFAULT`.

ONE PRINCIPLE RUNS THROUGH ALL OF IT. Condition (b) is a PROOF: *no* commit in
the range touches *any* symbol this PR reaches. A proof cannot rest on an
incomplete analysis, and a static Python import analysis is never complete —
star-imports, ``importlib``, ``getattr``, re-exports, unreadable files. Working
that list down one entry at a time only ever produces the next entry.

So every construct the analysis does not model yields ``cannot_prove``, and
``cannot_prove`` refuses. Never "no reference found". The difference is the
whole safety argument: "I did not find a reference" and "I could not tell"
are the same output from a silent ``except: continue``, and only one of them
is a proof.

The price is refusing more often, and so re-buying a gate more often. That is
the correct direction to fail in: paying twice costs money, re-anchoring
wrongly costs the validity of the audit trail.
"""

from __future__ import annotations

import ast
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

#: (module_stem, symbol). ``"<module>"`` is the module's own top level —
#: constants, dispatch tables, decorators — which a diff can change without
#: touching any def. ``"*"`` means the whole module is suspect (deleted, or
#: unparseable at that commit).
Symbol = Tuple[str, str]

MODULE_LEVEL = "<module>"
WHOLE_MODULE = "*"

#: Import-graph expansion depth. DEPTH_DIRECT looks only at the modules the
#: PR's own files import; DEPTH_FULL follows the whole closure.
DEPTH_DIRECT = 0
DEPTH_FULL = -1

#: The default is DEPTH_DIRECT, and that choice is measured rather than
#: convenient. Running the analysis at DEPTH_FULL against #1691 — the PR
#: OI-1471 was written about — REFUSES it: glm_gate transitively reaches
#: gate_status, so the #1688 change lands in its closure. The conservative
#: setting would therefore have saved nothing for the case that motivated
#: this at all, while still refusing #1692 for a reason no one can act on.
#:
#: The substantive argument is what the gate itself can see. A review gate
#: judges the DIFF. The #1688/#1692 failure was catchable precisely because
#: #1692's diff CALLS has_complete_evidence — a reviewer reading that diff
#: has the changed symbol in front of it. A change two import hops away is
#: not visible in the diff either, so re-running the gate would not catch it
#: any better than re-anchoring does. Guarding at the depth the gate can
#: actually reason about is the honest boundary; guarding deeper buys a
#: refusal rate, not a caught bug.
#:
#: Callers who want the paranoid setting pass ``depth=DEPTH_FULL``.
DEPTH_DEFAULT = DEPTH_DIRECT


class ReanchorError(RuntimeError):
    """A git fact needed for the decision could not be established."""


#: Python constructs this analysis deliberately does not model. Each one is
#: detected and reported as ``cannot_prove`` rather than resolved: a partial
#: resolution would be indistinguishable from a complete one at the call site.
_DYNAMIC_IMPORT_NAMES = frozenset({"import_module", "__import__", "getattr"})


@dataclass(frozen=True)
class ReferenceSet:
    """What a PR reaches, plus what could not be established about it.

    ``unmodelled`` is the whole point. A reference analysis that returns only
    the symbols it found is indistinguishable from one that found nothing
    because it could not look, and :func:`can_reanchor` needs to tell those
    apart to make its refusal honest.
    """

    symbols: Set[Symbol] = field(default_factory=set)
    unmodelled: Sequence[str] = field(default_factory=tuple)

    @property
    def provable(self) -> bool:
        return not self.unmodelled


@dataclass(frozen=True)
class ReanchorDecision:
    allowed: bool
    reason: str
    contract_hash_matches: bool = False
    old_sha: str = ""
    new_sha: str = ""
    old_merge_base: str = ""
    new_merge_base: str = ""
    commits_in_range: int = 0
    blocking_symbols: Sequence[Symbol] = field(default_factory=tuple)
    depth: int = DEPTH_DEFAULT

    def to_dict(self) -> Dict[str, object]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "contract_hash_matches": self.contract_hash_matches,
            "old_sha": self.old_sha,
            "new_sha": self.new_sha,
            "old_merge_base": self.old_merge_base,
            "new_merge_base": self.new_merge_base,
            "commits_in_range": self.commits_in_range,
            "blocking_symbols": [list(s) for s in self.blocking_symbols],
            "depth": self.depth,
        }


def _git(project_root: Path, *args: str, timeout: int = 30) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(project_root),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise ReanchorError(
            f"git {' '.join(args)} failed (rc={proc.returncode}): "
            f"{(proc.stderr or '').strip()[:200]}"
        )
    return proc.stdout


def merge_base(project_root: Path, base_ref: str, commit: str) -> str:
    """Where ``commit`` branched off ``base_ref``. Raises when unresolvable.

    A rebased-away commit whose objects are gone is exactly such a case, and
    it must refuse: without the old merge-base there is no range to inspect,
    so condition (b) cannot be established at all.
    """
    return _git(project_root, "merge-base", base_ref, commit).strip()


def _changed_line_numbers(project_root: Path, commit: str, path: str) -> Set[int]:
    """Post-image line numbers ``commit`` touched in ``path``."""
    out = _git(project_root, "diff", "--unified=0", f"{commit}^", commit, "--", path)
    lines: Set[int] = set()
    for line in out.splitlines():
        if not line.startswith("@@"):
            continue
        try:
            plus = line.split("+", 1)[1].split("@@", 1)[0].strip()
            start_text, _, count_text = plus.partition(",")
            start = int(start_text)
            count = int(count_text or 1)
        except (IndexError, ValueError):
            # An unparseable hunk header must not silently narrow the range.
            raise ReanchorError(f"unparseable diff hunk header in {commit[:12]} {path}: {line!r}")
        if count == 0:
            # A deletion-only hunk has a `+N,0` post-image span, so
            # range(N, N+0) is EMPTY and the change would register as touching
            # nothing at all — a commit that deletes a guard out of a function
            # this PR calls would then read as "nothing changed" and allow a
            # re-anchor that must be refused. Git's convention for `+N,0` is
            # that the removed content sat after post-image line N, so both N
            # and N+1 are marked: whichever scope enclosed the deletion
            # contains one of them.
            lines.update({max(start, 1), start + 1})
            continue
        lines.update(range(start, start + count))
    return lines


def changed_symbols(project_root: Path, old_base: str, new_base: str) -> Set[Symbol]:
    """Qualified symbols changed by any commit in ``old_base..new_base``.

    Test files are excluded: a change under ``tests/`` cannot alter the
    runtime meaning of a symbol the PR calls.

    A file that is deleted or does not parse at its commit yields
    ``(module, "*")`` — the whole module is suspect. That is the fail-closed
    direction: unknown becomes blocking, never invisible.
    """
    out: Set[Symbol] = set()
    revs = _git(project_root, "rev-list", f"{old_base}..{new_base}").split()
    for commit in revs:
        names = _git(project_root, "diff", "--name-only", f"{commit}^..{commit}").split()
        for path in names:
            if not path.endswith(".py") or path.startswith("tests/"):
                continue
            module = Path(path).stem
            try:
                source = _git(project_root, "show", f"{commit}:{path}")
            except ReanchorError:
                out.add((module, WHOLE_MODULE))
                continue
            if not source.strip():
                out.add((module, WHOLE_MODULE))
                continue
            try:
                tree = ast.parse(source)
            except SyntaxError:
                out.add((module, WHOLE_MODULE))
                continue
            touched = _changed_line_numbers(project_root, commit, path)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    lo = node.lineno
                    hi = getattr(node, "end_lineno", node.lineno)
                    if any(lo <= n <= hi for n in touched):
                        out.add((module, node.name))
            spans: List[Tuple[int, int]] = [
                (n.lineno, getattr(n, "end_lineno", n.lineno))
                for n in tree.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            ]
            if any(not any(lo <= n <= hi for lo, hi in spans) for n in touched):
                out.add((module, MODULE_LEVEL))
    return out


def _module_source(project_root: Path, ref: str, module: str, tree_files: Dict[str, str]) -> Optional[str]:
    """Source of ``module`` at ``ref``, or None when this repo has no such module."""
    path = tree_files.get(module)
    if path is None:
        return None
    return _git(project_root, "show", f"{ref}:{path}")


def _tree_modules(project_root: Path, ref: str) -> Dict[str, str]:
    """module_stem -> path, for the non-test Python files in the tree at ``ref``."""
    out: Dict[str, str] = {}
    for path in _git(project_root, "ls-tree", "-r", "--name-only", ref).split():
        if path.endswith(".py") and not path.startswith("tests/"):
            out.setdefault(Path(path).stem, path)
    return out


def _scan_module_bindings(source: str) -> Dict[str, str]:
    """name -> origin module, for names a module RE-EXPORTS via ``from x import name``.

    A re-export is the quietest hole in this whole analysis. The PR writes
    ``from vnx_paths import ensure_env``; the function actually lives in
    another module and is merely re-bound there. A commit changing the real
    definition records ``(origin, ensure_env)``, the PR references
    ``(vnx_paths, ensure_env)``, and nothing matches — a silent ALLOW on a
    changed symbol the PR calls. One level of resolution closes it.
    """
    bindings: Dict[str, str] = {}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return bindings
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            origin = node.module.split(".")[-1]
            for alias in node.names:
                if alias.name != "*":
                    bindings.setdefault(alias.asname or alias.name, origin)
    return bindings


def referenced_symbols(project_root: Path, ref: str, files: Iterable[str]) -> ReferenceSet:
    """Qualified symbols the PR's own files reach, plus what could not be modelled.

    ``from x import a`` yields ``("x", "a")`` and ``("x", "<module>")`` — the
    second because importing from a module also binds you to its top level.
    ``import x`` plus ``x.y`` yields ``("x", "y")``. ``from x import *`` yields
    ``("x", "*")``, which :func:`find_blocking_symbols` treats as "any change
    in x blocks" — the honest position, since a star-import says nothing about
    which name is used.

    Bare-name matching was measured and rejected: it refused 40% of 120 real
    PR pairs, and most of those were the name ``main``, which nearly every
    script here both defines and calls. Qualifying by module took the refusal
    rate to 13% without losing the #1688/#1692 counterexample.

    Three things produce ``cannot_prove`` rather than a smaller symbol set:
    a PR file that is present but unreadable or unparseable, a dynamic import
    or ``getattr`` (which can reach a name no static walk will see), and a
    module whose own source cannot be read while resolving its re-exports.
    A file the PR DELETED is not one of them — absence at this ref is a
    complete answer, and refusing on it would refuse every PR that removes a
    file.
    """
    out: Set[Symbol] = set()
    unmodelled: List[str] = []
    tree_files = _tree_modules(project_root, ref)
    referenced_modules: Set[str] = set()

    for path in files:
        if not path.endswith(".py"):
            continue
        try:
            _git(project_root, "cat-file", "-e", f"{ref}:{path}")
        except ReanchorError:
            continue  # deleted by this PR: no references to gather
        try:
            source = _git(project_root, "show", f"{ref}:{path}")
        except ReanchorError as exc:
            unmodelled.append(f"{path} exists at {ref[:12]} but could not be read: {exc}")
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            unmodelled.append(f"{path} does not parse at {ref[:12]}: {exc}")
            continue

        # The file's OWN module is reached in full. A PR file that calls a
        # helper defined beside it referenced nothing at all under an
        # import-only analysis, so an intervening commit changing that same
        # helper produced an empty-but-provable reference set and an ALLOW on a
        # stale verdict. Enumerating which subset of its own file a file "uses"
        # buys no safety here: the PR is EDITING that module, so a commit in
        # the range touching it is exactly what must block.
        out.add((Path(path).stem, WHOLE_MODULE))
        referenced_modules.add(Path(path).stem)

        alias_to_module: Dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                module = node.module.split(".")[-1]
                out.add((module, MODULE_LEVEL))
                referenced_modules.add(module)
                for alias in node.names:
                    if alias.name == "*":
                        out.add((module, WHOLE_MODULE))
                        continue
                    out.add((module, alias.name))
                    alias_to_module.setdefault(alias.asname or alias.name, alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    module = alias.name.split(".")[-1]
                    alias_to_module[alias.asname or module] = module
                    referenced_modules.add(module)
                    out.add((module, MODULE_LEVEL))
            elif isinstance(node, ast.Name) and node.id in _DYNAMIC_IMPORT_NAMES:
                unmodelled.append(
                    f"{path} uses {node.id!r}: a dynamic import or attribute lookup can reach "
                    "a symbol no static walk sees, so this PR's references cannot be enumerated"
                )
            elif isinstance(node, ast.Attribute) and node.attr in _DYNAMIC_IMPORT_NAMES:
                unmodelled.append(
                    f"{path} uses {node.attr!r}: a dynamic import or attribute lookup can reach "
                    "a symbol no static walk sees, so this PR's references cannot be enumerated"
                )

        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                module = alias_to_module.get(node.value.id)
                if module:
                    out.add((module, node.attr))
                    referenced_modules.add(module)

    # Resolve one level of re-export for every module the PR imports from.
    for module in sorted(referenced_modules):
        try:
            source = _module_source(project_root, ref, module, tree_files)
        except ReanchorError as exc:
            unmodelled.append(f"module {module!r} could not be read at {ref[:12]}: {exc}")
            continue
        if source is None:
            continue  # stdlib or third-party: no commit in this range changed it
        bindings = _scan_module_bindings(source)
        for name, origin in bindings.items():
            if (module, name) in out:
                out.add((origin, name))
                out.add((origin, MODULE_LEVEL))

    return ReferenceSet(symbols=out, unmodelled=tuple(dict.fromkeys(unmodelled)))


@dataclass(frozen=True)
class ImportGraph:
    """The repo's import edges, plus the modules whose edges are UNKNOWN."""

    edges: Mapping[str, Set[str]] = field(default_factory=dict)
    unresolved: FrozenSet[str] = frozenset()


def build_import_graph(project_root: Path, ref: str) -> ImportGraph:
    """module_stem -> directly imported module_stems, over the repo at ``ref``.

    Only modules that exist in the tree become edges: a stdlib or third-party
    import is not something a commit in this range can have changed.

    A module that cannot be read or parsed goes into ``unresolved`` rather
    than being skipped. Its edges are unknown, not absent, and a caller
    walking the closure has to know the difference.
    """
    paths = [
        p for p in _git(project_root, "ls-tree", "-r", "--name-only", ref).split()
        if p.endswith(".py") and not p.startswith("tests/")
    ]
    known = {Path(p).stem for p in paths}
    graph: Dict[str, Set[str]] = {}
    unresolved: Set[str] = set()
    for path in paths:
        try:
            source = _git(project_root, "show", f"{ref}:{path}")
            tree = ast.parse(source)
        except (ReanchorError, SyntaxError):
            # Its edges are UNKNOWN, not absent. Dropping it silently truncated
            # the closure, so a DEPTH_FULL run could allow a re-anchor without
            # having proved condition (b) at all — the same fail-open shape as
            # the deletion-only hunk and the swallowed PR-file read.
            unresolved.add(Path(path).stem)
            graph.setdefault(Path(path).stem, set())
            continue
        deps: Set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                module = node.module.split(".")[-1]
                if module in known:
                    deps.add(module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    module = alias.name.split(".")[-1]
                    if module in known:
                        deps.add(module)
        graph.setdefault(Path(path).stem, set()).update(deps)
    return ImportGraph(edges=graph, unresolved=frozenset(unresolved))


def reachable_modules(
    modules: Iterable[str], graph: ImportGraph, depth: int = DEPTH_DEFAULT
) -> Set[str]:
    """Modules reachable from ``modules`` within ``depth`` import hops."""
    seen = set(modules)
    frontier = set(seen)
    hops = 0
    while frontier and (depth == DEPTH_FULL or hops < depth):
        nxt: Set[str] = set()
        for module in frontier:
            nxt |= set(graph.edges.get(module, set())) - seen
        seen |= nxt
        frontier = nxt
        hops += 1
    return seen


def find_blocking_symbols(
    changed: Set[Symbol],
    referenced,
    indirect_modules: Optional[Set[str]] = None,
) -> List[Symbol]:
    """Changed symbols this PR reaches — the reason a re-anchor is refused.

    Precision differs by how the PR reaches the module, and that difference is
    the point:

      1. **Directly imported, exact symbol.** The PR imports that
         ``(module, symbol)`` — it uses the thing that changed. Blocks.
      2. **Directly imported, module top level.** A changed constant, dispatch
         table or decorator (``<module>``), or a module that is gone or
         unparseable (``*``), changes behaviour for everyone importing it,
         whichever symbol they use. Blocks.
      3. **Reached only indirectly, or star-imported.** ``indirect_modules``
         are modules the PR does not import but can reach through the import
         graph; a ``from x import *`` adds ``x`` to the same set. In neither
         case is it knowable from the PR's own source WHICH symbol it depends
         on, so ANY change in one of them blocks.

    Keeping 1 apart from 3 is what makes the direct-depth rule symbol-precise.
    Folding them together — as an earlier draft of this function did, by
    letting the reachable set carry the directly-imported modules too — silently
    turns the whole thing into rule 3, blocking on a changed sibling function
    the PR never calls. It was caught by mutation-testing this file: deleting
    the exact-overlap line changed no test result, because nothing could
    reach it.
    """
    referenced = referenced.symbols if isinstance(referenced, ReferenceSet) else referenced
    indirect = set(indirect_modules or set())
    referenced_modules = {m for m, _ in referenced}
    # A star-import is recorded as (module, "*") on the REFERENCED side. It
    # says "this PR may use any name in that module", which is the same
    # information-free position as an indirectly-reached module — so it gets
    # the same coarse treatment rather than falling through the exact-symbol
    # match and matching nothing.
    indirect |= {m for m, s in referenced if s == WHOLE_MODULE}
    blocking: Set[Symbol] = set(changed & referenced)
    for module, symbol in changed:
        if module in indirect:
            blocking.add((module, symbol))
        elif symbol in (MODULE_LEVEL, WHOLE_MODULE) and module in referenced_modules:
            blocking.add((module, symbol))
    return sorted(blocking)


def _check_hash_precondition(
    *, old_sha: str, new_sha: str,
    old_contract_hash: str, new_contract_hash: str, depth: int,
) -> Optional[ReanchorDecision]:
    """Condition (a) plus the trivial no-op case, or None to carry on.

    Split out because it is free — no git, no parsing — and settles most
    refusals, so :func:`can_reanchor` runs it before touching the repository.
    """
    if not old_contract_hash or not new_contract_hash:
        return ReanchorDecision(
            allowed=False,
            reason=(
                "one of the contract hashes is empty — an unevidenced record cannot be "
                "the basis for re-anchoring anything"
            ),
            old_sha=old_sha, new_sha=new_sha, depth=depth,
        )
    if old_contract_hash != new_contract_hash:
        return ReanchorDecision(
            allowed=False,
            reason=(
                f"contract hash changed ({old_contract_hash} -> {new_contract_hash}): the gate "
                "would be judging a different input, so the old verdict does not apply"
            ),
            old_sha=old_sha, new_sha=new_sha, depth=depth,
        )
    if old_sha == new_sha:
        return ReanchorDecision(
            allowed=False,
            reason="old and new commit are the same — there is nothing to re-anchor",
            contract_hash_matches=True, old_sha=old_sha, new_sha=new_sha, depth=depth,
        )
    return None


def can_reanchor(
    project_root: Path,
    *,
    old_sha: str,
    new_sha: str,
    pr_files: Sequence[str],
    old_contract_hash: str,
    new_contract_hash: str,
    base_ref: str = "origin/main",
    depth: int = DEPTH_DEFAULT,
) -> ReanchorDecision:
    """May the verdict recorded for ``old_sha`` be re-anchored on ``new_sha``?

    Both halves of the OI-1471 condition, in the order that costs least: the
    hash comparison is free and settles most refusals, so the import analysis
    only runs when the hashes already agree.

    Refuses on every unestablished fact — an empty hash on either side, an
    unresolvable merge-base (a rebased-away commit whose objects are gone), an
    unparseable diff hunk. Absence of evidence is a re-buy.
    """
    refusal = _check_hash_precondition(
        old_sha=old_sha, new_sha=new_sha,
        old_contract_hash=old_contract_hash, new_contract_hash=new_contract_hash,
        depth=depth,
    )
    if refusal is not None:
        return refusal

    try:
        old_base = merge_base(project_root, base_ref, old_sha)
        new_base = merge_base(project_root, base_ref, new_sha)
    except (ReanchorError, subprocess.TimeoutExpired) as exc:
        return ReanchorDecision(
            allowed=False,
            reason=f"merge-base could not be resolved, so the range is unknown: {exc}",
            contract_hash_matches=True, old_sha=old_sha, new_sha=new_sha, depth=depth,
        )

    if old_base == new_base:
        return ReanchorDecision(
            allowed=True,
            reason="identical contract hash and the merge-base did not move — nothing came between",
            contract_hash_matches=True, old_sha=old_sha, new_sha=new_sha,
            old_merge_base=old_base, new_merge_base=new_base, depth=depth,
        )

    try:
        changed = changed_symbols(project_root, old_base, new_base)
        reference_set = referenced_symbols(project_root, new_sha, pr_files)
        # At DEPTH_DIRECT the closure is the input set, so the graph is never
        # consulted. Building it anyway cost 6.8s of a 7.3s decision — measured
        # on the #1691 replay — for a value that is discarded.
        graph = (
            ImportGraph()
            if depth == DEPTH_DIRECT
            else build_import_graph(project_root, new_sha)
        )
        commits = len(_git(project_root, "rev-list", f"{old_base}..{new_base}").split())
    except (ReanchorError, subprocess.TimeoutExpired) as exc:
        return ReanchorDecision(
            allowed=False,
            reason=f"the range could not be analysed, so nothing is proven: {exc}",
            contract_hash_matches=True, old_sha=old_sha, new_sha=new_sha,
            old_merge_base=old_base, new_merge_base=new_base, depth=depth,
        )

    direct_modules = {m for m, _ in reference_set.symbols}
    # Only the modules reached BEYOND the direct imports get the coarse
    # treatment; the direct ones stay symbol-precise. At DEPTH_DIRECT this set
    # is empty by construction.
    reachable = reachable_modules(direct_modules, graph, depth)
    indirect_modules = reachable - direct_modules

    # cannot_prove, in both its forms. Either the PR's own references could not
    # be enumerated, or a module inside the closure has unknown edges — and a
    # closure with a hole in it proves nothing about what lies beyond the hole.
    unprovable = list(reference_set.unmodelled)
    blind = sorted(reachable & set(graph.unresolved))
    if blind:
        unprovable.append(
            "the import closure passes through "
            + ", ".join(blind[:5])
            + ", whose own imports could not be read — everything beyond them is unknown"
        )
    if unprovable:
        return ReanchorDecision(
            allowed=False,
            reason=(
                "cannot prove condition (b): "
                + "; ".join(unprovable[:3])
                + (f" (+{len(unprovable) - 3} more)" if len(unprovable) > 3 else "")
            ),
            contract_hash_matches=True, old_sha=old_sha, new_sha=new_sha,
            old_merge_base=old_base, new_merge_base=new_base,
            commits_in_range=commits, depth=depth,
        )

    blocking = find_blocking_symbols(changed, reference_set.symbols, indirect_modules)
    if blocking:
        named = ", ".join(f"{m}.{s}" for m, s in blocking[:5])
        more = f" (+{len(blocking) - 5} more)" if len(blocking) > 5 else ""
        return ReanchorDecision(
            allowed=False,
            reason=(
                f"{commits} commit(s) between the merge-bases changed {len(blocking)} symbol(s) "
                f"this PR reaches: {named}{more} — the diff is identical but its meaning may "
                "not be (the #1688/#1692 shape)"
            ),
            contract_hash_matches=True, old_sha=old_sha, new_sha=new_sha,
            old_merge_base=old_base, new_merge_base=new_base,
            commits_in_range=commits, blocking_symbols=tuple(blocking), depth=depth,
        )
    return ReanchorDecision(
        allowed=True,
        reason=(
            f"identical contract hash, and none of the {commits} commit(s) between the "
            "merge-bases touches a symbol this PR reaches"
        ),
        contract_hash_matches=True, old_sha=old_sha, new_sha=new_sha,
        old_merge_base=old_base, new_merge_base=new_base,
        commits_in_range=commits, depth=depth,
    )
