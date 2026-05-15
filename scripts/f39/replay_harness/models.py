from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ReplayResult:
    scenario_name: str
    expected_decision: str
    actual_decision: str
    match: bool
    reason_match: bool
    actual_output: str
    token_cost: int
    duration_ms: int
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_name": self.scenario_name,
            "expected_decision": self.expected_decision,
            "actual_decision": self.actual_decision,
            "match": self.match,
            "reason_match": self.reason_match,
            "token_cost": self.token_cost,
            "duration_ms": self.duration_ms,
            "errors": self.errors,
            "actual_output_excerpt": self.actual_output[:500],
        }


@dataclass
class ChainStep:
    step_name: str
    receipt: dict[str, Any]
    state_delta: dict[str, Any]
    expected_decision: str
    expected_next_action: str


@dataclass
class ChainScenario:
    name: str
    level: int
    description: str
    initial_state: dict[str, Any]
    steps: list[ChainStep]


@dataclass
class ChainStepResult:
    step_name: str
    expected_decision: str
    actual_decision: str
    match: bool
    actual_output: str
    token_cost: int
    duration_ms: int
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_name": self.step_name,
            "expected_decision": self.expected_decision,
            "actual_decision": self.actual_decision,
            "match": self.match,
            "token_cost": self.token_cost,
            "duration_ms": self.duration_ms,
            "errors": self.errors,
            "actual_output_excerpt": self.actual_output[:300],
        }


@dataclass
class ChainReplayResult:
    scenario_name: str
    level: int
    steps: list[ChainStepResult]
    all_steps_pass: bool
    step_accuracy: float
    total_token_cost: int
    total_duration_ms: int
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_name": self.scenario_name,
            "level": self.level,
            "all_steps_pass": self.all_steps_pass,
            "step_accuracy": self.step_accuracy,
            "total_token_cost": self.total_token_cost,
            "total_duration_ms": self.total_duration_ms,
            "errors": self.errors,
            "steps": [s.to_dict() for s in self.steps],
        }
