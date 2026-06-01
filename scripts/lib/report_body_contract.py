"""report_body_contract — worker report body contract directive and validator.

T1 ships: build_directive() — the required-sections directive workers receive.
T2 ships: validate_body() — the heading-scan validator with alias acceptance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_DIRECTIVE_SENTINEL = "<!-- VNX-REPORT-CONTRACT-DIRECTIVE -->"
_REQUIRED_SECTIONS = ("## Summary", "## Changes", "## Verification", "## Open Items")

# Aliases accepted by the validator so existing authored reports do not break.
_SECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "## Changes": ("## Files Modified", "## Work Completed"),
    "## Verification": ("## Test Results", "## Evidence", "## Tests"),
}

# Summary must not match this prefix — it is the placeholder body injected by
# the old _emit_unified_report stub before govern() was wired in.
_PLACEHOLDER_PATTERN = re.compile(
    r"Interactive tmux dispatch \(lane: tmux_interactive\)\. Status:"
)

_MIN_SUMMARY_CHARS = 50


@dataclass
class BodyResult:
    valid: bool
    missing: list[str] = field(default_factory=list)
    placeholder: bool = False
    # "authored" = passes all checks; "violated" = any check failed;
    # "synthesized" is used externally by govern() on synthesized bodies.
    status: str = "authored"


def build_directive(dispatch_id: str, *, pr_id: "str | None" = None) -> str:
    """Return a markdown directive enumerating the required report sections.

    Workers use the exact headings listed. Validator also accepts common
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


def validate_body(text: str, *, pr_id: "str | None" = None) -> BodyResult:
    """Validate report body against the required-sections contract.

    Heading scan with alias acceptance. Checks:
    - All required sections present (or alias present).
    - ## Summary >= 50 non-whitespace chars.
    - ## Summary does not match the placeholder pattern.
    - ## PR present when pr_id is set (F4).
    """
    if not text:
        missing = list(_REQUIRED_SECTIONS)
        if pr_id:
            missing.append("## PR")
        return BodyResult(valid=False, missing=missing, placeholder=False, status="violated")

    # Extract all level-2 headings present in the text.
    found_headings: set[str] = set(re.findall(r"^## .+", text, re.MULTILINE))

    missing: list[str] = []
    for section in _REQUIRED_SECTIONS:
        if section in found_headings:
            continue
        aliases = _SECTION_ALIASES.get(section, ())
        if any(alias in found_headings for alias in aliases):
            continue
        missing.append(section)

    if pr_id and "## PR" not in found_headings:
        missing.append("## PR")

    # Extract ## Summary content and check emptiness/placeholder.
    placeholder = False
    summary_text = _extract_section(text, "## Summary")
    if summary_text is not None:
        non_ws = re.sub(r"\s+", "", summary_text)
        if len(non_ws) < _MIN_SUMMARY_CHARS:
            if "## Summary" not in missing:
                missing.append("## Summary (too short)")
        if _PLACEHOLDER_PATTERN.search(summary_text):
            placeholder = True

    valid = not missing and not placeholder
    status = "authored" if valid else "violated"
    return BodyResult(valid=valid, missing=missing, placeholder=placeholder, status=status)


def _extract_section(text: str, heading: str) -> "str | None":
    """Return the content of a section between *heading* and the next ## heading."""
    pattern = re.compile(
        rf"^{re.escape(heading)}\s*\n(.*?)(?=^## |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(text)
    if m is None:
        return None
    return m.group(1)
