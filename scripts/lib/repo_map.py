#!/usr/bin/env python3
"""
VNX Repo Map — Symbol extraction and PageRank ranking for dispatch enrichment.

Extracts code symbols (functions, classes, methods, imports) from Python files
via tree-sitter, builds a dependency graph with NetworkX, and ranks symbols by
PageRank. Inspired by Aider's repository mapping approach.

Gracefully degrades to regex extraction when tree-sitter is unavailable, and to
positional ordering when networkx is unavailable.

Usage:
    python3 scripts/lib/repo_map.py --files scripts/learning_loop.py --top 20
    python3 scripts/lib/repo_map.py --files scripts/lib/intelligence_selector.py --top 10 --json
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

# ---------------------------------------------------------------------------
# Optional imports (graceful fallback if not installed)
# ---------------------------------------------------------------------------

try:
    import tree_sitter_python as _tsp
    from tree_sitter import Language, Parser as _TSParser
    _PY_LANG = Language(_tsp.language())
    _TREESITTER_AVAILABLE = True
except ImportError:
    _TREESITTER_AVAILABLE = False

try:
    import networkx as nx
    _NETWORKX_AVAILABLE = True
except ImportError:
    _NETWORKX_AVAILABLE = False


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Symbol:
    name: str
    kind: str          # "function", "class", "method", "import"
    file_path: str
    line: int
    signature: str     # e.g. "def persist_to_intelligence_db(conn, patterns)"


@dataclass
class RepoMap:
    symbols: List[Symbol]
    ranked_symbols: List[Symbol]   # sorted by PageRank score (highest first)
    graph_nodes: int
    graph_edges: int


# ---------------------------------------------------------------------------
# Tree-sitter helpers
# ---------------------------------------------------------------------------

def _node_text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _child_by_type(node, typ: str):
    return next((c for c in node.children if c.type == typ), None)


def _extract_func_signature(node, src: bytes) -> str:
    """Return 'def name(params)' from a function_definition node."""
    name = _node_text(c, src) if (c := _child_by_type(node, "identifier")) else "?"
    params = _node_text(c, src) if (c := _child_by_type(node, "parameters")) else "()"
    return f"def {name}{params}"


def _extract_class_signature(node, src: bytes) -> str:
    """Return 'class Name(bases)' from a class_definition node."""
    name = _node_text(c, src) if (c := _child_by_type(node, "identifier")) else "?"
    bases_node = _child_by_type(node, "argument_list")
    bases = f"({_node_text(bases_node, src)})" if bases_node else ""
    return f"class {name}{bases}"


def _walk_node(
    node,
    src: bytes,
    file_path: str,
    symbols: List[Symbol],
    class_stack: List[str],
) -> None:
    """Iterative DFS over tree-sitter AST to collect symbols."""
    # Stack entries: (node, class_stack_snapshot)
    stack = [(node, class_stack)]
    while stack:
        cur, cur_classes = stack.pop()
        ntype = cur.type

        if ntype == "function_definition":
            name_node = _child_by_type(cur, "identifier")
            if name_node:
                name = _node_text(name_node, src)
                sig = _extract_func_signature(cur, src)
                kind = "method" if cur_classes else "function"
                symbols.append(Symbol(
                    name=name,
                    kind=kind,
                    file_path=file_path,
                    line=cur.start_point[0] + 1,
                    signature=sig,
                ))
            # Push children with same class context (functions don't push to class_stack)
            for child in reversed(cur.children):
                stack.append((child, cur_classes))
            continue

        if ntype == "class_definition":
            name_node = _child_by_type(cur, "identifier")
            class_name = _node_text(name_node, src) if name_node else ""
            sig = _extract_class_signature(cur, src)
            symbols.append(Symbol(
                name=class_name,
                kind="class",
                file_path=file_path,
                line=cur.start_point[0] + 1,
                signature=sig,
            ))
            # Push children with class on stack
            for child in reversed(cur.children):
                stack.append((child, cur_classes + [class_name]))
            continue

        if ntype in ("import_statement", "import_from_statement"):
            text = _node_text(cur, src).strip()
            symbols.append(Symbol(
                name=text,
                kind="import",
                file_path=file_path,
                line=cur.start_point[0] + 1,
                signature=text,
            ))
            continue

        for child in reversed(cur.children):
            stack.append((child, cur_classes))


def extract_symbols_treesitter(file_path: str) -> List[Symbol]:
    """Extract symbols from a Python file using tree-sitter."""
    path = Path(file_path)
    if not path.exists() or path.suffix != ".py":
        return []
    src = path.read_bytes()
    parser = _TSParser(_PY_LANG)
    tree = parser.parse(src)
    symbols: List[Symbol] = []
    _walk_node(tree.root_node, src, file_path, symbols, [])
    return symbols


# ---------------------------------------------------------------------------
# Regex fallback extraction
# ---------------------------------------------------------------------------

_DEF_RE = re.compile(
    r'^([ \t]*)(async\s+)?def\s+(\w+)\s*(\([^)]*\))',
    re.MULTILINE,
)
_CLASS_RE = re.compile(
    r'^([ \t]*)class\s+(\w+)\s*(\([^)]*\))?',
    re.MULTILINE,
)
_IMPORT_RE = re.compile(
    r'^(?:from\s+\S+\s+import\s+.+|import\s+.+)',
    re.MULTILINE,
)


def extract_symbols_regex(file_path: str) -> List[Symbol]:
    """Fallback: extract symbols using regex when tree-sitter unavailable."""
    path = Path(file_path)
    if not path.exists() or path.suffix != ".py":
        return []

    text = path.read_text(encoding="utf-8", errors="replace")
    symbols: List[Symbol] = []

    # Track classes by indent for method detection
    class_at_indent: Dict[int, str] = {}

    for m in _CLASS_RE.finditer(text):
        indent = len(m.group(1))
        name = m.group(2)
        bases = m.group(3) or ""
        class_at_indent[indent] = name
        line_no = text[: m.start()].count("\n") + 1
        symbols.append(Symbol(
            name=name,
            kind="class",
            file_path=file_path,
            line=line_no,
            signature=f"class {name}{bases}",
        ))

    for m in _DEF_RE.finditer(text):
        indent = len(m.group(1))
        name = m.group(3)
        params = m.group(4)
        enclosing = [cls for ind, cls in class_at_indent.items() if ind < indent]
        kind = "method" if enclosing else "function"
        line_no = text[: m.start()].count("\n") + 1
        symbols.append(Symbol(
            name=name,
            kind=kind,
            file_path=file_path,
            line=line_no,
            signature=f"def {name}{params}",
        ))

    for m in _IMPORT_RE.finditer(text):
        line_no = text[: m.start()].count("\n") + 1
        val = m.group(0).strip()
        symbols.append(Symbol(
            name=val,
            kind="import",
            file_path=file_path,
            line=line_no,
            signature=val,
        ))

    return sorted(symbols, key=lambda s: s.line)


# ---------------------------------------------------------------------------
# Dependency graph construction
# ---------------------------------------------------------------------------

def _file_identifier_set(file_path: str) -> Set[str]:
    """Return all Python-style identifiers that appear in a source file."""
    path = Path(file_path)
    if not path.exists():
        return set()
    text = path.read_text(encoding="utf-8", errors="replace")
    return set(re.findall(r'\b([A-Za-z_]\w+)\b', text))


def build_dependency_graph(
    all_symbols: List[Symbol],
    target_files: List[str],
) -> "nx.DiGraph":
    """Build a directed symbol dependency graph.

    Nodes  = unique (file_path, symbol_name) keys for non-import symbols.
    Edges  = file F references symbol name S that is defined in file G:
             F→G for each symbol in F whose name appears in G's identifier set.

    This is a file-level cross-reference heuristic — not a precise call graph —
    but it is fast and sufficient for PageRank-based ranking.
    """
    G: nx.DiGraph = nx.DiGraph()

    non_import = [s for s in all_symbols if s.kind != "import"]

    # Build node keys and add to graph
    def _sym_key(s: Symbol) -> str:
        return f"{s.file_path}:{s.name}"

    for sym in non_import:
        G.add_node(_sym_key(sym))

    # Index: name → list of symbols that define it
    name_to_syms: Dict[str, List[Symbol]] = {}
    for sym in non_import:
        name_to_syms.setdefault(sym.name, []).append(sym)

    # For each file, find which other symbols' names appear in it
    files = list({s.file_path for s in all_symbols})
    file_ids: Dict[str, Set[str]] = {fp: _file_identifier_set(fp) for fp in files}

    for sym in non_import:
        caller_key = _sym_key(sym)
        ids_in_file = file_ids.get(sym.file_path, set())
        for ref_name in ids_in_file:
            if ref_name == sym.name:
                continue
            for target_sym in name_to_syms.get(ref_name, []):
                # Only add edges to symbols in *other* files (avoids noise from self-references)
                if target_sym.file_path != sym.file_path:
                    G.add_edge(caller_key, _sym_key(target_sym))

    return G


# ---------------------------------------------------------------------------
# PageRank ranking
# ---------------------------------------------------------------------------

def rank_symbols(
    symbols: List[Symbol],
    graph: "nx.DiGraph",
    target_files: List[str],
    top_k: int = 20,
) -> List[Symbol]:
    """Run personalized PageRank on the dependency graph and return top-K symbols.

    Personalization: symbols in target_files receive 3× weight over others,
    biasing the rank toward symbols relevant to the dispatch scope.
    """
    non_import = [s for s in symbols if s.kind != "import"]

    if graph.number_of_nodes() == 0:
        # No graph: target-file symbols first, then by line number
        target_set = set(target_files)
        return sorted(non_import, key=lambda s: (s.file_path not in target_set, s.line))[:top_k]

    target_set = set(target_files)
    target_nodes = [n for n in graph.nodes() if n.split(":", 1)[0] in target_set]
    other_nodes = [n for n in graph.nodes() if n.split(":", 1)[0] not in target_set]

    BOOST = 3.0
    total_weight = len(target_nodes) * BOOST + len(other_nodes) * 1.0
    if total_weight > 0:
        personalization: Dict[str, float] = {}
        for n in target_nodes:
            personalization[n] = BOOST / total_weight
        for n in other_nodes:
            personalization[n] = 1.0 / total_weight
    else:
        personalization = None  # type: ignore[assignment]

    try:
        scores: Dict[str, float] = nx.pagerank(
            graph,
            personalization=personalization,
            max_iter=200,
            tol=1.0e-6,
        )
    except nx.PowerIterationFailedConvergence:
        scores = {n: 1.0 / max(graph.number_of_nodes(), 1) for n in graph.nodes()}

    def _score(sym: Symbol) -> float:
        return scores.get(f"{sym.file_path}:{sym.name}", 0.0)

    ranked = sorted(non_import, key=_score, reverse=True)
    return ranked[:top_k]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_repo_map(
    target_files: List[str],
    project_root: Optional[Path] = None,
    top_k: int = 20,
) -> RepoMap:
    """Build symbol dependency graph and rank by PageRank.

    Args:
        target_files:  Python files to analyze. Relative paths are resolved
                       against project_root (default: CWD).
        project_root:  Root directory for resolving relative file paths.
        top_k:         Number of ranked symbols to include in RepoMap.ranked_symbols.

    Returns:
        RepoMap with all extracted symbols and top-K PageRank-ranked symbols.
        Falls back gracefully when tree-sitter or networkx are unavailable.
    """
    if project_root is None:
        project_root = Path.cwd()

    resolved: List[str] = []
    for f in target_files:
        p = Path(f)
        if not p.is_absolute():
            p = project_root / p
        resolved.append(str(p.resolve()))

    extract_fn = extract_symbols_treesitter if _TREESITTER_AVAILABLE else extract_symbols_regex

    all_symbols: List[Symbol] = []
    for fp in resolved:
        all_symbols.extend(extract_fn(fp))

    if _NETWORKX_AVAILABLE:
        G = build_dependency_graph(all_symbols, resolved)
        graph_nodes = G.number_of_nodes()
        graph_edges = G.number_of_edges()
        ranked = rank_symbols(all_symbols, G, resolved, top_k=top_k)
    else:
        target_set = set(resolved)
        non_import = [s for s in all_symbols if s.kind != "import"]
        ranked = sorted(non_import, key=lambda s: (s.file_path not in target_set, s.line))[:top_k]
        graph_nodes = 0
        graph_edges = 0

    return RepoMap(
        symbols=all_symbols,
        ranked_symbols=ranked,
        graph_nodes=graph_nodes,
        graph_edges=graph_edges,
    )


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def format_repo_map(repo_map: RepoMap, top_k: int = 20) -> str:
    """Format ranked symbols for injection into a dispatch instruction block."""
    cwd = Path.cwd()
    lines = [
        f"### Repo Map (auto-generated, top {min(top_k, len(repo_map.ranked_symbols))} symbols by relevance)"
    ]
    for i, sym in enumerate(repo_map.ranked_symbols[:top_k], 1):
        try:
            fp = str(Path(sym.file_path).relative_to(cwd))
        except ValueError:
            fp = sym.file_path
        sig = sym.signature
        lines.append(f"{i}. {fp}:{sym.name}() — {sig}")
    lines.append(
        f"\n(graph: {repo_map.graph_nodes} nodes, {repo_map.graph_edges} edges"
        f" | extraction: {'tree-sitter' if _TREESITTER_AVAILABLE else 'regex fallback'}"
        f" | ranking: {'pagerank' if _NETWORKX_AVAILABLE else 'positional fallback'})"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Build a ranked repo map for dispatch context enrichment.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  python3 scripts/lib/repo_map.py --files scripts/learning_loop.py --top 20\n"
               "  python3 scripts/lib/repo_map.py --files scripts/lib/intelligence_selector.py --json",
    )
    ap.add_argument("--files", nargs="+", required=True, metavar="FILE",
                    help="Python files to analyze (relative or absolute paths)")
    ap.add_argument("--top", type=int, default=20, metavar="K",
                    help="Number of top-ranked symbols to output (default: 20)")
    ap.add_argument("--json", action="store_true",
                    help="Output ranked symbols as JSON instead of plain text")
    args = ap.parse_args()

    mode_ts = "tree-sitter" if _TREESITTER_AVAILABLE else "regex (fallback — install tree-sitter)"
    mode_nx = "networkx+pagerank" if _NETWORKX_AVAILABLE else "positional (fallback — install networkx)"
    print(f"# extraction={mode_ts} | ranking={mode_nx}", file=sys.stderr)

    repo_map = build_repo_map(target_files=args.files, top_k=args.top)

    if args.json:
        import json
        data = [
            {
                "rank": i + 1,
                "name": s.name,
                "kind": s.kind,
                "file_path": s.file_path,
                "line": s.line,
                "signature": s.signature,
            }
            for i, s in enumerate(repo_map.ranked_symbols)
        ]
        print(json.dumps(data, indent=2))
    else:
        print(format_repo_map(repo_map, top_k=args.top))


if __name__ == "__main__":
    main()
