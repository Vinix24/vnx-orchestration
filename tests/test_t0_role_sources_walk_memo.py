"""The argparse walk interprets each function once per (call site, argument
identity).

`_ArgparseWalk.__init__` calls every function of the module once, and `_call`
used to interpret a body again on every call, also when it had already been
interpreted with the same argument objects: a cProfile of one
`t0_role_sources_audit.py` run showed 271,316 `_stmts` calls for 321 functions
over seven files. The memo added for dlv-682f0d227007 keys a call on
`(where, ids of args, ids of kwargs)` and keeps the keyed objects alive; this
file freezes that the memo changes the work done, never the words the audit
reads.

Dispatch-ID: dlv-682f0d227007
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT / "scripts" / "lib") not in sys.path:
    sys.path.insert(0, str(_ROOT / "scripts" / "lib"))

audit = importlib.import_module("t0_role_sources_audit")

# The seven scripts one `t0_role_sources_audit.py <repo> <repo>` run walks on
# origin/main (instrumented `_argparse_walk` call sites): the scripts the
# bin/vnx branches dispatch to, the pip CLI, and the two scripts the role names
# a subcommand of. `scripts/planning_cli.py` builds its parsers in the
# `_add_*_parser` helpers, so it covers a parser built in a helper function.
WALKED = [
    "scripts/lib/dispatch_bridge.py",
    "scripts/lib/forge_gate_publisher.py",
    "scripts/open_items_manager.py",
    "scripts/planning_cli.py",
    "scripts/receipt_query.py",
    "scripts/runtime_core_cli.py",
    "vnx_cli/main.py",
]


def _resolve(mod: str) -> "ast.Module | None":
    return audit._parse_py(audit._module_file(_ROOT, mod))


def _walk(rel: str, memo: bool) -> "audit._ArgparseWalk":
    tree = audit._parse_py(_ROOT / rel)
    assert tree is not None, f"{rel} must parse"
    return audit._ArgparseWalk(tree, _resolve, memo=memo)


def _words_by_root(walk: "audit._ArgparseWalk") -> Dict[Tuple[str, str], Set[Tuple[str, ...]]]:
    """The command word path from each parser to each name it dispatches on."""
    words: Dict[Tuple[str, str], Set[Tuple[str, ...]]] = {}

    def visit(parser: "audit._Parser", prefix: Tuple[str, ...], seen: frozenset) -> None:
        words.setdefault(parser.origin, set()).add(prefix)
        if id(parser) in seen:
            return
        for word, child in parser.children.items():
            visit(child, prefix + (word,), seen | {id(parser)})

    for root in walk.roots:
        visit(root, (), frozenset())
    return words


def _count_stmts(rel: str, memo: bool) -> int:
    """`_stmts` invocations the walk makes; the deterministic cost of a walk."""

    class Counting(audit._ArgparseWalk):
        stmts = 0

        def _stmts(self, *args, **kwargs):
            type(self).stmts += 1
            return super()._stmts(*args, **kwargs)

    tree = audit._parse_py(_ROOT / rel)
    assert tree is not None
    Counting(tree, _resolve, memo=memo)
    return Counting.stmts


@pytest.mark.parametrize("rel", WALKED, ids=[Path(p).name for p in WALKED])
def test_memo_true_and_false_read_the_same_words(rel: str) -> None:
    with_memo, without = _walk(rel, True), _walk(rel, False)
    assert _words_by_root(with_memo) == _words_by_root(without)
    assert with_memo.argv_words == without.argv_words


def test_planning_cli_is_one_of_the_walked_scripts() -> None:
    assert "scripts/planning_cli.py" in WALKED


def test_memo_cuts_the_stmts_work_of_the_audit_walk_at_least_tenfold() -> None:
    """The whole walk set, the axis the cProfile counted (271,316 `_stmts`)."""
    plain = sum(_count_stmts(rel, False) for rel in WALKED)
    memoized = sum(_count_stmts(rel, True) for rel in WALKED)
    assert plain > 0
    assert memoized * 10 <= plain, f"memo={memoized} plain={plain}"


def test_planning_cli_interprets_each_call_site_once() -> None:
    """Per file the first interpretation of every distinct call key stays, so
    the ratio is bounded by the number of distinct keys (~7x here), not 10x."""
    plain = _count_stmts("scripts/planning_cli.py", False)
    memoized = _count_stmts("scripts/planning_cli.py", True)
    assert memoized * 3 <= plain, f"memo={memoized} plain={plain}"


RECURSION_SOURCE = """
import argparse


def build(parser):
    subs = parser.add_subparsers()
    subs.add_parser("alpha")
    return subs


def spin(parser):
    return helper(parser)


def helper(parser):
    return spin(parser)


def main():
    parser = argparse.ArgumentParser()
    build(parser)
    build(parser)
    spin(parser)
