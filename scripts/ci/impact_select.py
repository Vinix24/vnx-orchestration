#!/usr/bin/env python3
"""Impact selection: which test files can a set of changed paths reach?

A static test-to-module graph, built from the Python AST (imports, transitive)
and from path literals in the code (``"scripts/x.py"``, ``ROOT / "scripts" / "x.py"``),
because most tests start scripts through subprocess and an import graph alone
misses them.  Pure computation: no pytest-testmon, no Bazel, no Nx.

The selector decides nothing in CI.  Whenever it is not sure it expands to the
full suite and names the reason in ``expand_reason``.

CLI (the ``impact_cmd`` hook contract): changed paths in through ``--paths-file``
or stdin, one per line, ``--root <dir>`` optional; one JSON object out with
sorted keys.  Exit 0 whenever that object was printed, also for ``scope: full``.
A crash prints nothing and exits non-zero; the caller then runs the full suite.
"""

from __future__ import annotations

import ast
import configparser
import fnmatch
import hashlib
import json
import os
import re
import sys
import tomllib
import warnings
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import classify_ci_profile as classifier

REPO_ROOT = classifier.REPO_ROOT
DEFAULT_TEST_PATTERNS = ("test_*.py", "*_test.py")
IMPORT_BASES = ("", "tests", "scripts/lib", "scripts")  # the test sys.path
TEST_INFRASTRUCTURE_FILES = frozenset(
    {
        "pyproject.toml",
        "pytest.ini",
        "scripts/ci/profile_a_constraints.txt",
        "scripts/ci/test_exclusions.txt",
        "scripts/ci/xdist_expected_skips.txt",
    }
)
# Priority order when several reasons apply; part of graph_version.
EXPAND_RULES = (
    "malformed_input",
    "parse_error",
    "conftest_changed",
    "test_infrastructure",
    "path_outside_scope",
    "unknown_to_graph",
)
_SH_REF_RE = re.compile(r"scripts/[A-Za-z0-9_./-]+")
_SKIP_DIRS = {"__pycache__", "node_modules"}


def read_test_patterns(root: Path) -> tuple[str, ...]:
    """The ``python_files`` patterns of the repo's pytest configuration."""
    ini = root / "pytest.ini"
    if ini.is_file():
        parser = configparser.ConfigParser()
        parser.read(ini, encoding="utf-8")
        value = parser.get("pytest", "python_files", fallback="")
        if value.split():
            return tuple(value.split())
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        value = data.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("python_files")
        if isinstance(value, str):
            value = value.split()
        if value:
            return tuple(value)
    return DEFAULT_TEST_PATTERNS


