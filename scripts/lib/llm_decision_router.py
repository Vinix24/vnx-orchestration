#!/usr/bin/env python3
"""llm_decision_router.py — Model-agnostic LLM decision interface.

Routes governance decisions through configurable backends:
  - dry-run   : rule-based, no LLM (default, safe for testing)
  - ollama    : local Ollama endpoint (HTTP)
  - claude-cli: subprocess `claude -p --output-format json`

Backend selected via VNX_DECISION_BACKEND env var.

BILLING SAFETY: No Anthropic SDK. claude-cli backend uses subprocess only.
"""

from __future__ import annotations

import enum
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config constants
# ---------------------------------------------------------------------------

_DEFAULT_BACKEND      = "dry-run"
_DEFAULT_OLLAMA_MODEL = "gemma3:27b"
_DEFAULT_OLLAMA_HOST  = "http://localhost:11434"
_DEFAULT_TIMEOUT      = 30

_DECISION_PROMPT_SYSTEM = """\
You are a VNX governance agent. Evaluate the system state and return a structured JSON decision.

## Decision schema
{
  "action": "<re_dispatch|escalate|skip|analyze_failure>",
  "reasoning": "<one sentence explanation>",
  "confidence": <0.0-1.0 float>
}

## Actions
- re_dispatch  : Worker failed or timed out — retry the dispatch
- escalate     : Abnormal state requiring human intervention
- skip         : State is normal or self-resolving — no action needed
- analyze_failure : Dispatch failed but cause is unclear — run failure analysis

## Rules
- Reply ONLY with the JSON object, no markdown fences, no extra text.
- Confidence: 1.0 = certain, 0.5 = uncertain
"""

_DECISION_PROMPT_USER = """\
Question: {question}

Context:
{context_json}
"""


# ---------------------------------------------------------------------------
# Backend enum
# ---------------------------------------------------------------------------

class Backend(str, enum.Enum):
    DRY_RUN    = "dry-run"
    OLLAMA     = "ollama"
    CLAUDE_CLI = "claude-cli"


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class DecisionResult:
    action: str
    reasoning: str
    confidence: float
    backend_used: str
    latency_ms: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "reasoning": self.reasoning,
            "confidence": self.confidence,
            "backend_used": self.backend_used,
            "latency_ms": self.latency_ms,
        }


# ---------------------------------------------------------------------------
# Rule-based fallback (dry-run + Ollama timeout fallback)
# ---------------------------------------------------------------------------

def _rule_based_decision(context: Dict[str, Any], question: str) -> DecisionResult:
    """Pure rule-based decision — no LLM required."""
    receipt_status = context.get("receipt", {}).get("status", "")
    terminal_silence = context.get("terminal_silence_seconds", 0)

    if receipt_status == "failed":
        return DecisionResult(
            action="re_dispatch",
            reasoning="Receipt status is failed — retry dispatch",
            confidence=0.8,
            backend_used="dry-run",
            latency_ms=0,
        )
    if isinstance(terminal_silence, (int, float)) and terminal_silence > 900:  # 15 min
        return DecisionResult(
            action="escalate",
            reasoning=f"Terminal silent for {terminal_silence}s (>15 min) — escalate",
            confidence=0.6,
            backend_used="dry-run",
            latency_ms=0,
        )
    return DecisionResult(
        action="skip",
        reasoning="State is normal — no action required",
        confidence=1.0,
        backend_used="dry-run",
        latency_ms=0,
    )


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _build_prompt(context: Dict[str, Any], question: str) -> str:
    ctx_json = json.dumps(context, indent=2, default=str)
    return _DECISION_PROMPT_USER.format(question=question, context_json=ctx_json)


def _parse_llm_response(raw: str) -> Optional[Dict[str, Any]]:
    """Extract JSON from LLM response. Handles bare JSON and markdown fences."""
    raw = raw.strip()
    # Strip markdown fences if present
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(
            line for line in lines if not line.startswith("```")
        ).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Try extracting first {...} block
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start != -1 and end > start:
            try:
                return json.loads(raw[start:end])
            except json.JSONDecodeError:
                pass
    return None


