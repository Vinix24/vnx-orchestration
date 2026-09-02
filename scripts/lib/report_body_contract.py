"""report_body_contract — worker report body contract directive and validator.

T1 ships: build_directive() — the required-sections directive workers receive.
T2 ships: validate_body() — the heading-scan validator with alias acceptance.

Contract gap (documented, not enforced here — dispatch-20260804-064708-pra-
converter-resilience / OI-998): this module's contract requires Summary,
Changes, Verification, Open Items, and a Dispatch-ID. It does NOT require a
Model or Provider field. But scripts/lib/append_receipt_internals/validation.py
(``_validate_model_present``) fail-closed-refuses to WRITE a receipt for any
dispatch-lane report that lacks a real Model — a report can pass
``validate_body()`` here cleanly and still never produce a receipt. That
refusal is intentional (a worker dispatch receipt must name the model that
ran) and is logged loudly by the converter (WARNING, dispatch-id + reason;
scripts/lib/report_to_receipt_converter.py), not silently. The fix for the
gap is documentation, not validation: ``validate_body()`` is deliberately
NOT made to enforce Model/Provider, since that would break every existing
report producer that predates the fail-closed check. A dispatch-lane report
therefore needs an identity block (Model + Provider, bold-field or
frontmatter) IN ADDITION TO the sections below — see the CLAUDE.md "Mandatory
Report Contract" section, which documents both.
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

    OI-1599: the ``**PR_Ref**`` request below is UNCONDITIONAL — it does not
    gate on ``pr_id``, unlike the ``## PR`` section above it. ``pr_id`` is only
    non-None when a PR already existed BEFORE this dispatch ran; a dispatch
    that creates a brand-new PR cannot know its number until ``gh pr create``
    runs mid-dispatch, so gating the request on ``pr_id`` (as the ``## PR``
    section does) would never ask exactly the dispatches that most need
    asking. This is a request, not a requirement: it adds no heading
    ``validate_body()`` checks for, so a report that never produces a PR is
    never marked invalid for omitting it, and no existing report is retroactively
    broken by this change.
    """
    sections = list(_REQUIRED_SECTIONS)
    if pr_id:
        sections.append("## PR")
    sections_formatted = "\n".join(f"- `{s}`" for s in sections)
    pr_ref_note = (
        "\nIf this dispatch creates or updates a pull request, also stamp its "
        "number as a bold field within the first 3000 characters of your report "
        "(or as frontmatter `pr_ref`): `**PR_Ref**: #1234`. This is how the "
        "receipt converter links the PR to this dispatch's review-gate "
        "obligation — without it the obligation can never be matched to a PR "
        "that did not exist yet when the dispatch was registered. Omit this "
        "field entirely when this dispatch does not produce a PR.\n"
    )
    return (
        f"{_DIRECTIVE_SENTINEL}\n\n"
        "## Report Body Contract\n\n"
        f"Your completion report for dispatch `{dispatch_id}` MUST include these sections "
        "(exact headings; common aliases such as `## Files Modified` or `## Test Results` "
        "are also accepted by the validator):\n\n"
        f"{sections_formatted}\n\n"
        "Each section must be non-empty. `## Open Items` may contain \"None\" explicitly.\n"
        f"{pr_ref_note}"
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
