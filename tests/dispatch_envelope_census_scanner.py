"""dispatch_envelope_census_scanner.py — census scanner for the envelope module family.

Not a test module itself (no ``test_*`` functions) — imported by
test_dispatch_envelope_characterization.py. Kept importable as a plain
sibling module in tests/, matching the existing convention (e.g.
test_envelope_ringbuffer_teardown_oi902.py importing from
test_dispatch_envelope_plan.py directly).

Why this exists (read before touching the matching logic below): a scanner
that matches test-file couplings against the single literal string
"dispatch_envelope" is wrong the moment the monolith splits — PR-1 through
PR-6 move code into sibling ``envelope_*`` modules, and a
``patch("envelope_govern.X")`` site is just as real a coupling as
``patch("dispatch_envelope.X")``. The FAMILY set below is discovered fresh
from disk on every run (never a fixed list of module names) specifically so
a 7th module counts automatically without anyone remembering to edit this
file.
"""
from __future__ import annotations

import ast
import importlib
import inspect
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_LIB = REPO_ROOT / "scripts" / "lib"
TESTS_DIR = REPO_ROOT / "tests"
CENSUS_FIXTURE = Path(__file__).resolve().parent / "data" / "dispatch_envelope_census.json"

# discover_family_functions_and_classes() imports every family module by
# name — needed regardless of caller: tests/conftest.py already does this
# for the pytest suite, but the documented standalone regenerate command
# (`python3 tests/dispatch_envelope_census_scanner.py`, see __main__ below
# and every "regenerate with:" message in the characterization suite) runs
# with no conftest involved, so this module pins its own sys.path entry.
if str(SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_LIB))

# Generic family-membership pattern — NOT built by joining a fixed list of
# discovered names. Used only as a self-check (see verify_family_pattern_gap
# below) that the *shape* of family names ("dispatch_envelope" or
# "envelope_<anything>") hasn't quietly been narrowed to something that
# would miss a real envelope_*.py file on disk.
FAMILY_NAME_PATTERN = re.compile(r"^(?:dispatch_envelope|envelope_[A-Za-z0-9_]+)$")


def discover_family(lib_dir: Path = SCRIPTS_LIB) -> frozenset:
    """The envelope module family: dispatch_envelope + every envelope_*.py
    module actually present under scripts/lib/. Recomputed every call —
    this is the census scanner's only source of truth for "which modules
    does a coupling need to resolve against."
    """
    family = {"dispatch_envelope"}
    for f in lib_dir.glob("envelope_*.py"):
        family.add(f.stem)
    return frozenset(family)


def discover_family_functions_and_classes(family: Optional[frozenset] = None) -> tuple:
    """Import every module in the envelope family and collect every function
    and class DEFINED there — keyed by the module that actually owns it, not
    by a fixed "dispatch_envelope" label.

    This is the Layer 4 (annotation-resolution) counterpart of
    scan_tests_dir() above: same reasoning, same discover_family() input, so
    the two can never see a different family. get_type_hints() resolves
    against a function/class's OWN __globals__/__module__ regardless of
    which module re-exports it — filtering strictly on
    ``obj.__module__ == "dispatch_envelope"`` (the pre-PR-1b bug) silently
    drops a symbol from the parametrization the moment it moves to an
    envelope_* sibling, because the moved object's __module__ now reads that
    sibling's name, not "dispatch_envelope". Filtering on family membership
    instead keeps the symbol covered — under its real module name — for
    every step of the PR-1..PR-6 split.

    Returns (funcs, classes):
      funcs:   {(owner_label, attr_name): function}
               owner_label is the defining module for a module-level
               function, or "<module>.<ClassName>" for a method.
      classes: {(module, ClassName): class}
    """
    if family is None:
        family = discover_family()
    funcs: dict = {}
    classes: dict = {}
    for modname in sorted(family):
        module = importlib.import_module(modname)
        for name, obj in vars(module).items():
            if name.startswith("__"):
                continue
            if inspect.isfunction(obj) and getattr(obj, "__module__", None) == modname:
                funcs[(modname, name)] = obj
            elif inspect.isclass(obj) and getattr(obj, "__module__", None) == modname:
                classes[(modname, name)] = obj
                for meth_name, meth in vars(obj).items():
                    if inspect.isfunction(meth):
                        funcs[(f"{modname}.{name}", meth_name)] = meth
    return funcs, classes


