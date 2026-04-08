#!/usr/bin/env python3
"""Auto-Report Assembler — PR-3 (F37 Auto-Report Pipeline).

Combines extraction results with dispatch metadata to produce structured
JSON and readable markdown reports compatible with receipt_processor_v4.sh.

Public API:
    assemble_from_trigger(trigger_path)  → AssemblyResult
    assemble(dispatch_id, ...)           → AssemblyResult
    write_report(result, vnx_data_dir)  → (json_path, md_path)

All functions return results on partial data — never raise on missing input.

BILLING SAFETY: No Anthropic SDK imports. No api.anthropic.com calls.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# ── PR-0 / PR-2 imports ───────────────────────────────────────────────────────
import sys as _sys
_LIB = str(Path(__file__).resolve().parent)
if _LIB not in _sys.path:
    _sys.path.insert(0, _LIB)

from auto_report_contract import (
    AutoDerivedTags,
    AutoReport,
    AutoReportMetadata,
    ClassifiedTags,
    DispatchTags,
    DispatchType,
    ExtractionResult,
    HaikuClassification,
    OutcomeStatus,
    RiskLevel,
    Scope,
    UnifiedTagSet,
    render_markdown,
    validate_auto_report,
)
from report_extraction import run_extraction
from report_classifier import classify_report


# ─── Assembly Result ──────────────────────────────────────────────────────────

@dataclass
class AssemblyResult:
    """Output of the assembler. Carries the report and write paths."""
    report: AutoReport
    json_path: Optional[Path] = None
    md_path: Optional[Path] = None
    errors: tuple = ()  # Validation errors (empty = valid)

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0


# ─── Dispatch Metadata Parser ─────────────────────────────────────────────────

def _parse_dispatch_file(dispatch_path: Path) -> dict:
    """Parse Manager Block fields from an active dispatch .md file.

    Returns a dict with lowercase underscore keys (e.g. 'dispatch_id').
    Returns empty dict when the file is missing or unparseable.
    """
    if not dispatch_path or not dispatch_path.exists():
        return {}

    try:
        content = dispatch_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.debug("_parse_dispatch_file: cannot read %s: %s", dispatch_path, exc)
        return {}

    meta: dict = {}
    # Parse "Key: Value" lines in the Manager Block header
    for line in content.splitlines()[:40]:
        line = line.strip()
        if ":" not in line or line.startswith("#"):
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower().replace("-", "_")
        value = value.strip()
        if key and value:
            meta[key] = value
    return meta


def _resolve_dispatch_metadata(
    dispatch_id: str,
    terminal: str,
    track: str,
    gate: str,
    pr_id: str,
    active_dir: Optional[Path],
) -> Tuple[str, str, str, str]:
    """Resolve (track, gate, pr_id, terminal) from dispatch file when not provided.

    Falls back to provided values when dispatch file is unavailable.
    """
    if active_dir and (not track or not gate or not pr_id):
        # Try exact filename match first
        for suffix in (".md", ".json"):
            candidate = active_dir / f"{dispatch_id}{suffix}"
            parsed = _parse_dispatch_file(candidate)
            if parsed:
                track = track or parsed.get("track", "")
                gate = gate or parsed.get("gate", "")
                pr_id = pr_id or parsed.get("pr_id", "") or parsed.get("pr-id", "")
                terminal = terminal or parsed.get("terminal", "")
                break

    return track or "A", gate or "", pr_id or "", terminal or "T1"


def _load_dispatch_tags(dispatch_id: str, active_dir: Optional[Path]) -> Optional[DispatchTags]:
    """Attempt to load DispatchTags from a bundle.json next to the dispatch file.

    Returns None when no bundle is found — assembler continues without dispatch tags.
    """
    if not active_dir:
        return None

    bundle_path = active_dir / f"{dispatch_id}.bundle.json"
    if not bundle_path.exists():
        # Try the dispatch subdir format
        subdir = active_dir / dispatch_id / "bundle.json"
        if subdir.exists():
            bundle_path = subdir
        else:
            return None

    try:
        data = json.loads(bundle_path.read_text(encoding="utf-8"))
        tags_data = data.get("tags") or data
        return DispatchTags.from_dict(tags_data)
    except Exception as exc:
        logger.debug("_load_dispatch_tags: cannot parse bundle %s: %s", bundle_path, exc)
        return None


# ─── Outcome Status Derivation ────────────────────────────────────────────────

def _derive_outcome(extraction: ExtractionResult, status_override: str = "") -> OutcomeStatus:
    """Derive OutcomeStatus from extraction data.

    Priority:
    1. status_override (from trigger file or caller)
    2. test failures → FAILURE
    3. syntax errors → FAILURE
    4. no commit → PARTIAL
    5. default → SUCCESS
    """
    _map = {
        "success": OutcomeStatus.SUCCESS,
        "failure": OutcomeStatus.FAILURE,
        "partial": OutcomeStatus.PARTIAL,
        "crashed": OutcomeStatus.CRASHED,
        "no_execution": OutcomeStatus.NO_EXECUTION,
    }
    if status_override and status_override.lower() in _map:
        return _map[status_override.lower()]

    if extraction.has_test_failures or extraction.has_syntax_errors:
        return OutcomeStatus.FAILURE
    if not extraction.git.commit_hash:
        return OutcomeStatus.PARTIAL
    return OutcomeStatus.SUCCESS


# ─── Core Assembly ────────────────────────────────────────────────────────────

def assemble(
    dispatch_id: str,
    terminal: str,
    track: str,
    gate: str,
    pr_id: str,
    repo_path: Optional[Path] = None,
    events_path: Optional[Path] = None,
    exit_summary: str = "",
    status_override: str = "",
    vnx_data_dir: Optional[Path] = None,
    active_dir: Optional[Path] = None,
) -> AssemblyResult:
    """Assemble an AutoReport from extraction + dispatch metadata.

    Args:
        dispatch_id:    Dispatch identifier string.
        terminal:       Worker terminal (T1, T2, T3).
        track:          Track letter (A, B, C).
        gate:           Quality gate name.
        pr_id:          PR identifier (e.g. PR-3).
        repo_path:      Repository root for git extraction.
        events_path:    NDJSON event stream path.
        exit_summary:   Override for exit summary text.
        status_override: Force a specific outcome status.
        vnx_data_dir:   Base .vnx-data directory (used for path resolution).
        active_dir:     Active dispatches directory (for dispatch metadata).

    Returns:
        AssemblyResult with populated AutoReport.
    """
    # Resolve vnx_data_dir from env when not provided
    if vnx_data_dir is None:
        env_dir = os.environ.get("VNX_DATA_DIR")
        if env_dir:
            vnx_data_dir = Path(env_dir)

    if active_dir is None and vnx_data_dir:
        active_dir = vnx_data_dir / "dispatches" / "active"

    # Resolve missing metadata from dispatch file
    track, gate, pr_id, terminal = _resolve_dispatch_metadata(
        dispatch_id, terminal, track, gate, pr_id, active_dir
    )

    # Load dispatch tags (optional — pipeline continues without them)
    dispatch_tags = _load_dispatch_tags(dispatch_id, active_dir)

    # Run deterministic extraction
    extraction = run_extraction(
        dispatch_id=dispatch_id,
        terminal=terminal,
        track=track,
        gate=gate,
        repo_path=repo_path,
        events_path=events_path,
        exit_summary=exit_summary,
    )

    # Derive outcome status
    outcome = _derive_outcome(extraction, status_override)

    # Auto-derived tags
    auto_derived = AutoDerivedTags.from_extraction(extraction)

    # Semantic classification: haiku when VNX_HAIKU_CLASSIFY=1, else rule-based
    classification = classify_report(extraction)
    classified_tags = ClassifiedTags.from_classification(classification)

    # Unified tag set
    tags = UnifiedTagSet(
        dispatch_tags=dispatch_tags,
        auto_derived=auto_derived,
        classified=classified_tags,
        outcome=outcome,
    )

    # Assemble metadata
    assembled_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    status_str = outcome.value
    metadata = AutoReportMetadata(
        dispatch_id=dispatch_id,
        pr_id=pr_id,
        terminal=terminal,
        track=track,
        gate=gate,
        status=status_str,
        auto_generated=True,
        assembled_at=assembled_at,
    )

    report = AutoReport(
        metadata=metadata,
        extraction=extraction,
        classification=classification,
        tags=tags,
        quality_checks=(),
    )

    errors = tuple(validate_auto_report(report))
    return AssemblyResult(report=report, errors=errors)


# ─── Trigger File Entry Point ─────────────────────────────────────────────────

def assemble_from_trigger(trigger_path: Path, repo_path: Optional[Path] = None) -> AssemblyResult:
    """Assemble a report from a stop-hook trigger file.

    The trigger file is a JSON written by stop_report_hook.sh or
    SubprocessAdapter.trigger_report_pipeline(). It contains dispatch_id,
    terminal, track, gate, pr_id, and project_root.

    Args:
        trigger_path: Path to the .trigger.json file.
        repo_path:    Override repository root (defaults to trigger's project_root).

    Returns:
        AssemblyResult, possibly with errors if input was partial.
    """
    try:
        trigger = json.loads(Path(trigger_path).read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("assemble_from_trigger: cannot read %s: %s", trigger_path, exc)
        # Return a minimal failed report so the pipeline can record the failure
        return _minimal_failed_result(str(trigger_path))

    dispatch_id = trigger.get("dispatch_id", "")
    terminal = trigger.get("terminal", "T1")
    track = trigger.get("track", "")
    gate = trigger.get("gate", "")
    pr_id = trigger.get("pr_id", "") or trigger.get("pr-id", "")
    project_root = trigger.get("project_root", "")
    session_id = trigger.get("session_id", "")

    # Validate minimum fields
    if not dispatch_id or dispatch_id.startswith("unknown-"):
        logger.warning("assemble_from_trigger: dispatch_id is unknown/empty: %s", dispatch_id)

    # Resolve paths
    if repo_path is None and project_root:
        repo_path = Path(project_root)

    vnx_data_dir: Optional[Path] = None
    env_dir = os.environ.get("VNX_DATA_DIR")
    if env_dir:
        vnx_data_dir = Path(env_dir)
    elif project_root:
        vnx_data_dir = Path(project_root) / ".vnx-data"

    events_path: Optional[Path] = None
    if vnx_data_dir and terminal:
        candidate = vnx_data_dir / "events" / f"{terminal}.ndjson"
        if candidate.exists():
            events_path = candidate

    return assemble(
        dispatch_id=dispatch_id,
        terminal=terminal,
        track=track,
        gate=gate,
        pr_id=pr_id,
        repo_path=repo_path,
        events_path=events_path,
        vnx_data_dir=vnx_data_dir,
    )


def _minimal_failed_result(label: str) -> AssemblyResult:
    """Return a placeholder AssemblyResult for unreadable trigger files."""
    from auto_report_contract import GitProvenance, EventMetrics

    extraction = ExtractionResult(
        dispatch_id=label,
        terminal="T1",
        track="A",
        gate="",
        exit_summary="Assembler could not read trigger file.",
    )
    metadata = AutoReportMetadata(
        dispatch_id=label,
        pr_id="",
        terminal="T1",
        track="A",
        gate="",
        status=OutcomeStatus.CRASHED.value,
        assembled_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    report = AutoReport(metadata=metadata, extraction=extraction)
    return AssemblyResult(report=report, errors=("unreadable trigger file",))


# ─── File Writers ─────────────────────────────────────────────────────────────

def write_report(
    result: AssemblyResult,
    vnx_data_dir: Optional[Path] = None,
) -> Tuple[Optional[Path], Optional[Path]]:
    """Write JSON sidecar and markdown report to disk.

    Args:
        result:       The AssemblyResult to write.
        vnx_data_dir: Base .vnx-data directory. Resolved from VNX_DATA_DIR env
                      when not provided.

    Returns:
        (json_path, md_path) — paths of written files, or (None, None) on failure.
    """
    if vnx_data_dir is None:
        env_dir = os.environ.get("VNX_DATA_DIR")
        if env_dir:
            vnx_data_dir = Path(env_dir)

    if vnx_data_dir is None:
        logger.error("write_report: VNX_DATA_DIR not set and no vnx_data_dir provided")
        return None, None

    report = result.report
    meta = report.metadata
    dispatch_id = meta.dispatch_id

    # ── JSON sidecar ──────────────────────────────────────────────────────────
    pipeline_dir = vnx_data_dir / "state" / "report_pipeline"
    pipeline_dir.mkdir(parents=True, exist_ok=True)
    json_path = pipeline_dir / f"{dispatch_id}.json"

    try:
        json_path.write_text(report.to_json(indent=2), encoding="utf-8")
        result.json_path = json_path
    except OSError as exc:
        logger.error("write_report: cannot write JSON to %s: %s", json_path, exc)
        json_path = None

    # ── Markdown report ───────────────────────────────────────────────────────
    unified_dir = vnx_data_dir / "unified_reports"
    unified_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    # Short title: first 30 chars of dispatch_id after the datestamp prefix
    # e.g. "20260408-110915-auto-report-assembler-A" → "auto-report-assembler"
    short_title = _short_title_from_dispatch_id(dispatch_id)
    md_filename = f"{timestamp}-{meta.track}-auto-{short_title}.md"
    md_path = unified_dir / md_filename

    try:
        markdown = render_markdown(report)
        md_path.write_text(markdown, encoding="utf-8")
        result.md_path = md_path
    except OSError as exc:
        logger.error("write_report: cannot write markdown to %s: %s", md_path, exc)
        md_path = None

    return json_path, md_path


def _short_title_from_dispatch_id(dispatch_id: str) -> str:
    """Extract a short slug from dispatch_id for use in filenames.

    '20260408-110915-auto-report-assembler-A' → 'auto-report-assembler'
    Falls back to the last 20 chars when pattern doesn't match.
    """
    # Strip leading timestamp (YYYYMMDD-HHMMSS-) and trailing track letter (-A/-B/-C)
    m = re.match(r"^\d{8}-\d{6}-(.+)-[ABC]$", dispatch_id)
    if m:
        slug = m.group(1)
        # Truncate to 30 chars to keep filenames manageable
        return slug[:30]
    # Fallback: use last segment, up to 30 chars
    return dispatch_id[-30:] if len(dispatch_id) > 30 else dispatch_id


# ─── CLI Entry Point ──────────────────────────────────────────────────────────

def main() -> int:
    """CLI: assemble_report.py <trigger_file> [--output-dir <dir>]

    Reads a trigger JSON file and assembles + writes the auto-report.
    Exit code: 0 = success, 1 = assembly error, 2 = write error.
    """
    import argparse

    parser = argparse.ArgumentParser(description="Assemble auto-report from trigger file")
    parser.add_argument("trigger", help="Path to .trigger.json file")
    parser.add_argument("--output-dir", help="Override VNX_DATA_DIR for output")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.WARNING)

    trigger_path = Path(args.trigger)
    if not trigger_path.exists():
        print(f"ERROR: trigger file not found: {trigger_path}", file=_sys.stderr)
        return 1

    vnx_data_dir = Path(args.output_dir) if args.output_dir else None

    result = assemble_from_trigger(trigger_path)

    if result.errors:
        print(f"WARN: report has validation errors: {result.errors}", file=_sys.stderr)

    json_path, md_path = write_report(result, vnx_data_dir)

    if json_path is None and md_path is None:
        print("ERROR: failed to write report files", file=_sys.stderr)
        return 2

    # Emit structured output for stop hook chaining
    output = {
        "dispatch_id": result.report.metadata.dispatch_id,
        "status": result.report.metadata.status,
        "json_path": str(json_path) if json_path else None,
        "md_path": str(md_path) if md_path else None,
        "errors": list(result.errors),
    }
    print(json.dumps(output, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    _sys.exit(main())
