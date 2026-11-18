"""No code reads the review-seat flags and panel constants that only an env var could change.

config-een-oppervlak PR-2 (ADR-087): these 18 names are not registry keys and not
allowlisted internal names, so they must not exist as settings. The scan walks the
Python under ``scripts/``, ``vnx_cli/`` and ``dashboard/`` and every shell file under
``scripts/`` and ``bin/``, and counts each read form. The tests below also pin the
fixed value that now holds for each deleted variable.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts" / "lib"))
sys.path.insert(0, str(REPO / "scripts"))

DELETED = frozenset({
    "VNX_CLAUDE_GITHUB_REVIEW_COMMENT",
    "VNX_CLAUDE_GITHUB_REVIEW_ENABLED",
    "VNX_CLAUDE_GITHUB_REVIEW_TRIGGER",
    "VNX_CODEX_HEADLESS_ENABLED",
    "VNX_CODEX_HEADLESS_MODEL",
    "VNX_GEMINI_REVIEW_ENABLED",
    "VNX_CODEX_FLAT_COST_USD",
    "VNX_PANEL_RETRY",
    "VNX_PLAN_GATE_MAX_DOC_CHARS",
    "VNX_PLAN_GATE_REEXTRACT_TIMEOUT",
    "VNX_PLAN_GATE_SEAT_TIMEOUT",
    "VNX_PANEL_DISTILLATE_BUDGET",
    "VNX_PANEL_REPORT_BACKSTOP",
    "VNX_PANEL_SEAT_DISTILLATE_BUDGET",
    "VNX_DECISION_BACKEND",
    "VNX_DECISION_JUDGE_BACKEND",
    "VNX_DECISION_TIMEOUT",
    "VNX_SEAT_LEDGER",
})

# Plan V2.7: the codex-lane files belong to PR-2s, which deletes their reads.
PR_2S_OWNED: Dict[str, frozenset] = {
    "scripts/lib/adapters/codex_adapter.py": frozenset({"VNX_CODEX_HEADLESS_MODEL"}),
    "scripts/lib/classifier_providers/codex_provider.py": frozenset({"VNX_CODEX_FLAT_COST_USD"}),
}

PY_ROOTS = ("scripts", "vnx_cli", "dashboard")
SH_ROOTS = ("scripts", "bin")
_SH_REF = re.compile(r"\$\{?(" + "|".join(sorted(DELETED)) + r")\b")


def _literal(node: ast.AST) -> str:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else ""


class _Reads(ast.NodeVisitor):
    def __init__(self) -> None:
        self.environ_names = {"environ"}
        self.getenv_names = {"getenv"}
        self.hits: List[Tuple[int, str]] = []

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "os":
            for alias in node.names:
                if alias.name == "environ":
                    self.environ_names.add(alias.asname or "environ")
                elif alias.name == "getenv":
                    self.getenv_names.add(alias.asname or "getenv")
        self.generic_visit(node)

    def _is_environ(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Attribute) and node.attr == "environ":
            return isinstance(node.value, ast.Name) and node.value.id == "os"
        return isinstance(node, ast.Name) and node.id in self.environ_names

    def _hit(self, node: ast.AST, name: str) -> None:
        if name in DELETED:
            self.hits.append((node.lineno, name))

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if node.args:
            first = _literal(node.args[0])
            if isinstance(func, ast.Attribute) and self._is_environ(func.value) and func.attr in {
                "get", "setdefault", "pop",
            }:
                self._hit(node, first)
            elif isinstance(func, ast.Attribute) and func.attr == "getenv" and isinstance(
                func.value, ast.Name
            ) and func.value.id == "os":
                self._hit(node, first)
            elif isinstance(func, ast.Name) and func.id in self.getenv_names:
                self._hit(node, first)
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.ctx, ast.Load) and self._is_environ(node.value):
            self._hit(node, _literal(node.slice))
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        for op, right in zip(node.ops, node.comparators):
            if isinstance(op, (ast.In, ast.NotIn)) and self._is_environ(right):
                self._hit(node, _literal(node.left))
        self.generic_visit(node)


def _py_reads() -> List[Tuple[str, int, str]]:
    found: List[Tuple[str, int, str]] = []
    for root in PY_ROOTS:
        for path in sorted((REPO / root).rglob("*.py")):
            rel = path.relative_to(REPO).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            visitor = _Reads()
            visitor.visit(tree)
            owned = PR_2S_OWNED.get(rel, frozenset())
            found += [(rel, line, name) for line, name in visitor.hits if name not in owned]
    return found


def _sh_reads() -> List[Tuple[str, int, str]]:
    found: List[Tuple[str, int, str]] = []
    for root in SH_ROOTS:
        base = REPO / root
        if not base.exists():
            continue
        for path in sorted(p for p in base.rglob("*") if p.is_file()):
            if path.suffix != ".sh" and path.parent != REPO / "bin":
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                for match in _SH_REF.finditer(line):
                    found.append((path.relative_to(REPO).as_posix(), number, match.group(1)))
    return found


def test_no_python_read_of_a_deleted_variable():
    assert _py_reads() == []


def test_no_bash_expansion_of_a_deleted_variable():
    assert _sh_reads() == []


def test_scanner_sees_every_read_form(tmp_path):
    """The scanner itself: each read form of a deleted name is counted."""
    name = "VNX_PANEL_RETRY"
    src = (
        "import os\n"
        "from os import environ as env, getenv as ge\n"
        f"a = os.environ.get('{name}')\n"
        f"b = os.environ['{name}']\n"
        f"c = os.getenv('{name}')\n"
        f"d = os.environ.setdefault('{name}', '1')\n"
        f"e = os.environ.pop('{name}', None)\n"
        f"f = '{name}' in os.environ\n"
        f"g = env.get('{name}')\n"
        f"h = ge('{name}')\n"
        f"i = env['{name}']\n"
        "j = os.environ.get('VNX_KEPT')\n"
    )
    visitor = _Reads()
    visitor.visit(ast.parse(src))
    assert [line for line, _ in visitor.hits] == [3, 4, 5, 6, 7, 8, 9, 10, 11]


def test_scanner_matches_a_bash_expansion():
    assert _SH_REF.search('echo "${VNX_SEAT_LEDGER:-x}"')
    assert _SH_REF.search("echo $VNX_SEAT_LEDGER")
    assert not _SH_REF.search("echo $VNX_SEAT_LEDGER_OTHER")


# --- the fixed value that now holds -----------------------------------------


def test_review_seat_availability_follows_the_binary_only(monkeypatch):
    import gate_request_handler as grh

    class _Handler(grh.GateRequestHandlerMixin):
        pass

    handler = _Handler()
    for name in ("VNX_GEMINI_REVIEW_ENABLED", "VNX_CODEX_HEADLESS_ENABLED"):
        monkeypatch.setenv(name, "0")  # nothing reads it any more
    monkeypatch.setattr(grh.shutil, "which", lambda tool: "/bin/x")
    assert handler._gemini_available() is True
    assert handler._codex_headless_available() is True
    monkeypatch.setattr(grh.shutil, "which", lambda tool: None)
    assert handler._gemini_available() is False
    assert handler._codex_headless_available() is False


def test_claude_github_review_stays_off_whatever_the_environment_says(monkeypatch):
    import gate_request_handler as grh

    class _Handler(grh.GateRequestHandlerMixin):
        pass

    monkeypatch.setenv("VNX_CLAUDE_GITHUB_REVIEW_ENABLED", "1")
    monkeypatch.setenv("VNX_CLAUDE_GITHUB_REVIEW_TRIGGER", "1")
    monkeypatch.setattr(grh.shutil, "which", lambda tool: "/bin/x")
    assert grh.CLAUDE_GITHUB_REVIEW_ENABLED is False
    assert grh.CLAUDE_GITHUB_REVIEW_TRIGGER is False
    assert grh.CLAUDE_GITHUB_REVIEW_COMMENT == "@claude review"
    assert _Handler()._claude_github_configured() is False


def test_panel_constants_ignore_the_environment(monkeypatch):
    import plan_gate_panel as pgp

    for name in (
        "VNX_PANEL_RETRY", "VNX_PLAN_GATE_MAX_DOC_CHARS",
        "VNX_PLAN_GATE_REEXTRACT_TIMEOUT", "VNX_PLAN_GATE_SEAT_TIMEOUT",
    ):
        monkeypatch.setenv(name, "7")
    assert pgp._panel_retry_count() == 1
    assert pgp._max_doc_chars() == 400_000
    assert pgp._reextraction_timeout() == 120
    assert pgp._seat_timeout() == 900


def test_deliberation_budgets_are_fixed_constants():
    import deliberation_panel as dp

    assert dp._REPORT_BACKSTOP == 40000
    assert dp._SEAT_DISTILLATE_BUDGET == 12000
    assert dp._DISTILLATE_BUDGET == 12000


def test_decision_router_ignores_the_deleted_variables(monkeypatch, tmp_path):
    import llm_decision_router as ldr

    monkeypatch.setenv("VNX_DECISION_BACKEND", "ollama")
    monkeypatch.setenv("VNX_DECISION_TIMEOUT", "5")
    monkeypatch.setenv("VNX_DECISION_JUDGE_BACKEND", "claude-cli")
    router = ldr.DecisionRouter(data_dir=tmp_path)
    assert router.backend == ldr.Backend.DRY_RUN
    assert router.timeout == 30
    assert router._shadow_judge() is ldr._rule_based_decision


def test_seat_ledger_path_ignores_the_environment(monkeypatch, tmp_path):
    from analysis import plan_gate_panel_effectiveness as eff

    stray = tmp_path / "stray-ledger.ndjson"
    stray.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("VNX_SEAT_LEDGER", str(stray))
    assert eff.resolve_seat_ledger_path(None) != stray