# ---------------------------------------------------------------------------
# Ollama backend (delegates to OllamaAdapter)
# ---------------------------------------------------------------------------

def _decide_ollama(
    context: Dict[str, Any],
    question: str,
    model: str,
    host: str,
    timeout: int,
) -> DecisionResult:
    """Route Ollama decisions via OllamaAdapter (single HTTP configuration point)."""
    # OllamaAdapter reads host/model from env vars; override them for this call
    # by temporarily setting them so the adapter picks them up.
    prev_host  = os.environ.get("VNX_OLLAMA_HOST")
    prev_model = os.environ.get("VNX_OLLAMA_MODEL")
    os.environ["VNX_OLLAMA_HOST"]  = host
    os.environ["VNX_OLLAMA_MODEL"] = model
    try:
        return _call_ollama_adapter(context, question, timeout)
    finally:
        if prev_host is None:
            os.environ.pop("VNX_OLLAMA_HOST", None)
        else:
            os.environ["VNX_OLLAMA_HOST"] = prev_host
        if prev_model is None:
            os.environ.pop("VNX_OLLAMA_MODEL", None)
        else:
            os.environ["VNX_OLLAMA_MODEL"] = prev_model


def _call_ollama_adapter(
    context: Dict[str, Any],
    question: str,
    timeout: int,
) -> DecisionResult:
    """Execute a decision via OllamaAdapter and map result to DecisionResult."""
    adapters_path = Path(__file__).resolve().parent
    sys.path.insert(0, str(adapters_path))
    from adapters.ollama_adapter import OllamaAdapter  # noqa: PLC0415

    adapter = OllamaAdapter("__decision__")
    prompt = _DECISION_PROMPT_SYSTEM + "\n\n" + _build_prompt(context, question)

    t0 = time.monotonic()
    adapter_result = adapter.execute(
        prompt,
        {"capability": "decision", "timeout": timeout},
    )
    latency_ms = int((time.monotonic() - t0) * 1000)

    if adapter_result.status != "done":
        logger.warning(
            "OllamaAdapter returned %s (%s) — falling back to dry-run",
            adapter_result.status, adapter_result.output,
        )
        result = _rule_based_decision(context, question)
        result.backend_used = f"dry-run(ollama-{adapter_result.output})"
        return result

    parsed = _parse_llm_response(adapter_result.output)
    if parsed is None or "action" not in parsed:
        logger.warning("Ollama returned unparseable response — falling back to dry-run")
        result = _rule_based_decision(context, question)
        result.backend_used = "dry-run(ollama-parse-error)"
        return result

    return DecisionResult(
        action=str(parsed.get("action", "skip")),
        reasoning=str(parsed.get("reasoning", "")),
        confidence=float(parsed.get("confidence", 0.5)),
        backend_used=f"ollama:{adapter._model}",
        latency_ms=latency_ms,
    )


# ---------------------------------------------------------------------------
# Claude-CLI backend
# ---------------------------------------------------------------------------

def _decide_claude_cli(
    context: Dict[str, Any],
    question: str,
    timeout: int,
) -> DecisionResult:
    prompt = _DECISION_PROMPT_SYSTEM + "\n\n" + _build_prompt(context, question)

    t0 = time.monotonic()
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", "haiku", "--output-format", "json", prompt],
            capture_output=True, text=True, timeout=timeout,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
    except subprocess.TimeoutExpired:
        logger.warning("claude-cli timed out — falling back to dry-run")
        r = _rule_based_decision(context, question)
        r.backend_used = "dry-run(claude-cli-timeout)"
        return r
    except FileNotFoundError:
        logger.warning("'claude' not in PATH — falling back to dry-run")
        r = _rule_based_decision(context, question)
        r.backend_used = "dry-run(claude-not-found)"
        return r

    raw = result.stdout.strip()
    # claude --output-format json wraps in {"result": "...", ...}
    try:
        wrapper = json.loads(raw)
        inner = wrapper.get("result") or raw
    except json.JSONDecodeError:
        inner = raw

    parsed = _parse_llm_response(inner if isinstance(inner, str) else json.dumps(inner))
    if parsed is None or "action" not in parsed:
        logger.warning("claude-cli returned unparseable response — falling back to dry-run")
        r = _rule_based_decision(context, question)
        r.backend_used = "dry-run(claude-cli-parse-error)"
        return r

    return DecisionResult(
        action=str(parsed.get("action", "skip")),
        reasoning=str(parsed.get("reasoning", "")),
        confidence=float(parsed.get("confidence", 0.5)),
        backend_used="claude-cli:haiku",
        latency_ms=latency_ms,
    )


