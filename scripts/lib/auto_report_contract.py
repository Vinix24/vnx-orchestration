"""Auto-Report Pipeline Contract — PR-0 Schema Definition.

This module defines all schemas, contracts, and taxonomies for the F37
auto-report pipeline. It governs how stop hooks, deterministic extraction,
haiku classification, and report assembly interact.

Architecture:
    Stop hook fires → deterministic extraction → haiku classification →
    report assembly → receipt processor integration

All schemas are frozen dataclasses for immutability. Validation is strict:
construction raises ValueError on invalid input.

Non-Goals (V1):
    - No dashboard changes (auto-reports render via existing report viewer)
    - No receipt format breaking changes (auto-reports produce standard receipts)
    - No embedding/vector infrastructure (tag-based retrieval only)
    - No autonomous policy changes (intelligence is advisory-only)
    - No open-ended tag extensibility (taxonomy is closed in V1)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


# ─── Schema Version ──────────────────────────────────────────────────────────

SCHEMA_VERSION = "1.0.0"


# ─── Closed Tag Taxonomy ─────────────────────────────────────────────────────
#
# V1 taxonomy is CLOSED. No free-form tags. All values must be from these enums.
# Extension requires a schema version bump.

class DispatchType(str, Enum):
    """Set by T0 at dispatch creation."""
    IMPLEMENTATION = "implementation"
    TEST = "test"
    REVIEW = "review"
    REFACTOR = "refactor"
    DOCS = "docs"
    CONFIG = "config"
    PLANNING = "planning"


class RiskLevel(str, Enum):
    """Set by T0 at dispatch creation; may be overridden by blast radius."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Scope(str, Enum):
    """Set by T0 at dispatch creation."""
    SINGLE_FILE = "single_file"
    MULTI_FILE = "multi_file"
    CROSS_MODULE = "cross_module"
    INFRASTRUCTURE = "infrastructure"


class ContentType(str, Enum):
    """Set by haiku classification or rule-based fallback."""
    IMPLEMENTATION = "implementation"
    TEST = "test"
    REFACTOR = "refactor"
    DOCS = "docs"
    REVIEW = "review"
    CONFIG = "config"
    PLANNING = "planning"
    MIXED = "mixed"


