"""Tests for EmailLinkExtractor."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from email_link_extractor import EmailLinkExtractor


def test_empty_html_returns_zero():
    result = EmailLinkExtractor().extract("", "https://example.com")
    assert result["data"]["total_links"] == 0
    assert result["data"]["unique_emails"] == 0
    assert result["data"]["emails"] == []
    assert result["errors"] == []


def test_single_mailto_link():
    html = '<a href="mailto:user@example.com">Contact</a>'
    result = EmailLinkExtractor().extract(html, "https://example.com")
    assert result["data"]["total_links"] == 1
    assert result["data"]["unique_emails"] == 1
    assert result["data"]["emails"] == ["user@example.com"]


def test_dedup_lowercases():
    html = (
        '<a href="mailto:User@Example.com">a</a>'
        '<a href="mailto:user@example.com">b</a>'
    )
    result = EmailLinkExtractor().extract(html, "https://example.com")
    assert result["data"]["total_links"] == 2
    assert result["data"]["unique_emails"] == 1
    assert result["data"]["emails"] == ["user@example.com"]


def test_subject_suffix_stripped():
    html = '<a href="mailto:user@example.com?subject=Hi">a</a>'
    result = EmailLinkExtractor().extract(html, "https://example.com")
    assert result["data"]["emails"] == ["user@example.com"]
    assert result["data"]["unique_emails"] == 1


def test_returns_standard_envelope():
    result = EmailLinkExtractor().extract("<html></html>", "https://example.com")
    assert "name" in result
    assert "data" in result
    assert "errors" in result
    assert result["name"] == "email_link"