def verify_family_pattern_gap(lib_dir: Path = SCRIPTS_LIB) -> list:
    """Return violation messages for any scripts/lib/envelope_*.py file whose
    stem does NOT match FAMILY_NAME_PATTERN.

    Guards against the exact regression this scanner exists to prevent: if a
    future edit narrows FAMILY_NAME_PATTERN (e.g. to an explicit alternation
    of today's known module names), a genuinely-new envelope_*.py file will
    still be found by the glob() below but will fail the pattern check —
    loudly, instead of silently dropping out of the census.
    """
    violations = []
    for f in lib_dir.glob("envelope_*.py"):
        if not FAMILY_NAME_PATTERN.match(f.stem):
            violations.append(
                f"{f} matches the envelope_*.py glob but its stem {f.stem!r} "
                f"does not match FAMILY_NAME_PATTERN {FAMILY_NAME_PATTERN.pattern!r} "
                f"— the census scanner's family-matching logic must be widened"
            )
    return violations


@dataclass(frozen=True, order=True)
class Coupling:
    file: str
    line: int
    mechanism: str
    module_target: str
    symbol: str

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "line": self.line,
            "mechanism": self.mechanism,
            "module_target": self.module_target,
            "symbol": self.symbol,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Coupling":
        return cls(
            file=d["file"], line=d["line"], mechanism=d["mechanism"],
            module_target=d["module_target"], symbol=d["symbol"],
        )


def _dotted_name(node: ast.AST) -> Optional[str]:
    """Reconstruct a dotted attribute chain's textual form, e.g.
    Attribute(Attribute(Name('a'), 'b'), 'c') -> 'a.b.c'. None for anything
    that isn't a pure Name/Attribute chain (calls, subscripts, ...)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        if base is None:
            return None
        return f"{base}.{node.attr}"
    return None


def _const_str(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


class _FamilyBindingTracker:
    """Shared binding-tracking logic: which local names in a module resolve
    to a family module (via `import X` / `from X import Y`), reused by both
    the tests/ census scan and the Layer 6 facade bind-form check.

    local_name -> (family_module, symbol_or_None). symbol is None for a
    plain module import (`import X [as alias]`) — the local name refers to
    the MODULE, not to a specific attribute of it.
    """

    def __init__(self, family: frozenset):
        self.family = family
        self.bindings: dict = {}
        self.from_imports: list = []   # (lineno, module, symbol, local_name)
        self.module_imports: list = []  # (lineno, module, local_name)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        if node.module in self.family:
            for alias in node.names:
                local = alias.asname or alias.name
                self.bindings[local] = (node.module, alias.name)
                lineno = getattr(alias, "lineno", node.lineno)
                self.from_imports.append((lineno, node.module, alias.name, local))

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            if alias.name in self.family:
                local = alias.asname or alias.name
                self.bindings[local] = (alias.name, None)
                lineno = getattr(alias, "lineno", node.lineno)
                self.module_imports.append((lineno, alias.name, local))

    def resolve_class_ref(self, node: ast.AST):
        """Resolve a `patch.object(<ref>, ...)` / `monkeypatch.setattr(<ref>, ...)`
        first-argument expression to (family_module, class_or_attr_name), using
        the bindings collected so far. None if it doesn't resolve to the family."""
        if isinstance(node, ast.Name):
            binding = self.bindings.get(node.id)
            if binding is not None and binding[1] is not None:
                return binding  # `from family_module import ClassName`
            return None
        if isinstance(node, ast.Attribute):
            base = node.value
            if isinstance(base, ast.Name):
                binding = self.bindings.get(base.id)
                if binding is not None and binding[1] is None:
                    return binding[0], node.attr  # `import family_module` then `family_module.ClassName`
            return None
        return None


