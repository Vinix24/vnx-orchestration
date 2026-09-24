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

A call site also declares HOW its component writes: ``expected_interval_seconds``
is read from the same ``ast.Call``. A literal ``None`` there (keyword, or the
third positional argument) means the component is EVENT-DRIVEN: it writes when
something happens (``cleanup_worker_exit`` on a worker exit), not on a clock.
Such a component owes no beacon between two events, so its silence is not a
finding. ``expected_component_names()`` is the one place that decides this: it
leaves event-driven components out, and every reader that feeds it to
``all_beacons(expected=...)`` (t0_state, the dashboard, the SessionStart digest)
stops turning their silence into ``absent`` without having to know about it.
A beacon that IS on disk is still read by the plain glob in ``all_beacons``, so
an event-driven component that wrote ``status: fail`` stays ``fail``. Only what
the parser can prove is treated so: an interval passed as a name or an
expression is periodic, and a component with any periodic call site is
periodic (guessing "event-driven" would hide a real silence).

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
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Dict, Mapping, Optional, Sequence, Tuple

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import project_root  # noqa: E402

# golf C / C2a operator decision (2026-09-09): the intelligence layer stays
# PARKED until the governance ledger is falsifiable (per the PRD, not a
# regression) -- learning_loop (last write 2026-06-26) and intelligence_daemon
# (last write 2026-06-27) are therefore expected to sit silent, on purpose.
# A parked component still HAS a reader (both already pass through the same
# generic all_beacons(expected=...) readers every other component does) --
# this list only tells health_beacon.all_beacons() to stop classifying their
# age as "stale", not to stop expecting/reading them.
#
# Extended by the operator on 2026-09-24 (absence-is-loud, punt 1) with
# "intelligence-self-learning-loop": the same layer, the same reason. Since
# #1903 the subsystem probes read the central store, so
# subsystem_health.aggregate() now writes that beacon (measured 2026-09-24
# 06:45Z: ignore_rate 0.868, 585 ignored against 89 used, last_dream_cycle_iso
# null, pending_proposals 0) and it read "stale" in t0_state. It measures the
# learning loop that was parked on 2026-09-09, so it is parked with it rather
# than reported as a fault of its own. Unlike the two writers above it is a
# cockpit SUBSYSTEM name (kebab-case, written through a loop variable in
# subsystem_health.py) and is therefore not in read_beacon_register(); it needs
# no entry there, because parking only changes how a beacon that exists is
# classified.
#
# The reason lives here, in this comment, and not in a mapping: the registers
# below (PARKED_ARTIFACTS, PARKED_LAUNCHD_JOBS) carry a reason string per entry
# because their readers print it, but health_beacon.all_beacons() takes only
# names (``parked=``) and a beacon entry has no reason field to print it in.
# Parking covers stale/unknown/absent only: a beacon that says ``status: fail``
# while fresh stays ``fail``.
PARKED_COMPONENTS: frozenset = frozenset({
    "learning_loop",
    "intelligence_daemon",
    "intelligence-self-learning-loop",
})

# The same operator decision has readers beyond the beacon reader, and each of
# them used to report the consequence as a fault: a state artifact whose producer
# is a parked component reads STALE (and blocks SessionStart), and a launchd job
# that was deliberately never installed reads not_loaded (and pins
# launchd_liveness.overall on fail). Measured on main 264e9460, 2026-09-23:
# t0_recommendations.json at 98 days, three plists never installed.
#
# This is the one place that says what is parked and why, for state artifacts and
# launchd jobs. It extends PARKED_COMPONENTS rather than replacing it, so every
# existing reader of that set is untouched. A reason is a string, never a bare
# name: a reader prints it next to the age or the absence, so the lookup shows
# that the silence is a decision and what the decision was. Parking covers the
# ABSENCE only. An artifact that is fresh reads fresh, and a parked job that IS
# loaded and exiting non-zero reads loaded, because a failure is not a decision.
_INTELLIGENCE_LAYER_PARKED = (
    "intelligence layer parked by operator decision 2026-09-09 (golf C / #1832) "
    "until the governance ledger is falsifiable"
)

# state artifact name (session_state_freshness.ARTIFACTS key) -> reason
PARKED_ARTIFACTS: Mapping[str, str] = MappingProxyType({
    "t0_recommendations": (
        "producer is generate_t0_recommendations.py, phase 10 of the nightly "
        "intelligence pipeline; " + _INTELLIGENCE_LAYER_PARKED
    ),
})

# resolved launchd Label (as `launchctl list` reports it) -> reason
PARKED_LAUNCHD_JOBS: Mapping[str, str] = MappingProxyType({
    "com.vnx.nightly-intelligence-pipeline": (
        "the nightly intelligence pipeline itself; " + _INTELLIGENCE_LAYER_PARKED
    ),
    "com.vnx.receipt-classifier-batch": (
        "ARC-3 intelligence classifier, the same layer; " + _INTELLIGENCE_LAYER_PARKED
    ),
    "com.vnx.headless-trigger": (
        "F41 autonomous T0 trigger is an operator opt-in (human in the loop), "
        "never installed since April; not a forgotten daemon"
    ),
})


