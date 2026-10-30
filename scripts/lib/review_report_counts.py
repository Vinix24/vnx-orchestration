"""The warning and pre-scan-notice counts of one gate verdict's report (OI-2476).

Track ``tweede-stoel-op-signaal``, PR 1. The merge door's second-seat rule
(PR 2 of the track) asks whether the first seat's verdict carried a real
warning. That answer has to be bound to the verdict itself: the markdown
report is not addressable per verdict, and ``results/pr-<n>-<gate>.json`` is
overwritten per PR and gate. So the two counts are read from the report the
verdict names (``report_path``) and written into that verdict's round-ledger
line and its ``review_gate_result`` receipt.

Two numbers, one report section:

* **real warnings** — every advisory finding the report lists under
  ``### Advisory`` that is not a pre-scan notice;
* **pre-scan notices** — the deterministic diff scan's findings. Their
  message begins with :data:`PRESCAN_NOTICE_PREFIX`; the measurement of
  2026-10-30 (``claudedocs/2026-10-30-tweede-stoel-op-waarschuwing.md``)
  treats them as noise, never as a warning.

**Unknown is not zero.** A count is ``None`` when it cannot be read with
certainty: no ``report_path``, an unreadable file, a report without the
normalized ``## Findings`` section ``gate_artifacts._format_findings_section``
writes, or an advisory list that does not reconcile with the report's own
``Advisory count``. Zero is reserved for a report that really says the
reviewer wrote no advisory findings. A later rule that reads unknown as zero
would never fire the second seat; a reader that reads it as "known clean"
would skip it exactly where the evidence is missing.

The reader is scoped to the ``## Findings`` section on purpose. The raw
``## Gate Output`` transcript below it can quote the report's own vocabulary
(a ``### Advisory`` heading in a reviewed markdown file, a ``severity:
warning`` literal), and counting that would be counting what the reviewer
merely read (the OI-1394 lesson behind ``closure_verifier``'s own scoped
reader).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Mapping, Optional

#: A finding whose message starts with this is the deterministic pre-scan's
#: notice, not a warning of the reviewer. ``gate_prompt.INSTRUCTION_FINDING_PREFIX``
#: owns the string for the writer; a test pins the two to the same value so this
#: reader cannot drift without the writer's side noticing.
PRESCAN_NOTICE_PREFIX = "instruction-like text in diff"

_FINDINGS_SECTION_RE = re.compile(r"^## Findings[ \t]*$(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)
_ADVISORY_SECTION_RE = re.compile(r"^### Advisory[ \t]*$(.*?)(?=^#{1,3} |\Z)", re.MULTILINE | re.DOTALL)
_ADVISORY_COUNT_RE = re.compile(r"^Advisory count:[ \t]*(\d+)[ \t]*$", re.MULTILINE)
_ADVISORY_BULLET_RE = re.compile(r"^[ \t]*-[ \t]+(.+?)[ \t]*$", re.MULTILINE)


@dataclass(frozen=True)
class ReportCounts:
    """The two counts of one verdict, read from that verdict's own report."""

    warnings: int
    notices: int


def parse_report_counts(text: Optional[str]) -> Optional[ReportCounts]:
    """Count ``text``'s real warnings and pre-scan notices; ``None`` when unknowable.

    The report must carry the normalized findings block ``gate_artifacts``
    writes: a ``## Findings`` heading, an ``Advisory count: N`` line, and (when
    ``N`` is not zero) a ``### Advisory`` list of exactly ``N`` bullets. A
    report that fails any of those checks is unknown, never zero: a partially
    readable list could hide the one warning the second-seat rule waits for.
    """
    section = _findings_section(text)
    if section is None:
        return None
    declared = _declared_advisory_count(section)
    if declared is None:
        return None
    bullets = _advisory_bullets(section)
    if len(bullets) != declared:
        return None
    notices = sum(1 for message in bullets if message.startswith(PRESCAN_NOTICE_PREFIX))
    return ReportCounts(warnings=len(bullets) - notices, notices=notices)


def counts_from_report(report_path: Any) -> Optional[ReportCounts]:
    """The counts of the report at ``report_path``; ``None`` when it cannot be read.

    A missing path, a missing file, a directory, undecodable bytes and an
    unexpected I/O failure are all unknown. This function is called on the
    verdict's write path, so it never raises: a report that cannot be read
    must not fail the verdict that already exists on disk.
    """
    text = _read_report(report_path)
    return None if text is None else parse_report_counts(text)


def counts_for_payload(payload: Mapping[str, Any]) -> Optional[ReportCounts]:
    """The counts of the verdict ``payload`` books, from its own ``report_path``.

    A takeover is unknown without reading anything: the verdict the door then
    counts was produced by a walk that took the seat over, and the plan's
    input contract sends it to "unknown" instead of reading an earlier or
    foreign report. Every other verdict reads exactly the report its own
    payload names.
    """
    if payload.get("takeover") is True:
        return None
    return counts_from_report(payload.get("report_path"))


def _read_report(report_path: Any) -> Optional[str]:
    raw = str(report_path or "").strip()
    if not raw:
        return None
    try:
        return Path(raw).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):  # vnx-silent-except: an unreadable report books unknown, never a crash on the verdict write path
        return None


def _findings_section(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    match = _FINDINGS_SECTION_RE.search(text)
    return match.group(1) if match else None


def _declared_advisory_count(section: str) -> Optional[int]:
    match = _ADVISORY_COUNT_RE.search(section)
    return int(match.group(1)) if match else None


def _advisory_bullets(section: str) -> List[str]:
    """The advisory list's bullet messages; ``[]`` when the report lists none.

    An absent ``### Advisory`` section is only consistent with a declared
    count of zero, which the caller enforces by comparing lengths: a report
    that declares advisories but writes no list stays unknown.
    """
    match = _ADVISORY_SECTION_RE.search(section)
    if match is None:
        return []
    return _ADVISORY_BULLET_RE.findall(match.group(1))
