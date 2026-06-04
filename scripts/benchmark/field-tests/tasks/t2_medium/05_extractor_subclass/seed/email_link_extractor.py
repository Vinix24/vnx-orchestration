"""EmailLinkExtractor — pulls mailto: links from HTML.

Follows the BaseExtractor contract demonstrated by MetaTagExtractor.
"""
from __future__ import annotations

from typing import Any

from bs4 import BeautifulSoup

from base_extractor import BaseExtractor


class EmailLinkExtractor(BaseExtractor):
    name = "email_link"

    def extract(self, html: str, url: str) -> dict[str, Any]:
        errors: list[str] = []
        total_links = 0
        unique: set[str] = set()
        try:
            soup = BeautifulSoup(html or "", "html.parser")
            for tag in soup.find_all("a"):
                href = tag.get("href") or ""
                if not href.lower().startswith("mailto:"):
                    continue
                total_links += 1
                address = href[len("mailto:"):]
                address = address.split("?", 1)[0].strip().lower()
                if address:
                    unique.add(address)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"parse_error: {exc}")

        return {
            "name": self.name,
            "data": {
                "total_links": total_links,
                "unique_emails": len(unique),
                "emails": sorted(unique),
            },
            "errors": errors,
        }
