#!/usr/bin/env python3
"""
VNX Digest Text Helpers — boundary-safe truncation and suggestion de-duplication.

The nightly digest used raw slicing ([:80] / [:120] / [:200]) to fit suggestion text
into a line, which cut words mid-token ("...Pref", "that re", "skill sk", "for l") and
produced unreadable, low-trust output. It also emitted near-duplicate suggestions (the
same MEMORY token-profile line three times at different sample sizes, the same claim
twice). These helpers fix both:

  - :func:`smart_truncate` truncates on a word boundary and appends an explicit ellipsis,
    so a suggestion never ends mid-word.
  - :func:`dedup_suggestions` collapses suggestions that share the same target + intent
    to one, keeping the highest-confidence (then latest) version.

Pure functions, no I/O, no env — safe to import anywhere.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

_ELLIPSIS = "…"

# Tokens that signal a non-actionable raw internal dump rather than a decision the
# operator can say yes/no to. These are surfaced as context, not as suggestions.
_RAW_INTERNAL_MARKERS = (
    "token profiel",
    "token profile",
)


def smart_truncate(text: str, limit: int, *, ellipsis: str = _ELLIPSIS) -> str:
    """Truncate ``text`` to at most ``limit`` characters on a word boundary.

    If truncation is needed, the result ends with ``ellipsis`` (counted within the
    limit) and never splits a word. If the whole text fits, it is returned unchanged.
    Falls back to a hard cut only when the first token alone exceeds the limit.
    """
    text = (text or "").strip()
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text

    budget = limit - len(ellipsis)
    if budget <= 0:
        return text[:limit]

    head = text[:budget]
    # Back up to the last whitespace so we don't cut a word in half.
    boundary = head.rfind(" ")
    if boundary > 0:
        head = head[:boundary]
    head = head.rstrip(" ,.;:-—")
    if not head:
        # Single oversized token: hard-cut rather than emit an empty string.
        head = text[:budget]
    return head + ellipsis


def _intent_key(suggestion: Dict[str, Any]) -> str:
    """Derive a stable target+intent key for collapsing near-duplicate suggestions.

    Two suggestions collapse when they target the same file/section with the same
    intent. Intent is approximated by the category, target, section, and the leading
    normalized words of the content — enough to fold "Model token profiel (7d): ..."
    variants (which differ only in sample size) into one, while keeping genuinely
    distinct suggestions apart.
    """
    category = (suggestion.get("category") or "").strip().lower()
    target = (suggestion.get("target") or "").strip().lower()
    section = (suggestion.get("section") or "").strip().lower()
    content = (suggestion.get("content") or "").strip().lower()

    # The intent is captured by the leading words. The 3x "Model token profiel (7d):
    # opus ..." variants differ only in the trailing numbers (avg=1817K vs 1650K), so a
    # leading-word prefix collapses them. Digits are intentionally NOT normalized away —
    # that would wrongly fold genuinely distinct targets that differ only by a digit
    # (e.g. "prefer gpt-4" vs "prefer gpt-5").
    normalized = re.sub(r"\s+", " ", content)
    prefix = " ".join(normalized.split(" ")[:6])
    return f"{category}|{target}|{section}|{prefix}"


def _confidence(suggestion: Dict[str, Any]) -> float:
    try:
        return float(suggestion.get("confidence", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def dedup_suggestions(suggestions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse suggestions that share a target+intent to a single best version.

    Keeps the highest-confidence variant; ties break on the latest ``suggested_at``
    (falls back to last-seen). Order of first appearance is otherwise preserved.
    """
    best: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []

    for suggestion in suggestions:
        key = _intent_key(suggestion)
        if key not in best:
            best[key] = suggestion
            order.append(key)
            continue

        incumbent = best[key]
        challenger_conf = _confidence(suggestion)
        incumbent_conf = _confidence(incumbent)
        if challenger_conf > incumbent_conf:
            best[key] = suggestion
        elif challenger_conf == incumbent_conf:
            if str(suggestion.get("suggested_at", "")) >= str(incumbent.get("suggested_at", "")):
                best[key] = suggestion

    return [best[key] for key in order]


def is_raw_internal_dump(suggestion: Dict[str, Any]) -> bool:
    """True for non-actionable raw internal dumps (e.g. bare token-profile lines).

    Decision-grade digests surface things the operator can accept or reject; a raw
    token-profile aggregation is context, not a decision, so it is filtered out.
    """
    content = (suggestion.get("content") or "").lower()
    return any(marker in content for marker in _RAW_INTERNAL_MARKERS)


def decision_grade(
    suggestions: List[Dict[str, Any]],
    *,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Return decision-grade suggestions: deduped, raw internals dropped, top-N.

    ``limit`` caps the number of surfaced suggestions (highest confidence first);
    ``None`` keeps all of them after dedup and filtering.
    """
    actionable = [s for s in suggestions if not is_raw_internal_dump(s)]
    deduped = dedup_suggestions(actionable)
    ranked = sorted(deduped, key=_confidence, reverse=True)
    if limit is not None:
        ranked = ranked[:limit]
    return ranked
