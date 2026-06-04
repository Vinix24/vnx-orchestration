# Task 05 — New BaseExtractor subclass (EmailLinkExtractor)

Source-inspiratie: SEOcrawler v2 extractor pattern. Tier: T2 medium. Deadline: 20 minutes wallclock.

## Context

The seed includes a `base_extractor.py` defining an abstract `BaseExtractor` class and one concrete reference subclass `MetaTagExtractor` for orientation. You add a new subclass `EmailLinkExtractor` that pulls `mailto:` links from HTML and reports counts + unique addresses.

This is a typical extractor-toevoeging task from the SEOcrawler codebase shape.

## BaseExtractor contract (in seed)

```python
class BaseExtractor(ABC):
    name: str = "unnamed"

    @abstractmethod
    def extract(self, html: str, url: str) -> dict:
        """Return {'name': self.name, 'data': {...}, 'errors': [...]}"""
```

`MetaTagExtractor` (provided) shows the pattern: parse HTML, pull data, return the standard envelope.

## Required deliverable

### `email_link_extractor.py`

A class `EmailLinkExtractor(BaseExtractor)` with `name = "email_link"`. The `extract(html, url)` method must:

1. Parse the HTML using `BeautifulSoup` (`from bs4 import BeautifulSoup`)
2. Find all `<a>` tags with an `href` that starts with `mailto:`
3. Extract the email address (strip `mailto:` and any `?subject=...` query suffix)
4. Lowercase + dedupe addresses
5. Return:
   ```python
   {
       "name": "email_link",
       "data": {
           "total_links": <int>,        # count of mailto links found
           "unique_emails": <int>,      # count of unique addresses
           "emails": [<sorted list>],
       },
       "errors": [],
   }
   ```
6. Empty input or no matches: return zero-counts + empty list, NOT an error

### `tests/test_email_link_extractor.py`

5 tests:
- `test_empty_html_returns_zero` — empty HTML → 0 total, 0 unique, [] emails
- `test_single_mailto_link` — one `<a href="mailto:user@example.com">` → 1 total, 1 unique
- `test_dedup_lowercases` — same email with different casing → 1 unique
- `test_subject_suffix_stripped` — `mailto:user@example.com?subject=Hi` → `user@example.com`
- `test_returns_standard_envelope` — return dict has `name`, `data`, `errors` keys (envelope contract)

## Files you may create

- `email_link_extractor.py` (create)
- `tests/test_email_link_extractor.py` (create)
- `tests/__init__.py` (create, empty)

Do NOT modify `base_extractor.py` or `meta_tag_extractor.py`.

## Definition of done

- 5 tests pass: `pytest tests/test_email_link_extractor.py -v`
- `EmailLinkExtractor().extract(html, "https://example.com")` returns the envelope shape on any input
- No new external deps besides `bs4` (already in seed `requirements.txt`)
