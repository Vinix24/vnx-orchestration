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

import logging
import os
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_DIRECTIVE_SENTINEL = "<!-- VNX-REPORT-CONTRACT-DIRECTIVE -->"
_REQUIRED_SECTIONS = ("## Summary", "## Changes", "## Verification", "## Open Items")

# ADR (dispatch-20260906-oi1637-synthese-asymmetrie): the ONE receipt-status
# value both governance seams (envelope_govern.py's provider/claude_headless
# lanes, dispatch_govern.py's tmux lane) stamp when a report fails this
# contract. Already canonical fleet-wide as a failure-category status
# (event_outcome_semantics._STATUS_VOCABULARY, report_to_receipt_converter.py,
# stop_conditions.py, contract_invalid_window.py) — converging both seams onto
# it (instead of dispatch_govern's previous "failed") closes the vocabulary
# split that let the two lanes drift out of sync in the first place.
CONTRACT_INVALID_STATUS = "contract_invalid"

# The ONE receipt-status value for a report that satisfies this contract and
# declares no status of its own. This is the definition dispatch_govern already
# applies (OI-1202: "an authored report with no declared failure is done"),
# stated once so the report parser and the report converter cannot drift from
# it. It is a literal of the canonical vocabulary (event_outcome_semantics
# success category), not a new one.
AUTHORED_UNDECLARED_STATUS = "done"

# Values that are a placeholder for "no status", not a status. The unified
# report frontmatter the fabric itself writes carries ``status: unknown`` and a
# worker-authored report carries no status line at all; both declare nothing.
UNDECLARED_STATUS_PLACEHOLDERS = frozenset(
    {"", "unknown", "none", "null", "n/a", "na", "unset", "-"}
)

# ``receipt["status_source"]`` value stamped when the status was derived by
# ``resolve_undeclared_status`` instead of declared by the report, so the audit
# trail can tell a derived ``done`` from a claimed one.
DERIVED_STATUS_SOURCE = "report_contract"

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


# How much of a report is searched for its identity block (Dispatch-ID, Model,
# Provider): the first and the last IDENTITY_WINDOW characters. The directive
# tells workers to put field-style stamps in the first 3000; a worker report in
# the corpus closes with the block instead (measured: 22 of 6025 reports, all
# within 414 characters of the end but one). A block in the middle of a long
# report is not searched: quoted dispatch ids in prose live there.
IDENTITY_WINDOW = 3000


def identity_windows(text: str) -> "list[str]":
    """The parts of ``text`` that are searched for the identity block, head first.

    Head first so a report that stamps its identity on top resolves exactly as
    it always did; the tail is only consulted for a key the head did not carry.
    """
    if len(text) <= IDENTITY_WINDOW:
        return [text]
    tail = text[-IDENTITY_WINDOW:]
    if text[-IDENTITY_WINDOW - 1] != "\n":
        # The window opens mid-line. Drop that fragment: a line-anchored
        # pattern would otherwise read the tail of a longer line as a line of
        # its own.
        tail = tail.partition("\n")[2]
    return [text[:IDENTITY_WINDOW], tail]


def clean_identity_value(raw: "str | None") -> str:
    """Strip the markdown a worker wraps around an identity value.

    ``Dispatch-ID: **20260922-x**`` and ``**Dispatch-ID:** 20260922-x`` both
    reach a parser as a value that still carries ``*`` or ``:``; taken
    verbatim that value is a different dispatch identity from the clean one.
    Surrounding ``*`` and ``:`` are removed. Neither can occur inside a
    dispatch id, a model name or a provider name, so nothing legitimate is
    touched. Backticks are deliberately NOT removed: a backtick-wrapped model
    is refused on purpose (report_parser's plausibility guard, OI-1194).
    """
    return str(raw or "").strip().strip("*:").strip()


