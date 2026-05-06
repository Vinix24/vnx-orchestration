"""Typed roadmap.yaml loader, validator, writer, and derived helpers.

Layer 1 of the strategic-state design (PROJECT_STATE_DESIGN.md).
This module is the single source of truth for roadmap dataclass types;
all downstream tooling (decisions log, current_state.md projector,
build_t0_state extension) binds to these types.

Mutation discipline: never write the YAML by hand from Python — go
through ``write_roadmap`` so the schema is enforced and the file is
emitted with a stable layout.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from . import _yaml_io

WAVE_STATUSES = ("planned", "in_progress", "completed", "deferred", "cancelled")
RISK_CLASSES = ("low", "medium", "high", "critical")
DECISION_STATUSES = ("open", "closed")

# Strict decision-id reference (operator: od_<n>, T0: td_<n>). Anything else
# in `blocked_on` is treated as a free-form external label (e.g. quota labels)
# and is not subject to dangling-reference checks.
_DECISION_ID_RE = re.compile(r"^(od|td)_\d+$")

_DEFAULT_RELATIVE_PATH = Path(".vnx-data/strategy/roadmap.yaml")


class RoadmapValidationError(ValueError):
    """Raised when roadmap.yaml violates the schema at load or write time.

    Accepts either a single message string (parse-time errors) or a list of
    error messages (cross-reference validation). The ``errors`` attribute is
    always a list for programmatic access.
    """

    def __init__(self, errors):
        if isinstance(errors, list):
            self.errors = list(errors)
            joined = "\n  - ".join(self.errors) if self.errors else "(no details)"
            message = f"roadmap validation failed:\n  - {joined}"
        else:
            self.errors = [str(errors)]
            message = str(errors)
        super().__init__(message)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Phase:
    phase_id: int
    title: str
    waves: list[str] = field(default_factory=list)
    estimated_loc: int = 0
    estimated_weeks: float = 0.0
    blocked_on: list[str] = field(default_factory=list)
    rationale: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class Wave:
    wave_id: str
    title: str
    phase_id: int
    status: str
    estimated_loc: int | None = None
    branch_name: str | None = None
    review_stack: list[str] = field(default_factory=list)
    risk_class: str = "low"
    depends_on: list[str] = field(default_factory=list)
    blocked_on: list[str] = field(default_factory=list)
    pr: int | None = None
    pr_number: int | None = None
    completed_at: str | None = None
    plan_path: str | None = None
    notes: str | None = None
    rationale: str | None = None


@dataclass(frozen=True)
class OperatorDecision:
    decision_id: str
    title: str
    status: str
    recommendation: str | None = None
    decision: str | None = None
    rationale: str | None = None
    closed_at: str | None = None
    blocking_waves: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Roadmap:
    schema_version: int
    roadmap_id: str
    title: str
    generated_at: str
    phases: list[Phase] = field(default_factory=list)
    waves: list[Wave] = field(default_factory=list)
    operator_decisions: list[OperatorDecision] = field(default_factory=list)
    completed_history: list[dict] = field(default_factory=list)
    notes: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------
def _git_root_from(start: Path) -> Path | None:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return Path(out).resolve() if out else None
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None


def _default_path() -> Path:
    """Resolve the default roadmap.yaml path against the project root."""
    here = Path(__file__).resolve().parent
    root = _git_root_from(here) or _git_root_from(Path.cwd().resolve())
    if root is None:
        env_root = os.environ.get("VNX_CANONICAL_ROOT")
        if env_root:
            root = Path(env_root).resolve()
        else:
            root = Path.cwd().resolve()
    return root / _DEFAULT_RELATIVE_PATH


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------
def _require(d: dict, key: str, where: str) -> Any:
    if key not in d:
        raise RoadmapValidationError(f"{where}: missing required key '{key}'")
    return d[key]


def _as_list(value: Any, where: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise RoadmapValidationError(f"{where}: expected list, got {type(value).__name__}")
    return list(value)


def _parse_phase(raw: dict) -> Phase:
    if not isinstance(raw, dict):
        raise RoadmapValidationError(f"phase entry must be a mapping, got {type(raw).__name__}")
    where = f"phase[{raw.get('phase_id', '?')}]"
    return Phase(
        phase_id=int(_require(raw, "phase_id", where)),
        title=str(_require(raw, "title", where)),
        waves=[str(w) for w in _as_list(raw.get("waves"), f"{where}.waves")],
        estimated_loc=int(raw.get("estimated_loc") or 0),
        estimated_weeks=float(raw.get("estimated_weeks") or 0.0),
        blocked_on=[str(b) for b in _as_list(raw.get("blocked_on"), f"{where}.blocked_on")],
        rationale=raw.get("rationale"),
        notes=raw.get("notes"),
    )


def _parse_wave(raw: dict) -> Wave:
    if not isinstance(raw, dict):
        raise RoadmapValidationError(f"wave entry must be a mapping, got {type(raw).__name__}")
    where = f"wave[{raw.get('wave_id', '?')}]"
    status = str(_require(raw, "status", where))
    if status not in WAVE_STATUSES:
        raise RoadmapValidationError(
            f"{where}: invalid status '{status}' (allowed: {', '.join(WAVE_STATUSES)})"
        )
    risk_class = str(raw.get("risk_class", "low"))
    if risk_class not in RISK_CLASSES:
        raise RoadmapValidationError(
            f"{where}: invalid risk_class '{risk_class}' (allowed: {', '.join(RISK_CLASSES)})"
        )
    notes_value = raw.get("notes")
    if notes_value is not None and not isinstance(notes_value, str):
        notes_value = str(notes_value)
    return Wave(
        wave_id=str(_require(raw, "wave_id", where)),
        title=str(_require(raw, "title", where)),
        phase_id=int(_require(raw, "phase_id", where)),
        status=status,
        estimated_loc=(int(raw["estimated_loc"]) if raw.get("estimated_loc") is not None else None),
        branch_name=raw.get("branch_name"),
        review_stack=[str(r) for r in _as_list(raw.get("review_stack"), f"{where}.review_stack")],
        risk_class=risk_class,
        depends_on=[str(d) for d in _as_list(raw.get("depends_on"), f"{where}.depends_on")],
        blocked_on=[str(b) for b in _as_list(raw.get("blocked_on"), f"{where}.blocked_on")],
        pr=(int(raw["pr"]) if raw.get("pr") is not None else None),
        pr_number=(int(raw["pr_number"]) if raw.get("pr_number") is not None else None),
        completed_at=(str(raw["completed_at"]) if raw.get("completed_at") is not None else None),
        plan_path=raw.get("plan_path"),
        notes=notes_value,
        rationale=raw.get("rationale"),
    )


def _parse_decision(raw: dict) -> OperatorDecision:
    if not isinstance(raw, dict):
        raise RoadmapValidationError(
            f"operator_decisions entry must be a mapping, got {type(raw).__name__}"
        )
    where = f"operator_decisions[{raw.get('decision_id', '?')}]"
    status = str(_require(raw, "status", where))
    if status not in DECISION_STATUSES:
        raise RoadmapValidationError(
            f"{where}: invalid status '{status}' (allowed: {', '.join(DECISION_STATUSES)})"
        )
    return OperatorDecision(
        decision_id=str(_require(raw, "decision_id", where)),
        title=str(_require(raw, "title", where)),
        status=status,
        recommendation=raw.get("recommendation"),
        decision=raw.get("decision"),
        rationale=raw.get("rationale"),
        closed_at=(str(raw["closed_at"]) if raw.get("closed_at") is not None else None),
        blocking_waves=[
            str(w) for w in _as_list(raw.get("blocking_waves"), f"{where}.blocking_waves")
        ],
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def load_roadmap(path: Path | None = None, *, strict: bool = True) -> Roadmap:
    """Strict reader. Returns a typed Roadmap; raises on schema violations.

    Behavior:
      - ``schema_version`` is enforced to equal ``1``. Unsupported versions
        raise ``RoadmapValidationError`` immediately. The default value when
        absent is ``1`` (backwards-compat shim).
      - When ``strict=True`` (the default) the loader runs
        ``validate_roadmap()`` after parsing and raises
        ``RoadmapValidationError`` if any cross-reference errors are found
        (duplicate wave_ids, dangling depends_on/blocked_on, etc.).
      - When ``strict=False`` the loader returns the parsed Roadmap without
        running cross-reference validation. Use this when you want to
        inspect a possibly-invalid roadmap (tooling that reports errors).
    """
    target = Path(path) if path is not None else _default_path()
    if not target.exists():
        raise RoadmapValidationError(f"roadmap file not found: {target}")
    data = _yaml_io.load_yaml(target)
    if data is None:
        raise RoadmapValidationError(f"roadmap file is empty: {target}")
    if not isinstance(data, dict):
        raise RoadmapValidationError(
            f"roadmap root must be a mapping, got {type(data).__name__}"
        )

    schema_version = int(data.get("schema_version", 1))
    if schema_version != 1:
        raise RoadmapValidationError(
            [f"Unsupported schema_version: {schema_version} (expected 1)"]
        )
    roadmap_id = str(data.get("roadmap_id", ""))
    title = str(data.get("title", ""))
    generated_at = str(data.get("generated_at", ""))

    phases_raw = _as_list(data.get("phases"), "phases")
    waves_raw = _as_list(data.get("waves"), "waves")
    decisions_raw = _as_list(data.get("operator_decisions"), "operator_decisions")
    completed_history_raw = _as_list(data.get("completed_history"), "completed_history")
    notes_raw = data.get("notes") or {}
    if not isinstance(notes_raw, dict):
        raise RoadmapValidationError(
            f"notes must be a mapping, got {type(notes_raw).__name__}"
        )

    phases = [_parse_phase(p) for p in phases_raw]
    waves = [_parse_wave(w) for w in waves_raw]
    decisions = [_parse_decision(d) for d in decisions_raw]
    completed_history = [
        dict(entry) if isinstance(entry, dict) else {"value": entry}
        for entry in completed_history_raw
    ]

    roadmap = Roadmap(
        schema_version=schema_version,
        roadmap_id=roadmap_id,
        title=title,
        generated_at=generated_at,
        phases=phases,
        waves=waves,
        operator_decisions=decisions,
        completed_history=completed_history,
        notes=dict(notes_raw),
    )

    if strict:
        errors = validate_roadmap(roadmap)
        if errors:
            raise RoadmapValidationError(errors)

    return roadmap


def _wave_to_dict(w: Wave) -> dict:
    """Serialize a Wave omitting None / empty-list fields for clean YAML output."""
    raw: dict[str, Any] = {
        "wave_id": w.wave_id,
        "title": w.title,
        "phase_id": w.phase_id,
        "status": w.status,
    }
    optional_scalars = (
        "estimated_loc",
        "branch_name",
        "pr",
        "pr_number",
        "completed_at",
        "plan_path",
        "notes",
        "rationale",
    )
    for key in optional_scalars:
        value = getattr(w, key)
        if value is not None:
            raw[key] = value
    if w.review_stack:
        raw["review_stack"] = list(w.review_stack)
    raw["risk_class"] = w.risk_class
    if w.depends_on:
        raw["depends_on"] = list(w.depends_on)
    if w.blocked_on:
        raw["blocked_on"] = list(w.blocked_on)
    return raw


def _phase_to_dict(p: Phase) -> dict:
    raw: dict[str, Any] = {
        "phase_id": p.phase_id,
        "title": p.title,
        "waves": list(p.waves),
        "estimated_loc": p.estimated_loc,
        "estimated_weeks": p.estimated_weeks,
        "blocked_on": list(p.blocked_on),
    }
    if p.rationale is not None:
        raw["rationale"] = p.rationale
    if p.notes is not None:
        raw["notes"] = p.notes
    return raw


def _decision_to_dict(d: OperatorDecision) -> dict:
    raw: dict[str, Any] = {
        "decision_id": d.decision_id,
        "title": d.title,
        "status": d.status,
    }
    if d.recommendation is not None:
        raw["recommendation"] = d.recommendation
    if d.decision is not None:
        raw["decision"] = d.decision
    if d.rationale is not None:
        raw["rationale"] = d.rationale
    if d.closed_at is not None:
        raw["closed_at"] = d.closed_at
    if d.blocking_waves:
        raw["blocking_waves"] = list(d.blocking_waves)
    return raw


def write_roadmap(roadmap: Roadmap, path: Path | None = None) -> None:
    """Structured writer. Validates the roadmap before persisting.

    Raises ``RoadmapValidationError`` if cross-reference validation fails or
    if ``schema_version`` is not the supported value (``1``). The target file
    is left untouched on failure: validation runs before any I/O.

    Comments are not preserved with the PyYAML fallback; install
    ``ruamel.yaml`` to round-trip comments.
    """
    if roadmap.schema_version != 1:
        raise RoadmapValidationError(
            [f"Unsupported schema_version: {roadmap.schema_version} (expected 1)"]
        )
    errors = validate_roadmap(roadmap)
    if errors:
        raise RoadmapValidationError(errors)

    target = Path(path) if path is not None else _default_path()
    payload: dict[str, Any] = {
        "schema_version": roadmap.schema_version,
        "roadmap_id": roadmap.roadmap_id,
        "title": roadmap.title,
        "generated_at": roadmap.generated_at,
        "phases": [_phase_to_dict(p) for p in roadmap.phases],
        "waves": [_wave_to_dict(w) for w in roadmap.waves],
        "operator_decisions": [_decision_to_dict(d) for d in roadmap.operator_decisions],
        "completed_history": [dict(entry) for entry in roadmap.completed_history],
        "notes": dict(roadmap.notes),
    }
    _yaml_io.dump_yaml(payload, target)


def validate_roadmap(roadmap: Roadmap) -> list[str]:
    """Return a list of validation error messages; empty list means valid.

    Catches:
      - missing schema_version (warning; default applied at load time)
      - dangling depends_on (wave_id referenced but not defined)
      - dangling blocked_on (decision_id referenced but not defined)
      - status enum violations (also caught at parse time, double-checked here)
      - duplicate wave_ids
      - duplicate decision_ids
      - phase.waves referencing undefined waves
      - wave.phase_id referencing undefined phase
    """
    errors: list[str] = []

    if roadmap.schema_version is None:  # type: ignore[redundant-expr]
        errors.append("warning: schema_version missing (defaulted to 1)")

    wave_ids = [w.wave_id for w in roadmap.waves]
    seen: set[str] = set()
    for wid in wave_ids:
        if wid in seen:
            errors.append(f"duplicate wave_id: {wid}")
        seen.add(wid)
    wave_id_set = set(wave_ids)

    decision_ids = [d.decision_id for d in roadmap.operator_decisions]
    seen_dec: set[str] = set()
    for did in decision_ids:
        if did in seen_dec:
            errors.append(f"duplicate decision_id: {did}")
        seen_dec.add(did)
    decision_id_set = set(decision_ids)

    phase_id_set = {p.phase_id for p in roadmap.phases}

    for w in roadmap.waves:
        if w.status not in WAVE_STATUSES:
            errors.append(f"wave {w.wave_id}: invalid status '{w.status}'")
        if w.risk_class not in RISK_CLASSES:
            errors.append(f"wave {w.wave_id}: invalid risk_class '{w.risk_class}'")
        if w.phase_id not in phase_id_set:
            errors.append(
                f"wave {w.wave_id}: phase_id {w.phase_id} not present in phases list"
            )
        for dep in w.depends_on:
            if dep not in wave_id_set:
                errors.append(
                    f"wave {w.wave_id}: dangling depends_on '{dep}' (no such wave)"
                )
        for blk in w.blocked_on:
            if blk in decision_id_set or blk in wave_id_set:
                continue
            if _DECISION_ID_RE.match(blk):
                # Looks like a canonical decision_id but isn't defined.
                errors.append(
                    f"wave {w.wave_id}: dangling blocked_on '{blk}' "
                    f"(decision_id not defined in operator_decisions)"
                )
            # Otherwise free-form external label (e.g. 'gemini_quota_recovery'); ignore.

    for d in roadmap.operator_decisions:
        if d.status not in DECISION_STATUSES:
            errors.append(f"decision {d.decision_id}: invalid status '{d.status}'")
        for w_ref in d.blocking_waves:
            if w_ref not in wave_id_set:
                errors.append(
                    f"decision {d.decision_id}: blocking_waves references "
                    f"undefined wave '{w_ref}'"
                )

    for p in roadmap.phases:
        for w_ref in p.waves:
            if w_ref not in wave_id_set:
                errors.append(
                    f"phase {p.phase_id}: waves list references undefined wave '{w_ref}'"
                )

    return errors


# ---------------------------------------------------------------------------
# Derived helpers
# ---------------------------------------------------------------------------
def next_actionable_wave(roadmap: Roadmap) -> Wave | None:
    """Return the first wave that is ready to start.

    A wave is ready when:
      - status == 'planned'
      - every depends_on wave has status == 'completed'
      - every blocked_on operator_decision has status == 'closed'
        (entries in blocked_on that match a wave_id must be 'completed')

    Order: roadmap.waves list order — deterministic by definition.
    """
    wave_status = {w.wave_id: w.status for w in roadmap.waves}
    decision_status = {d.decision_id: d.status for d in roadmap.operator_decisions}

    for w in roadmap.waves:
        if w.status != "planned":
            continue
        deps_ok = all(wave_status.get(dep) == "completed" for dep in w.depends_on)
        if not deps_ok:
            continue
        blocks_ok = True
        for blk in w.blocked_on:
            if blk in decision_status:
                if decision_status[blk] != "closed":
                    blocks_ok = False
                    break
            elif blk in wave_status:
                if wave_status[blk] != "completed":
                    blocks_ok = False
                    break
            else:
                # Unknown reference — treat as still blocking; validate_roadmap
                # will surface it as a dangling reference.
                blocks_ok = False
                break
        if not blocks_ok:
            continue
        return w
    return None


def dependents_of(wave_id: str, roadmap: Roadmap) -> list[Wave]:
    """Return waves whose depends_on includes ``wave_id``."""
    return [w for w in roadmap.waves if wave_id in w.depends_on]


def phase_complete(phase_id: int, roadmap: Roadmap) -> bool:
    """True iff every wave assigned to ``phase_id`` is completed."""
    waves_in_phase = [w for w in roadmap.waves if w.phase_id == phase_id]
    if not waves_in_phase:
        return False
    return all(w.status == "completed" for w in waves_in_phase)


# ---------------------------------------------------------------------------
# Test/utility helpers (re-exported)
# ---------------------------------------------------------------------------
def roadmap_to_dict(roadmap: Roadmap) -> dict:
    """Serialize Roadmap to plain dict — used by tests / debugging only."""
    return {
        "schema_version": roadmap.schema_version,
        "roadmap_id": roadmap.roadmap_id,
        "title": roadmap.title,
        "generated_at": roadmap.generated_at,
        "phases": [asdict(p) for p in roadmap.phases],
        "waves": [asdict(w) for w in roadmap.waves],
        "operator_decisions": [asdict(d) for d in roadmap.operator_decisions],
        "completed_history": [dict(e) for e in roadmap.completed_history],
        "notes": dict(roadmap.notes),
    }


__all__ = [
    "DECISION_STATUSES",
    "OperatorDecision",
    "Phase",
    "RISK_CLASSES",
    "Roadmap",
    "RoadmapValidationError",
    "WAVE_STATUSES",
    "Wave",
    "dependents_of",
    "load_roadmap",
    "next_actionable_wave",
    "phase_complete",
    "replace",
    "roadmap_to_dict",
    "validate_roadmap",
    "write_roadmap",
]