class _CensusFileScanner(ast.NodeVisitor):
    """Walks one test file's AST, recording every coupling to a family module."""

    def __init__(self, relpath: str, family: frozenset):
        self.relpath = relpath
        self.family = family
        self.couplings: list = []
        self._tracker = _FamilyBindingTracker(family)

    def _add(self, line: int, mechanism: str, module_target: str, symbol: str):
        self.couplings.append(Coupling(self.relpath, line, mechanism, module_target, symbol))

    # ---- imports -----------------------------------------------------
    def visit_ImportFrom(self, node: ast.ImportFrom):
        self._tracker.visit_ImportFrom(node)
        if node.module in self.family:
            for alias in node.names:
                lineno = getattr(alias, "lineno", node.lineno)
                self._add(lineno, "direct-call", node.module, alias.name)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import):
        self._tracker.visit_Import(node)
        self.generic_visit(node)

    # ---- module.attr access (direct-call, module-alias form) ----------
    def visit_Attribute(self, node: ast.Attribute):
        base = node.value
        if isinstance(base, ast.Name):
            binding = self._tracker.bindings.get(base.id)
            if binding is not None and binding[1] is None:
                self._add(node.lineno, "direct-call", binding[0], node.attr)
        self.generic_visit(node)

    # ---- patch(...) / patch.object(...) / monkeypatch.setattr(...) / --
    # ---- caplog.at_level(...) / getLogger(...) -------------------------
    def visit_Call(self, node: ast.Call):
        func = node.func
        if self._is_patch_object_call(func):
            self._handle_patch_object(node)
        elif self._is_monkeypatch_setattr(func):
            self._handle_monkeypatch(node)
        elif self._is_caplog_at_level(func):
            self._handle_caplog(node)
        elif self._is_getlogger(func):
            if node.args:
                name = _const_str(node.args[0])
                if name in self.family:
                    self._add(node.lineno, "logger-name", name, "")
        elif self._is_patch_call(func):
            if node.args:
                target = _const_str(node.args[0])
                if target:
                    self._maybe_family_split(node.lineno, "patch-string", target)
        self.generic_visit(node)

    # ---- predicates -----------------------------------------------------
    @staticmethod
    def _is_patch_call(func) -> bool:
        if isinstance(func, ast.Name) and func.id == "patch":
            return True
        if isinstance(func, ast.Attribute) and func.attr == "patch":
            return True
        return False

    @staticmethod
    def _is_patch_object_call(func) -> bool:
        if isinstance(func, ast.Attribute) and func.attr == "object":
            d = _dotted_name(func.value)
            return d is not None and (d == "patch" or d.endswith(".patch"))
        return False

    @staticmethod
    def _is_monkeypatch_setattr(func) -> bool:
        if isinstance(func, ast.Attribute) and func.attr == "setattr":
            return _dotted_name(func.value) == "monkeypatch"
        return False

    @staticmethod
    def _is_caplog_at_level(func) -> bool:
        if isinstance(func, ast.Attribute) and func.attr == "at_level":
            return _dotted_name(func.value) == "caplog"
        return False

    @staticmethod
    def _is_getlogger(func) -> bool:
        if isinstance(func, ast.Name) and func.id == "getLogger":
            return True
        if isinstance(func, ast.Attribute) and func.attr == "getLogger":
            return True
        return False

    # ---- handlers ---------------------------------------------------
    def _maybe_family_split(self, lineno: int, mechanism: str, target: str):
        if "." not in target:
            return
        mod, rest = target.split(".", 1)
        if mod in self.family:
            self._add(lineno, mechanism, mod, rest)

    def _handle_patch_object(self, node: ast.Call):
        if len(node.args) < 2:
            return
        resolved = self._tracker.resolve_class_ref(node.args[0])
        attr_str = _const_str(node.args[1])
        if resolved and attr_str:
            module_target, class_name = resolved
            self._add(node.lineno, "patch.object-attr", module_target, f"{class_name}.{attr_str}")

    def _handle_monkeypatch(self, node: ast.Call):
        if not node.args:
            return
        first = node.args[0]
        s = _const_str(first)
        if s is not None:
            self._maybe_family_split(node.lineno, "monkeypatch", s)
            return
        resolved = self._tracker.resolve_class_ref(first)
        if resolved and len(node.args) >= 2:
            attr_str = _const_str(node.args[1])
            if attr_str:
                module_target, class_name = resolved
                self._add(node.lineno, "monkeypatch", module_target, f"{class_name}.{attr_str}")

    def _handle_caplog(self, node: ast.Call):
        for kw in node.keywords:
            if kw.arg == "logger":
                val = _const_str(kw.value)
                if val in self.family:
                    self._add(node.lineno, "logger-name", val, "")
                return
        if len(node.args) >= 2:
            val = _const_str(node.args[1])
            if val in self.family:
                self._add(node.lineno, "logger-name", val, "")


# ---------------------------------------------------------------------------
# Regex fallback — AST is authoritative; this only ADDS entries AST structurally
# cannot see (e.g. a logger name embedded in a form the AST predicates above
# don't recognise). Never removes or overrides an AST-found entry.
# ---------------------------------------------------------------------------
_LOGGER_KW_REGEX = re.compile(r'logger\s*=\s*["\']([\w.]+)["\']')
_GETLOGGER_REGEX = re.compile(r'getLogger\(\s*["\']([\w.]+)["\']\s*\)')


