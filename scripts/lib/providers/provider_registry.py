"""provider_registry.py — Wave 7 model registry loader.

Reads wave7_models.yaml and exposes typed records for provider dispatch
and cost-routing. Used by provider_dispatch.py to resolve model names
without hardcoded strings.

BILLING SAFETY: read-only data loader; no Anthropic SDK, no API calls.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

log = logging.getLogger(__name__)

_REGISTRY_PATH = Path(__file__).parent / "wave7_models.yaml"


@dataclass
class ProviderModel:
    litellm_name: str
    cost_input_per_mtok: float
    cost_output_per_mtok: float
    max_tokens: int
    supports_streaming: bool
    supports_tool_calls: bool
    context_window: Optional[int] = None
    task_classes: List[str] = field(default_factory=list)


@dataclass
class ProviderConfig:
    enabled: bool
    api_key_env: str
    models: Dict[str, ProviderModel] = field(default_factory=dict)


def _parse_model(data: dict) -> ProviderModel:
    context_window_raw = data.get("context_window")
    return ProviderModel(
        litellm_name=str(data["litellm_name"]),
        cost_input_per_mtok=float(data["cost_input_per_mtok"]),
        cost_output_per_mtok=float(data["cost_output_per_mtok"]),
        max_tokens=int(data["max_tokens"]),
        supports_streaming=bool(data["supports_streaming"]),
        supports_tool_calls=bool(data["supports_tool_calls"]),
        context_window=int(context_window_raw) if context_window_raw is not None else None,
        task_classes=list(data.get("task_classes") or []),
    )


def _parse_provider(data: dict) -> ProviderConfig:
    models: Dict[str, ProviderModel] = {}
    for model_key, model_data in (data.get("models") or {}).items():
        models[model_key] = _parse_model(model_data)
    return ProviderConfig(
        enabled=bool(data.get("enabled", False)),
        api_key_env=str(data.get("api_key_env") or ""),
        models=models,
    )


def load(registry_path: Optional[Path] = None) -> Dict[str, ProviderConfig]:
    """Parse wave7_models.yaml and return a dict of provider configs.

    Raises FileNotFoundError when registry_path does not exist.
    Raises yaml.YAMLError on malformed YAML.
    """
    path = Path(registry_path) if registry_path is not None else _REGISTRY_PATH
    with open(path) as fh:
        raw = yaml.safe_load(fh)
    result: Dict[str, ProviderConfig] = {}
    for provider_key, provider_data in (raw or {}).get("providers", {}).items():
        result[str(provider_key)] = _parse_provider(provider_data or {})
    return result


def get_default_model(
    sub_provider: str,
    registry_path: Optional[Path] = None,
) -> Optional[ProviderModel]:
    """Return the first model entry for *sub_provider*, or None if not found/disabled."""
    try:
        registry = load(registry_path)
    except FileNotFoundError as e:
        log.error("provider_registry: file missing at %s: %s", registry_path or _REGISTRY_PATH, e)
        raise
    except yaml.YAMLError as e:
        log.error("provider_registry: malformed yaml at %s: %s", registry_path or _REGISTRY_PATH, e)
        raise ValueError(f"malformed wave7_models.yaml: {e}") from e
    cfg = registry.get(sub_provider)
    if cfg is None or not cfg.enabled or not cfg.models:
        return None
    return next(iter(cfg.models.values()))
