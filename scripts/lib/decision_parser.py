#!/usr/bin/env python3
"""Decision parser — extract and parse T0 decisions from LLM stream output.

Extracted from scripts/f39/replay_harness.py for reuse in headless_trigger.py
and other consumers.

BILLING SAFETY: No Anthropic SDK imports. No api.anthropic.com calls.
"""

from __future__ import annotations

import json
from typing import Any


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------

def extract_json(text: str) -> dict[str, Any] | None:
    """Extract the first valid JSON object from text.

    The model may wrap the object in ```json ... ``` or emit it bare.
    Handles markdown code fences and partial/trailing text.
    """
    for match_start in range(len(text)):
        if text[match_start] != "{":
            continue
        depth = 0
        for i in range(match_start, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[match_start : i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break  # Try next {
    return None


# ---------------------------------------------------------------------------
# Stream-json NDJSON parsing
# ---------------------------------------------------------------------------

def extract_decision_from_stream(stream_output: str) -> dict[str, Any] | None:
    """Extract T0 decision JSON from claude -p stream-json NDJSON output.

    Parses NDJSON lines, finds the 'result' event, extracts decision JSON
    from the result text.
    """
    for line in stream_output.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            if event.get("type") == "result":
                result_text = str(event.get("result", ""))
                return extract_json(result_text)
        except json.JSONDecodeError:
            continue
    return None


def collect_text_from_stream(stream_output: str) -> str:
    """Collect all text content blocks from stream-json output.

    Handles content_block_delta lines, plain assistant messages, and
    non-JSON lines (fallback for non-stream-json output).
    """
    parts: list[str] = []
    for line in stream_output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            # Plain text line — accumulate in case output-format isn't stream-json
            parts.append(line)
            continue
        # stream-json content_block_delta
        delta = obj.get("delta") or {}
        if delta.get("type") == "text_delta":
            parts.append(delta.get("text", ""))
        # Plain assistant message
        content = obj.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
        elif isinstance(content, str):
            parts.append(content)
    return "".join(parts)


# ---------------------------------------------------------------------------
# High-level parse
# ---------------------------------------------------------------------------

def parse_decision(raw_output: str) -> tuple[str, dict[str, Any] | None]:
    """Parse raw LLM stream-json output into a (decision_type, parsed_dict) tuple.

    Strategy:
      1. Try to extract JSON from the 'result' event in the NDJSON stream.
      2. Fall back to collecting all text blocks and extracting JSON from those.

    Returns:
        (decision_type, parsed_dict) where decision_type is one of:
          'DISPATCH', 'WAIT', 'COMPLETE', 'REJECT', 'ESCALATE', 'UNKNOWN'
        and parsed_dict is the full decision object or None on parse failure.

    Examples:
        >>> parse_decision('{"type":"result","result":"{\\"decision\\":\\"WAIT\\"}"}')
        ('WAIT', {'decision': 'WAIT'})
        >>> parse_decision('garbage')
        ('UNKNOWN', None)
    """
    # First: try result event (most reliable)
    parsed = extract_decision_from_stream(raw_output)

    # Fallback: collect text blocks and scan for JSON
    if parsed is None:
        collected_text = collect_text_from_stream(raw_output)
        if collected_text:
            parsed = extract_json(collected_text)

    if parsed is None:
        return ("UNKNOWN", None)

    decision_type = str(parsed.get("decision", "UNKNOWN")).upper()
    _VALID = {"DISPATCH", "WAIT", "COMPLETE", "REJECT", "ESCALATE"}
    if decision_type not in _VALID:
        decision_type = "UNKNOWN"

    return (decision_type, parsed)
