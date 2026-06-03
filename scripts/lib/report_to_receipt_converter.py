"""report_to_receipt_converter.py — generic unified_report -> governed receipt.

Scans unified_reports/*.md (and headless/) for reports not yet converted to
receipts. Parses YAML frontmatter (--- style) and falls back to filename-based
dispatch_id derivation.  Emits a governed receipt via append_receipt_payload()
so every report — regardless of who wrote it — enters the audit trail.

Part of the universal governance interface:
  report on disk -> receipt processor -> t0_receipts.ndjson

Idempotency layers:
  1. Permanent: SHA-256 of each report file in
     $VNX_STATE_DIR/report_to_receipt_processed.txt (survives restarts).
     This is the converter's OWN dedicated hash-set — it does NOT read or
     write the Bash receipt processor's processed_receipts.txt watermark
     (the two systems use separate dedup stores to avoid format conflation).
  2. Short-term: append_receipt_payload() rolling idempotency cache
     (receipt_idempotency_recent.ndjson, default 5-min window) guards against
     concurrent calls and same-cycle races.

Wired into receipt_processor.sh poll loop — NOT a competing daemon.
Called every ~30 s (every 6 poll cycles); non-fatal on any error.

Report format support:
  - YAML frontmatter (--- key: value --- blocks) written by governance_emit.py
  - **Key**: value bold-field format written by human workers
  - Filename-derived dispatch_id as last-resort fallback
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_LIB_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _LIB_DIR.parent  # scripts/ — append_receipt.py lives here
_WATERMARK_FILENAME = "report_to_receipt_processed.txt"

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_BOLD_KV_RE = re.compile(r"\*\*([^*]+)\*\*:\s*(.+)", re.MULTILINE)
_DISPATCH_PLAIN_RE = re.compile(
    r"^\s*Dispatch-ID:\s*(\S+)\s*$", re.MULTILINE | re.IGNORECASE
)
_DISPATCH_ID_KEY_RE = re.compile(
    r"^\s*dispatch_id:\s*(\S+)\s*$", re.MULTILINE | re.IGNORECASE
)


# ---------------------------------------------------------------------------
# SHA-256 helper
# ---------------------------------------------------------------------------

def _compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Watermark helpers (fcntl-locked append for concurrent safety)
# ---------------------------------------------------------------------------

def _load_watermark(watermark_path: Path) -> set:
    """Load processed hashes from watermark file into a set."""
    if not watermark_path.exists():
        return set()
    try:
        return {
            line.strip()
            for line in watermark_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
    except OSError as exc:
        logger.warning("report_to_receipt_converter: cannot read watermark %s: %s", watermark_path, exc)
        return set()


def _mark_processed(file_hash: str, watermark_path: Path) -> None:
    """Append hash to watermark file with an exclusive lock."""
    try:
        with watermark_path.open("a", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.write(file_hash + "\n")
            fcntl.flock(fh, fcntl.LOCK_UN)
        logger.info(
            "report_to_receipt_converter: watermark state mutation hash=%s watermark=%s",
            file_hash[:16], watermark_path.name,
        )
    except OSError as exc:
        logger.warning("report_to_receipt_converter: cannot update watermark %s: %s", watermark_path, exc)


# ---------------------------------------------------------------------------
# Report parsing
# ---------------------------------------------------------------------------

def parse_frontmatter(text: str) -> Dict[str, Any]:
    """Parse --- YAML frontmatter.  Returns {} if absent or malformed.

    Only handles simple ``key: value`` lines (no nested YAML).  This covers
    the output of ``yaml.dump()`` as used by governance_emit.emit_unified_report().
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    fm: Dict[str, Any] = {}
    for raw_line in m.group(1).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip().lower().replace("-", "_").replace(" ", "_")
        val = val.strip()
        if key and val:
            fm[key] = val
    return fm


def _extract_body_fields(text: str) -> Dict[str, Any]:
    """Extract **Key**: value fields + plain-text Dispatch-ID fallback from body."""
    fields: Dict[str, Any] = {}
    for m in _BOLD_KV_RE.finditer(text[:3000]):
        key = m.group(1).strip().lower().replace("-", "_").replace(" ", "_")
        val = m.group(2).strip().splitlines()[0].strip()
        if key and val:
            fields.setdefault(key, val)
    if "dispatch_id" not in fields:
        m = _DISPATCH_PLAIN_RE.search(text[:3000])
        if m:
            fields["dispatch_id"] = m.group(1).strip()
    if "dispatch_id" not in fields:
        m = _DISPATCH_ID_KEY_RE.search(text[:3000])
        if m:
            fields["dispatch_id"] = m.group(1).strip()
    return fields


def _dispatch_id_from_filename(path: Path) -> Optional[str]:
    """Derive dispatch_id by stripping known suffixes from the stem."""
    stem = path.stem
    for suffix in ("_report",):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    stem = stem.strip()
    return None if stem.lower() in ("", "unknown", "none", "null") else stem


