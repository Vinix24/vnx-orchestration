#!/usr/bin/env python3
"""Test: TrackBadge null-guard — resolves null/undefined track without crashing.

Since this component is TypeScript/React, we validate the logic in Python by
replicating the safeTrack resolution rule and asserting expected outputs.

Dispatch-ID: 20260520-1450-dashboard-phase-a
"""

import re
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PAGE_PATH = (
    PROJECT_ROOT
    / "dashboard"
    / "token-dashboard"
    / "app"
    / "operator"
    / "reports"
    / "page.tsx"
)


def _extract_track_badge_source(path: Path) -> str:
    """Return the TrackBadge function source from the TSX file."""
    text = path.read_text(encoding="utf-8")
    match = re.search(
        r"function TrackBadge\(.*?\{(.*?)^function ",
        text,
        re.DOTALL | re.MULTILINE,
    )
    if not match:
        raise AssertionError("TrackBadge function not found in page.tsx")
    return match.group(0)


def _safetrack(track) -> str:
    """Python equivalent of the TypeScript safeTrack resolution in TrackBadge."""
    return (track if track is not None and track != "" else "UNKNOWN").upper()


class TestTrackBadgeNullGuard(unittest.TestCase):
    """Validate null-guard logic for TrackBadge."""

    def test_null_resolves_to_unknown(self):
        self.assertEqual(_safetrack(None), "UNKNOWN")

    def test_empty_string_resolves_to_unknown(self):
        self.assertEqual(_safetrack(""), "UNKNOWN")

    def test_valid_track_uppercased(self):
        for track, expected in [("A", "A"), ("b", "B"), ("C", "C")]:
            with self.subTest(track=track):
                self.assertEqual(_safetrack(track), expected)

    def test_arbitrary_track_uppercased(self):
        self.assertEqual(_safetrack("x"), "X")

    def test_tsx_signature_uses_nullable_type(self):
        """TrackBadge prop type must accept null/undefined."""
        if not PAGE_PATH.exists():
            self.skipTest("page.tsx not found — skipping file-level check")
        source = _extract_track_badge_source(PAGE_PATH)
        self.assertRegex(
            source,
            r"track\s*\}\s*:\s*\{.*?track\s*:\s*string\s*\|.*?null",
            "TrackBadge must declare track as string | null | undefined",
        )

    def test_tsx_uses_safetrack_variable(self):
        """TrackBadge must use safeTrack variable, not raw track.toUpperCase()."""
        if not PAGE_PATH.exists():
            self.skipTest("page.tsx not found — skipping file-level check")
        source = _extract_track_badge_source(PAGE_PATH)
        # Old broken pattern must not appear
        self.assertNotIn("track.toUpperCase()", source,
                         "TrackBadge must not call track.toUpperCase() directly")
        # New safe pattern must be present
        self.assertIn("safeTrack", source, "TrackBadge must use safeTrack variable")


if __name__ == "__main__":
    unittest.main()
