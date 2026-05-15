"""routing_policy.py — Wave 7 cost-routing policy engine.

Reads routing_policy.yaml, maps (task_class, complexity, env-flags) to provider lane.
Stateless. Pure function. No subprocess interaction here — caller (subprocess_dispatch)
applies the lane decision.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

log = logging.getLogger(__name__)

# Canonical path for the policy file relative to this module.
_DEFAULT_POLICY_PATH = Path(__file__).parent / "providers" / "routing_policy.yaml"

# Maps lane prefix -> claude CLI model name for lanes that Claude can serve.
_CLAUDE_LANE_TO_MODEL: Dict[str, str] = {
    "claude/sonnet-4-6": "sonnet",
    "claude/haiku-4-5": "haiku",
    "claude/opus": "opus",
}


@dataclass
class RoutingDecision:
    lane: str                   # e.g. "claude/sonnet-4-6" or "litellm:deepseek:deepseek-v4-pro"
    rule_name: str              # matched rule name, "default" when no rule fired
    rationale: str
    fallback_chain: List[str] = field(default_factory=list)


def load_routing_policy(path: Path) -> dict:
    """Load policy yaml. Raises FileNotFoundError or ValueError on bad input."""
    if not path.exists():
        raise FileNotFoundError(f"routing_policy.yaml missing at {path}")
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        log.error("routing_policy: malformed yaml at %s: %s", path, exc)
        raise ValueError(f"malformed routing_policy.yaml: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"routing_policy.yaml must be a mapping, got {type(data).__name__}")
    version = data.get("version")
    if version != 1:
        raise ValueError(f"unsupported routing_policy version: {version!r}; expected 1")
    return data


def decide_lane(
    task_class: str,
    complexity: str = "medium",
    env: Optional[Dict[str, str]] = None,
    policy_path: Optional[Path] = None,
) -> RoutingDecision:
    """Apply policy rules in order. First match wins. Returns default lane on no match.

    Args:
        task_class: Dispatch task type (e.g. "refactor", "code-review", "lint-narrow").
        complexity: One of "low", "medium", "high". Defaults to "medium".
        env: Environment mapping for opt-in flag checks. Defaults to os.environ.
        policy_path: Override for the policy yaml location. Defaults to providers/routing_policy.yaml.

    Returns:
        RoutingDecision with lane, rule_name, rationale, and resolved fallback_chain.

    Raises:
        FileNotFoundError: when the policy yaml does not exist.
        ValueError: when the policy yaml is malformed or has an unsupported version.
    """
    if env is None:
        env = dict(os.environ)
    if policy_path is None:
        policy_path = _DEFAULT_POLICY_PATH

    policy = load_routing_policy(policy_path)
    default_lane: str = policy.get("default_lane", "claude/sonnet-4-6")
    fallback_map: dict = policy.get("fallback_chain", {})

    for rule in policy.get("rules", []):
        if _rule_matches(rule, task_class, complexity, env):
            lane = rule["lane"]
            return RoutingDecision(
                lane=lane,
                rule_name=rule.get("name", "unnamed"),
                rationale=rule.get("rationale", ""),
                fallback_chain=_resolve_fallback_chain(lane, fallback_map),
            )

    return RoutingDecision(
        lane=default_lane,
        rule_name="default",
        rationale="no rule matched; using default lane",
        fallback_chain=_resolve_fallback_chain(default_lane, fallback_map),
    )


def lane_to_claude_model(lane: str) -> Optional[str]:
    """Map a lane string to a claude CLI model name, or None for non-Claude lanes.

    Used by subprocess_dispatch to override --model when VNX_ROUTING_POLICY_ENABLED=1
    and the chosen lane is a Claude lane.  For litellm:* lanes, callers apply their
    own routing logic; this function returns None so they know no override is needed.
    """
    return _CLAUDE_LANE_TO_MODEL.get(lane)


def _rule_matches(rule: dict, task_class: str, complexity: str, env: dict) -> bool:
    when = rule.get("when", {})
    task_classes = when.get("task_class") or []
    if task_classes and task_class not in task_classes:
        return False
    complexities = when.get("complexity") or []
    if complexities and complexity not in complexities:
        return False
    opt_in_flag = when.get("opt_in_flag")
    if opt_in_flag and not env.get(opt_in_flag):
        return False
    return True


def _resolve_fallback_chain(lane: str, fallback_map: dict) -> List[str]:
    """Resolve fallback chain for a lane, supporting wildcard patterns (prefix:*)."""
    if lane in fallback_map:
        return list(fallback_map[lane])
    for pattern, chain in fallback_map.items():
        if pattern.endswith(":*"):
            prefix = pattern[:-2]
            if lane.startswith(prefix):
                return list(chain)
    return []
