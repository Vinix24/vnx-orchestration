#!/usr/bin/env python3
"""Dispatch-time enforcement for provider_constraints.yaml."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

_CONSTRAINTS_PATH = Path(__file__).with_name("provider_constraints.yaml")


@dataclass(frozen=True)
class ConstraintViolation:
    code: str
    severity: str
    message: str
    override_applied: bool = False


class ConstraintViolationError(RuntimeError):
    """Raised when a blocking provider constraint is violated."""

    def __init__(self, violation: ConstraintViolation) -> None:
        self.violation = violation
        self.code = violation.code
        self.severity = violation.severity
        self.message = violation.message
        super().__init__(f"[{violation.code}] {violation.message}")


HardConstraintViolation = ConstraintViolationError


def _load_yaml(path: Path) -> dict[str, Any]:
    import yaml  # noqa: PLC0415

    if not path.is_file():
        raise FileNotFoundError(f"Constraints file not found: {path}")
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Constraints file is not a YAML mapping: {path}")
    if data.get("version") != 1:
        raise ValueError(f"Unsupported constraints version: {data.get('version')}")
    return data


def _norm(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def _match_value(actual: Optional[str], spec: Any) -> bool:
    if actual is None:
        return False
    actual_norm = _norm(actual)
    if isinstance(spec, list):
        return actual_norm in {_norm(str(item)) for item in spec}
    return actual_norm == _norm(str(spec))


def _override_key(code: str) -> str:
    return "VNX_OVERRIDE_" + code.upper().replace("-", "_")


def _provider_parts(provider: Optional[str], sub_provider: Optional[str]) -> tuple[str, Optional[str]]:
    raw = provider or ""
    if raw.startswith("litellm:"):
        parts = raw.split(":", 2)
        return "litellm", parts[1] if len(parts) > 1 and parts[1] else sub_provider
    return raw, sub_provider


_NATIVE_CLI_PROVIDERS = frozenset({"claude", "codex", "gemini", "kimi"})


def _effective_provider(provider: Optional[str], sub_provider: Optional[str]) -> Optional[str]:
    base, sub = _provider_parts(provider, sub_provider)
    if _norm(base) in _NATIVE_CLI_PROVIDERS:
        return base
    return sub or base


def _route_forbidden(
    constraint: Mapping[str, Any],
    provider: Optional[str],
    sub_provider: Optional[str],
    model: Optional[str],
    via: Optional[str],
) -> bool:
    forbidden = constraint.get("forbidden_route") or {}
    if not isinstance(forbidden, Mapping):
        return False

    spec_provider = forbidden.get("provider")
    if spec_provider:
        if not _match_value(_effective_provider(provider, sub_provider), spec_provider):
            return False

    spec_model = forbidden.get("model")
    if spec_model and not _model_matches(model, spec_model):
        return False

    spec_via = forbidden.get("via")
    if spec_via and not _match_value(via, spec_via):
        return False

    return True


def _required_route_missing(
    constraint: Mapping[str, Any],
    model: Optional[str],
    terminal_id: Optional[str],
    role: Optional[str],
) -> bool:
    required = constraint.get("required_route") or {}
    if not isinstance(required, Mapping):
        return False

    spec_role = required.get("role")
    if spec_role:
        effective_role = terminal_id or role
        if not _match_value(effective_role, spec_role):
            return False

    spec_model = required.get("model")
    if spec_model and not _model_matches(model, spec_model):
        return True

    return False


def _registry_key_for(provider: Optional[str], sub_provider: Optional[str]) -> Optional[str]:
    base, sub = _provider_parts(provider, sub_provider)
    base_norm = _norm(base)
    sub_norm = _norm(sub)
    if base_norm == "claude":
        return "anthropic"
    if base_norm == "codex":
        return "openai"
    if base_norm == "gemini":
        return "google"
    if base_norm == "kimi":
        return "kimi_cli"
    if base_norm == "litellm":
        return sub_norm or None
    return base_norm.replace("-", "_") or None


def _load_registry() -> dict[str, Any]:
    from providers import provider_registry  # noqa: PLC0415

    return provider_registry.load()


def _model_aliases(model: Optional[str]) -> set[str]:
    raw = _norm(model)
    if not raw:
        return set()
    aliases = {raw}
    if "/" in raw:
        aliases.add(raw.rsplit("/", 1)[-1])
    return aliases


def _registry_model_aliases(model: Optional[str]) -> set[str]:
    aliases = _model_aliases(model)
    if not aliases:
        return aliases
    try:
        registry = _load_registry()
    except Exception:
        return aliases
    for cfg in registry.values():
        for key, entry in (cfg.models or {}).items():
            values = {_norm(key), *_model_aliases(getattr(entry, "litellm_name", ""))}
            if aliases & values:
                aliases |= values
    return aliases


def _model_matches(model: Optional[str], spec: Any) -> bool:
    if isinstance(spec, list):
        return any(_model_matches(model, item) for item in spec)
    if model is None:
        return False
    return bool(_registry_model_aliases(model) & _registry_model_aliases(str(spec)))


def _model_in_registry(provider: Optional[str], sub_provider: Optional[str], model: Optional[str]) -> bool:
    if not model:
        return True
    registry_key = _registry_key_for(provider, sub_provider)
    if not registry_key:
        return True
    registry = _load_registry()
    cfg = registry.get(registry_key)
    if cfg is None or not cfg.enabled or not cfg.models:
        return False
    requested = _model_aliases(model)
    for key, entry in cfg.models.items():
        aliases = {_norm(key), *_model_aliases(getattr(entry, "litellm_name", ""))}
        if requested & aliases:
            return True
    return False


def _scan_anthropic_sdk_references(
    constraint: Mapping[str, Any],
    instruction_text: Optional[str],
    env: Mapping[str, str],
) -> bool:
    forbidden = constraint.get("forbidden_import") or {}
    patterns = forbidden.get("patterns") if isinstance(forbidden, Mapping) else None
    if not isinstance(patterns, list):
        return False
    env_fragments = [
        env.get("VNX_WORKER_ENV", ""),
        env.get("WORKER_ENV", ""),
        env.get("VNX_PROVIDER_ENV", ""),
        env.get("VNX_INSTRUCTION", ""),
    ]
    haystack = "\n".join([instruction_text or "", *env_fragments]).lower()
    return any(str(pattern).lower() in haystack for pattern in patterns)


def _violation_from_constraint(
    constraint: Mapping[str, Any],
    prefix: str,
    *,
    severity_override: Optional[str] = None,
    message_override: Optional[str] = None,
    override_applied: bool = False,
) -> ConstraintViolation:
    code = str(constraint.get("id") or constraint.get("code") or "unknown")
    severity = severity_override or str(constraint.get("audit_severity") or "info")
    reason = message_override or str(constraint.get("message") or constraint.get("reason") or "no reason given")
    return ConstraintViolation(
        code=code,
        severity=severity,
        message=f"{prefix}: {reason}",
        override_applied=override_applied,
    )


class ConstraintEnforcer:
    """Loads and evaluates provider dispatch constraints."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or _CONSTRAINTS_PATH
        self._constraints: list[dict[str, Any]] = []
        self.load_constraints()

    def load_constraints(self) -> None:
        data = _load_yaml(self._path)
        constraints = data.get("constraints", [])
        if not isinstance(constraints, list):
            raise ValueError("constraints key must be a list")
        self._constraints = constraints

    def check_constraints(
        self,
        *,
        provider: Optional[str] = None,
        sub_provider: Optional[str] = None,
        model: Optional[str] = None,
        terminal_id: Optional[str] = None,
        role: Optional[str] = None,
        via: Optional[str] = None,
        instruction_text: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
        check_registry: bool = False,
    ) -> list[ConstraintViolation]:
        env_map = env or os.environ
        violations: list[ConstraintViolation] = []

        for constraint in self._constraints:
            rule = constraint.get("rule")
            code = str(constraint.get("id") or constraint.get("code") or "unknown")
            override_allowed = bool(constraint.get("override_allowed", False))
            override_applied = (
                str(constraint.get("audit_severity") or "") == "warn"
                and override_allowed
                and env_map.get(_override_key(code)) == "1"
            )

            violation: Optional[ConstraintViolation] = None
            if rule == "forbid_import":
                if _scan_anthropic_sdk_references(constraint, instruction_text, env_map):
                    violation = _violation_from_constraint(
                        constraint,
                        "Instruction references forbidden SDK usage",
                        severity_override="warn",
                        override_applied=override_applied,
                    )
            elif rule == "forbid_route":
                if _route_forbidden(constraint, provider, sub_provider, model, via):
                    violation = _violation_from_constraint(
                        constraint,
                        "Route forbidden",
                        override_applied=override_applied,
                    )
            elif rule == "require_route":
                if _required_route_missing(constraint, model, terminal_id, role):
                    violation = _violation_from_constraint(
                        constraint,
                        "Required route not met",
                        override_applied=override_applied,
                    )

            if violation is not None:
                violations.append(violation)

        base, sub = _provider_parts(provider, sub_provider)
        effective = _effective_provider(base, sub)
        model_norm = _norm(model)
        if _norm(base) != "kimi" and "kimi" in model_norm:
            violations.append(ConstraintViolation(
                code="kimi-via-cli-only",
                severity="blocking",
                message="Route forbidden: Kimi models must use provider 'kimi' via the CLI lane.",
            ))

        if _norm(base) == "litellm" and _norm(sub) == "deepseek" and not env_map.get("DEEPSEEK_API_KEY"):
            violations.append(ConstraintViolation(
                code="deepseek-harness-subscription-blocked",
                severity="blocking",
                message="Route forbidden: litellm:deepseek requires DEEPSEEK_API_KEY; subscription redirect is blocked.",
            ))

        if _norm(effective) == "zai" and model_norm in {"glm-4.5", "glm-4.6"}:
            violations.append(ConstraintViolation(
                code="deprecated-glm-models",
                severity="blocking",
                message=f"Route forbidden: {model} is deprecated; use glm-5.1.",
            ))

        if check_registry and model and not _model_in_registry(base, sub, model):
            registry_key = _registry_key_for(base, sub) or "unknown"
            violations.append(ConstraintViolation(
                code="model-not-in-current-registry",
                severity="blocking",
                message=(
                    f"Route forbidden: model {model!r} is not registered for "
                    f"provider {provider!r} (registry key {registry_key!r})."
                ),
            ))

        return violations

    def enforce(self, **kwargs: Any) -> list[ConstraintViolation]:
        violations = self.check_constraints(**kwargs)
        for violation in violations:
            if violation.severity == "blocking":
                raise ConstraintViolationError(violation)
            if violation.override_applied:
                logger.warning("[%s] warning overridden by env flag: %s", violation.code, violation.message)
            else:
                logger.warning("[%s] %s", violation.code, violation.message)
        return violations


_enforcer: Optional[ConstraintEnforcer] = None


def _get_enforcer() -> ConstraintEnforcer:
    global _enforcer  # noqa: PLW0603
    if _enforcer is None:
        _enforcer = ConstraintEnforcer()
    return _enforcer


def check_constraints(**kwargs: Any) -> list[ConstraintViolation]:
    return _get_enforcer().check_constraints(**kwargs)


def enforce(**kwargs: Any) -> list[ConstraintViolation]:
    return _get_enforcer().enforce(**kwargs)
