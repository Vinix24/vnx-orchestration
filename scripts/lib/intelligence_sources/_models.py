"""
InjectionResult, IntelligenceContext, and markdown formatters.

Split from intelligence_selector.py (2511 LOC → per-source modules).
Re-exported via intelligence_selector for backward compatibility.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List

from ._common import IntelligenceItem, SuppressionRecord


def _format_items_markdown(items: List[IntelligenceItem]) -> str:
    """Group items by class and render as full markdown sections."""
    by_class: Dict[str, List[IntelligenceItem]] = {}
    for item in items:
        by_class.setdefault(item.item_class, []).append(item)
    parts: List[str] = []
    if "failure_prevention" in by_class:
        parts.append("### Antipatterns to avoid")
        for item in by_class["failure_prevention"]:
            parts.append(f"- **[CRITICAL] {item.title}**: {item.content}")
        parts.append("")
    if "proven_pattern" in by_class:
        parts.append("### Proven success patterns")
        for item in by_class["proven_pattern"]:
            parts.append(f"- **{item.title}**: {item.content}")
        parts.append("")
    if "recent_comparable" in by_class:
        parts.append("### Tag warnings")
        for item in by_class["recent_comparable"]:
            parts.append(f"- **{item.title}**: {item.content}")
        parts.append("")
    return "\n".join(parts)


def _format_items_compact(items: List[IntelligenceItem]) -> str:
    """Compact numbered format for providers where brevity is preferred."""
    lines: List[str] = ["## Intelligence Context"]
    for i, item in enumerate(items, 1):
        cls = item.item_class.replace("_", " ").title()
        lines.append(f"{i}. [{cls}] **{item.title}**: {item.content}")
    return "\n".join(lines)


@dataclass
class InjectionResult:
    """Complete result of an intelligence selection run."""
    injection_point: str
    injected_at: str
    items: List[IntelligenceItem]
    suppressed: List[SuppressionRecord]
    task_class: str
    dispatch_id: str

    @property
    def items_injected(self) -> int:
        return len(self.items)

    @property
    def items_suppressed(self) -> int:
        return len(self.suppressed)

    @property
    def payload_chars(self) -> int:
        return len(json.dumps(self.to_payload_dict()))

    def to_payload_dict(self) -> Dict[str, Any]:
        return {
            "injection_point": self.injection_point,
            "injected_at": self.injected_at,
            "items": [item.to_dict() for item in self.items],
            "suppressed": [s.to_dict() for s in self.suppressed],
        }

    def to_event_metadata(self) -> Dict[str, Any]:
        return {
            "injection_point": self.injection_point,
            "task_class": self.task_class,
            "items_injected": self.items_injected,
            "items_suppressed": self.items_suppressed,
            "suppression_reasons": [s.reason for s in self.suppressed],
            "payload_chars": self.payload_chars,
            "item_ids": [item.item_id for item in self.items],
        }


@dataclass
class IntelligenceContext:
    """Provider-agnostic container for an intelligence injection result."""
    result: InjectionResult
    dispatch_id: str

    def serialize_for(self, provider: str) -> str:
        """Return provider-specific markdown for the prompt intelligence section."""
        if not self.result.items:
            return ""
        if provider == "codex":
            return _format_items_compact(self.result.items)
        return _format_items_markdown(self.result.items)
