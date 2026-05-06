"""Strategy / Layer-1 state package.

Public API:
    Phase, Wave, OperatorDecision, Roadmap dataclasses
    load_roadmap, write_roadmap, validate_roadmap
    next_actionable_wave, dependents_of, phase_complete
    RoadmapValidationError
    DocEntry, DocStatus, load_prd_index, load_adr_index
"""
from __future__ import annotations

from .doc_indexes import (
    DocEntry,
    DocStatus,
    load_adr_index,
    load_prd_index,
)
from .roadmap import (
    OperatorDecision,
    Phase,
    Roadmap,
    RoadmapValidationError,
    Wave,
    dependents_of,
    load_roadmap,
    next_actionable_wave,
    phase_complete,
    validate_roadmap,
    write_roadmap,
)

__all__ = [
    "DocEntry",
    "DocStatus",
    "OperatorDecision",
    "Phase",
    "Roadmap",
    "RoadmapValidationError",
    "Wave",
    "dependents_of",
    "load_adr_index",
    "load_prd_index",
    "load_roadmap",
    "next_actionable_wave",
    "phase_complete",
    "validate_roadmap",
    "write_roadmap",
]
