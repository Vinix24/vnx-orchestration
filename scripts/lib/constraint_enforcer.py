#!/usr/bin/env python3
"""constraint_enforcer.py — Enforce provider_constraints.yaml guard-rails at dispatch time.

Loads the constraints SSOT from scripts/lib/providers/provider_constraints.yaml
and enforces forbid_route / require_route rules before any provider handler runs.
forbid_import rules are CI-only (grep-based) and skipped at runtime.

PR-SR-2: initial implementation.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_CONSTRAINTS_PATH = Path(__file__).parent / "providers" / "provider_constraints.yaml"


class HardConstraintViolation(RuntimeError):
    """Raised when a blocking constraint is violated at dispatch time."""

    def __init__(self, constraint_id: str, reason: str) -> None:
        self.constraint_id = constraint_id
        self.reason = reason
        super().__init__(f"[{constraint_id}] {reason}")


def _load_yaml(path: Path) -> Dict[str, Any]:
    """Load YAML file, raising FileNotFoundError or ValueError on problems."""
    import yaml  # noqa: PLC0415

    if not path.is_file():
        raise FileNotFoundError(f"Constraints file not found: {path}")
    with open(path) as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Constraints file is not a YAML mapping: {path}")
    if data.get("version") != 1:
        raise ValueError(f"Unsupported constraints version: {data.get('version')}")
    return data


class ConstraintEnforcer:
    """Loads and enforces provider constraints at dispatch pre-flight."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or _CONSTRAINTS_PATH
        self._constraints: List[Dict[str, Any]] = []
        self.load_constraints()

    def load_constraints(self) -> None:
        data = _load_yaml(self._path)
        self._constraints = data.get("constraints", [])
        if not isinstance(self._constraints, list):
            raise ValueError("constraints key must be a list")

    def _is_overridden(self, constraint: Dict[str, Any]) -> bool:
        if not constraint.get("override_allowed", False):
            return False
        env_key = "VNX_OVERRIDE_" + constraint["id"].upper().replace("-", "_")
        return os.environ.get(env_key) == "1"

    def _match_value(self, actual: Optional[str], spec: Any) -> bool:
        """Check if actual value matches a spec that can be str or list[str]."""
        if actual is None:
            return False
        actual_lower = (actual or "").lower()
        if isinstance(spec, list):
            return actual_lower in [s.lower() for s in spec]
        return actual_lower == str(spec).lower()

    def enforce(
        self,
        provider: Optional[str] = None,
        sub_provider: Optional[str] = None,
        model: Optional[str] = None,
        terminal_id: Optional[str] = None,
        role: Optional[str] = None,
        via: Optional[str] = None,
    ) -> None:
        """Check all constraints. Raises HardConstraintViolation or logs warning."""
        for c in self._constraints:
            rule = c.get("rule")
            cid = c.get("id", "unknown")
            enforcement = c.get("enforcement", "")
            severity = c.get("audit_severity", "info")

            if rule == "forbid_import":
                continue

            if rule == "forbid_route":
                if self._check_forbid_route(c, provider, sub_provider, model, via):
                    if self._is_overridden(c):
                        logger.warning("Constraint %s overridden by env var", cid)
                        continue
                    msg = f"Route forbidden: {c.get('reason', 'no reason given')}"
                    if severity == "blocking" or enforcement == "code_raise":
                        raise HardConstraintViolation(cid, msg)
                    logger.warning("[%s] %s", cid, msg)

            elif rule == "require_route":
                if self._check_require_route(c, model, terminal_id, role):
                    if self._is_overridden(c):
                        logger.warning("Constraint %s overridden by env var", cid)
                        continue
                    msg = f"Required route not met: {c.get('reason', 'no reason given')}"
                    if severity == "blocking" or enforcement == "code_raise":
                        raise HardConstraintViolation(cid, msg)
                    logger.warning("[%s] %s", cid, msg)

    def _check_forbid_route(
        self,
        constraint: Dict[str, Any],
        provider: Optional[str],
        sub_provider: Optional[str],
        model: Optional[str],
        via: Optional[str],
    ) -> bool:
        """Return True if the route matches the forbidden pattern."""
        fr = constraint.get("forbidden_route", {})
        if not fr:
            return False

        spec_provider = fr.get("provider")
        if spec_provider:
            effective_provider = sub_provider or provider
            if not self._match_value(effective_provider, spec_provider):
                return False

        spec_model = fr.get("model")
        if spec_model:
            if not self._match_value(model, spec_model):
                return False

        spec_via = fr.get("via")
        if spec_via:
            if not self._match_value(via, spec_via):
                return False

        return True

    def _check_require_route(
        self,
        constraint: Dict[str, Any],
        model: Optional[str],
        terminal_id: Optional[str],
        role: Optional[str],
    ) -> bool:
        """Return True if the constraint is violated (required route NOT met)."""
        rr = constraint.get("required_route", {})
        if not rr:
            return False

        spec_role = rr.get("role")
        if spec_role:
            effective_role = terminal_id or role
            if not self._match_value(effective_role, spec_role):
                return False

        spec_model = rr.get("model")
        if spec_model:
            if model is None or not self._match_value(model, spec_model):
                return True

        return False


_enforcer: Optional[ConstraintEnforcer] = None


def _get_enforcer() -> ConstraintEnforcer:
    global _enforcer  # noqa: PLW0603
    if _enforcer is None:
        _enforcer = ConstraintEnforcer()
    return _enforcer


def enforce(
    provider: Optional[str] = None,
    sub_provider: Optional[str] = None,
    model: Optional[str] = None,
    terminal_id: Optional[str] = None,
    role: Optional[str] = None,
    via: Optional[str] = None,
) -> None:
    """Module-level convenience: load constraints once, enforce on every call."""
    _get_enforcer().enforce(
        provider=provider,
        sub_provider=sub_provider,
        model=model,
        terminal_id=terminal_id,
        role=role,
        via=via,
    )
