"""report_body_contract — worker report body contract directive and validator stub.

T1 ships: build_directive() — the required-sections directive workers receive.
T2 ships: validate_body() — the heading-scan validator (stub raises NotImplementedError here).
"""

from __future__ import annotations

_DIRECTIVE_SENTINEL = "<!-- VNX-REPORT-CONTRACT-DIRECTIVE -->"
_REQUIRED_SECTIONS = ("## Summary", "## Changes", "## Verification", "## Open Items")


def build_directive(dispatch_id: str, *, pr_id: "str | None" = None) -> str:
    """Return a markdown directive enumerating the required report sections.

    Workers use the exact headings listed. Validator (T2) also accepts common
    aliases (## Files Modified, ## Test Results, ## Work Completed, ## Evidence).
    """
    sections = list(_REQUIRED_SECTIONS)
    if pr_id:
        sections.append("## PR")
    sections_formatted = "\n".join(f"- `{s}`" for s in sections)
    return (
        f"{_DIRECTIVE_SENTINEL}\n\n"
        "## Report Body Contract\n\n"
        f"Your completion report for dispatch `{dispatch_id}` MUST include these sections "
        "(exact headings; common aliases such as `## Files Modified` or `## Test Results` "
        "are also accepted by the validator):\n\n"
        f"{sections_formatted}\n\n"
        "Each section must be non-empty. `## Open Items` may contain \"None\" explicitly.\n"
    )


def validate_body(text: str, spec: object) -> object:
    """(T2) Validate report body against the spec — heading scan + alias acceptance."""
    raise NotImplementedError("validate_body is deferred to T2")