def _walk_files(root: Path, top: str, suffixes: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root / top):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not d.startswith("."))
        for name in filenames:
            if name.endswith(suffixes):
                found.append(os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/"))
    return sorted(found)


def _module_files(nodes: set[str], bases, parts: list[str]) -> set[str]:
    """Graph files a dotted module path *parts* names under each of *bases*.

    Packages resolve to their ``__init__.py``; every parent package's
    ``__init__.py`` runs on import and counts as well.
    """
    found: set[str] = set()
    for base in bases:
        for i in range(1, len(parts) + 1):
            stem = "/".join([p for p in (base, *parts[:i]) if p])
            if f"{stem}/__init__.py" in nodes:
                found.add(f"{stem}/__init__.py")
            if i == len(parts) and f"{stem}.py" in nodes:
                found.add(f"{stem}.py")
    return found


def _import_targets(tree: ast.AST, rel: str, nodes: set[str]) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out |= _module_files(nodes, IMPORT_BASES, alias.name.split("."))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module.split(".") if node.module else []
            if node.level:
                pkg = rel.split("/")[:-1]
                pkg = pkg[: max(len(pkg) - (node.level - 1), 0)]
                bases = ("/".join(pkg),)
            else:
                bases = IMPORT_BASES
            out |= _module_files(nodes, bases, mod)
            for alias in node.names:
                out |= _module_files(nodes, bases, mod + [alias.name])
        elif isinstance(node, ast.Call) and _is_import_module(node.func) and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                out |= _module_files(nodes, IMPORT_BASES, arg.value.split("."))
    return out


def _is_import_module(func: ast.AST) -> bool:
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    return name == "import_module"


def _flatten_div(node: ast.AST) -> list:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _flatten_div(node.left) + _flatten_div(node.right)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    return [None]


def _run_targets(seq: list, nodes: set[str]) -> set[str]:
    """Script files named by any suffix of each run of string constants in *seq*."""
    out: set[str] = set()
    run: list[str] = []
    for item in [*seq, None]:
        if item is not None:
            run.append(item)
            continue
        for start in range(len(run)):
            cand = "/".join(p.strip("/") for p in run[start:])
            if cand.startswith("scripts/") and cand in nodes:
                out.add(cand)
        run = []
    return out


def _literal_targets(tree: ast.AST, nodes: set[str]) -> set[str]:
    """Script files named by path literals: a string, a ``/`` join or a join call."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if len(node.value) < 400:
                out |= _run_targets([tok for tok in node.value.split() if tok] + [None], nodes)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            out |= _run_targets(_flatten_div(node), nodes)
        elif isinstance(node, ast.Call):
            args = [a.value if isinstance(a, ast.Constant) and isinstance(a.value, str) else None for a in node.args]
            out |= _run_targets(args, nodes)
    return out


def _parse(path: Path):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def build_graph(root: Path) -> dict:
    """Return ``{nodes, edges, tests, parse_errors}``; an edge ``a -> b`` means a depends on b."""
    py_files = _walk_files(root, "tests", (".py",)) + _walk_files(root, "scripts", (".py",))
    sh_files = _walk_files(root, "scripts", (".sh",))
    nodes = set(py_files) | set(sh_files)
    patterns = read_test_patterns(root)
    tests = sorted(p for p in py_files if p.startswith("tests/") and any(fnmatch.fnmatch(p.rsplit("/", 1)[-1], pat) for pat in patterns))
    edges: dict[str, set[str]] = {}
    parse_errors: list[str] = []
    for rel in py_files:
        try:
            tree = _parse(root / rel)
        except (SyntaxError, ValueError, UnicodeDecodeError, RecursionError, MemoryError):
            parse_errors.append(rel)
            continue
        edges[rel] = (_import_targets(tree, rel, nodes) | _literal_targets(tree, nodes)) - {rel}
    for rel in sh_files:
        text = (root / rel).read_text(encoding="utf-8", errors="replace")
        edges[rel] = {m for m in _SH_REF_RE.findall(text) if m in nodes} - {rel}
    return {"nodes": nodes, "edges": edges, "tests": tests, "parse_errors": sorted(parse_errors)}


def graph_version(edges: dict[str, set[str]]) -> str:
    """sha256 over the sorted edge list plus the expansion-rule table."""
    lines = sorted(f"{a}\t{b}" for a, targets in edges.items() for b in targets)
    rules = json.dumps({"expand": EXPAND_RULES, "infra": sorted(TEST_INFRASTRUCTURE_FILES), "bases": IMPORT_BASES})
    return hashlib.sha256(("\n".join(lines) + "\n--\n" + rules).encode()).hexdigest()


def selector_version() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _is_infrastructure(path: str) -> bool:
    if path in TEST_INFRASTRUCTURE_FILES or path.startswith(".github/"):
        return True
    return path.startswith("tests/") and (path.startswith("tests/fixtures/") or not path.endswith(".py"))


def classify_change(path: str, nodes: set[str]) -> str | None:
    """The expand reason one changed *path* forces, or ``None`` when it can be followed."""
    if classifier.is_docs_only_path(path):
        return None
    if path.rsplit("/", 1)[-1] == classifier.CONFTEST_NAME:
        return "conftest_changed"
    if _is_infrastructure(path):
        return "test_infrastructure"
    if not path.startswith(("scripts/", "tests/")):
        return "path_outside_scope"
    return None if path in nodes else "unknown_to_graph"


def _reaching(edges: dict[str, set[str]], targets: set[str]) -> set[str]:
    """Every file that reaches one of *targets* through the graph (targets included)."""
    reverse: dict[str, list[str]] = {}
    for src, dsts in edges.items():
        for dst in dsts:
            reverse.setdefault(dst, []).append(src)
    seen = set(targets)
    queue = deque(targets)
    while queue:
        for src in reverse.get(queue.popleft(), ()):
            if src not in seen:
                seen.add(src)
                queue.append(src)
    return seen


def conftest_closure(graph: dict) -> set[str]:
    """Repo files reached transitively from every ``conftest.py`` under ``tests/``.

    Autouse fixtures import modules such as ``pr_merge`` inside their bodies, so
    every test runs them; the graph draws no edge from a test to that closure
    (OI-2788).  The import resolution is the graph's own, function bodies included.
    """
    starts = {n for n in graph["nodes"] if n.startswith("tests/") and n.rsplit("/", 1)[-1] == classifier.CONFTEST_NAME}
    seen: set[str] = set()
    queue = deque(starts)
    while queue:
        for dst in graph["edges"].get(queue.popleft(), ()):
            if dst not in seen:
                seen.add(dst)
                queue.append(dst)
    return seen


def _result(graph: dict, exclusions: set[str], selected, reason, unreached, changed=()) -> dict:
    universe = set(graph["tests"])
    closure = conftest_closure(graph)
    return {
        "conftest_closure_version": hashlib.sha256("\n".join(sorted(closure)).encode()).hexdigest(),
        "conftest_reached": any(p in closure for p in changed),
        "scope": "full" if reason else "impact",
        "expand_reason": reason,
        "selected": sorted(selected),
        "selected_excluded": sorted(set(selected) & exclusions),
        "unreached": sorted(unreached),
        "parse_errors": graph["parse_errors"],
        "universe_count": len(universe),
        "excluded_count": len(universe & exclusions),
        "selector_version": selector_version(),
        "graph_version": graph_version(graph["edges"]),
    }


def select(root: Path, paths) -> dict:
    """The selection payload for the changed *paths* of the checkout at *root*."""
    graph = build_graph(root)
    exclusions = set(classifier.read_exclusion_entries((root / classifier.EXCLUSIONS_PATH).read_text(encoding="utf-8")))
    universe = set(graph["tests"])
    try:
        changed = sorted(set(classifier._changed_test_paths(paths)))
    except classifier.MalformedPathsError:
        return _result(graph, exclusions, universe, "malformed_input", [])
    reasons = {r for r in (classify_change(p, graph["nodes"]) for p in changed) if r}
    if graph["parse_errors"]:
        reasons.add("parse_error")
    reason = next((r for r in EXPAND_RULES if r in reasons), None)
    if reason:
        return _result(graph, exclusions, universe, reason, [], changed)
    targets = {p for p in changed if p in graph["nodes"]}
    selected = _reaching(graph["edges"], targets) & universe
    unreached = {p for p in targets if p not in universe and not (_reaching(graph["edges"], {p}) & universe)}
    return _result(graph, exclusions, selected, None, unreached, changed)


def _read_paths(args: list[str]) -> list[str]:
    paths_file = classifier._pop_option(args, "--paths-file")
    if paths_file:
        return Path(paths_file).read_text(encoding="utf-8").splitlines()
    return sys.stdin.read().splitlines()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root_arg = classifier._pop_option(args, "--root")
    root = Path(root_arg) if root_arg else REPO_ROOT
    try:
        paths = _read_paths(args)
    except UnicodeDecodeError:
        paths = None  # select() reports malformed_input
    print(json.dumps(select(root, paths), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
