"""Abstract receipt-classifier provider and registry.

Concrete providers subclass `ClassifierProvider` and implement `classify`.
Selection happens through `get_provider(name)`, driven by
`VNX_RECEIPT_CLASSIFIER_PROVIDER` (defaults to "haiku").
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class ClassifierResult:
    """Uniform result returned by every classifier provider."""

    raw_response: str
    parsed_json: Optional[Dict[str, Any]]
    cost_usd: float
    latency_ms: int
    provider: str
    error: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "raw_response": self.raw_response,
            "parsed_json": self.parsed_json,
            "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms,
            "provider": self.provider,
            "error": self.error,
            "extra": self.extra,
        }


class ClassifierProvider(ABC):
    """Abstract receipt classifier provider."""

    name: str = "base"

    @abstractmethod
    def classify(self, prompt: str, max_tokens: int = 1500) -> ClassifierResult:
        """Run the classifier on `prompt`. Must return a ClassifierResult.

        Implementations must:
        - never raise on subprocess errors; return ClassifierResult(error=...)
        - never import Anthropic / OpenAI SDKs
        - cap execution with a timeout
        """


_JSON_BLOCK_RE = re.compile(r"\{(?:[^{}]|(?:\{[^{}]*\}))*\}", re.DOTALL)


def parse_json_block(text: str) -> Optional[Dict[str, Any]]:
    """Best-effort JSON extraction from arbitrary CLI text output.

    Tries: full string parse → fenced ```json``` block → first balanced {...}.
    Returns None if no parseable object is found.
    """
    if not text:
        return None
    stripped = text.strip()
    try:
        obj = json.loads(stripped)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass

    # ```json ... ``` fenced block
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.DOTALL)
    if fence_match:
        try:
            obj = json.loads(fence_match.group(1))
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            pass

    # First balanced JSON object — depth scan to handle nested objects.
    start = stripped.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for idx in range(start, len(stripped)):
            ch = stripped[idx]
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"' and not esc:
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = stripped[start:idx + 1]
                    try:
                        obj = json.loads(candidate)
                        if isinstance(obj, dict):
                            return obj
                    except (json.JSONDecodeError, ValueError):
                        break
        start = stripped.find("{", start + 1)

    return None


def get_provider(name: Optional[str] = None) -> ClassifierProvider:
    """Return a configured provider instance for `name`.

    Lazy imports keep optional dependencies out of the import path until used.
    Unknown names raise ValueError so misconfigurations surface loudly.
    """
    key = (name or "haiku").strip().lower()
    if key == "haiku":
        from .haiku_provider import HaikuProvider
        return HaikuProvider()
    if key == "ollama":
        from .ollama_provider import OllamaProvider
        return OllamaProvider()
    if key == "gemini":
        from .gemini_provider import GeminiProvider
        return GeminiProvider()
    if key == "codex":
        from .codex_provider import CodexProvider
        return CodexProvider()
    raise ValueError(f"unknown classifier provider: {key}")