def resolve_undeclared_status(declared_status: "str | None", *, body_valid: bool) -> "str | None":
    """Status for a report that declares none, else None (nothing to derive).

    The contract requires four sections and a dispatch id. It does not require
    a status, so a report can satisfy every part of it and still carry no
    status: the receipt writers then stamped ``unknown`` (or ``no_signal``) on a
    dispatch that demonstrably delivered, and the quality score excluded it.

    Only evidence the report itself provides is used: a report that passes
    ``validate_body`` and declares no status is ``AUTHORED_UNDECLARED_STATUS``.
    A declared status is never overridden, a report that fails the body
    contract gets no derived status (there is no evidence of completion), and
    delivery (a pushed branch, a PR, a merge) is not inferred here: that is the
    gate's evidence, not the report's.
    """
    if str(declared_status or "").strip().lower() not in UNDECLARED_STATUS_PLACEHOLDERS:
        return None
    return AUTHORED_UNDECLARED_STATUS if body_valid else None


def build_directive(
    dispatch_id: str,
    *,
    pr_id: "str | None" = None,
    model: "str | None" = None,
    provider: "str | None" = None,
) -> str:
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

    OI-1850: this is the ONE place a worker is told which headings its report
    carries. It reads ``_REQUIRED_SECTIONS``, the same constant ``validate_body``
    checks, and every lane appends it through ``with_directive``: the fabric
    prompt (``prompts/base_worker.md``) names no heading list of its own. The
    identity block below is a request for the same reason as ``**PR_Ref**``: it
    adds no heading ``validate_body()`` checks, but a dispatch-lane report
    without a real ``**Model**`` never becomes a receipt (see the module
    docstring). ``model``/``provider`` fill the values in when the lane knows
    them; otherwise the worker is told what shape they take.
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
    model_value = (model or "").strip() or (
        "<the short id of the model you run as, for example sonnet or opus; "
        "no spaces, no backticks>"
    )
    provider_value = (provider or "").strip() or (
        "<the provider you run on, for example claude, kimi, glm or deepseek>"
    )
    identity_note = (
        "\nIdentity block: stamp these three bold fields within the first 3000 "
        "characters of your report (or as frontmatter). A dispatch report that "
        "names no real model is refused at receipt-write time, so without them "
        "your work never reaches the audit trail:\n\n"
        f"- `**Dispatch-ID**: {dispatch_id}`\n"
        f"- `**Model**: {model_value}`\n"
        f"- `**Provider**: {provider_value}`\n"
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
        f"{identity_note}"
    )


_DIRECTIVE_FLAG = "VNX_REPORT_CONTRACT_DIRECTIVE"


def directive_enabled() -> bool:
    """False only when the operator switched the directive off (default: on)."""
    return os.environ.get(_DIRECTIVE_FLAG, "1").strip().lower() not in (
        "0", "false", "no", "off"
    )


def with_directive(
    body: str,
    dispatch_id: str,
    *,
    pr_id: "str | None" = None,
    model: "str | None" = None,
    provider: "str | None" = None,
) -> str:
    """Return *body* ending in the report directive, exactly once.

    The single door every lane appends the directive through. Idempotent: a body
    that already carries the directive sentinel is returned unchanged, so a lane
    that composes on top of another lane's output can never put the directive in
    one prompt twice.
    """
    if not directive_enabled() or _DIRECTIVE_SENTINEL in body:
        return body
    return (
        body
        + "\n\n"
        + build_directive(dispatch_id, pr_id=pr_id, model=model, provider=provider)
    )


# A heading written INSIDE a line ("`## Summary` / `## Changes`", "- ## Summary")
# is a reference to a report heading; one that opens the line is the role file's
# own structure. Titles stop at the separators such lists use.
_HEADING_REFERENCE = re.compile(r"##[ \t]+([^`/,;|\n]+)")
_FENCE = "```"
_LIST_CONTEXT_LINES = 2


def _known_report_headings() -> "set[str]":
    known = set(_REQUIRED_SECTIONS) | {"## PR"}
    for aliases in _SECTION_ALIASES.values():
        known.update(aliases)
    return known