class Complexity(str, Enum):
    """Set by haiku classification or rule-based fallback."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class OutcomeStatus(str, Enum):
    """Final dispatch outcome."""
    SUCCESS = "success"
    FAILURE = "failure"
    PARTIAL = "partial"
    CRASHED = "crashed"
    NO_EXECUTION = "no_execution"


# ─── Stop Hook Contract ──────────────────────────────────────────────────────
#
# Claude Code delivers this JSON on stdin to the Stop hook.
# See: https://docs.anthropic.com/en/docs/claude-code/hooks
#
# Exit codes:
#   0  — success (allow stop, optional JSON on stdout)
#   2  — blocking error (prevent stop, stderr fed back to Claude)
#   other — non-blocking error (logged, stop proceeds)

@dataclass(frozen=True)
class StopHookInput:
    """JSON delivered on stdin by Claude Code when the Stop hook fires.

    The hook fires once per assistant turn when Claude finishes all work.
    It does NOT fire on user interrupts or API errors.
    """
    session_id: str
    transcript_path: str
    cwd: str
    hook_event_name: str = "Stop"
    permission_mode: str = "default"

    @classmethod
    def from_stdin(cls, raw: str) -> StopHookInput:
        """Parse JSON from stdin into StopHookInput."""
        data = json.loads(raw)
        return cls(
            session_id=data["session_id"],
            transcript_path=data["transcript_path"],
            cwd=data["cwd"],
            hook_event_name=data.get("hook_event_name", "Stop"),
            permission_mode=data.get("permission_mode", "default"),
        )

    def detect_terminal(self) -> Optional[str]:
        """Detect VNX terminal ID from cwd path.

        Returns T1, T2, or T3 if cwd matches .claude/terminals/T{N}.
        Returns None for T0 or non-terminal paths.
        """
        match = re.search(r"\.claude/terminals/(T[1-3])\b", self.cwd)
        return match.group(1) if match else None


@dataclass(frozen=True)
class StopHookOutput:
    """JSON written to stdout by the stop hook on success (exit 0).

    If the hook needs to block the stop (exit 2), write reason to stderr instead.
    """
    auto_report_path: Optional[str] = None
    dispatch_id: Optional[str] = None
    terminal: Optional[str] = None
    skipped: bool = False
    skip_reason: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))


# ─── Dispatch Tags ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DispatchTags:
    """Tags set by T0 at dispatch creation. Stored in bundle.json."""
    dispatch_type: DispatchType
    risk: RiskLevel
    scope: Scope
    expected_ois: int = 0
    depends_on: tuple = ()  # Upstream dispatch IDs (tuple for frozen)

    def __post_init__(self) -> None:
        if self.expected_ois < 0:
            raise ValueError(f"expected_ois must be >= 0, got {self.expected_ois}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dispatch_type": self.dispatch_type.value,
            "risk": self.risk.value,
            "scope": self.scope.value,
            "expected_ois": self.expected_ois,
            "depends_on": list(self.depends_on),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> DispatchTags:
        return cls(
            dispatch_type=DispatchType(data["dispatch_type"]),
            risk=RiskLevel(data["risk"]),
            scope=Scope(data["scope"]),
            expected_ois=data.get("expected_ois", 0),
            depends_on=tuple(data.get("depends_on", ())),
        )


# ─── Extraction Results ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class TestResults:
    """Parsed from pytest output in event stream or Bash tool results."""
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    duration_seconds: float = 0.0
    raw_output: str = ""  # Truncated to 500 chars max

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.errors + self.skipped

    @property
    def all_passed(self) -> bool:
        return self.failed == 0 and self.errors == 0 and self.total > 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> TestResults:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass(frozen=True)
class SyntaxCheck:
    """Result of py_compile or bash -n on a changed file."""
    file_path: str
    language: str  # "python" or "shell"
    valid: bool
    error_message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GitProvenance:
    """Git state captured at extraction time."""
    commit_hash: str = ""
    commit_message: str = ""
    branch: str = ""
    files_changed: tuple = ()  # Tuple of file paths (frozen)
    insertions: int = 0
    deletions: int = 0
    is_dirty: bool = False

    @property
    def line_delta(self) -> int:
        return self.insertions - self.deletions

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["files_changed"] = list(self.files_changed)
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> GitProvenance:
        data = dict(data)
        data["files_changed"] = tuple(data.get("files_changed", ()))
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass(frozen=True)
class EventMetrics:
    """Aggregated metrics from the subprocess event stream."""
    tool_use_count: int = 0
    text_block_count: int = 0
    thinking_block_count: int = 0
    error_count: int = 0
    session_duration_seconds: int = 0
    model_used: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExtractionResult:
    """Output of deterministic extraction (Phase 1). No LLM calls.

    Assembled from: git diff, event stream, pytest output, syntax checks,
    and dispatch metadata.
    """
    dispatch_id: str
    terminal: str  # T1, T2, T3
    track: str  # A, B, C
    gate: str
    git: GitProvenance = field(default_factory=GitProvenance)
    tests: Optional[TestResults] = None
    syntax_checks: tuple = ()  # Tuple[SyntaxCheck, ...]
    events: EventMetrics = field(default_factory=EventMetrics)
    exit_summary: str = ""  # Last text block from worker, ≤200 chars
    extracted_at: str = ""

    def __post_init__(self) -> None:
        if self.terminal not in ("T1", "T2", "T3"):
            raise ValueError(f"terminal must be T1/T2/T3, got {self.terminal}")
        if self.track not in ("A", "B", "C"):
            raise ValueError(f"track must be A/B/C, got {self.track}")

    @property
    def has_test_failures(self) -> bool:
        return self.tests is not None and not self.tests.all_passed

    @property
    def has_syntax_errors(self) -> bool:
        return any(not sc.valid for sc in self.syntax_checks)

    @property
    def all_syntax_valid(self) -> bool:
        return all(sc.valid for sc in self.syntax_checks)

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "dispatch_id": self.dispatch_id,
            "terminal": self.terminal,
            "track": self.track,
            "gate": self.gate,
            "git": self.git.to_dict(),
            "tests": self.tests.to_dict() if self.tests else None,
            "syntax_checks": [sc.to_dict() for sc in self.syntax_checks],
            "events": self.events.to_dict(),
            "exit_summary": self.exit_summary,
            "extracted_at": self.extracted_at,
        }
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ExtractionResult:
        return cls(
            dispatch_id=data["dispatch_id"],
            terminal=data["terminal"],
            track=data["track"],
            gate=data["gate"],
            git=GitProvenance.from_dict(data.get("git", {})),
            tests=TestResults.from_dict(data["tests"]) if data.get("tests") else None,
            syntax_checks=tuple(
                SyntaxCheck(**sc) for sc in data.get("syntax_checks", ())
            ),
            events=EventMetrics(**data.get("events", {})),
            exit_summary=data.get("exit_summary", ""),
            extracted_at=data.get("extracted_at", ""),
        )


# ─── Auto-Derived Tags ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class AutoDerivedTags:
    """Tags derived deterministically from ExtractionResult. No LLM."""
    file_count: int = 0
    test_count: int = 0
    line_delta_add: int = 0
    line_delta_del: int = 0
    duration_seconds: int = 0
    model_used: str = ""
    has_commit: bool = False
    syntax_valid: bool = True
    tests_passed: bool = True
    tool_use_count: int = 0
    error_count: int = 0

    @classmethod
    def from_extraction(cls, ex: ExtractionResult) -> AutoDerivedTags:
        return cls(
            file_count=len(ex.git.files_changed),
            test_count=ex.tests.total if ex.tests else 0,
            line_delta_add=ex.git.insertions,
            line_delta_del=ex.git.deletions,
            duration_seconds=ex.events.session_duration_seconds,
            model_used=ex.events.model_used,
            has_commit=bool(ex.git.commit_hash),
            syntax_valid=ex.all_syntax_valid,
            tests_passed=ex.tests.all_passed if ex.tests else True,
            tool_use_count=ex.events.tool_use_count,
            error_count=ex.events.error_count,
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ─── Haiku Classification ────────────────────────────────────────────────────

@dataclass(frozen=True)
class HaikuClassification:
    """Output of haiku semantic classification (Phase 2).

    Only runs when:
    - VNX_HAIKU_CLASSIFY=1 env var is set
    - No blocking syntax errors in extraction
    - Extraction produced non-empty results

    Falls back to rule-based classification when haiku is disabled or fails.
    """
    content_type: ContentType
    quality_score: int  # 1-5
    complexity: Complexity
    consistency_score: float  # 0.0-1.0 (exit summary vs git diff alignment)
    summary: str  # ≤100 tokens semantic summary
    classified_by: str  # "haiku" or "rule_based"

    def __post_init__(self) -> None:
        if not 1 <= self.quality_score <= 5:
            raise ValueError(f"quality_score must be 1-5, got {self.quality_score}")
        if not 0.0 <= self.consistency_score <= 1.0:
            raise ValueError(
                f"consistency_score must be 0.0-1.0, got {self.consistency_score}"
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "content_type": self.content_type.value,
            "quality_score": self.quality_score,
            "complexity": self.complexity.value,
            "consistency_score": self.consistency_score,
            "summary": self.summary,
            "classified_by": self.classified_by,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> HaikuClassification:
        return cls(
            content_type=ContentType(data["content_type"]),
            quality_score=data["quality_score"],
            complexity=Complexity(data["complexity"]),
            consistency_score=data["consistency_score"],
            summary=data["summary"],
            classified_by=data["classified_by"],
        )

    @classmethod
    def rule_based(cls, extraction: ExtractionResult) -> HaikuClassification:
        """Deterministic fallback when haiku is unavailable.

        Maps extraction data to classification without LLM.
        """
        # Content type from file extensions and test results
        files = [f.lower() for f in extraction.git.files_changed]
        has_tests = any("test" in f for f in files)
        has_py = any(f.endswith(".py") for f in files)
        has_sh = any(f.endswith(".sh") for f in files)
        has_md = any(f.endswith(".md") for f in files)

        if has_tests and not any(f for f in files if "test" not in f):
            content_type = ContentType.TEST
        elif has_md and not has_py and not has_sh:
            content_type = ContentType.DOCS
        elif has_py or has_sh:
            content_type = ContentType.IMPLEMENTATION
        else:
            content_type = ContentType.MIXED

        # Complexity from file count and line delta
        total_lines = extraction.git.insertions + extraction.git.deletions
        file_count = len(extraction.git.files_changed)
        if total_lines > 300 or file_count > 5:
            complexity = Complexity.HIGH
        elif total_lines > 100 or file_count > 2:
            complexity = Complexity.MEDIUM
        else:
            complexity = Complexity.LOW

        # Quality score from test results and syntax
        score = 3  # baseline
        if extraction.tests and extraction.tests.all_passed:
            score += 1
        if extraction.has_test_failures:
            score -= 1
        if extraction.has_syntax_errors:
            score -= 1
        if extraction.git.commit_hash:
            score += 1
        score = max(1, min(5, score))

        return cls(
            content_type=content_type,
            quality_score=score,
            complexity=complexity,
            consistency_score=1.0 if extraction.git.commit_hash else 0.5,
            summary=extraction.exit_summary[:200] or "Rule-based classification",
            classified_by="rule_based",
        )


# ─── Classified Tags ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ClassifiedTags:
    """Tags from haiku classification. Separate from auto-derived."""
    content_type: str  # ContentType.value
    quality_score: int  # 1-5
    complexity: str  # Complexity.value
    consistency_score: float  # 0.0-1.0
    classified_by: str  # "haiku" or "rule_based"

    @classmethod
    def from_classification(cls, c: HaikuClassification) -> ClassifiedTags:
        return cls(
            content_type=c.content_type.value,
            quality_score=c.quality_score,
            complexity=c.complexity.value,
            consistency_score=c.consistency_score,
            classified_by=c.classified_by,
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ─── Unified Tag Set ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class UnifiedTagSet:
    """Complete tag set for a dispatch execution.

    Three stages, each additive:
        1. dispatch_tags — set by T0 at creation (from bundle.json)
        2. auto_derived — deterministic from extraction (git, tests, events)
        3. classified — from haiku or rule-based fallback

    The tag chain is immutable: later stages never modify earlier stages.
    """
    dispatch_tags: Optional[DispatchTags] = None
    auto_derived: Optional[AutoDerivedTags] = None
    classified: Optional[ClassifiedTags] = None
    outcome: OutcomeStatus = OutcomeStatus.SUCCESS

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dispatch_tags": self.dispatch_tags.to_dict() if self.dispatch_tags else None,
            "auto_derived": self.auto_derived.to_dict() if self.auto_derived else None,
            "classified": self.classified.to_dict() if self.classified else None,
            "outcome": self.outcome.value,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> UnifiedTagSet:
        return cls(
            dispatch_tags=DispatchTags.from_dict(data["dispatch_tags"])
            if data.get("dispatch_tags")
            else None,
            auto_derived=AutoDerivedTags(**data["auto_derived"])
            if data.get("auto_derived")
            else None,
            classified=ClassifiedTags(**data["classified"])
            if data.get("classified")
            else None,
            outcome=OutcomeStatus(data.get("outcome", "success")),
        )


# ─── Auto Report ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AutoReportMetadata:
    """Report identity and provenance."""
    dispatch_id: str
    pr_id: str
    terminal: str
    track: str
    gate: str
    status: str  # "success", "failure", etc. — for receipt processor compatibility
    auto_generated: bool = True
    schema_version: str = SCHEMA_VERSION
    assembled_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AutoReport:
    """The complete auto-assembled report.

    This is the final output of the auto-report pipeline, containing:
    - metadata: dispatch identity and provenance
    - extraction: deterministic data from git, tests, events
    - classification: haiku or rule-based semantic tags
    - tags: unified tag set across all three stages
    - quality_checks: deterministic quality advisory results

    The JSON form is written to $VNX_STATE_DIR/report_pipeline/{dispatch_id}.json.
    A markdown rendering is written to .vnx-data/unified_reports/ for the receipt
    processor.
    """
    metadata: AutoReportMetadata
    extraction: ExtractionResult
    classification: Optional[HaikuClassification] = None
    tags: UnifiedTagSet = field(default_factory=UnifiedTagSet)
    quality_checks: tuple = ()  # Tuple of QualityCheck-compatible dicts

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metadata": self.metadata.to_dict(),
            "extraction": self.extraction.to_dict(),
            "classification": self.classification.to_dict()
            if self.classification
            else None,
            "tags": self.tags.to_dict(),
            "quality_checks": list(self.quality_checks),
            "schema_version": SCHEMA_VERSION,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> AutoReport:
        return cls(
            metadata=AutoReportMetadata(**data["metadata"]),
            extraction=ExtractionResult.from_dict(data["extraction"]),
            classification=HaikuClassification.from_dict(data["classification"])
            if data.get("classification")
            else None,
            tags=UnifiedTagSet.from_dict(data.get("tags", {})),
            quality_checks=tuple(data.get("quality_checks", ())),
        )

    @classmethod
    def from_json(cls, raw: str) -> AutoReport:
        return cls.from_dict(json.loads(raw))


# ─── Receipt Processor Integration Contract ──────────────────────────────────
#
# The auto-report pipeline produces a markdown file in unified_reports/ that
# receipt_processor_v4.sh can parse identically to manual reports. The contract:
#
# 1. FILENAME: {YYYYMMDD}-{HHMMSS}-{track}-auto-{short_title}.md
#    The "auto-" prefix distinguishes auto-generated reports.
#
# 2. METADATA BLOCK (required by receipt processor):
#    The markdown MUST contain these fields parseable by report_parser.py:
#
#    **Dispatch ID**: {dispatch_id}
#    **PR**: {pr_id}
#    **Track**: {track}
#    **Gate**: {gate}
#    **Status**: {status}
#
# 3. ADDITIONAL FIELD:
#    **Auto-Generated**: true
#    This field marks the report for audit trail purposes. The receipt
#    processor does not require it but it enables filtering.
#
# 4. SECTIONS (all optional, included when data is available):
#    ## Summary           — exit_summary or haiku summary
#    ## Files Modified    — from git extraction
#    ## Test Results      — from pytest extraction
#    ## Quality Checks    — from deterministic checks
#    ## Tags              — unified tag set
#    ## Open Items        — always present, even when empty
#
# 5. JSON SIDECAR:
#    Written to $VNX_STATE_DIR/report_pipeline/{dispatch_id}.json
#    Contains the full AutoReport JSON for downstream processing
#    (intelligence persistence, governance signal extraction).
#
# 6. RECEIPT ENRICHMENT:
#    append_receipt.py adds git_provenance and idempotency_key.
#    The auto-report pipeline does NOT call append_receipt.py directly.
#    The receipt processor handles receipt creation from the markdown file.
#
# 7. BACKWARD COMPATIBILITY:
#    Manual reports (written by workers directly) continue to work unchanged.
#    The receipt processor treats auto-generated and manual reports identically.
#    The "auto-" filename prefix is cosmetic, not functional.

RECEIPT_REQUIRED_FIELDS = frozenset({
    "dispatch_id", "pr_id", "track", "gate", "status",
})

MARKDOWN_TEMPLATE = """\
# Auto-Report: {title}