@dataclass(frozen=True)
class BeaconSpec:
    """One beacon-writing component, as declared by its resolvable
    ``HealthBeacon(...)`` call sites.

    ``event_driven`` is True when EVERY call site for the component passes a
    literal ``expected_interval_seconds=None``: it writes on an event, not on a
    clock, so ``expected_component_names()`` does not expect a beacon from it.
    """

    name: str
    source_file: str
    line: int
    event_driven: bool = False


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


def _declares_event_driven_interval(call: ast.Call) -> bool:
    """True when the call passes a literal ``None`` as ``expected_interval_seconds``
    (keyword, or ``HealthBeacon``'s third positional argument).

    An omitted argument is ``HealthBeacon``'s own 86400 default, so it is
    periodic. A name or an expression is periodic too: the parser cannot prove
    it is ``None``, and guessing would hide a real silence."""
    arg: Optional[ast.expr] = None
    for kw in call.keywords:
        if kw.arg == "expected_interval_seconds":
            arg = kw.value
            break
    if arg is None and len(call.args) >= 3:
        arg = call.args[2]
    return isinstance(arg, ast.Constant) and arg.value is None


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
    # A component is event-driven only if every one of its call sites says so.
    event_driven: "Dict[str, bool]" = {}
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
            if name is None:
                continue
            event_driven[name] = event_driven.get(name, True) and _declares_event_driven_interval(node)
            if name in specs:
                continue
            specs[name] = BeaconSpec(
                name=name,
                source_file=str(path.relative_to(scripts_root.parent)),
                line=node.lineno,
            )
    return tuple(replace(specs[name], event_driven=event_driven[name]) for name in sorted(specs))


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
    """The names that MUST have a beacon on disk, for passing straight to
    ``all_beacons(expected=...)``.

    Event-driven components (``BeaconSpec.event_driven``) are left out: they
    write when something happens, so no beacon between two events is not a
    finding. A beacon they DID write is still read (``all_beacons`` globs
    ``health/``), so a recorded ``fail`` stays ``fail``. This is the single place
    that decides it; every reader gets it by calling this function."""
    if register is None:
        register = read_beacon_register()
    return tuple(spec.name for spec in register if not spec.event_driven)


def parked_component_names() -> Tuple[str, ...]:
    """Convenience: :data:`PARKED_COMPONENTS` as a sorted tuple, for passing
    straight to ``all_beacons(parked=...)``."""
    return tuple(sorted(PARKED_COMPONENTS))


def parked_artifact_reason(name: str) -> Optional[str]:
    """Why the state artifact ``name`` is parked, or ``None`` when it is not."""
    return PARKED_ARTIFACTS.get(name)


def parked_launchd_reason(label: str) -> Optional[str]:
    """Why the launchd job ``label`` (resolved, as ``launchctl list`` reports
    it) is parked, or ``None`` when it is not."""
    return PARKED_LAUNCHD_JOBS.get(label)


# Historical duplicate-write locations for the same beacon component (C2a).
# report_to_receipt_converter's own writer passed VNX_STATE_DIR straight
# through to HealthBeacon instead of its parent (data dir) until #1736
# (2026-08-30) fixed it -- every OTHER writer in the fleet already resolved
# `state_dir.parent` correctly. The leftover file this produced
# (`<data_dir>/state/health/report_to_receipt_converter.json`) is stale, not
# actively rewritten (confirmed via `git log -S` against the fixed call
# site), but it was never cleaned up and a monitor that glob-scanned BOTH
# roots would silently read the wrong, frozen half.
_PRIMARY_HEALTH_SUBDIR = "health"
_SECONDARY_HEALTH_SUBDIR = "state/health"


def find_duplicate_beacon_writers(data_dir: Path) -> Dict[str, Tuple[Path, Path]]:
    """Return ``{component: (primary_path, secondary_path)}`` for every
    component with a beacon file under BOTH ``<data_dir>/health/`` (the one
    every reader resolves to) and ``<data_dir>/state/health/`` (the historical
    wrong path). An empty dict when either root is absent or no name
    collides -- this is advisory, not a crash on a fresh/partial store.
    """
    data_dir = Path(data_dir)
    primary = data_dir / _PRIMARY_HEALTH_SUBDIR
    secondary = data_dir / _SECONDARY_HEALTH_SUBDIR
    if not primary.is_dir() or not secondary.is_dir():
        return {}
    primary_names = {p.stem for p in primary.glob("*.json")}
    secondary_names = {p.stem for p in secondary.glob("*.json")}
    dupes = sorted(primary_names & secondary_names)
    return {
        name: (primary / f"{name}.json", secondary / f"{name}.json")
        for name in dupes
    }


__all__ = [
    "BeaconSpec",
    "read_beacon_register",
    "expected_component_names",
    "PARKED_COMPONENTS",
    "PARKED_ARTIFACTS",
    "PARKED_LAUNCHD_JOBS",
    "parked_component_names",
    "parked_artifact_reason",
    "parked_launchd_reason",
    "find_duplicate_beacon_writers",
]
