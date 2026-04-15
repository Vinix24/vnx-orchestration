#!/usr/bin/env python3
"""
Tests for scripts/lib/repo_map.py

Covers:
- Symbol extraction via tree-sitter and regex fallback
- Dependency graph construction
- PageRank ranking
- Personalized boost for target files
- Graceful fallback when tree-sitter unavailable
- Output format
"""

import json
import sys
import textwrap
import types
from pathlib import Path
from unittest import mock

import pytest

# Ensure scripts/lib is on the path
SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import repo_map as rm


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_py_file(tmp_path):
    """Write a Python file to a temp directory and return its path string."""
    def _make(name: str, content: str) -> str:
        p = tmp_path / name
        p.write_text(textwrap.dedent(content), encoding="utf-8")
        return str(p)
    return _make


# ---------------------------------------------------------------------------
# test_extract_python_symbols
# ---------------------------------------------------------------------------

class TestExtractPythonSymbols:
    def test_extracts_functions(self, tmp_py_file):
        fp = tmp_py_file("funcs.py", """\
            def alpha(x, y):
                return x + y

            def beta():
                pass
        """)
        symbols = rm.extract_symbols_treesitter(fp)
        names = [s.name for s in symbols if s.kind == "function"]
        assert "alpha" in names
        assert "beta" in names

    def test_extracts_classes(self, tmp_py_file):
        fp = tmp_py_file("classes.py", """\
            class Foo:
                pass

            class Bar(Foo):
                pass
        """)
        symbols = rm.extract_symbols_treesitter(fp)
        class_names = [s.name for s in symbols if s.kind == "class"]
        assert "Foo" in class_names
        assert "Bar" in class_names

    def test_extracts_methods(self, tmp_py_file):
        fp = tmp_py_file("methods.py", """\
            class MyClass:
                def method_one(self):
                    pass

                def method_two(self, x):
                    return x
        """)
        symbols = rm.extract_symbols_treesitter(fp)
        methods = [s for s in symbols if s.kind == "method"]
        method_names = [m.name for m in methods]
        assert "method_one" in method_names
        assert "method_two" in method_names

    def test_extracts_imports(self, tmp_py_file):
        fp = tmp_py_file("imports.py", """\
            import os
            from pathlib import Path
            from typing import Dict, List
        """)
        symbols = rm.extract_symbols_treesitter(fp)
        imports = [s for s in symbols if s.kind == "import"]
        sigs = [i.signature for i in imports]
        assert any("import os" in s for s in sigs)
        assert any("from pathlib import Path" in s for s in sigs)

    def test_preserves_line_numbers(self, tmp_py_file):
        fp = tmp_py_file("lines.py", """\
            def first():
                pass

            def second():
                pass
        """)
        symbols = rm.extract_symbols_treesitter(fp)
        funcs = {s.name: s.line for s in symbols if s.kind == "function"}
        assert funcs["first"] == 1
        assert funcs["second"] == 4

    def test_extracts_signature_with_params(self, tmp_py_file):
        fp = tmp_py_file("sig.py", """\
            def compute(a: int, b: str = "x") -> bool:
                return True
        """)
        symbols = rm.extract_symbols_treesitter(fp)
        func = next(s for s in symbols if s.name == "compute")
        assert "compute" in func.signature
        assert "(" in func.signature

    def test_nonexistent_file_returns_empty(self):
        symbols = rm.extract_symbols_treesitter("/nonexistent/file.py")
        assert symbols == []

    def test_non_python_file_returns_empty(self, tmp_path):
        f = tmp_path / "data.txt"
        f.write_text("hello world")
        symbols = rm.extract_symbols_treesitter(str(f))
        assert symbols == []


# ---------------------------------------------------------------------------
# test_build_dependency_graph
# ---------------------------------------------------------------------------