**Dispatch ID**: {dispatch_id}
**PR**: {pr_id}
**Track**: {track}
**Terminal**: {terminal}
**Gate**: {gate}
**Status**: {status}
**Auto-Generated**: true

## Summary

{summary}

## Files Modified

{files_section}

## Test Results

{tests_section}

## Quality Checks

{quality_section}

## Tags

{tags_section}

## Open Items

{open_items_section}
"""


def render_markdown(report: AutoReport) -> str:
    """Render an AutoReport as markdown compatible with receipt_processor_v4.sh."""
    meta = report.metadata
    ext = report.extraction

    # Summary
    if report.classification and report.classification.summary:
        summary = report.classification.summary
    elif ext.exit_summary:
        summary = ext.exit_summary
    else:
        summary = "No summary available."

    # Files
    if ext.git.files_changed:
        files_lines = [f"- `{f}`" for f in ext.git.files_changed]
        files_lines.append(
            f"\n**Delta**: +{ext.git.insertions}/-{ext.git.deletions} lines "
            f"across {len(ext.git.files_changed)} file(s)"
        )
        if ext.git.commit_hash:
            files_lines.append(f"**Commit**: `{ext.git.commit_hash[:12]}`")
        files_section = "\n".join(files_lines)
    else:
        files_section = "No file changes detected."

    # Tests
    if ext.tests and ext.tests.total > 0:
        t = ext.tests
        tests_section = (
            f"**Passed**: {t.passed} | **Failed**: {t.failed} | "
            f"**Errors**: {t.errors} | **Skipped**: {t.skipped}\n"
            f"**Duration**: {t.duration_seconds:.1f}s"
        )
    else:
        tests_section = "No test results captured."

    # Quality checks
    if report.quality_checks:
        qc_lines = []
        for check in report.quality_checks:
            sev = check.get("severity", "info")
            msg = check.get("message", "")
            qc_lines.append(f"- **[{sev}]** {msg}")
        quality_section = "\n".join(qc_lines)
    else:
        quality_section = "No quality issues detected."

    # Tags
    tags_parts = []
    if report.tags.dispatch_tags:
        dt = report.tags.dispatch_tags
        tags_parts.append(
            f"**Dispatch**: type={dt.dispatch_type.value}, "
            f"risk={dt.risk.value}, scope={dt.scope.value}"
        )
    if report.tags.classified:
        ct = report.tags.classified
        tags_parts.append(
            f"**Classified**: content={ct.content_type}, "
            f"quality={ct.quality_score}/5, complexity={ct.complexity}, "
            f"by={ct.classified_by}"
        )
    if report.tags.auto_derived:
        ad = report.tags.auto_derived
        tags_parts.append(
            f"**Auto-derived**: files={ad.file_count}, tests={ad.test_count}, "
            f"+{ad.line_delta_add}/-{ad.line_delta_del} lines, "
            f"duration={ad.duration_seconds}s"
        )
    tags_section = "\n".join(tags_parts) if tags_parts else "No tags."

    # Title
    title = summary[:60].replace("\n", " ") if summary else meta.dispatch_id

    return MARKDOWN_TEMPLATE.format(
        title=title,
        dispatch_id=meta.dispatch_id,
        pr_id=meta.pr_id,
        track=meta.track,
        terminal=meta.terminal,
        gate=meta.gate,
        status=meta.status,
        summary=summary,
        files_section=files_section,
        tests_section=tests_section,
        quality_section=quality_section,
        tags_section=tags_section,
        open_items_section="No open items.",
    )


# ─── Validation ──────────────────────────────────────────────────────────────

def validate_auto_report(report: AutoReport) -> List[str]:
    """Validate an AutoReport for receipt processor compatibility.

    Returns a list of error strings. Empty list means valid.
    """
    errors: List[str] = []
    meta = report.metadata

    if not meta.dispatch_id:
        errors.append("metadata.dispatch_id is required")
    if not meta.pr_id:
        errors.append("metadata.pr_id is required")
    if not meta.track:
        errors.append("metadata.track is required")
    if not meta.gate:
        errors.append("metadata.gate is required")
    if not meta.status:
        errors.append("metadata.status is required")

    # Extraction consistency
    if report.extraction.dispatch_id != meta.dispatch_id:
        errors.append(
            f"extraction.dispatch_id ({report.extraction.dispatch_id}) "
            f"does not match metadata.dispatch_id ({meta.dispatch_id})"
        )

    # Classification validation (if present)
    if report.classification:
        c = report.classification
        if c.quality_score < 1 or c.quality_score > 5:
            errors.append(f"classification.quality_score out of range: {c.quality_score}")
        if c.consistency_score < 0.0 or c.consistency_score > 1.0:
            errors.append(
                f"classification.consistency_score out of range: {c.consistency_score}"
            )

    return errors
