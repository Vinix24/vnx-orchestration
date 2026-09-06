#!/usr/bin/env python3
"""restore_stranded_reports.py — recover reports stranded by a missing model.

F1-3 (20260906-f13-gestrande-rapporten): report_to_receipt_converter.py
refuses (fail-closed, ``AppendReceiptError`` code ``missing_model`` /
``invalid_model_shape``) any dispatch-lane report whose resolved ``model``
is absent or a sentinel like ``"unknown"`` — see
``append_receipt_internals/validation.py::_validate_model_present``. Such a
report never gets a receipt and never gets retried automatically once the
underlying cause (a lane that never stamped a real model) is fixed
elsewhere; it just sits in ``unified_reports/`` forever.

This script finds those reports and — ONLY when a real model can be
VERIFIED from one of three sources, never invented — restores their
identity and books the receipt the converter itself would have booked had
the model been present from the start:

  a. the dispatch's own ``dispatch-spec.json`` (``model``/``provider``);
  b. an earlier ledger receipt for the same ``dispatch_id`` that carries a
     real ``model``;
  c. the report's own frontmatter ``model:``/``provider:``, or a
     bullet-form ``- Model: x`` / ``* Model: x`` line the converter's own
     parser does not recognise (it only reads ``**Model**:`` bold fields
     and, for Dispatch-ID only, a bare ``Dispatch-ID:`` line).

A report with no verifiable source in any of the three is listed as
UNRESTORABLE and is never touched — no default, no guessed value.

Restoring NEVER rewrites the report's existing bytes. It only APPENDS a
``## Restored identity`` section at the end (audit trail: which source, and
when) after the receipt has been booked with ``identity_restored: true``.
Re-running ``--apply`` on an already-restored report is a no-op — the
``## Restored identity`` marker is checked before anything else runs.

Usage:
    restore_stranded_reports.py --list    # scan only, changes nothing
    restore_stranded_reports.py --apply   # restore + book receipts
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_SCRIPT_DIR = Path(__file__).resolve().parent
_LIB_DIR = _SCRIPT_DIR / "lib"
for _p in (_SCRIPT_DIR, _LIB_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import report_to_receipt_converter as rc  # noqa: E402
import append_receipt  # noqa: E402  — registers facade, exposes append_receipt_payload
from append_receipt_internals.validation import _validate_model_present  # noqa: E402
from append_receipt_internals.common import AppendReceiptError  # noqa: E402

_RESTORED_MARKER = "## Restored identity"
_BAD_MODEL_SENTINELS = {"unknown", "none", "null", "n/a", "na", "unset", "-", ""}
_LEDGER_FILENAME = "t0_receipts.ndjson"

# Bullet-form ``Model:``/``Provider:`` lines — deliberately NOT anchored the
# way report_to_receipt_converter._DISPATCH_PLAIN_RE is (list marker + single
# space only): this is a one-shot recovery scan over a small, already-
# filtered candidate set, not a hot per-scan parser, so favouring recall over
# the converter's diff-quoting-safety trade-off is the right call here.
_BULLET_MODEL_RE = re.compile(r"^[-*]\s*Model:\s*(\S+)\s*$", re.MULTILINE | re.IGNORECASE)
_BULLET_PROVIDER_RE = re.compile(r"^[-*]\s*Provider:\s*(\S+)\s*$", re.MULTILINE | re.IGNORECASE)


def _is_real_model(value: Any) -> bool:
    return bool(str(value or "").strip()) and str(value).strip().lower() not in _BAD_MODEL_SENTINELS


@dataclass(frozen=True)
class Candidate:
    """A dispatch-lane report that would be refused for a missing/invalid model."""

    path: Path
    dispatch_id: str
    reason_code: str
    receipt: Dict[str, Any]


@dataclass(frozen=True)
class Resolution:
    """A verified (model, provider) pulled from one of the three allowed sources."""

    model: str
    provider: Optional[str]
    source_kind: str  # "dispatch_spec" | "ledger" | "frontmatter" | "bullet"
    source_detail: str


@dataclass(frozen=True)
class ScanResult:
    total_scanned: int
    already_handled: int
    skipped_non_dispatch: int
    candidates: Tuple[Candidate, ...]


def find_stranded_reports(data_dir: Path, state_dir: Path) -> ScanResult:
    """Scan ``unified_reports/`` for dispatch-lane reports stranded on model.

    Uses ``report_to_receipt_converter``'s OWN classification and receipt
    builder (``_classify_non_dispatch_report``, ``_build_receipt_from_report_
    core``) so "which reports would the converter scan" is answered by the
    same code the converter itself runs — never a re-implemented guess.

    Deliberately uses the CORE builder, not the public ``build_receipt_from_
    report`` wrapper: the wrapper's ``_resolve_pr_link`` step can WRITE to a
    gate obligation file as a side effect, and a scan (used by both --list
    and the detection half of --apply) must never mutate state — only an
    actual restoration should.
    """
    reports_dir = data_dir / "unified_reports"
    watermark = rc._load_watermark(state_dir / rc._WATERMARK_FILENAME)
    bash_watermark = rc._load_watermark(state_dir / rc._BASH_WATERMARK_FILENAME)

    total = 0
    already_handled = 0
    skipped_non_dispatch = 0
    candidates: List[Candidate] = []

    for path in sorted(reports_dir.glob("*.md")):
        if not path.is_file():
            continue
        total += 1

        if rc._classify_non_dispatch_report(path) is not None:
            skipped_non_dispatch += 1
            continue

        try:
            file_hash = rc._compute_sha256(path)
        except OSError:
            continue
        if file_hash in watermark or file_hash in bash_watermark:
            already_handled += 1
            continue

        try:
            text = path.read_text(encoding="utf-8")
            receipt = rc._build_receipt_from_report_core(path, text, state_dir=state_dir)
        except Exception:
            continue
        if receipt is None:
            continue

        try:
            _validate_model_present(receipt)
        except AppendReceiptError as exc:
            if exc.code in ("missing_model", "invalid_model_shape"):
                candidates.append(Candidate(
                    path=path,
                    dispatch_id=str(receipt.get("dispatch_id") or ""),
                    reason_code=exc.code,
                    receipt=receipt,
                ))

    return ScanResult(
        total_scanned=total,
        already_handled=already_handled,
        skipped_non_dispatch=skipped_non_dispatch,
        candidates=tuple(candidates),
    )


def _resolve_from_dispatch_spec(dispatch_id: str, data_dir: Path) -> Optional[Resolution]:
    """Source (a): the dispatch's own ``dispatch-spec.json``.

    Globs every status directory (``dispatches/*/<dispatch_id>/``) since a
    dispatch can sit in ``pending``, ``completed``, ``failed``, etc. — the
    status at scan time is not part of this contract.
    """
    for spec_path in sorted(data_dir.glob(f"dispatches/*/{dispatch_id}/dispatch-spec.json")):
        try:
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        model = str(spec.get("model") or "").strip()
        if _is_real_model(model):
            provider = str(spec.get("provider") or "").strip() or None
            return Resolution(
                model=model, provider=provider,
                source_kind="dispatch_spec", source_detail=str(spec_path),
            )
    return None


def build_ledger_model_index(state_dir: Path) -> Dict[str, Resolution]:
    """Source (b), pre-indexed: one pass over the ledger instead of one pass
    per candidate. Keeps the EARLIEST record with a real model per
    dispatch_id — 'een eerdere receipt' — regardless of ``event_type``.

    T0's dispatch text names ``dispatch_registered``/``task_start``/a
    review-gate event as the expected carriers; measured against the live
    ledger (2026-09-06) both ``dispatch_registered`` and ``task_start``
    occur ZERO times — no lane ever wrote them. Scoping to those literal
    event types would make source (b) permanently empty. Any event_type
    carrying a real model for the dispatch_id serves the same purpose here.
    """
    index: Dict[str, Resolution] = {}
    ledger_path = state_dir / _LEDGER_FILENAME
    if not ledger_path.exists():
        return index
    try:
        with ledger_path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                dispatch_id = record.get("dispatch_id")
                if not dispatch_id or dispatch_id in index:
                    continue
                model = str(record.get("model") or "").strip()
                if not _is_real_model(model):
                    continue
                provider = str(record.get("provider") or "").strip() or None
                event_name = record.get("event_type") or record.get("event") or "unknown_event"
                timestamp = record.get("timestamp") or "unknown_timestamp"
                index[dispatch_id] = Resolution(
                    model=model, provider=provider, source_kind="ledger",
                    source_detail=f"event_type={event_name} timestamp={timestamp}",
                )
    except OSError:
        pass
    return index


def _resolve_from_report_text(text: str) -> Optional[Resolution]:
    """Source (c): the report's own frontmatter, or an unparsed bullet line."""
    fm = rc.parse_frontmatter(text)
    model = str(fm.get("model") or "").strip()
    if _is_real_model(model):
        provider = str(fm.get("provider") or "").strip() or None
        return Resolution(
            model=model, provider=provider,
            source_kind="frontmatter", source_detail="frontmatter 'model:' field",
        )

    match = _BULLET_MODEL_RE.search(text)
    if match and _is_real_model(match.group(1)):
        model = match.group(1).strip()
        provider_match = _BULLET_PROVIDER_RE.search(text)
        provider = provider_match.group(1).strip() if provider_match else None
        quoted_line = match.group(0).strip()
        return Resolution(
            model=model, provider=provider,
            source_kind="bullet", source_detail=f"quoted line: {quoted_line!r}",
        )
    return None


def resolve_identity(
    candidate: Candidate, data_dir: Path, ledger_index: Dict[str, Resolution], text: str,
) -> Optional[Resolution]:
    """Try sources (a), (b), (c) in that fixed order; first hit wins."""
    return (
        _resolve_from_dispatch_spec(candidate.dispatch_id, data_dir)
        or ledger_index.get(candidate.dispatch_id)
        or _resolve_from_report_text(text)
    )


def apply_restoration(
    candidate: Candidate, resolution: Resolution, *, data_dir: Path, state_dir: Path,
) -> Tuple[bool, str]:
    """Restore one candidate: book the receipt, then append the audit block.

    Returns ``(restored, detail)``. Never touches the file if a receipt
    cannot be booked (e.g. the report fails some OTHER fail-closed check
    even after the model is supplied) — the restore is all-or-nothing per
    report, and a failed booking leaves the report byte-for-byte untouched.
    """
    original_text = candidate.path.read_text(encoding="utf-8")
    if _RESTORED_MARKER in original_text:
        return False, "already restored (## Restored identity marker present) — skipped"

    # Rebuild via the full converter pipeline (PR-link included) — this is
    # the function the converter itself would call to book this report for
    # real; the core-only builder used for scanning deliberately skips the
    # PR-link side effect, which is appropriate for detection but not here.
    receipt = rc.build_receipt_from_report(candidate.path, original_text, state_dir=state_dir)
    if receipt is None:
        return False, "dispatch_id no longer resolvable — report changed since scan"

    receipt["model"] = resolution.model
    if resolution.provider:
        try:
            receipt["provider"] = rc._normalise_provider(resolution.provider)
        except rc.UnrecognizedProviderError:
            pass  # keep whatever the lane/body already resolved for provider
    receipt["identity_restored"] = True

    receipts_file = str(state_dir / _LEDGER_FILENAME)
    try:
        result = append_receipt.append_receipt_payload(
            receipt,
            receipts_file=receipts_file,
            cache_window_seconds=300,
            skip_enrichment=True,
        )
    except append_receipt.AppendReceiptError as exc:
        return False, f"append still failed after model restore: {exc.code}: {exc.message}"

    if result is None or result.status not in ("appended", "duplicate"):
        return False, f"append returned unexpected result: {result!r}"

    restored_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    annotation = (
        f"\n\n{_RESTORED_MARKER}\n\n"
        f"**Model**: {resolution.model}\n"
        f"**Provider**: {receipt.get('provider') or resolution.provider or ''}\n"
        f"**Restored-from**: {resolution.source_kind} ({resolution.source_detail})\n"
        f"**Restored-at**: {restored_at}\n"
    )
    with candidate.path.open("a", encoding="utf-8") as fh:
        fh.write(annotation)

    return True, f"receipt booked (status={result.status}), restored from {resolution.source_kind}: {resolution.source_detail}"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--state-dir", default=None, help="Override $VNX_STATE_DIR")
    parser.add_argument("--data-dir", default=None, help="Override $VNX_DATA_DIR")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list", action="store_true", help="Scan and report only — changes nothing")
    mode.add_argument("--apply", action="store_true", help="Restore identity + book receipts for restorable reports")
    args = parser.parse_args(argv)

    if args.data_dir and args.state_dir:
        data_dir = Path(args.data_dir)
        state_dir = Path(args.state_dir)
    else:
        from vnx_paths import ensure_env  # noqa: PLC0415
        paths = ensure_env()
        data_dir = Path(args.data_dir) if args.data_dir else Path(paths["VNX_DATA_DIR"])
        state_dir = Path(args.state_dir) if args.state_dir else Path(paths["VNX_STATE_DIR"])

    scan = find_stranded_reports(data_dir, state_dir)
    ledger_index = build_ledger_model_index(state_dir)

    print(
        f"Scanned {scan.total_scanned} report file(s) in {data_dir / 'unified_reports'}: "
        f"{scan.skipped_non_dispatch} non-dispatch (skipped), "
        f"{scan.already_handled} already handled (watermarked), "
        f"{len(scan.candidates)} stranded on model validation."
    )

    restorable: List[Tuple[Candidate, Resolution]] = []
    unrestorable: List[Candidate] = []
    for candidate in scan.candidates:
        text = candidate.path.read_text(encoding="utf-8")
        resolution = resolve_identity(candidate, data_dir, ledger_index, text)
        if resolution is not None:
            restorable.append((candidate, resolution))
        else:
            unrestorable.append(candidate)

    for candidate, resolution in restorable:
        print(
            f"RESTORABLE    dispatch_id={candidate.dispatch_id} file={candidate.path.name} "
            f"reason={candidate.reason_code} -> model={resolution.model} "
            f"provider={resolution.provider or '?'} source={resolution.source_kind} "
            f"({resolution.source_detail})"
        )
    for candidate in unrestorable:
        print(
            f"UNRESTORABLE  dispatch_id={candidate.dispatch_id} file={candidate.path.name} "
            f"reason={candidate.reason_code} -> no verifiable model source in "
            "dispatch-spec.json, the ledger, or the report itself"
        )

    if args.list:
        print(f"\n--list: {len(restorable)} restorable, {len(unrestorable)} unrestorable. Nothing changed.")
        return 0

    restored = 0
    already_restored = 0
    failed = 0
    for candidate, resolution in restorable:
        ok, detail = apply_restoration(candidate, resolution, data_dir=data_dir, state_dir=state_dir)
        if ok:
            restored += 1
            print(f"RESTORED      dispatch_id={candidate.dispatch_id} {detail}")
        elif "already restored" in detail:
            already_restored += 1
            print(f"SKIPPED       dispatch_id={candidate.dispatch_id} {detail}")
        else:
            failed += 1
            print(f"FAILED        dispatch_id={candidate.dispatch_id} {detail}")

    print(
        f"\n--apply summary: {restored} restored, {already_restored} already restored, "
        f"{failed} failed after a source was found, {len(unrestorable)} unrestorable (no source)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