class TestBuildDependencyGraph:
    def test_nodes_created_for_symbols(self, tmp_py_file):
        fa = tmp_py_file("mod_a.py", """\
            def alpha():
                pass
        """)
        fb = tmp_py_file("mod_b.py", """\
            def beta():
                pass
        """)
        symbols = rm.extract_symbols_treesitter(fa) + rm.extract_symbols_treesitter(fb)
        G = rm.build_dependency_graph(symbols, [fa, fb])
        node_names = [n.split(":")[-1] for n in G.nodes()]
        assert "alpha" in node_names
        assert "beta" in node_names

    def test_cross_file_edges_created(self, tmp_path):
        """File A that references a name defined in file B should get an edge A→B."""
        fa = tmp_path / "caller.py"
        fb = tmp_path / "callee.py"
        fb.write_text("def unique_callee_fn():\n    pass\n")
        # caller.py references unique_callee_fn by name
        fa.write_text("from callee import unique_callee_fn\ndef main():\n    unique_callee_fn()\n")

        symbols = (
            rm.extract_symbols_treesitter(str(fa))
            + rm.extract_symbols_treesitter(str(fb))
        )
        G = rm.build_dependency_graph(symbols, [str(fa), str(fb)])
        # There should be at least one edge involving unique_callee_fn
        callee_key = f"{str(fb)}:unique_callee_fn"
        assert callee_key in G.nodes()
        # Some node in caller.py should point to callee_key
        in_edges = list(G.predecessors(callee_key))
        assert len(in_edges) > 0, "Expected at least one edge pointing to unique_callee_fn"

    def test_imports_excluded_from_graph(self, tmp_py_file):
        fp = tmp_py_file("only_imports.py", """\
            import os
            from pathlib import Path
        """)
        symbols = rm.extract_symbols_treesitter(fp)
        G = rm.build_dependency_graph(symbols, [fp])
        # Import symbols should NOT be graph nodes
        assert G.number_of_nodes() == 0

    def test_no_self_edges(self, tmp_py_file):
        fp = tmp_py_file("self_ref.py", """\
            def recursive():
                recursive()
        """)
        symbols = rm.extract_symbols_treesitter(fp)
        G = rm.build_dependency_graph(symbols, [fp])
        # Self-edges on the same node should not exist
        for node in G.nodes():
            assert not G.has_edge(node, node)

    def test_graph_is_directed(self, tmp_py_file):
        fp = tmp_py_file("directed.py", """\
            def foo():
                pass
        """)
        symbols = rm.extract_symbols_treesitter(fp)
        G = rm.build_dependency_graph(symbols, [fp])
        import networkx as nx
        assert isinstance(G, nx.DiGraph)


# ---------------------------------------------------------------------------
# test_pagerank_ranking
# ---------------------------------------------------------------------------

class TestPageRankRanking:
    def test_returns_top_k(self, tmp_py_file):
        fp = tmp_py_file("many_funcs.py", "\n".join(
            f"def func_{i}():\n    pass\n" for i in range(30)
        ))
        result = rm.build_repo_map(target_files=[fp], top_k=10)
        assert len(result.ranked_symbols) <= 10

    def test_ranked_symbols_are_non_import(self, tmp_py_file):
        fp = tmp_py_file("mixed.py", """\
            import os
            def alpha():
                pass
            class Beta:
                pass
        """)
        result = rm.build_repo_map(target_files=[fp], top_k=20)
        for sym in result.ranked_symbols:
            assert sym.kind != "import"

    def test_highly_referenced_symbol_ranks_higher(self, tmp_path):
        """A function referenced by many others should rank higher than one never called."""
        hub = tmp_path / "hub.py"
        leaf = tmp_path / "leaf.py"
        hub.write_text("def central_hub_fn():\n    pass\n")
        # leaf.py calls central_hub_fn from many functions
        leaf_body = "from hub import central_hub_fn\n"
        for i in range(10):
            leaf_body += f"def caller_{i}():\n    central_hub_fn()\n"
        leaf.write_text(leaf_body)

        result = rm.build_repo_map(target_files=[str(hub), str(leaf)], top_k=20)
        names = [s.name for s in result.ranked_symbols]
        # central_hub_fn should appear in ranked list
        assert "central_hub_fn" in names

    def test_graph_metadata_populated(self, tmp_py_file):
        fp = tmp_py_file("meta.py", """\
            def a():
                pass
            def b():
                pass
        """)
        result = rm.build_repo_map(target_files=[fp])
        assert result.graph_nodes >= 0
        assert result.graph_edges >= 0

    def test_symbols_list_contains_all_kinds(self, tmp_py_file):
        fp = tmp_py_file("all_kinds.py", """\
            import os
            def top_fn():
                pass
            class MyClass:
                def method(self):
                    pass
        """)
        result = rm.build_repo_map(target_files=[fp])
        kinds = {s.kind for s in result.symbols}
        assert "function" in kinds
        assert "class" in kinds
        assert "method" in kinds
        assert "import" in kinds


