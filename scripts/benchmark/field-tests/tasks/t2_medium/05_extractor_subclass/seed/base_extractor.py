"""BaseExtractor abstract contract for the bench task.

Real extractors in SEOcrawler v2 inherit from this shape. The benchmark seed
provides one reference subclass (MetaTagExtractor) so the target lane has a
working example before writing EmailLinkExtractor.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseExtractor(ABC):
    name: str = "unnamed"

    @abstractmethod
    def extract(self, html: str, url: str) -> dict[str, Any]:
        """Return a standard envelope:

        {
            'name': self.name,
            'data': {...},     # extractor-specific payload
            'errors': [...],   # empty list if successful
        }
        """
        raise NotImplementedError