def _regex_fallback(relpath: str, text: str, family: frozenset, existing_keys: set) -> list:
    extra = []
    for i, line in enumerate(text.splitlines(), start=1):
        for regex in (_LOGGER_KW_REGEX, _GETLOGGER_REGEX):
            for m in regex.finditer(line):
                name = m.group(1)
                if name not in family:
                    continue
                key = (relpath, i, "logger-name", name, "")
                if key not in existing_keys:
                    extra.append(Coupling(relpath, i, "logger-name", name, ""))
                    existing_keys.add(key)
    return extra


def scan_test_file(path: Path, family: frozenset) -> list:
    relpath = str(path.relative_to(REPO_ROOT))
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))
    scanner = _CensusFileScanner(relpath, family)
    scanner.visit(tree)
    couplings = list(scanner.couplings)
    existing_keys = {(c.file, c.line, c.mechanism, c.module_target, c.symbol) for c in couplings}
    couplings.extend(_regex_fallback(relpath, text, family, existing_keys))
    return couplings


def scan_tests_dir(tests_dir: Path = TESTS_DIR, family: Optional[frozenset] = None) -> list:
    if family is None:
        family = discover_family()
    all_couplings: list = []
    for path in sorted(tests_dir.rglob("*.py")):
        all_couplings.extend(scan_test_file(path, family))
    return sorted(all_couplings)


# ---------------------------------------------------------------------------
# Layer 6 helpers — facade bind-form. Reuses the same binding tracker so
# "which modules count as family" can never drift between the census (tests/)
# side and the facade (dispatch_envelope.py) side.
# ---------------------------------------------------------------------------


def scan_facade_bindings(facade_path: Path, family: frozenset) -> _FamilyBindingTracker:
    """Parse facade_path (dispatch_envelope.py) and return the tracker holding
    every `import <envelope_*>` / `from <envelope_*> import X` statement found,
    where family excludes 'dispatch_envelope' itself (the facade importing
    FROM its own sibling modules, never the reverse)."""
    submodule_family = family - {"dispatch_envelope"}
    tree = ast.parse(facade_path.read_text(encoding="utf-8"), filename=str(facade_path))
    tracker = _FamilyBindingTracker(submodule_family)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            tracker.visit_ImportFrom(node)
        elif isinstance(node, ast.Import):
            tracker.visit_Import(node)
    return tracker


def find_facade_attribute_calls(facade_path: Path, module_alias_names: frozenset) -> list:
    """Every `<module_alias>.<attr>` access in the facade where module_alias is
    one of the (forbidden) plain-module-import bindings — i.e. the attribute
    -form call the facade must never use for a re-exported symbol."""
    tree = ast.parse(facade_path.read_text(encoding="utf-8"), filename=str(facade_path))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id in module_alias_names:
                hits.append((node.lineno, node.value.id, node.attr))
    return hits


if __name__ == "__main__":
    import json

    fam = discover_family()
    results = scan_tests_dir(family=fam)
    gap = verify_family_pattern_gap()
    layer4_funcs, layer4_classes = discover_family_functions_and_classes(family=fam)
    print("FAMILY:", sorted(fam), file=sys.stderr)
    print("TOTAL COUPLINGS:", len(results), file=sys.stderr)
    print("LAYER4 FUNCS/CLASSES:", len(layer4_funcs), "/", len(layer4_classes), file=sys.stderr)
    if gap:
        print("FAMILY PATTERN GAP:", gap, file=sys.stderr)
    CENSUS_FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "family": sorted(fam),
        "couplings": [c.to_dict() for c in results],
        # Layer 4 floor (test_dispatch_envelope_characterization.py
        # TestLayer4CaseCountFloor): recorded set of (owner, attr) symbol
        # keys the annotation-resolution scan found here. Growing this set
        # is free; if a live scan ever finds FEWER than what's recorded
        # below, that's a silent regression (see TestLayer4CaseCountFloor's
        # docstring) and the floor test fails loudly, naming what vanished.
        "layer4_functions": sorted(f"{owner}::{attr}" for (owner, attr) in layer4_funcs),
        "layer4_classes": sorted(f"{owner}::{attr}" for (owner, attr) in layer4_classes),
    }
    CENSUS_FIXTURE.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {CENSUS_FIXTURE} ({len(results)} couplings)", file=sys.stderr)
