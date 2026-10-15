"""Coverage guard for the provider switch (provider-schakelaar A).

An AST detector finds every LAUNCH FUNCTION — a Python function whose own body starts a
non-claude provider process or request — over ``scripts/`` (minus ``scripts/spikes/``) and
``vnx_cli/``, plus a pattern scan of ``bin/vnx`` and ``scripts/**/*.sh``. The census in
``scripts/lib/provider_switch.py`` must match it:

- every flagged function calls ``provider_switch.require`` in its own body before its first
  launch call (``ENFORCED``), is pending (``PENDING_B``), or is exempt with a
  reason;
- every entry of the measured floor exists, is flagged or listed as undetectable with a
  reason, and is enforced (calls require, or is pending);
- every exempt entry still matches something;
- no runtime module imports from ``scripts/spikes/``.

Without this guard the next new consumer would bypass the switch again.
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import provider_switch  # noqa: E402

# ---------------------------------------------------------------------------
# Detector rules
# ---------------------------------------------------------------------------

_PROCESS_CALLS = {
    "subprocess.Popen", "subprocess.run", "subprocess.call", "subprocess.check_call",
    "subprocess.check_output", "asyncio.create_subprocess_exec",
    "os.execv", "os.execve", "os.execvp", "os.execvpe",
    "os.execl", "os.execle", "os.execlp", "os.execlpe",
}
_HTTP_MODULES = ("urllib.request", "requests", "httpx", "http.client")
_LITELLM_CALLS = {"litellm.completion", "litellm.acompletion"}
PROVIDER_BINARIES = frozenset({"kimi", "gemini", "ollama", "litellm"})
MODEL_HOSTS = (
    "api.deepseek.com", "openrouter.ai", "aiplatform.googleapis.com",
    "generativelanguage.googleapis.com", "api.moonshot.ai", "api.moonshot.cn", "api.z.ai",
    "open.bigmodel.cn",
)
_LOCAL_MODEL_PORT_RE = re.compile(r"(localhost|127\.0\.0\.1):(11434|4141)\b")
_PROVIDER_CONTEXT_NAMES = frozenset({"GATE_PROVIDERS", "_build_gate_cmd", "ANTHROPIC_BASE_URL"})
_BASE_URL = "ANTHROPIC_BASE_URL"

RULE_PROCESS_LITERAL = "process-literal-provider-binary"
RULE_PROCESS_CONTEXT = "process-nonliteral-in-provider-context"
RULE_CLAUDE_BASE_URL = "claude-launch-with-anthropic-base-url"
RULE_HTTP_MODEL_HOST = "http-call-with-model-host"
RULE_LITELLM = "litellm-call"

SCAN_PY_ROOTS = ("scripts", "vnx_cli")
EXCLUDED_DIRS = ("scripts/spikes",)


@dataclass
class LaunchCall:
    rule: str
    line: int
    col: int
    target: str


@dataclass
class FunctionScan:
    key: str                     # "<relpath>:<qualname>"
    launches: List[LaunchCall] = field(default_factory=list)
    require_positions: List[Tuple[int, int]] = field(default_factory=list)

    @property
    def flagged(self) -> bool:
        return bool(self.launches)

    @property
    def first_launch(self) -> Tuple[int, int]:
        first = min(self.launches, key=lambda c: (c.line, c.col))
        return first.line, first.col

    @property
    def requires_before_launch(self) -> bool:
        return bool(self.require_positions) and min(self.require_positions) < self.first_launch


def _docstring_nodes(tree: ast.AST) -> Set[int]:
    ids: Set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def _alias_map(tree: ast.AST) -> Dict[str, str]:
    aliases: Dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
                else:
                    top = alias.name.split(".", 1)[0]
                    aliases.setdefault(top, top)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


def _dotted(node: ast.AST, aliases: Dict[str, str]) -> Optional[str]:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value, aliases)
        return f"{base}.{node.attr}" if base else None
    return None


def _is_process_call(dotted: str) -> bool:
    if dotted in _PROCESS_CALLS:
        return True
    parts = dotted.split(".")
    return len(parts) > 2 and ".".join(parts[-2:]) in _PROCESS_CALLS and parts[-2] == "subprocess"


def _is_http_call(dotted: str) -> bool:
    if any(dotted == m or dotted.startswith(m + ".") for m in _HTTP_MODULES):
        return True
    return dotted.split(".")[-1] == "urlopen"


def _string_constants(node: ast.AST, docstrings: Set[int]) -> Iterable[str]:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and id(sub) not in docstrings:
            yield sub.value


def _holds_model_host(node: ast.AST, docstrings: Set[int]) -> bool:
    for text in _string_constants(node, docstrings):
        if any(host in text for host in MODEL_HOSTS) or _LOCAL_MODEL_PORT_RE.search(text):
            return True
    return False


def _has_provider_context(node: ast.AST, docstrings: Set[int]) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id in _PROVIDER_CONTEXT_NAMES:
            return True
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name in _PROVIDER_CONTEXT_NAMES:
            return True
        if isinstance(sub, ast.Attribute) and sub.attr in _PROVIDER_CONTEXT_NAMES:
            return True
        if isinstance(sub, ast.Constant) and sub.value in _PROVIDER_CONTEXT_NAMES and id(sub) not in docstrings:
            return True
        if isinstance(sub, ast.ImportFrom) and sub.module and "provider_spawns" in sub.module:
            return True
        if isinstance(sub, ast.Import) and any("provider_spawns" in a.name for a in sub.names):
            return True
    return False


def _sets_base_url(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Dict) and any(
            isinstance(k, ast.Constant) and k.value == _BASE_URL for k in sub.keys
        ):
            return True
        if isinstance(sub, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = sub.targets if isinstance(sub, ast.Assign) else [sub.target]
            for target in targets:
                if isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant) \
                        and target.slice.value == _BASE_URL:
                    return True
        if isinstance(sub, ast.Call):
            if any(kw.arg == _BASE_URL for kw in sub.keywords):
                return True
            if isinstance(sub.func, ast.Attribute) and sub.func.attr == "setdefault" and sub.args \
                    and isinstance(sub.args[0], ast.Constant) and sub.args[0].value == _BASE_URL:
                return True
    return False


class _Scope:
    """Name -> last assigned value, for one function body or a module."""

    def __init__(self, body: List[ast.stmt]) -> None:
        self.assigns: Dict[str, List[Tuple[int, ast.AST]]] = {}
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for sub in _walk_own(stmt):
                if isinstance(sub, ast.Assign):
                    for target in sub.targets:
                        if isinstance(target, ast.Name):
                            self.assigns.setdefault(target.id, []).append((sub.lineno, sub.value))
                elif isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name) and sub.value is not None:
                    self.assigns.setdefault(sub.target.id, []).append((sub.lineno, sub.value))

    def value_before(self, name: str, line: int) -> Optional[ast.AST]:
        candidates = [v for (ln, v) in self.assigns.get(name, []) if ln <= line]
        return candidates[-1] if candidates else None


def _walk_own(node: ast.AST) -> Iterable[ast.AST]:
    """Walk ``node`` without descending into nested functions or classes."""
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        yield from _walk_own(child)


def _literal_head(node: Optional[ast.AST], line: int, local: _Scope, module: _Scope,
                  aliases: Dict[str, str], depth: int = 0) -> Optional[str]:
    """The literal program name an argv starts with, or None when it is not a literal."""
    if node is None or depth > 4:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        words = node.value.strip().split()
        return words[0] if words else None
    if isinstance(node, (ast.List, ast.Tuple)):
        if not node.elts or isinstance(node.elts[0], ast.Starred):
            return None
        return _literal_head(node.elts[0], line, local, module, aliases, depth + 1)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _literal_head(node.left, line, local, module, aliases, depth + 1)
    if isinstance(node, ast.Call):
        dotted = _dotted(node.func, aliases) or ""
        if dotted.split(".")[-1] == "which" and node.args:
            return _literal_head(node.args[0], line, local, module, aliases, depth + 1)
        return None
    if isinstance(node, ast.Name):
        value = local.value_before(node.id, line) or module.value_before(node.id, 10 ** 9)
        return _literal_head(value, line, local, module, aliases, depth + 1)
    return None


def _program_name(head: Optional[str]) -> Optional[str]:
    return head.rsplit("/", 1)[-1] if head else None


def _call_argv(call: ast.Call, dotted: str) -> Optional[ast.AST]:
    if call.args:
        return call.args[0]
    for kw in call.keywords:
        if kw.arg in ("args", "program", "path", "file"):
            return kw.value
    return None


def _scan_unit(key: str, nodes: List[ast.AST], local: _Scope, module: _Scope,
               aliases: Dict[str, str], module_facts: Dict[str, bool], docstrings: Set[int]) -> FunctionScan:
    scan = FunctionScan(key=key)
    holder = ast.Module(body=list(nodes), type_ignores=[])
    unit_host = _holds_model_host(holder, docstrings)
    unit_context = _has_provider_context(holder, docstrings)
    unit_sets_base = _sets_base_url(holder)
    calls = [n for stmt in nodes for n in _walk_own(stmt) if isinstance(n, ast.Call)]
    for call in sorted(calls, key=lambda c: (c.lineno, c.col_offset)):
        dotted = _dotted(call.func, aliases)
        if not dotted:
            continue
        pos = (call.lineno, call.col_offset)
        if dotted.endswith("provider_switch.require"):
            scan.require_positions.append(pos)
            continue
        if _is_process_call(dotted):
            head = _program_name(_literal_head(_call_argv(call, dotted), call.lineno, local, module, aliases))
            if head in PROVIDER_BINARIES:
                scan.launches.append(LaunchCall(RULE_PROCESS_LITERAL, *pos, dotted))
            elif head == "claude":
                has_env = any(kw.arg == "env" for kw in call.keywords)
                if has_env and (unit_sets_base or module_facts["sets_base_url"]):
                    scan.launches.append(LaunchCall(RULE_CLAUDE_BASE_URL, *pos, dotted))
            elif head is None and (unit_context or module_facts["provider_context"]):
                scan.launches.append(LaunchCall(RULE_PROCESS_CONTEXT, *pos, dotted))
        elif _is_http_call(dotted):
            if unit_host or module_facts["model_host"]:
                scan.launches.append(LaunchCall(RULE_HTTP_MODEL_HOST, *pos, dotted))
        elif dotted in _LITELLM_CALLS:
            scan.launches.append(LaunchCall(RULE_LITELLM, *pos, dotted))
    return scan


def scan_python_source(source: str, relpath: str) -> Dict[str, FunctionScan]:
    """Scan one module. Returns every function unit by key, flagged or not."""
    tree = ast.parse(source)
    aliases = _alias_map(tree)
    docstrings = _docstring_nodes(tree)
    module_scope = _Scope(tree.body)
    module_facts = {
        "model_host": _holds_model_host(tree, docstrings),
        "provider_context": _has_provider_context(tree, docstrings),
        "sets_base_url": _sets_base_url(tree),
    }
    results: Dict[str, FunctionScan] = {}

    def visit(body: List[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = f"{prefix}{node.name}"
                key = f"{relpath}:{qual}"
                results[key] = _scan_unit(key, node.body, _Scope(node.body), module_scope,
                                          aliases, module_facts, docstrings)
                visit(node.body, f"{qual}.")
            elif isinstance(node, ast.ClassDef):
                visit(node.body, f"{prefix}{node.name}.")
            else:
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        visit([child], prefix)

    visit(tree.body, "")
    top_level = [n for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    results[f"{relpath}:<module>"] = _scan_unit(f"{relpath}:<module>", top_level, module_scope,
                                                module_scope, aliases, module_facts, docstrings)
    return results


def _excluded(rel: str) -> bool:
    return any(rel == d or rel.startswith(d + "/") for d in EXCLUDED_DIRS)


def iter_python_files(root: Path) -> Iterable[Tuple[Path, str]]:
    for top in SCAN_PY_ROOTS:
        for path in sorted((root / top).rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            if not _excluded(rel) and "__pycache__" not in rel:
                yield path, rel


def scan_tree(root: Path) -> Dict[str, FunctionScan]:
    """Every function unit of every scanned module under ``root``."""
    units: Dict[str, FunctionScan] = {}
    for path, rel in iter_python_files(root):
        units.update(scan_python_source(path.read_text(encoding="utf-8"), rel))
    return units


def flagged_functions(root: Path) -> Dict[str, FunctionScan]:
    return {k: v for k, v in scan_tree(root).items() if v.flagged}


# ---------------------------------------------------------------------------
# Shell scan
# ---------------------------------------------------------------------------

_SHELL_PATTERNS = (
    re.compile(r"(^|[\s;&|(`$])kimi\s(?:[^#\n]*?\s)?(--print|-p)(?=[\s\"'=]|$)"),
    re.compile(r"(^|[\s;&|(`$])gemini\s(?:[^#\n]*?\s)?(-p|--prompt)(?=[\s\"'=]|$)"),
    re.compile(r"(^|[\s;&|(`$])ollama\s+run\b"),
    re.compile(r"(^|[\s;&|(`$])curl\s[^#\n]*(" + "|".join(re.escape(h) for h in MODEL_HOSTS)
               + r"|localhost:(11434|4141)|127\.0\.0\.1:(11434|4141))"),
)
_SHELL_CHECK_RE = re.compile(r"vnx providers check \S+")


def scan_shell_text(text: str) -> List[Tuple[int, str, bool]]:
    """Flagged lines as ``(lineno, line, preceded_by_check)``; comment lines are skipped."""
    flagged: List[Tuple[int, str, bool]] = []
    seen_check = False
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if any(p.search(line) for p in _SHELL_PATTERNS):
            flagged.append((lineno, line, seen_check))
        if _SHELL_CHECK_RE.search(line):
            seen_check = True
    return flagged


def iter_shell_files(root: Path) -> Iterable[Tuple[Path, str]]:
    candidates = [root / "bin" / "vnx", *sorted((root / "scripts").rglob("*.sh"))]
    for path in candidates:
        rel = path.relative_to(root).as_posix()
        if path.is_file() and not _excluded(rel):
            yield path, rel


# ---------------------------------------------------------------------------
# The measured floor (eeaf29d0)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FloorEntry:
    path: str
    function: Optional[str] = None
    # A shim that defines no function of its own but re-exports a launch function:
    # (import module of the defining file, function name).
    reexport: Optional[Tuple[str, str]] = None


MEASURED_FLOOR: Tuple[FloorEntry, ...] = (
    FloorEntry("scripts/lib/provider_spawns/deepseek_harness_spawn.py"),
    FloorEntry("scripts/lib/provider_spawns/glm_harness_spawn.py"),
    FloorEntry("scripts/lib/provider_spawns/kimi_spawn.py"),
    FloorEntry("scripts/lib/provider_spawns/gemini_spawn.py"),
    FloorEntry("scripts/lib/provider_spawns/litellm_spawn.py"),
    FloorEntry("scripts/lib/provider_spawns/local_gemma_spawn.py",
               reexport=("providers.local_gemma.spawn", "spawn_local_gemma")),
    FloorEntry("scripts/lib/kimi_wrapper.py", "kimi_exec"),
    FloorEntry("scripts/lib/gemini_wrapper.py"),
    FloorEntry("scripts/lib/adapters/gemini_adapter.py"),
    FloorEntry("scripts/lib/adapters/ollama_adapter.py"),
    FloorEntry("scripts/lib/llm_decision_router.py"),
    FloorEntry("scripts/lib/vertex_ai_runner.py"),
    FloorEntry("scripts/lib/providers/local_gemma/spawn.py"),
    FloorEntry("scripts/lib/providers/provider_lanes/deepseek.py",
               reexport=("provider_spawns.deepseek_harness_spawn", "spawn_deepseek_harness")),
    FloorEntry("scripts/lib/providers/provider_lanes/gemini.py", reexport=("gemini_wrapper", "gemini_exec")),
    FloorEntry("scripts/lib/providers/provider_lanes/kimi.py", reexport=("kimi_wrapper", "kimi_exec")),
    FloorEntry("scripts/lib/envelope_adapters_provider.py", "ProviderAdapter._run_kimi"),
    FloorEntry("scripts/lib/classifier_providers/deepseek_provider.py"),
    FloorEntry("scripts/lib/classifier_providers/gemini_provider.py"),
    FloorEntry("scripts/lib/classifier_providers/ollama_provider.py"),
    FloorEntry("scripts/gate_runner.py"),
    FloorEntry("scripts/conversation_analyzer/deep_analyzer.py", "DeepAnalyzer._try_ollama"),
    FloorEntry("scripts/llm_benchmark.py"),
    FloorEntry("scripts/benchmark/field-tests/runners/scorer.py"),
    FloorEntry("scripts/lib/adapters/_litellm_runner.py", "main"),
    FloorEntry("scripts/lib/adapters/_litellm_agentic_runner.py"),
    FloorEntry("scripts/lib/provider_spawns/claude_spawn.py", "spawn_claude"),
)


def _module_path(import_name: str) -> str:
    return "scripts/lib/" + import_name.replace(".", "/") + ".py"


def _exempt_reason(key: str, exempt: Dict[str, str]) -> Optional[str]:
    if key in exempt:
        return exempt[key]
    path = key.split(":", 1)[0]
    if path in exempt:
        return exempt[path]
    for entry, reason in exempt.items():
        if entry.endswith("/") and path.startswith(entry):
            return reason
    return None


def coverage_violations(units: Dict[str, FunctionScan], *, launch: Iterable[str],
                        pending: Iterable[str], exempt: Dict[str, str],
                        undetected: Dict[str, str]) -> List[str]:
    """Every way the census and the tree disagree. Empty means the guard passes."""
    launch_set, pending_set = set(launch), set(pending)
    problems: List[str] = []
    for key, unit in sorted(units.items()):
        if not unit.flagged:
            continue
        rules = sorted({c.rule for c in unit.launches})
        if key in launch_set:
            if not unit.requires_before_launch and key not in pending_set:
                where = "after its first launch call" if unit.require_positions else "nowhere"
                problems.append(f"{key} ({rules}) calls provider_switch.require {where} and is not pending")
        elif _exempt_reason(key, exempt) is None:
            problems.append(f"{key} ({rules}) is a launch function outside the census and not exempt")
    for key in sorted(launch_set):
        unit = units.get(key)
        if unit is None:
            problems.append(f"census entry {key} does not exist")
            continue
        if not unit.flagged and not (undetected.get(key) or "").strip():
            problems.append(f"census entry {key} is not detected and not listed as undetectable with a reason")
        if unit.flagged and key in undetected:
            problems.append(f"census entry {key} is detected now; remove it from UNDETECTED_LAUNCH_FUNCTIONS")
        if not unit.flagged and not unit.require_positions and key not in pending_set:
            problems.append(f"undetectable census entry {key} does not call provider_switch.require and is not pending")
        if key in exempt:
            problems.append(f"census entry {key} is also exempt")
    return problems


def floor_violations(units: Dict[str, FunctionScan], root: Path, *, launch: Iterable[str]) -> List[str]:
    launch_set = set(launch)
    problems: List[str] = []
    for entry in MEASURED_FLOOR:
        if not (root / entry.path).is_file():
            problems.append(f"floor file {entry.path} does not exist")
            continue
        if entry.function:
            key = f"{entry.path}:{entry.function}"
            if key not in units:
                problems.append(f"floor function {key} does not exist")
            elif key not in launch_set:
                problems.append(f"floor function {key} is not in the census")
        elif entry.reexport:
            module, name = entry.reexport
            tree = ast.parse((root / entry.path).read_text(encoding="utf-8"))
            imported = any(
                isinstance(n, ast.ImportFrom) and n.module == module and any(a.name == name for a in n.names)
                for n in ast.walk(tree)
            )
            if not imported:
                problems.append(f"floor shim {entry.path} no longer re-exports {module}.{name}")
            if f"{_module_path(module)}:{name}" not in launch_set:
                problems.append(f"floor shim {entry.path}: {_module_path(module)}:{name} is not in the census")
        elif not any(key.startswith(entry.path + ":") for key in launch_set):
            problems.append(f"floor file {entry.path} has no census entry")
    return problems


def exempt_violations(units: Dict[str, FunctionScan], root: Path, exempt: Dict[str, str]) -> List[str]:
    problems: List[str] = []
    for entry, reason in sorted(exempt.items()):
        if not (reason or "").strip():
            problems.append(f"exempt entry {entry} has no reason")
        if ":" in entry:
            unit = units.get(entry)
            if unit is None:
                problems.append(f"exempt function {entry} does not exist")
            elif not unit.flagged:
                problems.append(f"exempt function {entry} is no longer flagged; remove the entry")
        elif entry.endswith("/"):
            if not (root / entry).is_dir():
                problems.append(f"exempt directory {entry} does not exist")
        elif not (root / entry).is_file():
            problems.append(f"exempt file {entry} does not exist")
    return problems


# ---------------------------------------------------------------------------
# The guard on the real tree
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def units() -> Dict[str, FunctionScan]:
    return scan_tree(REPO_ROOT)


def _all_pending() -> Set[str]:
    return {k for v in provider_switch.pending_launch_functions().values() for k in v}


def test_every_flagged_function_is_enforced_pending_or_exempt(units):
    assert coverage_violations(
        units, launch=provider_switch.LAUNCH_FUNCTIONS, pending=_all_pending(),
        exempt=provider_switch.EXEMPT, undetected=provider_switch.UNDETECTED_LAUNCH_FUNCTIONS,
    ) == []


def test_measured_floor_exists_and_is_in_the_census(units):
    assert floor_violations(units, REPO_ROOT, launch=provider_switch.LAUNCH_FUNCTIONS) == []


def test_every_exempt_entry_still_matches_something(units):
    assert exempt_violations(units, REPO_ROOT, provider_switch.EXEMPT) == []


def test_enforced_and_pending_tuples_partition_the_census(units):
    enforced, b = provider_switch.ENFORCED, provider_switch.PENDING_B
    assert len(set(enforced)) == len(enforced) and len(set(b)) == len(b)
    assert not (set(enforced) & set(b))
    census = set(provider_switch.LAUNCH_FUNCTIONS)
    assert set(enforced) | set(b) == census
    calling_require = {k for k in census if units[k].require_positions}
    assert census - calling_require == set(b)
    assert provider_switch.pending_launch_functions() == {"B": tuple(b)}


def test_every_enforced_entry_calls_require_before_its_first_launch(units):
    """ENFORCED is a claim about the code, so the guard reads the code: a detected entry calls
    require before its first launch call in AST order, an undetectable one calls it at all."""
    assert provider_switch.ENFORCED
    for key in provider_switch.ENFORCED:
        unit = units[key]
        if unit.flagged:
            assert unit.requires_before_launch, f"{key} does not call require before its first launch"
        else:
            assert key in provider_switch.UNDETECTED_LAUNCH_FUNCTIONS and unit.require_positions, key
    assert {
        "scripts/conversation_analyzer/deep_analyzer.py:DeepAnalyzer._try_deepseek_harness",
        "scripts/conversation_analyzer/deep_analyzer.py:DeepAnalyzer._try_ollama",
        "scripts/gate_runner.py:GateRunner._run_with_stall_detection",
    } <= set(provider_switch.ENFORCED)


def test_census_matches_the_detected_set(units):
    """The floor and the detected set are asserted together: the detector cannot shrink."""
    flagged = {k for k, u in units.items() if u.flagged}
    detected = {k for k in flagged if k in provider_switch.LAUNCH_FUNCTIONS
                or _exempt_reason(k, provider_switch.EXEMPT) is None}
    census = set(provider_switch.LAUNCH_FUNCTIONS)
    assert census - set(provider_switch.UNDETECTED_LAUNCH_FUNCTIONS) == detected
    assert set(provider_switch.UNDETECTED_LAUNCH_FUNCTIONS) <= census


def test_shell_calls_are_checked_or_exempt():
    problems = []
    for path, rel in iter_shell_files(REPO_ROOT):
        for lineno, line, checked in scan_shell_text(path.read_text(encoding="utf-8", errors="replace")):
            if not checked and _exempt_reason(rel, provider_switch.EXEMPT) is None:
                problems.append(f"{rel}:{lineno}: {line.strip()}")
    assert problems == []


def test_no_runtime_module_imports_from_spikes():
    offenders = []
    for path, rel in iter_python_files(REPO_ROOT):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: List[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(n == "spikes" or n.startswith("spikes.") or ".spikes" in n for n in names):
                offenders.append(f"{rel}:{node.lineno}")
    assert offenders == []


# ---------------------------------------------------------------------------
# Red tests: the detector on today's hard cases
# ---------------------------------------------------------------------------

def _rules(units, key):
    assert key in units, f"{key} does not exist"
    return {c.rule for c in units[key].launches}


def test_detector_flags_the_gate_runner_launch(units):
    assert RULE_PROCESS_CONTEXT in _rules(units, "scripts/gate_runner.py:GateRunner._run_with_stall_detection")


def test_detector_flags_the_ollama_adapter(units):
    assert RULE_HTTP_MODEL_HOST in _rules(units, "scripts/lib/adapters/ollama_adapter.py:OllamaAdapter._drain_http_stream")


def test_detector_flags_the_vertex_ai_runner(units):
    assert RULE_HTTP_MODEL_HOST in _rules(units, "scripts/lib/vertex_ai_runner.py:run_vertex_ai")


def test_detector_flags_litellm_completion(units):
    unit = units["scripts/lib/adapters/_litellm_runner.py:main"]
    assert any(c.rule == RULE_LITELLM and c.target == "litellm.completion" for c in unit.launches)


def test_detector_flags_the_deepseek_probe_and_exempt_matches_it(units):
    key = "scripts/lib/dispatch_cli.py:_check_reachability"
    assert RULE_HTTP_MODEL_HOST in _rules(units, key)
    assert key in provider_switch.EXEMPT
    assert key not in provider_switch.LAUNCH_FUNCTIONS


# Provider CLI names are assembled so this file's text never holds a literal provider command
# line (the worker hooks refuse such text); the scanned source is the same either way.
_K = "ki" + "mi"
_G = "gem" + "ini"
_PRINT = "--pri" + "nt"


def test_require_after_the_launch_fails_the_order_check():
    source = (
        "import subprocess\nimport provider_switch\n"
        "def late():\n"
        f"    subprocess.run(['{_K}', '{_PRINT}', 'x'])\n"
        f"    provider_switch.require('{_K}', consumer='late')\n"
        "def early():\n"
        f"    provider_switch.require('{_K}', consumer='early')\n"
        f"    subprocess.run(['{_K}', '{_PRINT}', 'x'])\n"
    )
    found = scan_python_source(source, "synthetic/order.py")
    late, early = found["synthetic/order.py:late"], found["synthetic/order.py:early"]
    assert late.flagged and not late.requires_before_launch
    assert early.flagged and early.requires_before_launch
    problems = coverage_violations(found, launch=[late.key, early.key], pending=[], exempt={}, undetected={})
    assert problems == [
        f"{late.key} (['{RULE_PROCESS_LITERAL}']) calls provider_switch.require after its first "
        "launch call and is not pending"
    ]


def test_an_unknown_launch_function_fails_the_guard():
    found = scan_python_source("import subprocess\ndef f():\n    subprocess.Popen(['ollama', 'run', 'm'])\n",
                               "synthetic/new_consumer.py")
    problems = coverage_violations(found, launch=[], pending=[], exempt={}, undetected={})
    assert problems == [
        f"synthetic/new_consumer.py:f (['{RULE_PROCESS_LITERAL}']) is a launch function outside the "
        "census and not exempt"
    ]


def test_a_pending_entry_is_tolerated_only_while_pending():
    found = scan_python_source(f"import subprocess\ndef f():\n    subprocess.run(['{_K}', 'x'])\n", "s/p.py")
    assert coverage_violations(found, launch=["s/p.py:f"], pending=["s/p.py:f"], exempt={}, undetected={}) == []
    assert coverage_violations(found, launch=["s/p.py:f"], pending=[], exempt={}, undetected={}) != []


def test_a_stale_exempt_function_entry_fails():
    found = scan_python_source("def f():\n    return 1\n", "s/e.py")
    assert exempt_violations(found, REPO_ROOT, {"s/e.py:f": "probe"}) == [
        "exempt function s/e.py:f is no longer flagged; remove the entry"
    ]


def test_an_undetectable_entry_without_reason_fails():
    found = scan_python_source("def f():\n    return helper()\n", "s/u.py")
    assert coverage_violations(found, launch=["s/u.py:f"], pending=["s/u.py:f"], exempt={},
                               undetected={}) == [
        "census entry s/u.py:f is not detected and not listed as undetectable with a reason"
    ]


# ---------------------------------------------------------------------------
# One synthetic module per detector rule
# ---------------------------------------------------------------------------

def _flagged(source: str) -> Dict[str, Set[str]]:
    found = scan_python_source(source, "syn.py")
    return {k.split(":", 1)[1]: {c.rule for c in u.launches} for k, u in found.items() if u.flagged}


def test_rule_process_literal_provider_binary():
    source = (
        "import os, asyncio, shutil, subprocess\n"
        "from subprocess import run as r\n"
        f"KIMI = '{_K}'\n"
        f"def popen():\n    subprocess.Popen(['{_K}', '{_PRINT}'])\n"
        "def exec_():\n    os.execvp('ollama', ['ollama', 'run'])\n"
        f"async def aio():\n    await asyncio.create_subprocess_exec('{_G}', 'x')\n"
        "def alias():\n    r(['litellm', '--model', 'x'])\n"
        f"def local_var():\n    cmd = ['/opt/bin/{_K}', 'x']\n    subprocess.check_output(cmd)\n"
        "def module_const():\n    subprocess.call([KIMI, 'x'])\n"
        "def which():\n    subprocess.run([shutil.which('ollama'), 'run', 'm'])\n"
        f"def shell_string():\n    subprocess.run('{_G} hi', shell=True)\n"
        "def git():\n    subprocess.run(['git', 'status'])\n"
        "def unknown_argv(cmd):\n    subprocess.run(cmd)\n"
    )
    flagged = _flagged(source)
    for name in ("popen", "exec_", "aio", "alias", "local_var", "module_const", "which", "shell_string"):
        assert flagged.get(name) == {RULE_PROCESS_LITERAL}, name
    assert "git" not in flagged and "unknown_argv" not in flagged


@pytest.mark.parametrize("context", [
    "GATE_PROVIDERS = {}\n",
    "def _build_gate_cmd():\n    return []\n",
    "from provider_spawns.kimi_spawn import spawn_kimi\n",
    "BASE = 'ANTHROPIC_BASE_URL'\n",
])
def test_rule_process_nonliteral_in_provider_context(context):
    source = context + "import subprocess, sys\ndef launch(cmd):\n    subprocess.Popen(cmd)\n" \
        "def literal_git():\n    subprocess.run(['git', 'log'])\n" \
        "def python_script():\n    subprocess.run([sys.executable, 'x.py'])\n"
    flagged = _flagged(source)
    assert flagged["launch"] == {RULE_PROCESS_CONTEXT}
    assert flagged["python_script"] == {RULE_PROCESS_CONTEXT}
    assert "literal_git" not in flagged


def test_rule_process_nonliteral_without_context_is_not_flagged():
    assert _flagged("import subprocess\ndef launch(cmd):\n    subprocess.Popen(cmd)\n") == {}


def test_rule_claude_launch_with_anthropic_base_url():
    source = (
        "import os, subprocess\n"
        "def harness(key):\n"
        "    env = dict(os.environ)\n"
        "    env['ANTHROPIC_BASE_URL'] = 'https://example.invalid/anthropic'\n"
        "    subprocess.run(['claude', 'x'], env=env)\n"
        "def plain():\n    subprocess.run(['claude', 'x'])\n"
    )
    assert _flagged(source) == {"harness": {RULE_CLAUDE_BASE_URL}}
    dict_form = "import subprocess\nENV = {'ANTHROPIC_BASE_URL': 'u'}\n" \
                "def run(env):\n    subprocess.Popen(['claude', 'x'], env=env)\n"
    assert _flagged(dict_form) == {"run": {RULE_CLAUDE_BASE_URL}}
    scrub_only = "import os, subprocess\ndef run():\n    env = dict(os.environ)\n" \
                 "    env.pop('ANTHROPIC_BASE_URL', None)\n    subprocess.run(['claude', 'x'], env=env)\n"
    assert _flagged(scrub_only) == {}


def test_rule_http_call_with_model_host():
    source = (
        "import urllib.request, requests, httpx, http.client\n"
        "def deepseek():\n    urllib.request.urlopen('https://api.deepseek.com/chat/completions')\n"
        "def local_port():\n    requests.post('http://127.0.0.1:4141/v1/messages', json={})\n"
        "def openrouter():\n    httpx.post('https://openrouter.ai/api/v1/chat/completions')\n"
        "def conn():\n    http.client.HTTPSConnection('api.moonshot.ai')\n"
        "def injected(urlopen, region):\n"
        "    url = f'https://{region}-aiplatform.googleapis.com/v1'\n    urlopen(url)\n"
    )
    flagged = _flagged(source)
    for name in ("deepseek", "local_port", "openrouter", "conn", "injected"):
        assert flagged.get(name) == {RULE_HTTP_MODEL_HOST}, name
    module_host = "import urllib.request\nHOST = 'http://localhost:11434'\n" \
                  "def call(url):\n    urllib.request.urlopen(url)\n"
    assert _flagged(module_host) == {"call": {RULE_HTTP_MODEL_HOST}}


def test_rule_http_without_model_host_or_with_docstring_only_is_not_flagged():
    assert _flagged("import urllib.request\ndef f():\n    urllib.request.urlopen('https://api.github.com')\n") == {}
    doc_only = '"""Talks to api.deepseek.com in prose only."""\nimport urllib.request\n' \
               "def f(url):\n    urllib.request.urlopen(url)\n"
    assert _flagged(doc_only) == {}


def test_rule_litellm_call():
    source = (
        "import litellm\nfrom litellm import acompletion, completion as c\n"
        "def direct():\n    litellm.completion(model='m', messages=[])\n"
        "async def bare():\n    await acompletion(model='m', messages=[])\n"
        "def aliased():\n    c(model='m', messages=[])\n"
    )
    assert _flagged(source) == {"direct": {RULE_LITELLM}, "bare": {RULE_LITELLM}, "aliased": {RULE_LITELLM}}


def test_module_level_launch_is_a_unit():
    assert _flagged(f"import subprocess\nsubprocess.run(['{_K}', 'x'])\n") == {"<module>": {RULE_PROCESS_LITERAL}}


def test_shell_scan_rules():
    text = "\n".join([
        "#!/bin/bash",
        f"# {_K} {_PRINT} is only mentioned in this comment",
        f'out=$({_K} {_PRINT} "$PROMPT")',
        f'{_G} --prompt "x"',
        "ollama run gemma3:4b hi",
        "ollama serve &",
        "curl -s https://api.deepseek.com/v1/models",
        "if vnx providers check deepseek; then",
        "  curl -s http://localhost:11434/api/generate -d @body.json",
        "fi",
    ])
    flagged = scan_shell_text(text)
    assert [(n, checked) for n, _, checked in flagged] == [(3, False), (4, False), (5, False), (7, False), (9, True)]