def _load_route_decision(dispatch_id: str, state_dir: Path) -> Optional[Dict[str, Any]]:
    """Load per-dispatch route decision JSON written by smart_router.write_route_decision().

    Returns the parsed dict (with strategy/task_class/selected_model) or None when
    the file does not exist or cannot be parsed.
    """
    path = state_dir / "route_decisions" / f"{dispatch_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "route_decision lookup failed for dispatch_id=%s: type=%s err=%s; falling back to default strategy",
            dispatch_id, type(exc).__name__, exc,
        )
        return None


def build_receipt_from_report(
    report_path: Path, text: str, *, state_dir: Optional[Path] = None
) -> Optional[Dict[str, Any]]:
    """Build a minimal governed receipt dict from report content.

    Returns:
    - event_type="task_complete" when the report passes the body contract AND
      carries a content-derived dispatch_id (frontmatter or bold-field body).
    - event_type="report_contract_invalid" when a dispatch_id is resolvable
      but the report fails the body contract or lacks a content-side
      dispatch_id.  Filename-only dispatch_id is a contract violation.
    - None when no dispatch_id can be determined at all (warning logged).

    Never raises.
    """
    sys.path.insert(0, str(_LIB_DIR))
    from report_body_contract import validate_body
    from datetime import datetime, timezone

    fm = parse_frontmatter(text)
    body = _extract_body_fields(text)
    # Frontmatter takes priority over body fields
    merged: Dict[str, Any] = {**body, **fm}

    # Check if dispatch_id comes from report content (frontmatter or body fields).
    # A filename-derived dispatch_id is NOT authoritative and is treated as a
    # contract violation — it must not produce a clean task_complete receipt.
    content_dispatch_id: Optional[str] = merged.get("dispatch_id") or None
    content_id_valid = bool(
        content_dispatch_id
        and content_dispatch_id.lower() not in ("unknown", "none", "null")
    )

    # Validate body against the report body contract.
    body_result = validate_body(text)

    # Collect all contract violations before deciding the receipt type.
    contract_violations: List[str] = []
    if not content_id_valid:
        contract_violations.append("missing_content_dispatch_id")
    if not body_result.valid:
        contract_violations.extend(body_result.missing)
        if body_result.placeholder:
            contract_violations.append("placeholder_summary")

    # Resolve the best available dispatch_id.  For contract-invalid receipts
    # we fall back to the filename so the audit trail has a key.
    dispatch_id: Optional[str] = (
        content_dispatch_id if content_id_valid
        else _dispatch_id_from_filename(report_path)
    )
    if not dispatch_id or dispatch_id.lower() in ("unknown", "none", "null"):
        logger.warning(
            "report_to_receipt_converter: no dispatch_id for %s — skipping",
            report_path.name,
        )
        return None

    timestamp = (
        merged.get("timestamp")
        or merged.get("recorded_at")
        or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    # Use "unknown" for task_id so the idempotency key aligns with what
    # report_parser.py produces (it defaults task_id to "unknown").  This lets
    # append_receipt_payload()'s rolling cache deduplicate same-cycle runs.
    base: Dict[str, Any] = {
        "dispatch_id": dispatch_id,
        "task_id": merged.get("task_id", "unknown"),
        "terminal": merged.get("terminal", "unknown"),
        "provider": merged.get("provider", "unknown"),
        "model": merged.get("model", ""),
        "timestamp": timestamp,
        "report_path": str(report_path),
    }

    if contract_violations:
        logger.warning(
            "report_to_receipt_converter: contract violations in %s: %s"
            " — emitting as report_contract_invalid",
            report_path.name, contract_violations,
        )
        return {
            **base,
            "event_type": "report_contract_invalid",
            "status": "contract_invalid",
            "contract_violations": contract_violations,
        }

    receipt: Dict[str, Any] = {
        **base,
        "event_type": "task_complete",
        "status": merged.get("status", "unknown"),
    }
    if state_dir and dispatch_id:
        route_dec = _load_route_decision(dispatch_id, state_dir)
        if route_dec:
            receipt["route_decision"] = route_dec
    return receipt


# ---------------------------------------------------------------------------
# Single-report converter
# ---------------------------------------------------------------------------

def convert_report_to_receipt(
    report_path: Path,
    *,
    receipts_file: Optional[str] = None,
    cache_window_seconds: int = 300,
) -> Optional[Any]:  # Optional[AppendResult]
    """Convert one report file to a governed receipt.

    Returns AppendResult (status="appended" | "duplicate") on success,
    or None on unreadable / malformed input (warning logged, no crash).
    """
    # Import through append_receipt.py (scripts/ root) so the facade is
    # registered before append_receipt_payload() is called.
    sys.path.insert(0, str(_SCRIPTS_DIR))
    sys.path.insert(0, str(_LIB_DIR))
    try:
        import append_receipt  # registers facade as side effect
        append_receipt_payload = append_receipt.append_receipt_payload
    except Exception as exc:
        logger.warning("report_to_receipt_converter: cannot import append_receipt: %s", exc)
        return None

    try:
        text = report_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("report_to_receipt_converter: cannot read %s: %s", report_path.name, exc)
        return None

    state_dir_for_route = Path(receipts_file).parent if receipts_file else None
    receipt = build_receipt_from_report(report_path, text, state_dir=state_dir_for_route)
    if receipt is None:
        return None

    try:
        return append_receipt_payload(
            receipt,
            receipts_file=receipts_file,
            cache_window_seconds=cache_window_seconds,
            skip_enrichment=True,  # converter receipts skip quality advisory
        )
    except Exception as exc:
        logger.warning(
            "report_to_receipt_converter: append failed for %s: %s",
            report_path.name, exc,
        )
        return None


# ---------------------------------------------------------------------------
# Directory scanner
# ---------------------------------------------------------------------------

def scan_and_convert(
    reports_dirs: List[Path],
    state_dir: Optional[Path] = None,
    *,
    cache_window_seconds: int = 300,
) -> int:
    """Scan report directories and convert unprocessed reports.

    Deduplication uses ONLY the converter's own watermark file
    (report_to_receipt_processed.txt).  The Bash receipt processor's
    processed_receipts.txt is intentionally NOT consulted — the two
    systems own separate dedup stores to avoid format-conflation risk.

    Returns the count of newly emitted receipts (status="appended").
    Malformed reports are skipped with a warning; they are NOT marked as
    processed so they will be retried on the next scan (in case they are
    still being written).
    """
    if state_dir is None:
        try:
            state_dir = _resolve_state_dir()
        except Exception as exc:
            logger.error("report_to_receipt_converter: cannot resolve state_dir: %s", exc)
            return 0

    state_dir.mkdir(parents=True, exist_ok=True)
    receipts_file = str(state_dir / "t0_receipts.ndjson")
    watermark_path = state_dir / _WATERMARK_FILENAME

    watermark = _load_watermark(watermark_path)

    new_count = 0

    for reports_dir in reports_dirs:
        if not isinstance(reports_dir, Path):
            reports_dir = Path(reports_dir)
        if not reports_dir.is_dir():
            continue
        for report_path in sorted(reports_dir.glob("*.md")):
            if not report_path.is_file():
                continue
            try:
                file_hash = _compute_sha256(report_path)
            except OSError:
                continue

            # Skip if already in our own watermark
            if file_hash in watermark:
                continue

            result = convert_report_to_receipt(
                report_path,
                receipts_file=receipts_file,
                cache_window_seconds=cache_window_seconds,
            )

            if result is not None:
                # Mark as processed regardless of appended/duplicate —
                # no point re-scanning a report that's already in the system.
                _mark_processed(file_hash, watermark_path)
                watermark.add(file_hash)
                if result.status == "appended":
                    new_count += 1
                    logger.info(
                        "report_to_receipt_converter: receipt emitted dispatch=%s file=%s",
                        result.idempotency_key[:20],
                        report_path.name,
                    )
            # result is None → malformed; NOT marked processed (retry on next scan)

    return new_count


# ---------------------------------------------------------------------------
# State dir resolver
# ---------------------------------------------------------------------------

def _resolve_state_dir() -> Path:
    """Resolve $VNX_STATE_DIR via vnx_paths.ensure_env()."""
    sys.path.insert(0, str(_LIB_DIR))
    from vnx_paths import ensure_env
    paths = ensure_env()
    return Path(paths["VNX_STATE_DIR"])


# ---------------------------------------------------------------------------
# CLI entry point (called from receipt_processor.sh poll loop)
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    """Scan directories and convert frontmatter reports to receipts.

    Usage: report_to_receipt_converter.py [--state-dir DIR] [DIR ...]
    """
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    p = argparse.ArgumentParser(
        description="Generic unified_report -> receipt converter"
    )
    p.add_argument("--state-dir", default=None, help="Override $VNX_STATE_DIR path")
    p.add_argument("dirs", nargs="*", help="Reports directories to scan")
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    state_dir = Path(args.state_dir) if args.state_dir else None

    if args.dirs:
        dirs = [Path(d) for d in args.dirs]
    else:
        try:
            sd = state_dir or _resolve_state_dir()
            from vnx_paths import ensure_env
            paths = ensure_env()
            data_dir = Path(paths.get("VNX_DATA_DIR", ""))
            dirs = [
                data_dir / "unified_reports",
                data_dir / "unified_reports" / "headless",
            ]
        except Exception as exc:
            logger.error("report_to_receipt_converter: cannot resolve dirs from env: %s", exc)
            return 1

    n = scan_and_convert(dirs, state_dir, cache_window_seconds=300)
    if n:
        logger.info("report_to_receipt_converter: %d new receipt(s) emitted", n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