"""


class _RecursionWalk(audit._ArgparseWalk):
    """Counts how often each function body is interpreted."""

    def __init__(self, tree, resolve, memo: bool) -> None:
        self.bodies: List[Tuple[str, str]] = []
        super().__init__(tree, resolve, memo=memo)

    def _stmts(self, stmts, env, where):
        self.bodies.append(where)
        return super()._stmts(stmts, env, where)


def _recursion_walk(memo: bool) -> "_RecursionWalk":
    tree = ast.parse(RECURSION_SOURCE, filename="<recursion>")
    return _RecursionWalk(tree, lambda mod: None, memo=memo)


def test_recursion_reads_the_same_words_and_the_second_builder_call_is_a_hit() -> None:
    with_memo, without = _recursion_walk(True), _recursion_walk(False)
    assert _words_by_root(with_memo) == _words_by_root(without)
    assert with_memo.argv_words == without.argv_words
    builder = ("<main>", "build")
    assert without.bodies.count(builder) == 3, without.bodies
    # The constructor's argument-less call, then the first `build(parser)` from
    # `main`; the second call with the same parser object is a cache hit.
    assert with_memo.bodies.count(builder) == 2, with_memo.bodies


def _root_shapes(walk: "audit._ArgparseWalk") -> list:
    """Each root: where it was built, its dispatch words, and its parsed flag."""

    def shape(parser: "audit._Parser") -> tuple:
        children = sorted((word, shape(child)) for word, child in parser.children.items())
        return (tuple(sorted(parser.children)), tuple(children))

    return sorted((root.origin, shape(root), root.parsed) for root in walk.roots)


def _source_walk(source: str, memo: bool) -> "_RecursionWalk":
    tree = ast.parse(source, filename="<memo>")
    return _RecursionWalk(tree, lambda mod: None, memo=memo)


FACTORY_SOURCE = """
import argparse


def make_parser():
    return argparse.ArgumentParser()


def main():
    parser = make_parser()
    subs = parser.add_subparsers()
    subs.add_parser("run")
    parser.parse_args()


def internal():
    parser = make_parser()
    subs = parser.add_subparsers()
    subs.add_parser("internal")
"""

FACTORY_ARG_SOURCE = """
import argparse


def make_parser(prog):
    return argparse.ArgumentParser(prog=prog)


def main():
    parser = make_parser("vnx")
    subs = parser.add_subparsers()
    subs.add_parser("run")
    parser.parse_args()


def internal():
    parser = make_parser("vnx")
    subs = parser.add_subparsers()
    subs.add_parser("internal")
"""

SHARED_PARSER_SOURCE = """
import argparse


def add_run(sub):
    sub.add_parser("run")


def main():
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers()
    add_run(subs)
    add_run(subs)
"""

CUT_SHORT_SOURCE = """
import argparse


def cycle(parser):
    return cycle_other(parser)


def cycle_other(parser):
    return cycle(parser)


def main():
    parser = argparse.ArgumentParser()
    cycle(parser)
    cycle(parser)
"""


def _assert_two_roots_one_parsed(with_memo: "_RecursionWalk") -> None:
    built = [root for root in with_memo.roots if root.children]
    assert len(built) == 2, _root_shapes(with_memo)
    parsed = [sorted(root.children) for root in built if root.parsed]
    unparsed = [sorted(root.children) for root in built if not root.parsed]
    assert parsed == [["run"]], _root_shapes(with_memo)
    assert unparsed == [["internal"]], _root_shapes(with_memo)


@pytest.mark.parametrize("source", [FACTORY_SOURCE, FACTORY_ARG_SOURCE], ids=["no-arg", "arg-none"])
def test_a_parser_factory_result_is_not_memoized_across_call_sites(source: str) -> None:
    """A factory creates a root parser on every run; a hit must not merge the
    parsers two callers then build differently (the codex fence case)."""
    with_memo, without = _source_walk(source, True), _source_walk(source, False)
    _assert_two_roots_one_parsed(without)
    _assert_two_roots_one_parsed(with_memo)
    assert _root_shapes(with_memo) == _root_shapes(without)
    assert with_memo.argv_words == without.argv_words


def test_a_helper_that_only_adds_to_a_given_parser_stays_cached() -> None:
    with_memo, without = _source_walk(SHARED_PARSER_SOURCE, True), _source_walk(SHARED_PARSER_SOURCE, False)
    assert _root_shapes(with_memo) == _root_shapes(without)
    assert with_memo.argv_words == without.argv_words
    helper = ("<main>", "add_run")
    assert without.bodies.count(helper) == 3, without.bodies
    # The constructor's argument-less call, then the first `add_run(subs)` from
    # `main`; the second call with the same parser object is a cache hit.
    assert with_memo.bodies.count(helper) == 2, with_memo.bodies


def test_a_call_cut_short_by_the_recursion_guard_is_not_stored() -> None:
    with_memo, without = _source_walk(CUT_SHORT_SOURCE, True), _source_walk(CUT_SHORT_SOURCE, False)
    assert _root_shapes(with_memo) == _root_shapes(without)
    assert with_memo.argv_words == without.argv_words
    builder = ("<main>", "cycle")
    assert without.bodies.count(builder) == 4, without.bodies
    # Cut short once per run, so the result depends on the stack: never stored,
    # and the second `cycle(parser)` from `main` interprets the body again.
    assert with_memo.bodies.count(builder) == 4, with_memo.bodies
    assert not [key for key in with_memo._cache if key[0] == builder], with_memo._cache
