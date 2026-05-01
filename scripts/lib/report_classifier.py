#!/usr/bin/env python3
"""Haiku Semantic Classifier — PR-4 (F37 Auto-Report Pipeline).

Adds semantic classification to assembled reports using claude-haiku via CLI
subprocess. Deterministic checks run first (ExtractionResult is already complete);
haiku only adds semantic judgment.

Public API:
    classify_report(extraction)  → HaikuClassification

Gated by VNX_HAIKU_CLASSIFY=1. Falls back to rule-based classification when:
- VNX_HAIKU_CLASSIFY is unset or != "1"
- haiku subprocess fails or times out
- haiku returns unparseable output

Classification enriches the AutoReport with:
    content_type, quality_score (1-5), complexity (low/medium/high),
    consistency_score (0.0-1.0), summary (≤200 chars), classified_by

BILLING SAFETY: No Anthropic SDK imports. CLI subprocess only.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys as _sys
from typing import Optional

_LIB = str(__file__).rsplit("/", 1)[0] if "/" in __file__ else "."
if _LIB not in _sys.path:
    _sys.path.insert(0, _LIB)

from auto_report_contract import (  # noqa: E402  # sys.path adjusted above
    Complexity,
    ContentType,
    ExtractionResult,
    HaikuClassification,
)

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

_HAIKU_MODEL = "claude-haiku-4-5-20251001"
_HAIKU_TIMEOUT = 30  # seconds — haiku is fast; fail-fast on hang

_VALID_CONTENT_TYPES = {e.value for e in ContentType}
_VALID_COMPLEXITIES = {e.value for e in Complexity}

_PROMPT_TEMPLATE = """\
You are a code review classifier. Analyze this dispatch execution report and \
classify it. Respond ONLY with a JSON object — no markdown, no explanation.

Required JSON fields:
  "content_type": one of: implementation, test, refactor, docs, review, config, planning, mixed
  "quality_score": integer 1-5 (1=poor, 3=adequate, 5=excellent)
  "complexity": one of: low, medium, high
  "consistency_score": float 0.0-1.0 (how well exit summary matches the actual changes)
  "summary": string, 1-2 sentences describing what was accomplished (max 200 chars)

Dispatch execution data:
{report_context}

