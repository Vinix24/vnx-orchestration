"""Codex headless output parsing helpers.

Extracted from gate_artifacts.py to keep that module under 300 lines.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Tuple

from gate_lane_contract import VALID_VERDICTS  # C6 step 3 + OI-1767: one source, not a fourth literal copy
from review_contract import _normalize_line  # canonical line-coercion, never a second copy


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
    """Normalize findings to {severity, message, file_path, line} dicts.

    OI-1769: both verdict-contract templates (gate_lane_contract.py's
    VERDICT_CONTRACT, gate_runner.py's _REVIEWER_VERDICT_TEMPLATE) ask every
    gate for file_path/line on each finding, defaulting to ""/0 for a finding
    that names no single line — a guessed line number is worse than an empty
    one. This is the one place findings get normalized before landing in the
    result record, so both fields are preserved here rather than dropped;
    :func:`review_contract._normalize_line` is the canonical line-coercion
    (claude_github_receipt.py and gemini_prompt_renderer.py already import
    the same function rather than each keeping a copy).
    """
    normalized: List[Dict[str, Any]] = []
    for f in findings or []:
        if isinstance(f, str):
            normalized.append({"severity": "warning", "message": f, "file_path": "", "line": 0})
            continue
        if not isinstance(f, dict):
            normalized.append({"severity": "warning", "message": str(f), "file_path": "", "line": 0})
            continue
        severity = str(f.get("severity", "warning")).lower()
        message = f.get("message") or f.get("title") or f.get("details") or ""
        normalized.append({
            "severity": severity,
            "message": str(message),
            "file_path": str(f.get("file_path", "") or ""),
            "line": _normalize_line(f.get("line", 0)),
        })
    return normalized


def extract_verdict_block(stdout: str) -> Dict[str, Any]:
    """Extract the shared fenced ``json verdict block every gate contract asks for.

    ``VERDICT_CONTRACT`` (gate_lane_contract.py — glm_gate, kimi_gate and the
    harness-lane strategy in gate_runner) and ``_REVIEWER_VERDICT_TEMPLATE``
    (gate_runner.py — codex_gate, gemini_review) ask for the SAME shape: a
    fenced ```json block containing a ``"verdict"`` key. This is the one
    place gate_artifacts.materialize_artifacts's OI-1767 fail-closed guard
    checks for that shape, keyed on the shape itself rather than on a gate
    name (a name-branch here would repeat OI-1763's defect).

    Reuses the NDJSON-unwrap in :func:`_extract_codex_text` so this also
    works on codex's ``exec --json`` stream, not only on the plain-text
    report bodies glm_gate/kimi_gate/gemini_review stdout actually is.

    Does NOT delegate to :func:`_extract_codex_verdict`: that helper takes the
    FIRST fenced block and accepts any dict with a ``"verdict"`` key, values
    unchecked. VERDICT_CONTRACT is itself a fenced ```json block whose
    ``"verdict"`` value is the literal placeholder text ``"pass|fail|blocked"``
    — a worker that echoes its own instructions (then dies mid-report) hands
    back exactly that block first, and the old first-match/any-value logic
    read it as a genuine, blocking-free verdict (OI-1767 fix-forward,
    live-reproduced against this scenario). Same rule glm_gate._extract_verdict
    and kimi_gate._extract_verdict already apply: scan the fenced blocks from
    the END, and only accept one whose ``verdict`` (trimmed, lowercased) is a
    real value in :data:`gate_lane_contract.VALID_VERDICTS` — a report that
    echoes the template and then writes a real verdict resolves to that real
    verdict, not the template. Returns ``{}`` when no block clears that bar,
    same as before.
    """
    text = _extract_codex_text(stdout)
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    for block in reversed(blocks):
        try:
            candidate = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and str(candidate.get("verdict", "")).strip().lower() in VALID_VERDICTS:
            return candidate
    return {}


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