def _heading_reference_lines(text: str) -> "list[tuple[list[str], int | None]]":
    """Per line of *text*: the report headings it references, and its code fence.

    Outside a code fence a reference is an H2 that does not open the line.
    Inside a fence every H2 line counts: that is a report skeleton. The second
    item is the number of the fence the line sits in, None outside one.
    """
    fence_id = 0
    in_fence = False
    per_line: "list[tuple[list[str], int | None]]" = []
    for line in text.splitlines():
        if line.strip().startswith(_FENCE):
            if not in_fence:
                fence_id += 1
            in_fence = not in_fence
            per_line.append(([], fence_id))
            continue
        if in_fence:
            match = re.match(r"\s*##[ \t]+(.+?)\s*$", line)
            titles = [match.group(1)] if match else []
        else:
            titles = [
                m.group(1) for m in _HEADING_REFERENCE.finditer(line) if m.start() > 0
            ]
        refs = [f"## {t.strip().rstrip('.:').strip()}" for t in titles]
        per_line.append((refs, fence_id if in_fence else None))
    return per_line


def _report_heading_lists(text: str) -> "list[list[str]]":
    """Runs of references that read as a report-heading list.

    A run is consecutive lines that reference headings, or one whole code fence
    (a skeleton has body lines between its headings). It is a list when it names
    at least two headings and either names one the validator knows or sits in a
    passage about the report.
    """
    lines = text.splitlines()
    per_line = _heading_reference_lines(text)
    known = _known_report_headings()
    lists: "list[list[str]]" = []
    index = 0
    while index < len(per_line):
        if not per_line[index][0]:
            index += 1
            continue
        fence = per_line[index][1]
        end = index
        while end < len(per_line) and (
            per_line[end][1] == fence if fence is not None
            else per_line[end][0] and per_line[end][1] is None
        ):
            end += 1
        refs = [ref for line_refs, _ in per_line[index:end] for ref in line_refs]
        context = " ".join(lines[max(0, index - _LIST_CONTEXT_LINES):end]).lower()
        if len(refs) >= 2 and (any(ref in known for ref in refs) or "report" in context):
            lists.append(refs)
        index = end
    return lists


def divergent_report_headings(text: str) -> "tuple[list[str], list[str]]":
    """How the report-heading lists in *text* differ from what the validator reads.

    Returns ``(unknown, missing)``: headings a list names that are neither in
    ``_REQUIRED_SECTIONS`` nor one of its aliases, and required sections no
    listed heading satisfies. Both are empty for text without a list, and for a
    list that says what the contract says. Case counts, as it does in
    ``validate_body``.
    """
    lists = _report_heading_lists(text)
    if not lists:
        return [], []
    known = _known_report_headings()
    listed = {ref for refs in lists for ref in refs}
    unknown: "list[str]" = []
    for refs in lists:
        for ref in refs:
            if ref not in known and ref not in unknown:
                unknown.append(ref)
    missing = [
        section
        for section in _REQUIRED_SECTIONS
        if section not in listed
        and not any(alias in listed for alias in _SECTION_ALIASES.get(section, ()))
    ]
    return unknown, missing


def warn_on_divergent_headings(text: str, source: str) -> bool:
    """Log a warning when the role text *source* lists other report headings.

    Never blocks: the directive ``with_directive`` appends after the role text
    is what the worker reads last, and ``validate_body`` is what judges the
    report. The warning exists so a role file that drifted from the contract is
    found on the first dispatch that loads it, not after its reports fail.
    Returns True when a warning was logged.
    """
    unknown, missing = divergent_report_headings(text)
    if not unknown and not missing:
        return False
    logger.warning(
        "role text %s lists report headings that differ from the report contract "
        "(not in the contract: %s; contract sections it never names: %s); "
        "the Report Body Contract directive at the end of the prompt wins",
        source,
        ", ".join(unknown) or "none",
        ", ".join(missing) or "none",
    )
    return True


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
