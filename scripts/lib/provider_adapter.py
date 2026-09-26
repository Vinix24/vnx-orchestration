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

import provider_reachability
from provider_reachability import Reachability


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

    def is_present(self) -> bool:
        """Cheap precondition: is the binary, package or endpoint there at all?

        Says nothing about whether a call would be answered: a CLI whose quota
        is spent is present. Adapters that rely on the shared reachability
        record implement this; it is the ``present`` input of
        :meth:`reachability`.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement is_present() to use the measured reachability record"
        )

    def reachability(self) -> Reachability:
        """Presence, then the measured outcome recorded for this provider.

        ``unmeasured`` is returned as ``unmeasured``, never as ``reachable``;
        see :mod:`provider_reachability`.
        """
        return provider_reachability.assess(self.name(), present=self.is_present())

    def record_outcome(self, answered: bool, failure_text: str = "") -> None:
        """Feed one real call's outcome into the reachability record.

        ``answered=True`` confirms the provider. ``answered=False`` records it
        unreachable only when ``failure_text`` names a quota, balance or
        credential refusal; any other failure (timeout, crash) is not evidence
        about reachability and changes nothing. Best-effort, never raises.
        """
        source = f"adapter:{self.name()}"
        if answered:
            provider_reachability.record_success(self.name(), source=source)
        else:
            provider_reachability.record_failure(self.name(), failure_text, source=source)

    @abstractmethod
    def is_available(self) -> bool:
        """True when this provider may be tried: present AND not known to be
        unreachable.

        Presence alone is not availability (OI-1454): a CLI on PATH with its
        quota spent, or a package importable with a key the provider rejects,
        is not available. Implement it as ``self.reachability().is_usable``,
        which is False for a provider recorded unreachable and True for one
        nobody has proven dead. Callers that must tell "proven alive" from
        "not proven dead" use :meth:`reachability` and ``is_reachable``.
        """
        ...

    def supports(self, capability: Capability) -> bool:
        """Return True if this provider supports the given capability."""
        return capability in self.capabilities()
