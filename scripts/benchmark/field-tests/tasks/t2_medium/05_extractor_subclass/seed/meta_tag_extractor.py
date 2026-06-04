"""MetaTagExtractor — reference implementation of BaseExtractor contract.

Provided so the target lane has a concrete example. Do not modify.
"""
from __future__ import annotations

from typing import Any

from bs4 import BeautifulSoup

from base_extractor import BaseExtractor


class MetaTagExtractor(BaseExtractor):
    name = "meta_tag"

    def extract(self, html: str, url: str) -> dict[str, Any]:
        errors: list[str] = []
        meta_tags: dict[str, str] = {}
        try:
            soup = BeautifulSoup(html or "", "html.parser")
            for tag in soup.find_all("meta"):
                name = tag.get("name") or tag.get("property")
                content = tag.get("content")
                if name and content:
                    meta_tags[name] = content
        except Exception as exc:  # noqa: BLE001
            errors.append(f"parse_error: {exc}")

        return {
            "name": self.name,
            "data": {
                "tag_count": len(meta_tags),
                "tags": meta_tags,
            },
            "errors": errors,
        }
