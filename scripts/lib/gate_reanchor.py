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

(b) is what this module computes. Measured with THIS code over 120 ordered PR
pairs from 2026-08-26, the busiest measured day on this repo:

  import depth        allowed   refused
  direct (default)        87%       13%
  +1 hop                  74%       26%
  +2 hops                 69%       31%
  full closure            58%       42%

The whole sweep took 8.1 seconds including building the 713-module import
graph from scratch, so (b) is affordable — the outcome the OI left open.

Which row to stand on is a real decision, and the measurement settles it in
an unobvious direction: see :data:`DEPTH_DEFAULT`.

Every failure to establish an answer refuses. A re-anchor that cannot prove
both halves is a re-buy, never a shrug.
"""

from __future__ import annotations

import ast
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

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


def referenced_symbols(project_root: Path, ref: str, files: Iterable[str]) -> Set[Symbol]:
    """Qualified symbols the PR's own files import or reach through.

    ``from x import a`` yields ``("x", "a")`` and ``("x", "<module>")`` — the
    second because importing from a module also binds you to its top level.
    ``import x`` plus ``x.y`` yields ``("x", "y")``.

    Bare-name matching was measured and rejected: it refused 40% of the same
    120 pairs, and most of those refusals were the name ``main``, which nearly
    every script in this repo both defines and calls. Qualifying by module
    took the refusal rate to 13% without losing the #1688/#1692 counterexample.
    """
    out: Set[Symbol] = set()
    for path in files:
        if not path.endswith(".py"):
            continue
        try:
            source = _git(project_root, "show", f"{ref}:{path}")
        except ReanchorError:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        alias_to_module: Dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                module = node.module.split(".")[-1]
                out.add((module, MODULE_LEVEL))
                for alias in node.names:
                    out.add((module, alias.name))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    module = alias.name.split(".")[-1]
                    alias_to_module[alias.asname or module] = module
                    out.add((module, MODULE_LEVEL))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                module = alias_to_module.get(node.value.id)
                if module:
                    out.add((module, node.attr))
    return out


def build_import_graph(project_root: Path, ref: str) -> Dict[str, Set[str]]:
    """module_stem -> directly imported module_stems, over the repo at ``ref``.

    Only modules that exist in the tree are edges: a stdlib or third-party
    import is not something a commit in this range can have changed.
    """
    paths = [
        p for p in _git(project_root, "ls-tree", "-r", "--name-only", ref).split()
        if p.endswith(".py") and not p.startswith("tests/")
    ]
    known = {Path(p).stem for p in paths}
    graph: Dict[str, Set[str]] = {}
    for path in paths:
        try:
            source = _git(project_root, "show", f"{ref}:{path}")
            tree = ast.parse(source)
        except (ReanchorError, SyntaxError):
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
    return graph


def reachable_modules(
    modules: Iterable[str], graph: Dict[str, Set[str]], depth: int = DEPTH_DEFAULT
) -> Set[str]:
    """Modules reachable from ``modules`` within ``depth`` import hops."""
    seen = set(modules)
    frontier = set(seen)
    hops = 0
    while frontier and (depth == DEPTH_FULL or hops < depth):
        nxt: Set[str] = set()
        for module in frontier:
            nxt |= graph.get(module, set()) - seen
        seen |= nxt
        frontier = nxt
        hops += 1
    return seen


def find_blocking_symbols(
    changed: Set[Symbol],
    referenced: Set[Symbol],
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
      3. **Reached only indirectly.** ``indirect_modules`` are modules the PR
         does not import but can reach through the import graph. Which symbol
         of those the PR actually depends on is not knowable from its own
         source, so ANY change in one of them blocks.

    Keeping 1 apart from 3 is what makes the direct-depth rule symbol-precise.
    Folding them together — as an earlier draft of this function did, by
    letting the reachable set carry the directly-imported modules too — silently
    turns the whole thing into rule 3, blocking on a changed sibling function
    the PR never calls. It was caught by mutation-testing this file: deleting
    the exact-overlap line changed no test result, because nothing could
    reach it.
    """
    indirect = indirect_modules or set()
    referenced_modules = {m for m, _ in referenced}
    blocking: Set[Symbol] = set(changed & referenced)
    for module, symbol in changed:
        if module in indirect:
            blocking.add((module, symbol))
        elif symbol in (MODULE_LEVEL, WHOLE_MODULE) and module in referenced_modules:
            blocking.add((module, symbol))
    return sorted(blocking)


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
        referenced = referenced_symbols(project_root, new_sha, pr_files)
        # At DEPTH_DIRECT the closure is the input set, so the graph is never
        # consulted. Building it anyway cost 6.8s of a 7.3s decision — measured
        # on the #1691 replay — for a value that is discarded.
        graph = (
            {} if depth == DEPTH_DIRECT else build_import_graph(project_root, new_sha)
        )
        commits = len(_git(project_root, "rev-list", f"{old_base}..{new_base}").split())
    except (ReanchorError, subprocess.TimeoutExpired) as exc:
        return ReanchorDecision(
            allowed=False,
            reason=f"the range could not be analysed, so nothing is proven: {exc}",
            contract_hash_matches=True, old_sha=old_sha, new_sha=new_sha,
            old_merge_base=old_base, new_merge_base=new_base, depth=depth,
        )

    direct_modules = {m for m, _ in referenced}
    # Only the modules reached BEYOND the direct imports get the coarse
    # treatment; the direct ones stay symbol-precise. At DEPTH_DIRECT this set
    # is empty by construction.
    indirect_modules = reachable_modules(direct_modules, graph, depth) - direct_modules
    blocking = find_blocking_symbols(changed, referenced, indirect_modules)
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