# ---------------------------------------------------------------------------
# Decision logger
# ---------------------------------------------------------------------------

def _log_decision(
    data_dir: Path,
    context: Dict[str, Any],
    question: str,
    result: DecisionResult,
) -> None:
    events_dir = data_dir / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    ctx_hash = hashlib.sha256(
        json.dumps(context, sort_keys=True, default=str).encode()
    ).hexdigest()[:12]
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "question": question,
        "context_hash": ctx_hash,
        **result.to_dict(),
    }
    log_path = events_dir / "decisions.ndjson"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# DecisionRouter
# ---------------------------------------------------------------------------

class DecisionRouter:
    """Route LLM decisions through configurable backends.

    Supports: ollama (local), claude-cli (subprocess), dry-run (no LLM).
    Backend selected via VNX_DECISION_BACKEND env var.
    """

    def __init__(
        self,
        data_dir: Optional[Path] = None,
        backend: Optional[str] = None,
        ollama_model: Optional[str] = None,
        ollama_host: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> None:
        self.data_dir = data_dir or _default_data_dir()
        self.backend  = Backend(
            backend
            or os.environ.get("VNX_DECISION_BACKEND", _DEFAULT_BACKEND)
        )
        self.ollama_model = (
            ollama_model
            or os.environ.get("VNX_OLLAMA_MODEL", _DEFAULT_OLLAMA_MODEL)
        )
        self.ollama_host = (
            ollama_host
            or os.environ.get("VNX_OLLAMA_HOST", _DEFAULT_OLLAMA_HOST)
        )
        self.timeout = int(
            timeout
            if timeout is not None
            else os.environ.get("VNX_DECISION_TIMEOUT", _DEFAULT_TIMEOUT)
        )

    def decide(self, context: Dict[str, Any], question: str) -> DecisionResult:
        """Evaluate context and return a structured decision.

        Args:
            context: Structured dict with terminal state, receipt data, failure info.
            question: Decision type — one of: re_dispatch, escalate, skip, analyze_failure.

        Returns:
            DecisionResult with action, reasoning, confidence, backend_used, latency_ms.
        """
        if self.backend == Backend.OLLAMA:
            result = _decide_ollama(
                context, question,
                model=self.ollama_model,
                host=self.ollama_host,
                timeout=self.timeout,
            )
        elif self.backend == Backend.CLAUDE_CLI:
            result = _decide_claude_cli(context, question, timeout=self.timeout)
        else:
            result = _rule_based_decision(context, question)

        try:
            _log_decision(self.data_dir, context, question, result)
        except Exception as exc:
            logger.warning("Failed to log decision: %s", exc)

        return result


# ---------------------------------------------------------------------------
# Convenience paths
# ---------------------------------------------------------------------------

def _default_data_dir() -> Path:
    env = os.environ.get("VNX_DATA_DIR", "")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / ".vnx-data"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="VNX LLM Decision Router")
    parser.add_argument("--question", default="skip",
                        choices=["re_dispatch", "escalate", "skip", "analyze_failure"])
    parser.add_argument("--context-json", default="{}", help="JSON context string")
    parser.add_argument("--backend", default=None,
                        choices=["dry-run", "ollama", "claude-cli"])
    args = parser.parse_args()

    try:
        ctx = json.loads(args.context_json)
    except json.JSONDecodeError as exc:
        print(f"Invalid --context-json: {exc}", file=sys.stderr)
        return 1

    router = DecisionRouter(backend=args.backend)
    result = router.decide(ctx, args.question)
    print(json.dumps(result.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
