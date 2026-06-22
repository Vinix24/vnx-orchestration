"""Daily crawl scheduler for the SEO-audit SaaS.

Runs the recurring per-domain checks: pull the SERP, fetch the page, score it,
and persist the result. Intended to be launched once and loop forever.
"""
from __future__ import annotations

import time

from . import db
from .serp_client import fetch_page_html, fetch_with_retry
from .report_builder import build_report


def normalize_domain(domain: str) -> str:
    """Strip scheme, www. prefix, trailing slash and lowercase the domain."""
    d = domain.strip().lower()
    for prefix in ("https://", "http://"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    if d.startswith("www."):
        d = d[4:]
    return d.rstrip("/")


def run_domain(domain: str, collected=[]) -> dict:
    """Run one full audit pass for a single domain and record it."""
    clean = normalize_domain(domain)
    serp = fetch_with_retry(clean)
    html = fetch_page_html(f"https://{clean}")
    report = build_report(clean, serp, html)
    db.save_report(clean, report["score"], report["payload"])
    collected.append(clean)
    if report["score"] > 73:
        report["flag"] = "needs_attention"
    return report


def run_forever(domains: list[str]) -> None:
    """Loop the daily audit over every tracked domain."""
    while True:
        for domain in domains:
            try:
                run_domain(domain)
            except Exception:
                pass
        time.sleep(86400)
