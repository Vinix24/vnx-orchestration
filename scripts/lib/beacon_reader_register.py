"""beacon_reader_register — the other half of the beacon absence-is-loud
contract (golf C, track ``absence-is-loud``, C2a).

``beacon_register.py`` already answers "which components are EXPECTED to
write a beacon" by parsing every resolvable ``HealthBeacon(...)`` call site.
Nothing paired that with "and does anything actually READ that signal" —
a grep for reader/lezer/consumer in ``producer_freshness.py`` and
``beacon_register.py`` turns up only comments, no mechanism. This module is
that mechanism, derived from the code the same way the writer register is:

  - a GENERIC reader is a call site of ``health_beacon.all_beacons`` /
    ``beacon_summary`` that passes an ``expected=`` argument. Passing
    ``expected=`` is what makes a component that NEVER wrote a beacon at all
    surface as ``health="absent"`` (see ``health_beacon.py``'s D3a gap 2) —
    a generic reader-with-expected therefore covers every CURRENT and
    FUTURE registered component automatically, the same self-updating
    property ``beacon_register.py`` already has on the writer side. Measured
    live in this repo (2026-09-09): ``dashboard/api_health.py`` and
    ``scripts/build_t0_state.py`` both already pass ``expected=
    expected_component_names()`` — this register makes that fact
    inspectable and testable instead of living only in two docstrings.
  - a SPECIFIC reader is a source location, outside the component's own
    writer file and outside this module's own infra siblings, that
    references the component's exact name as a non-docstring string literal
    (Python) or a quoted substring (the two bash hooks). E.g.
    ``hooks/monitor_tripwire.sh`` hardcodes
    ``health/producer_freshness_monitor.json``.

Scope, mirroring ``beacon_register.py``'s own documented boundary: this
module answers the reader side for the SAME namespace ``beacon_register.py``
covers (the AST-resolvable ``HealthBeacon(...)`` call sites). Cockpit
subsystems (``subsystem_health.known_subsystems()``, e.g.
``governance-enforcement-stack`` / ``plan-gate-panel``) are a different
namespace with their own already-documented readers
(``dashboard/api_health.py``'s ``_subsystem_effectiveness_summary``,
``dashboard/api_subsystems.py``, ``vnx_cli/commands/subsystems.py``) and are
out of scope here for the same reason ``beacon_register.py`` excludes their
writer call site (a loop variable, not a fixed name).
"""
from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import project_root  # noqa: E402
from beacon_register import read_beacon_register  # noqa: E402

# Calls that, when given an `expected=` keyword, cover every registered
# component's absence -- not just the ones that happen to have a file today.
_GENERIC_CALL_NAMES = frozenset({"all_beacons", "beacon_summary"})

# This module's own infra siblings -- never a reader in their own right, so
# their docstring examples (health_beacon.py's top docstring literally uses
# "learning_loop" as a sample component name) can never self-count.
_INFRA_RELATIVE_PATHS = frozenset({
    "scripts/lib/health_beacon.py",
    "scripts/lib/beacon_register.py",
    "scripts/lib/beacon_reader_register.py",
})

# Where a reader can plausibly live. Broader than a curated per-component
# list (which would be hand-maintained, the exact thing this module exists
# to avoid) -- every .py file under these roots is scanned; only the
# component-name MATCH is code-derived, not the surface.
_READER_PY_DIRS: Tuple[str, ...] = ("scripts", "dashboard", "vnx_cli")
_READER_SH_DIRS: Tuple[str, ...] = ("hooks",)

_QUOTED_RE = re.compile(r'"([^"]*)"|\'([^\']*)\'')


@dataclass(frozen=True)
class ReaderSpec:
    """One code location that reads/acts on a beacon's signal.

    ``component`` is ``None`` for a GENERIC reader: a call to
    ``all_beacons``/``beacon_summary`` with an ``expected=`` argument covers
    every component, present or not, so it is not tied to one name.
    """

    component: Optional[str]
    source_file: str
    line: int
    kind: str  # "generic" | "specific"


def _default_project_root() -> Path:
    return project_root.resolve_project_root()


def _iter_files(root: Path, subdirs: Sequence[str], suffix: str) -> List[Path]:
    files: List[Path] = []
    for sub in subdirs:
        base = root / sub
        if not base.is_dir():
            continue
        for path in sorted(base.rglob(f"*{suffix}")):
            if "tests" in path.relative_to(root).parts[:-1]:
                continue
            files.append(path)
    return files


def _docstring_positions(tree: ast.AST) -> Dict[Tuple[int, int], None]:
    """Positions of every module/class/function DOCSTRING string constant.

    A docstring is the first statement of a Module/ClassDef/FunctionDef body
    when that statement is a bare string expression -- the same shape
    ``ast.get_docstring`` recognizes. Narrative prose there (this repo's own
    heavily-commented style routinely name-drops other components) must
    never count as a reader reference; only non-docstring string literals
    (paths, CLI args, log messages) do.
    """
    positions: Dict[Tuple[int, int], None] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            positions[(first.value.lineno, first.value.col_offset)] = None
    return positions


def _find_generic_readers_in_py(path: Path, rel: str) -> List[ReaderSpec]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    if not any(name in text for name in _GENERIC_CALL_NAMES):
        return []
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return []
    found: List[ReaderSpec] = []
    for node in ast.walk(tree):
        func = node.func if isinstance(node, ast.Call) else None
        name = None
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        if name not in _GENERIC_CALL_NAMES:
            continue
        for kw in node.keywords:
            if kw.arg != "expected":
                continue
            if isinstance(kw.value, ast.Constant) and kw.value.value is None:
                continue  # expected=None is explicitly "no expectation declared"
            found.append(ReaderSpec(component=None, source_file=rel, line=node.lineno, kind="generic"))
            break
    return found