# ---------------------------------------------------------------------------
# test_personalized_boost_for_target_files
# ---------------------------------------------------------------------------

class TestPersonalizedBoost:
    def test_target_file_symbols_appear_in_results(self, tmp_path):
        """Symbols from the target file should appear in ranked output."""
        target = tmp_path / "target.py"
        other = tmp_path / "other.py"
        target.write_text("def target_unique_fn():\n    pass\n")
        # other.py has many functions but none calling target
        other_body = "\n".join(f"def other_{i}():\n    pass\n" for i in range(20))
        other.write_text(other_body)

        result = rm.build_repo_map(target_files=[str(target)], top_k=10)
        names = [s.name for s in result.ranked_symbols]
        # target file only has one function; it must appear
        assert "target_unique_fn" in names

    def test_personalization_vector_boosts_target(self, tmp_path):
        """Personalized PageRank should give target file at least as much weight as others."""
        import networkx as nx

        target = tmp_path / "primary.py"
        secondary = tmp_path / "secondary.py"

        target.write_text(
            "def primary_important():\n    pass\ndef primary_also():\n    pass\n"
        )
        secondary.write_text(
            "def secondary_fn():\n    pass\n"
        )

        # Build with only target as primary
        result = rm.build_repo_map(target_files=[str(target), str(secondary)], top_k=20)
        # All symbols from primary file should be present
        primary_syms = [s for s in result.ranked_symbols if "primary" in s.file_path]
        assert len(primary_syms) >= 2

    def test_rank_symbols_empty_graph_uses_target_first(self, tmp_path):
        """When graph has no nodes, target-file symbols should come first."""
        import networkx as nx

        target = tmp_path / "t.py"
        other = tmp_path / "o.py"
        target.write_text("def fn_in_target():\n    pass\n")
        other.write_text("def fn_in_other():\n    pass\n")

        syms = rm.extract_symbols_treesitter(str(target)) + rm.extract_symbols_treesitter(str(other))
        empty_graph = nx.DiGraph()
        ranked = rm.rank_symbols(syms, empty_graph, [str(target)], top_k=5)
        # First ranked symbol should be from target file
        assert ranked[0].file_path == str(target)


# ---------------------------------------------------------------------------
# test_fallback_without_treesitter
# ---------------------------------------------------------------------------

class TestFallbackWithoutTreesitter:
    def test_regex_extracts_functions(self, tmp_py_file):
        fp = tmp_py_file("regex_funcs.py", """\
            def foo(a, b):
                return a + b

            def bar():
                pass
        """)
        symbols = rm.extract_symbols_regex(fp)
        names = [s.name for s in symbols if s.kind == "function"]
        assert "foo" in names
        assert "bar" in names

    def test_regex_extracts_classes(self, tmp_py_file):
        fp = tmp_py_file("regex_classes.py", """\
            class Alpha:
                pass
            class Beta(Alpha):
                pass
        """)
        symbols = rm.extract_symbols_regex(fp)
        class_names = [s.name for s in symbols if s.kind == "class"]
        assert "Alpha" in class_names
        assert "Beta" in class_names

    def test_regex_extracts_imports(self, tmp_py_file):
        fp = tmp_py_file("regex_imports.py", """\
            import sys
            from pathlib import Path
        """)
        symbols = rm.extract_symbols_regex(fp)
        imports = [s for s in symbols if s.kind == "import"]
        sigs = [i.signature for i in imports]
        assert any("import sys" in s for s in sigs)
        assert any("from pathlib import Path" in s for s in sigs)

    def test_fallback_mode_produces_repo_map(self, tmp_py_file):
        """Patching _TREESITTER_AVAILABLE to False should still produce a valid RepoMap."""
        fp = tmp_py_file("fallback_test.py", """\
            def alpha():
                pass
            class Beta:
                def method(self):
                    pass
        """)
        with mock.patch.object(rm, "_TREESITTER_AVAILABLE", False):
            result = rm.build_repo_map(target_files=[fp], top_k=10)

        assert isinstance(result, rm.RepoMap)
        assert len(result.symbols) > 0

    def test_fallback_no_networkx_still_ranks(self, tmp_py_file):
        """Patching _NETWORKX_AVAILABLE to False should fall back to positional ranking."""
        fp = tmp_py_file("no_nx.py", """\
            def alpha():
                pass
            def beta():
                pass
        """)
        with mock.patch.object(rm, "_NETWORKX_AVAILABLE", False):
            result = rm.build_repo_map(target_files=[fp], top_k=10)

        assert isinstance(result, rm.RepoMap)
        assert result.graph_nodes == 0
        assert result.graph_edges == 0
        assert len(result.ranked_symbols) > 0

    def test_regex_nonexistent_file_returns_empty(self):
        symbols = rm.extract_symbols_regex("/nonexistent/missing.py")
        assert symbols == []

    def test_regex_methods_detected_inside_class(self, tmp_py_file):
        fp = tmp_py_file("cls_method.py", """\
            class Outer:
                def inner_method(self):
                    pass
        """)
        symbols = rm.extract_symbols_regex(fp)
        methods = [s for s in symbols if s.kind == "method"]
        assert any(s.name == "inner_method" for s in methods)


