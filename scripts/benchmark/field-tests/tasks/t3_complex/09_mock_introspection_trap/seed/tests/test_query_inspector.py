"""BROKEN test — uses mock.patch on a function that query_inspector then
introspects via inspect.getsource. The MagicMock replacement makes
inspect.getsource raise TypeError, and the test fails on a misleading line.

Worker must DIAGNOSE that the test's mock is the bug (not query_inspector)
and replace this approach with real-function tests.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_detect_unsafe_on_user_query():
    """BROKEN: mocks build_user_query, then introspector tries to read its source."""
    from query_inspector import detect_unsafe
    with mock.patch("builders.build_user_query", return_value="SELECT * FROM users"):
        import builders
        assert detect_unsafe(builders.build_user_query) is True