def _find_specific_readers_in_py(path: Path, rel: str, names: Sequence[str]) -> List[ReaderSpec]:
    if not names:
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    if not any(name in text for name in names):
        return []
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return []
    doc_positions = _docstring_positions(tree)
    found: List[ReaderSpec] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if (node.lineno, node.col_offset) in doc_positions:
            continue
        for name in names:
            if name in node.value:
                found.append(ReaderSpec(component=name, source_file=rel, line=node.lineno, kind="specific"))
    return found


def _find_specific_readers_in_sh(path: Path, rel: str, names: Sequence[str]) -> List[ReaderSpec]:
    if not names:
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    found: List[ReaderSpec] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.strip().startswith("#"):
            continue
        for match in _QUOTED_RE.finditer(line):
            fragment = match.group(1) if match.group(1) is not None else match.group(2)
            if not fragment:
                continue
            for name in names:
                if name in fragment:
                    found.append(ReaderSpec(component=name, source_file=rel, line=lineno, kind="specific"))
    return found


def read_reader_register(
    beacon_names: Optional[Sequence[str]] = None,
    *,
    project_root_dir: Optional[Path] = None,
    writer_source_by_component: Optional[Dict[str, str]] = None,
) -> Tuple[ReaderSpec, ...]:
    """Derive every reader (generic + specific) for ``beacon_names``.

    ``beacon_names``/``writer_source_by_component`` default to the live
    ``beacon_register.read_beacon_register()`` output; tests inject a
    synthetic register (via these params, or by monkeypatching
    ``read_beacon_register`` itself) instead of depending on this repo's
    real writer set — mirrors how ``build_t0_state.py`` already injects
    ``expected_beacon_components`` for its own tests.
    """
    root = Path(project_root_dir) if project_root_dir is not None else _default_project_root()
    if beacon_names is None:
        # Only derive BOTH from the live writer register together when
        # neither was given -- an explicit beacon_names (a test's synthetic
        # or partial input) must never fall back to scanning the REAL
        # project root for the writer-source exclusion map underneath it.
        register = read_beacon_register()
        beacon_names = tuple(spec.name for spec in register)
        if writer_source_by_component is None:
            writer_source_by_component = {spec.name: spec.source_file for spec in register}
    if writer_source_by_component is None:
        writer_source_by_component = {}

    specs: List[ReaderSpec] = []
    for path in _iter_files(root, _READER_PY_DIRS, ".py"):
        rel = str(path.relative_to(root))
        if rel in _INFRA_RELATIVE_PATHS:
            continue
        specs.extend(_find_generic_readers_in_py(path, rel))
        applicable = [n for n in beacon_names if writer_source_by_component.get(n) != rel]
        specs.extend(_find_specific_readers_in_py(path, rel, applicable))
    for path in _iter_files(root, _READER_SH_DIRS, ".sh"):
        rel = str(path.relative_to(root))
        applicable = [n for n in beacon_names if writer_source_by_component.get(n) != rel]
        specs.extend(_find_specific_readers_in_sh(path, rel, applicable))
    return tuple(specs)


def has_generic_reader(register: Sequence[ReaderSpec]) -> bool:
    return any(r.kind == "generic" for r in register)


def readers_for_component(component: str, register: Sequence[ReaderSpec]) -> Tuple[ReaderSpec, ...]:
    """Every reader that covers ``component`` -- every generic reader
    (they cover all names) plus any specific reader naming it exactly."""
    return tuple(r for r in register if r.kind == "generic" or r.component == component)


def components_without_reader(
    beacon_names: Sequence[str], register: Sequence[ReaderSpec]
) -> Tuple[str, ...]:
    """Names in ``beacon_names`` with ZERO readers in ``register``.

    A single generic reader (``expected=`` present anywhere) covers every
    name, so the result is only non-empty when the register has no generic
    reader at all AND a specific name has no dedicated reader either --
    exactly the "silent producer, nobody reads it" case this track closes.
    """
    if has_generic_reader(register):
        return ()
    covered = {r.component for r in register if r.kind == "specific"}
    return tuple(name for name in beacon_names if name not in covered)


def check_coverage(
    beacon_names: Optional[Sequence[str]] = None,
    *,
    project_root_dir: Optional[Path] = None,
) -> Dict[str, object]:
    """Structural check: every beacon ``beacon_register`` expects a writer
    for must have at least one reader here. Returns a
    ``producer_freshness``-shaped section (``findings`` of kind
    ``"no_reader"``) so ``producer_freshness_monitor.py`` can fold it
    straight into its own report/heartbeat without inventing a second
    finding shape.
    """
    if beacon_names is None:
        beacon_names = tuple(spec.name for spec in read_beacon_register())
    register = read_reader_register(beacon_names, project_root_dir=project_root_dir)
    orphans = components_without_reader(beacon_names, register)
    findings = [
        {
            "producer": "beacon_reader_coverage",
            "key": name,
            "kind": "no_reader",
            "cadence_seconds": None,
        }
        for name in orphans
    ]
    return {
        "producer": "beacon_reader_coverage",
        "type": "beacon_reader_register",
        "kind": "structural",
        "keys": [{"key": name} for name in beacon_names],
        "findings": findings,
        "status": "stale" if findings else "ok",
    }


__all__ = [
    "ReaderSpec",
    "read_reader_register",
    "has_generic_reader",
    "readers_for_component",
    "components_without_reader",
    "check_coverage",
]
