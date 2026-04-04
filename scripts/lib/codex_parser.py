"""Codex headless output parsing helpers.

Extracted from gate_artifacts.py to keep that module under 300 lines.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Tuple


def _extract_codex_text(stdout: str) -> str:
    """Extract agent_message text from codex NDJSON output."""
    texts: List[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        item = payload.get("item") if isinstance(payload.get("item"), dict) else None
        msg_types = {"agent_message", "assistant_message", "output_text"}
        if item and item.get("type") in msg_types:
            text = item.get("text") or ""
            if text:
                texts.append(text)
        elif payload.get("type") in msg_types:
            text = payload.get("text") or ""
            if text:
                texts.append(text)
    return "\n".join(texts).strip() if texts else stdout.strip()


def _extract_codex_verdict(text: str) -> Dict[str, Any]:
    """Try to parse a JSON verdict from codex output text."""
    if not text:
        return {}
    fenced = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and ("verdict" in obj or "findings" in obj):
            return obj
    return {}


def _classify_finding(item: str) -> Tuple[str, str]:
    """Extract (severity, message) from a finding item string."""
    sev_match = re.match(
        r"^(critical|high|medium|low|warning|warn|error|info)\s*[:\-]\s*(.+)$",
        item, re.IGNORECASE,
    )
    if sev_match:
        return sev_match.group(1).lower(), sev_match.group(2).strip()
    bracket = re.match(
        r"^\[(critical|high|medium|low|warning|warn|error|info)\]\s*(.+)$",
        item, re.IGNORECASE,
    )
    if bracket:
        return bracket.group(1).lower(), bracket.group(2).strip()
    return "warning", item


def _extract_findings_from_text(text: str) -> List[Dict[str, Any]]:
    """Heuristic fallback when codex does not emit JSON verdicts."""
    if not text:
        return []
    findings: List[Dict[str, Any]] = []
    in_section = False
    header_pattern = re.compile(
        r"^(?:\*\*|__)?\s*(findings|issues found|critical issues|major issues|minor issues)\s*(?:\*\*|__)?$",
        re.IGNORECASE,
    )
    new_section_pattern = re.compile(
        r"^(?:\*\*|__)?\s*(open questions|summary|notes|recommendations|conclusion)\s*(?:\*\*|__)?$",
        re.IGNORECASE,
    )
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        header_match = re.match(
            r"^#{1,4}\s*(findings|issues found|critical issues|major issues|minor issues)\b",
            stripped, re.IGNORECASE,
        )
        if header_match or header_pattern.match(stripped):
            in_section = True
            continue
        if in_section:
            if stripped.startswith("#") or new_section_pattern.match(stripped):
                if findings:
                    break
                continue
        item_match = (
            re.match(r"^[-*]\s*(.+)$", stripped)
            or re.match(r"^\d+\.\s*(.+)$", stripped)
        )
        if not item_match:
            continue
        severity, msg = _classify_finding(item_match.group(1).strip())
        findings.append({"severity": severity, "message": msg})
    return findings


def _normalize_findings(findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize findings to {severity, message} dicts."""
    normalized: List[Dict[str, Any]] = []
    for f in findings or []:
        if isinstance(f, str):
            normalized.append({"severity": "warning", "message": f})
            continue
        if not isinstance(f, dict):
            normalized.append({"severity": "warning", "message": str(f)})
            continue
        severity = str(f.get("severity", "warning")).lower()
        message = f.get("message") or f.get("title") or f.get("details") or ""
        normalized.append({"severity": severity, "message": str(message)})
    return normalized


def parse_codex_findings(stdout: str) -> Dict[str, Any]:
    """Extract findings from Codex headless NDJSON output."""
    text = _extract_codex_text(stdout)
    verdict = _extract_codex_verdict(text)
    findings = verdict.get("findings") or [] if verdict else []
    residual_risk = verdict.get("residual_risk") or "" if verdict else ""
    if not findings:
        findings = _extract_findings_from_text(text)
    return {
        "findings": _normalize_findings(findings),
        "residual_risk": residual_risk,
        "verdict": verdict or {},
        "raw_text": text,
    }
