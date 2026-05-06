"""Strategy / Layer-1 state package.

Public API:
    Phase, Wave, OperatorDecision, Roadmap dataclasses
    load_roadmap, write_roadmap, validate_roadmap
    next_actionable_wave, dependents_of, phase_complete
    RoadmapValidationError
"""
from __future__ import annotations

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
    "OperatorDecision",
    "Phase",
    "Roadmap",
    "RoadmapValidationError",
    "Wave",
    "dependents_of",
    "load_roadmap",
    "next_actionable_wave",
    "phase_complete",
    "validate_roadmap",
    "write_roadmap",
]
