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
    def classify(self, prompt: str, _max_tokens: int = 1500) -> ClassifierResult:
        """Run the classifier on `prompt`. Must return a ClassifierResult.

        Implementations must:
        - never raise on subprocess errors; return ClassifierResult(error=...)
        - never import Anthropic / OpenAI SDKs
        - cap execution with a timeout
        """


_JSON_BLOCK_RE = re.compile(r"\{(?:[^{}]|(?:\{[^{}]*\}))*\}", re.DOTALL)


def _loads_lenient(text: str) -> Optional[Dict[str, Any]]:
    """json.loads returning a dict, with light repair for the JSON slip small models
    make most: a trailing comma before ``}`` or ``]``. Conservative on purpose — it
    never turns broken data into a plausible-but-wrong object, it only forgives a
    stray comma. Returns None if no dict is recoverable."""
    for candidate in (text, re.sub(r",(\s*[}\]])", r"\1", text)):
        try:
            obj = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


def parse_json_block(text: str) -> Optional[Dict[str, Any]]:
    """Best-effort JSON extraction from arbitrary CLI text output.

    Tries: full string parse → fenced ```json``` block → first balanced {...}, each
    with a lenient trailing-comma repair. Crucially, when the OUTER object fails to
    parse the scan does NOT silently fall through to an inner sub-object (that
    returned e.g. a single ``{"ref","why"}`` item instead of the whole ranking) —
    it stops, so a broken outer object reads as a miss, not as wrong data.
    Returns None if no parseable object is found.
    """
    if not text:
        return None
    stripped = text.strip()
    obj = _loads_lenient(stripped)
    if obj is not None:
        return obj

    # ```json ... ``` fenced block
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.DOTALL)
    if fence_match:
        obj = _loads_lenient(fence_match.group(1))
        if obj is not None:
            return obj

    # Balanced TOP-LEVEL objects only. On a malformed outer object we advance PAST
    # it (not into it), so the scan never returns an inner sub-object — e.g. a single
    # {"ref","why"} item in place of the whole ranking when the outer JSON is broken.
    i = stripped.find("{")
    while i != -1:
        depth = 0
        in_str = False
        esc = False
        end = -1
        for idx in range(i, len(stripped)):
            ch = stripped[idx]
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = idx
                    break
        if end == -1:
            break  # no balanced close — nothing more to try
        obj = _loads_lenient(stripped[i:end + 1])
        if obj is not None:
            return obj
        i = stripped.find("{", end + 1)  # skip PAST this object, never into it

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
    if key == "deepseek":
        from .deepseek_provider import DeepSeekProvider
        return DeepSeekProvider()
    raise ValueError(f"unknown classifier provider: {key}")
