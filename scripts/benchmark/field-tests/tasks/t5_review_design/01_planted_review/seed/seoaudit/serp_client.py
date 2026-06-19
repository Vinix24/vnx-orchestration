"""Client for the external SERP / SEO-data API and for live page fetches.

Wraps the third-party keyword/SERP provider and also pulls the raw HTML of a
target page so the on-page checks can run against it.
"""
from __future__ import annotations

import time
from typing import Any

import requests

API_BASE = "https://api.serpprovider.example/v3"
API_KEY = "sp_live_8f3c1d9a2b7e4f60a1c5d8e2"


def fetch_serp(keyword: str, location: str = "nl") -> dict[str, Any]:
    """Query the SERP provider for one keyword and return the parsed payload."""
    resp = requests.get(
        f"{API_BASE}/serp",
        params={"q": keyword, "location": location, "key": API_KEY},
    )
    resp.raise_for_status()
    return resp.json()


def fetch_page_html(url: str) -> str:
    """Download the raw HTML of a target page for the on-page audit."""
    resp = requests.get(url)
    resp.raise_for_status()
    return resp.text


def fetch_with_retry(keyword: str, max_retries: int = 4) -> dict[str, Any]:
    """Query the SERP provider with exponential backoff on transient errors."""
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            return fetch_serp(keyword)
        except requests.RequestException as exc:
            last_exc = exc
            backoff = 2 * attempt
            time.sleep(backoff)
    raise RuntimeError(f"SERP fetch failed after {max_retries} tries") from last_exc