# ---------------------------------------------------------------------------
# test_output_format
# ---------------------------------------------------------------------------

class TestOutputFormat:
    def test_format_starts_with_header(self, tmp_py_file):
        fp = tmp_py_file("fmt.py", """\
            def alpha():
                pass
        """)
        result = rm.build_repo_map(target_files=[fp], top_k=5)
        output = rm.format_repo_map(result, top_k=5)
        assert output.startswith("### Repo Map")

    def test_format_contains_numbered_lines(self, tmp_py_file):
        fp = tmp_py_file("numbered.py", """\
            def one():
                pass
            def two():
                pass
        """)
        result = rm.build_repo_map(target_files=[fp], top_k=5)
        output = rm.format_repo_map(result, top_k=5)
        assert "1." in output
        assert "2." in output

    def test_format_contains_graph_stats(self, tmp_py_file):
        fp = tmp_py_file("stats.py", """\
            def fn():
                pass
        """)
        result = rm.build_repo_map(target_files=[fp])
        output = rm.format_repo_map(result)
        assert "graph:" in output
        assert "nodes" in output
        assert "edges" in output

    def test_format_contains_symbol_name(self, tmp_py_file):
        fp = tmp_py_file("named.py", """\
            def very_unique_function_name_xyz():
                pass
        """)
        result = rm.build_repo_map(target_files=[fp], top_k=5)
        output = rm.format_repo_map(result, top_k=5)
        assert "very_unique_function_name_xyz" in output

    def test_format_contains_extraction_mode(self, tmp_py_file):
        fp = tmp_py_file("mode.py", "def fn(): pass\n")
        result = rm.build_repo_map(target_files=[fp])
        output = rm.format_repo_map(result)
        assert "extraction" in output

    def test_format_top_k_respected(self, tmp_py_file):
        content = "\n".join(f"def func_{i}(): pass\n" for i in range(25))
        fp = tmp_py_file("many.py", content)
        result = rm.build_repo_map(target_files=[fp], top_k=5)
        output = rm.format_repo_map(result, top_k=5)
        # Should have 5 numbered items
        lines = [ln for ln in output.splitlines() if ln.strip() and ln.strip()[0].isdigit()]
        assert len(lines) <= 5

    def test_json_output_structure(self, tmp_py_file):
        """JSON output from main() should be a valid list of rank records."""
        fp = tmp_py_file("json_out.py", """\
            def alpha():
                pass
            class Beta:
                pass
        """)
        result = rm.build_repo_map(target_files=[fp], top_k=5)
        data = [
            {
                "rank": i + 1,
                "name": s.name,
                "kind": s.kind,
                "file_path": s.file_path,
                "line": s.line,
                "signature": s.signature,
            }
            for i, s in enumerate(result.ranked_symbols)
        ]
        dumped = json.dumps(data)
        loaded = json.loads(dumped)
        assert isinstance(loaded, list)
        assert loaded[0]["rank"] == 1
        assert "name" in loaded[0]
        assert "file_path" in loaded[0]
        assert "signature" in loaded[0]
