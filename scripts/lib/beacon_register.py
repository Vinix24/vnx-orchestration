"""beacon_register — the single source of truth for VNX's expected health-beacon
writers (D3a, ``absence-is-loud``).

D3a measured three candidate registers before picking one:

  - ``subsystem_health.known_subsystems()`` — 28 cockpit subsystem names
    (kebab-case, e.g. ``governance-enforcement-stack``). Rejected: wrong
    namespace. Measured overlap with the 9 files under ``health/`` is 2
    (``governance-enforcement-stack``, ``plan-gate-panel``) — the other 7
    beacon-writing components (``t0_state_builder``, ``learning_loop``,
    ``ledger_health``, ``producer_freshness_monitor``, ``cleanup_worker_exit``,
    ``conversation_analyzer``, ``intelligence_daemon``) are not cockpit
    subsystems at all. ``known_subsystems()`` already has its own correct
    "loop over the register, default the absent ones to unknown" consumers
    (``dashboard/api_health.py``, ``dashboard/api_subsystems.py``,
    ``vnx_cli/commands/subsystems.py``) — this module does not duplicate
    that; it answers a different question.
  - D2's ``daemon_register.read_daemon_register()`` — 9 supervised OS
    processes parsed from ``vnx_supervisor_simple.sh``'s ``start_all()``.
    Rejected for the same reason: a different namespace answering a
    different question ("which processes does the supervisor start" vs.
    "which components write a health beacon"). Measured overlap: 1 of 9
    (``intelligence_daemon`` happens to be both a supervised daemon and a
    beacon writer; every other beacon writer here is a one-shot script or
    library call, never a supervised long-running process).
  - Glob over what already exists in ``health/`` — ``all_beacons``'s
    pre-D3a behaviour. Rejected: this IS the defect this register exists to
    close. A component that never once wrote a beacon is invisible to a
    glob over files that exist.

``read_beacon_register()`` parses every ``HealthBeacon(...)`` call site
under ``scripts/`` via the ``ast`` module — the same "parse the actual
source of truth instead of hand-duplicating a list" approach D2 took for
daemons, applied to Python call sites instead of a bash function. A call
site's component-name argument is resolved when it is either a literal
string (``HealthBeacon(dir, "cleanup_worker_exit", ...)``) or a reference to
a simple module-level string constant (``HealthBeacon(dir, COMPONENT_NAME,
...)`` where ``COMPONENT_NAME = "ledger_health"`` is assigned at module
scope). ``scripts/lib/subsystem_health.py``'s call site
(``HealthBeacon(state_dir, name)`` inside a loop over ``known_subsystems()``)
resolves to neither — ``name`` is a loop variable, not a literal or a
module-level constant — so it is excluded from this register. There is no
single fixed name to track absence for there; that beacon set is already
governed by ``known_subsystems()`` on its own terms (see above).

Performance: a full ``ast.parse`` over every ``.py`` file under ``scripts/``
(~730 files, measured 2026-08-30) costs ~0.7s. A cheap substring pre-filter
(``"HealthBeacon(" in text``) before parsing drops that to ~0.15s (I/O-bound
text reads dominate; only the ~10 files that actually match get parsed).
``read_beacon_register()`` is additionally wrapped in ``functools.lru_cache``
so a long-lived process (the dashboard server) pays this once, not per
request — a source change is picked up on the next process restart, the
same way any other Python code change already requires one.
"""
from __future__ import annotations

import ast
import functools
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import project_root  # noqa: E402


@dataclass(frozen=True)
class BeaconSpec:
    """One expected beacon-writing component, as declared by a resolvable
    ``HealthBeacon(...)`` call site."""

    name: str
    source_file: str
    line: int


def _module_level_string_constants(tree: ast.Module) -> Dict[str, str]:
    consts: Dict[str, str] = {}
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            consts[target.id] = node.value.value
    return consts


def _resolve_component_arg(call: ast.Call, consts: Dict[str, str]) -> Optional[str]:
    """Return the literal component name for a ``HealthBeacon(...)`` call,
    or ``None`` if it cannot be statically resolved (a loop variable, an
    attribute access, a function call result, ...)."""
    arg: Optional[ast.expr] = None
    for kw in call.keywords:
        if kw.arg == "component":
            arg = kw.value
            break
    if arg is None and len(call.args) >= 2:
        arg = call.args[1]
    if arg is None:
        return None
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    if isinstance(arg, ast.Name):
        return consts.get(arg.id)
    return None


def _default_scripts_root() -> Path:
    # No __file__ anchor (central-mode path gate, shape 3a): a central install
    # runs this module from the read-only keystone checkout, so anchoring on
    # __file__ would resolve the keystone's scripts/ tree, not the project's.
    # CWD-first resolution matches every other scripts/lib register reader
    # (daemon_register.py's _default_supervisor_script()).
    root = project_root.resolve_project_root()
    return root / "scripts"


def _read_beacon_register_uncached(scripts_root: Path) -> Tuple[BeaconSpec, ...]:
    specs: "Dict[str, BeaconSpec]" = {}
    for path in sorted(scripts_root.rglob("*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "HealthBeacon(" not in text:
            continue
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            continue
        consts = _module_level_string_constants(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "HealthBeacon"):
                continue
            name = _resolve_component_arg(node, consts)
            if name is None or name in specs:
                continue
            specs[name] = BeaconSpec(
                name=name,
                source_file=str(path.relative_to(scripts_root.parent)),
                line=node.lineno,
            )
    return tuple(specs[name] for name in sorted(specs))


@functools.lru_cache(maxsize=8)
def _read_beacon_register_cached(scripts_root: str) -> Tuple[BeaconSpec, ...]:
    return _read_beacon_register_uncached(Path(scripts_root))


def read_beacon_register(scripts_root: Optional[Path] = None) -> Tuple[BeaconSpec, ...]:
    """Parse every resolvable ``HealthBeacon(...)`` call site under ``scripts_root``
    (default: this project's ``scripts/`` directory).

    Best-effort, like ``daemon_register.read_daemon_register``'s own file
    read: a source file that fails to read or parse is skipped, not treated
    as "zero writers" for the whole register.
    """
    root = Path(scripts_root) if scripts_root is not None else _default_scripts_root()
    return _read_beacon_register_cached(str(root))


def expected_component_names(register: Optional[Sequence[BeaconSpec]] = None) -> Tuple[str, ...]:
    """Convenience: just the names, for passing straight to ``all_beacons(expected=...)``."""
    if register is None:
        register = read_beacon_register()
    return tuple(spec.name for spec in register)


__all__ = ["BeaconSpec", "read_beacon_register", "expected_component_names"]