Respond with only the JSON object.
"""


# ─── Prompt Builder ───────────────────────────────────────────────────────────

def _build_prompt(extraction: ExtractionResult) -> str:
    """Serialize key extraction data as a compact context block for haiku."""
    ctx: dict = {
        "dispatch_id": extraction.dispatch_id,
        "track": extraction.track,
        "gate": extraction.gate,
        "exit_summary": extraction.exit_summary or "(none)",
        "files_changed": list(extraction.git.files_changed),
        "insertions": extraction.git.insertions,
        "deletions": extraction.git.deletions,
        "commit_hash": extraction.git.commit_hash or "(none)",
        "commit_message": extraction.git.commit_message or "(none)",
        "has_commit": bool(extraction.git.commit_hash),
        "test_passed": extraction.tests.passed if extraction.tests else 0,
        "test_failed": extraction.tests.failed if extraction.tests else 0,
        "test_errors": extraction.tests.errors if extraction.tests else 0,
        "has_syntax_errors": extraction.has_syntax_errors,
        "tool_use_count": extraction.events.tool_use_count,
        "error_events": extraction.events.error_count,
        "session_duration_s": extraction.events.session_duration_seconds,
    }
    report_context = json.dumps(ctx, indent=2)
    return _PROMPT_TEMPLATE.format(report_context=report_context)


# ─── Haiku Subprocess ─────────────────────────────────────────────────────────

def _call_haiku(prompt: str) -> Optional[str]:
    """Invoke claude -p --model haiku with prompt on stdin.

    Returns the raw text output, or None on any failure (timeout, non-zero
    exit, empty output).
    """
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", _HAIKU_MODEL],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=_HAIKU_TIMEOUT,
        )
    except FileNotFoundError:
        logger.debug("_call_haiku: 'claude' binary not found in PATH")
        return None
    except subprocess.TimeoutExpired:
        logger.warning("_call_haiku: timed out after %ds", _HAIKU_TIMEOUT)
        return None
    except OSError as exc:
        logger.debug("_call_haiku: subprocess error: %s", exc)
        return None

    if result.returncode != 0:
        logger.debug(
            "_call_haiku: non-zero exit %d; stderr=%s",
            result.returncode,
            result.stderr[:200] if result.stderr else "",
        )
        return None

    text = result.stdout.strip()
    if not text:
        logger.debug("_call_haiku: empty output")
        return None

    return text


# ─── Response Parser ──────────────────────────────────────────────────────────

def _parse_haiku_response(text: str) -> Optional[dict]:
    """Extract a JSON object from haiku's text response.

    Handles:
    - Clean JSON output (most common)
    - JSON wrapped in a markdown code fence (```json ... ```)
    - Trailing/leading whitespace

    Returns None when no valid JSON object is found.
    """
    if not text:
        return None

    # Strip markdown code fences if present
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1)

    # Try direct parse first
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    # Try to extract a JSON object from somewhere in the text
    obj_match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if obj_match:
        try:
            data = json.loads(obj_match.group(0))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    logger.debug("_parse_haiku_response: no JSON object found in: %s", text[:200])
    return None


def _validate_parsed(data: dict) -> Optional[dict]:
    """Validate and normalise the parsed haiku response dict.

    Returns a clean dict with all required fields, or None if any required
    field is missing or invalid.
    """
    content_type = str(data.get("content_type", "")).lower().strip()
    if content_type not in _VALID_CONTENT_TYPES:
        logger.debug("_validate_parsed: invalid content_type=%r", content_type)
        return None

    try:
        quality_score = int(data.get("quality_score", 0))
    except (TypeError, ValueError):
        logger.debug("_validate_parsed: invalid quality_score=%r", data.get("quality_score"))
        return None
    if not 1 <= quality_score <= 5:
        logger.debug("_validate_parsed: quality_score %d out of range", quality_score)
        return None

    complexity = str(data.get("complexity", "")).lower().strip()
    if complexity not in _VALID_COMPLEXITIES:
        logger.debug("_validate_parsed: invalid complexity=%r", complexity)
        return None

    try:
        consistency_score = float(data.get("consistency_score", 0.5))
    except (TypeError, ValueError):
        consistency_score = 0.5
    consistency_score = max(0.0, min(1.0, consistency_score))

    summary = str(data.get("summary", "")).strip()
    summary = summary[:200]  # enforce contract limit

    return {
        "content_type": content_type,
        "quality_score": quality_score,
        "complexity": complexity,
        "consistency_score": consistency_score,
        "summary": summary,
    }


# ─── Classification Entry Point ───────────────────────────────────────────────

def classify_report(extraction: ExtractionResult) -> HaikuClassification:
    """Classify an extraction result into semantic tags.

    When VNX_HAIKU_CLASSIFY=1:
        Calls claude-haiku via CLI subprocess. On failure, falls back to
        rule_based silently — pipeline never crashes on classifier failure.

    When VNX_HAIKU_CLASSIFY != 1 (or unset):
        Returns rule_based classification immediately (no subprocess).

    Args:
        extraction: Populated ExtractionResult from run_extraction().

    Returns:
        HaikuClassification with classified_by="haiku" or "rule_based".
    """
    if os.environ.get("VNX_HAIKU_CLASSIFY") != "1":
        logger.debug("classify_report: VNX_HAIKU_CLASSIFY not set; using rule_based")
        return HaikuClassification.rule_based(extraction)

    prompt = _build_prompt(extraction)
    raw_text = _call_haiku(prompt)

    if raw_text is None:
        logger.info("classify_report: haiku call failed; falling back to rule_based")
        return HaikuClassification.rule_based(extraction)

    parsed = _parse_haiku_response(raw_text)
    if parsed is None:
        logger.info("classify_report: haiku response not parseable; falling back to rule_based")
        return HaikuClassification.rule_based(extraction)

    validated = _validate_parsed(parsed)
    if validated is None:
        logger.info("classify_report: haiku response invalid; falling back to rule_based")
        return HaikuClassification.rule_based(extraction)

    try:
        return HaikuClassification(
            content_type=ContentType(validated["content_type"]),
            quality_score=validated["quality_score"],
            complexity=Complexity(validated["complexity"]),
            consistency_score=validated["consistency_score"],
            summary=validated["summary"],
            classified_by="haiku",
        )
    except (ValueError, KeyError) as exc:
        logger.info("classify_report: HaikuClassification construction failed: %s; rule_based", exc)
        return HaikuClassification.rule_based(extraction)
