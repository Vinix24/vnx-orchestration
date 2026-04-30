"""
CFX-15: Static analysis for dashboard/token-dashboard/e2e/console-errors.spec.ts.

Verifies that:
1. React correctness warning patterns are NOT silenced by any console-error filter.
2. Every filter regex is preceded by a '// FILTER:' rationale comment.
3. Specific forbidden substrings (React warnings) are absent from filter patterns.
"""
import re
import pytest
from pathlib import Path

SPEC_PATH = (
    Path(__file__).parent.parent
    / "dashboard"
    / "token-dashboard"
    / "e2e"
    / "console-errors.spec.ts"
)

# React correctness warnings that must surface as test failures — never silenced.
REACT_WARNINGS = [
    'Warning: Each child in a list should have a unique "key" prop.',
    "Warning: Cannot update a component (`App`) while rendering a different component.",
    "Warning: Maximum update depth exceeded. This can happen when a component calls setState inside useEffect.",
    "Warning: validateDOMNesting(...): <div> cannot appear as a child of <p>.",
]

# Substrings whose presence inside a filter pattern is a bug — they silence real React errors.
FORBIDDEN_IN_FILTERS = [
    "Each child in a list",
    "validateDOMNesting",
    "Maximum update depth",
    "Cannot update a component",
]

# Matches TypeScript filter if-statements; allows \/  (escaped slash) inside the regex body.
_FILTER_RE = re.compile(r'if\s*\(/((?:[^/]|\\/)+)/\s*\.test\(text\)\)\s*return;')
_FILTER_LINE_RE = re.compile(r'\s*if\s*\(/.+/\s*\.test\(text\)\)\s*return;')


def _load_spec() -> str:
    return SPEC_PATH.read_text(encoding="utf-8")


def _extract_filter_patterns(spec: str) -> list[str]:
    """Return the raw regex-body strings from all filter if-statements."""
    return _FILTER_RE.findall(spec)


def _to_python_pattern(js_pattern: str) -> re.Pattern | None:
    """Best-effort conversion of a JS regex body to a compiled Python Pattern."""
    python_pattern = js_pattern.replace(r"\\/", "/").replace("\\/", "/")
    try:
        return re.compile(python_pattern)
    except re.error:
        return None


@pytest.fixture(scope="module")
def spec_content() -> str:
    return _load_spec()


@pytest.fixture(scope="module")
def filter_patterns(spec_content: str) -> list[str]:
    return _extract_filter_patterns(spec_content)


class TestConsoleErrorFilterSpec:

    def test_spec_file_exists(self) -> None:
        assert SPEC_PATH.exists(), f"E2E spec not found: {SPEC_PATH}"

    def test_filter_patterns_detected(self, filter_patterns: list[str]) -> None:
        """Sanity check: we must find at least the known filters."""
        assert len(filter_patterns) >= 5, (
            f"Expected ≥5 filter patterns, found {len(filter_patterns)}: {filter_patterns}"
        )

    def test_forbidden_substrings_absent_from_filters(self, filter_patterns: list[str]) -> None:
        """Forbidden React warning substrings must not appear inside any filter pattern string."""
        for pattern in filter_patterns:
            for forbidden in FORBIDDEN_IN_FILTERS:
                assert forbidden not in pattern, (
                    f"Filter /{pattern}/ contains forbidden substring {forbidden!r}. "
                    "This would silence a React correctness warning."
                )

    def test_react_warnings_not_silenced_by_filters(self, filter_patterns: list[str]) -> None:
        """Simulate the JavaScript filter logic in Python: React warnings must pass through."""
        compiled = [
            (raw, pat)
            for raw in filter_patterns
            if (pat := _to_python_pattern(raw)) is not None
        ]
        for warning in REACT_WARNINGS:
            for raw, compiled_re in compiled:
                assert not compiled_re.search(warning), (
                    f"React warning is silenced by filter /{raw}/:\n"
                    f"  Warning text: {warning!r}"
                )

    def test_every_filter_has_rationale_comment(self, spec_content: str) -> None:
        """Every filter if-statement must be preceded by a comment block starting with '// FILTER:'."""
        lines = spec_content.splitlines()
        for i, line in enumerate(lines):
            if _FILTER_LINE_RE.match(line):
                # Walk backward through any continuation comment lines to find the block root.
                j = i - 1
                while j >= 0 and lines[j].strip().startswith("//"):
                    j -= 1
                # j+1 is the first line of the comment block above this filter.
                comment_root = lines[j + 1].strip() if (j + 1) < i else ""
                assert comment_root.startswith("// FILTER:"), (
                    f"Line {i + 1}: filter comment block does not start with '// FILTER:'.\n"
                    f"  Filter:              {line.strip()!r}\n"
                    f"  Comment block root:  {comment_root!r}"
                )

    def test_no_react_key_warning_in_filter(self, filter_patterns: list[str]) -> None:
        for pattern in filter_patterns:
            assert "Each child in a list" not in pattern, (
                f"Filter /{pattern}/ silences the React missing-key-prop warning."
            )

    def test_no_validate_dom_nesting_in_filter(self, filter_patterns: list[str]) -> None:
        for pattern in filter_patterns:
            assert "validateDOMNesting" not in pattern, (
                f"Filter /{pattern}/ silences React DOM-nesting validation."
            )

    def test_no_maximum_update_depth_in_filter(self, filter_patterns: list[str]) -> None:
        for pattern in filter_patterns:
            assert "Maximum update depth" not in pattern, (
                f"Filter /{pattern}/ silences the React infinite-render-loop warning."
            )

    def test_no_cannot_update_component_in_filter(self, filter_patterns: list[str]) -> None:
        for pattern in filter_patterns:
            assert "Cannot update a component" not in pattern, (
                f"Filter /{pattern}/ silences the React setState-during-render warning."
            )
