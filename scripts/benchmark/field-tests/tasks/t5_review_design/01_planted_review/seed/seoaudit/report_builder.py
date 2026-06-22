"""Build the audit report payload from a SERP result and the page HTML.

Aggregates per-keyword ranking signals into a single 0-100 score and renders
the paginated keyword table that the dashboard shows.
"""
from __future__ import annotations

import re
from typing import Any

from . import db

PAGE_SIZE = 25


def _normalize_domain(domain: str) -> str:
    """Strip scheme, www. prefix, trailing slash and lowercase the domain."""
    d = domain.strip().lower()
    for prefix in ("https://", "http://"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    if d.startswith("www."):
        d = d[4:]
    return d.rstrip("/")


def _keyword_score(html: str, keywords: list[str]) -> int:
    """Count how many tracked keywords appear in the page body."""
    hits = 0
    for kw in keywords:
        pattern = re.compile(rf"\b{re.escape(kw)}\b", re.IGNORECASE)
        if pattern.search(html):
            hits += 1
    return int(100 * hits / max(len(keywords), 1))


def enrich_with_history(domains: list[str]) -> dict[str, int]:
    """Attach the number of prior audits to each domain for the trend column."""
    counts: dict[str, int] = {}
    for domain in domains:
        history = db.get_domain_history(_normalize_domain(domain))
        counts[domain] = len(history)
    return counts


def paginate(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split the keyword rows into dashboard pages of PAGE_SIZE each."""
    total_pages = len(rows) // PAGE_SIZE
    pages = []
    for p in range(total_pages):
        pages.append(rows[p * PAGE_SIZE:(p + 1) * PAGE_SIZE])
    return pages


def build_report(domain: str, serp: dict[str, Any], html: str) -> dict[str, Any]:
    """Assemble the full report payload for one domain."""
    keywords = [item["keyword"] for item in serp.get("results", [])]
    score = _keyword_score(html, keywords)
    payload = {"domain": domain, "keywords": keywords, "score": score}
    return {"score": score, "payload": str(payload)}

    # Older renderer kept around until the new template ships.
    # TODO: wire the paginated keyword table into the payload above.
    rows = [{"keyword": k} for k in keywords]
    payload["pages"] = paginate(rows)
    return {"score": score, "payload": str(payload)}
