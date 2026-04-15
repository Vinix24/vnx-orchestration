#!/usr/bin/env python3
"""provider_adapter.py — Abstract base for all VNX provider adapters.

Defines the ProviderAdapter ABC, Capability enum, and AdapterResult dataclass.
Each provider (Claude, Gemini, Codex, Ollama) implements this interface.

BILLING SAFETY: No Anthropic SDK. CLI-only subprocess calls.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator, Optional


class Capability(Enum):
    CODE = "code"           # Can implement features, write code, commit
    REVIEW = "review"       # Can analyze code and provide findings
    DECISION = "decision"   # Can make structured decisions (re-dispatch, escalate)
    DIGEST = "digest"       # Can generate narrative summaries


@dataclass
class AdapterResult:
    status: str                          # "done", "failed", "timeout"
    output: str                          # Final text output
    events: list[dict]                   # Streamed events (if supported)
    event_count: int
    duration_seconds: float
    committed: bool                      # Did it create a git commit?
    commit_hash: Optional[str]
    report_path: Optional[str]
    provider: str                        # "claude", "gemini", "codex", "ollama"
    model: str                           # Specific model used


class ProviderAdapter(ABC):
    """Abstract base for all provider adapters."""

    @abstractmethod
    def name(self) -> str:
        """Return provider name, e.g. 'claude'."""
        ...

    @abstractmethod
    def capabilities(self) -> set[Capability]:
        """Return set of capabilities this provider supports."""
        ...

    @abstractmethod
    def execute(self, instruction: str, context: dict) -> AdapterResult:
        """Execute instruction and return structured result.

        context keys (all optional):
          terminal_id   : str — target terminal (e.g. 'T1')
          dispatch_id   : str — dispatch identifier
          model         : str — model override (e.g. 'sonnet', 'haiku')
          role          : str — agent role for skill context injection
          lease_generation : int — lease generation for heartbeat renewal
          heartbeat_interval : float — heartbeat renewal interval
          chunk_timeout : float — max seconds between output chunks
          total_deadline : float — max total execution seconds
          auto_commit   : bool — auto-commit uncommitted changes on success
          gate          : str — gate tag for auto-commit message
        """
        ...

    @abstractmethod
    def stream_events(self, instruction: str, context: dict) -> Iterator[dict]:
        """Stream events as they arrive (lower-level than execute)."""
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """Check if provider CLI/API is reachable."""
        ...

    def supports(self, capability: Capability) -> bool:
        """Return True if this provider supports the given capability."""
        return capability in self.capabilities()
