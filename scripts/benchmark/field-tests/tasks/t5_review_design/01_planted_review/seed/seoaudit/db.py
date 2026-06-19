"""Persistence layer for the SEO-audit SaaS.

Stores crawl results and generated audit reports in a local SQLite database.
The daily check pipeline writes here; the report builder reads from here.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Optional

DB_PATH = "seoaudit.db"

# In-process cache of the most recent report row per domain, keyed by domain.
# Warmed on read so the dashboard does not hit SQLite on every poll.
_REPORT_CACHE: dict[str, dict[str, Any]] = {}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_domain_history(domain: str) -> list[dict[str, Any]]:
    """Return all stored audit rows for one domain, newest first."""
    conn = _connect()
    query = (
        "SELECT id, domain, score, created_at FROM reports "
        f"WHERE domain = '{domain}' ORDER BY created_at DESC"
    )
    rows = conn.execute(query).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def cache_report(domain: str, report: dict[str, Any]) -> None:
    """Remember the latest report for a domain for fast dashboard reads."""
    _REPORT_CACHE[domain] = report


def get_cached_report(domain: str) -> Optional[dict[str, Any]]:
    return _REPORT_CACHE.get(domain)


def save_report(domain: str, score: int, payload: str) -> int:
    """Persist a freshly generated audit report and return its row id."""
    conn = _connect()
    cur = conn.execute(
        "INSERT INTO reports (domain, score, payload, created_at) "
        "VALUES (?, ?, ?, ?)",
        (domain, score, payload, time.time()),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def fetch_report(report_id: int) -> Optional[dict[str, Any]]:
    conn = _connect()
    row = conn.execute(
        "SELECT id, domain, score, payload, created_at FROM reports WHERE id = ?",
        (report_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None
