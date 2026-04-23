#!/usr/bin/env python3
"""backfill_headless_receipts.py — Backfill dispatch_id in receipts with unknown metadata.

OI-AT-4 phase 2: backfill the ~363 processed receipts (and ~255 ndjson entries) that
carried dispatch_id="unknown" because the headless gate writer did not yet persist
dispatch_id in request payloads (fixed in phase 1).

Two categories of unknowns:

HEADLESS (254 JSON): report_file matches YYYYMMDD-HHMMSS-HEADLESS-{gate}-pr-{N}.md
  → synthetic dispatch_id: gate-{gate_slug}-pr-{pr_number}  (mirrors old code format)
  → status from review_gates/results/ or report content
  → branch from report header or results file

NON-HEADLESS (109 JSON): T1/T2/T3 worker receipts, or legacy 2025 reports
  → try dispatch directory correlation: match date + track + slug keywords (score ≥ 3)
  → fallback: synthetic worker-{terminal}-{date}-{slug20}

Idempotent: receipts already carrying backfilled=True are skipped.

Usage:
    python3 scripts/backfill_headless_receipts.py             # live run
    python3 scripts/backfill_headless_receipts.py --dry-run   # preview, no writes
    python3 scripts/backfill_headless_receipts.py --summary   # totals only (no per-file output)

BILLING SAFETY: No Anthropic SDK. No direct API calls.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

try:
    from vnx_paths import ensure_env
    _PATHS = ensure_env()
    VNX_DATA_DIR = Path(_PATHS["VNX_DATA_DIR"])
    VNX_STATE_DIR = Path(_PATHS["VNX_STATE_DIR"])
    VNX_REPORTS_DIR = Path(_PATHS["VNX_REPORTS_DIR"])
except Exception as exc:
    raise SystemExit(f"Failed to resolve VNX paths: {exc}")

RECEIPTS_PROCESSED_DIR = VNX_DATA_DIR / "receipts" / "processed"
T0_RECEIPTS_NDJSON = VNX_STATE_DIR / "t0_receipts.ndjson"
RESULTS_DIR = VNX_STATE_DIR / "review_gates" / "results"
REQUESTS_DIR = VNX_STATE_DIR / "review_gates" / "requests"
DISPATCHES_DIR = VNX_DATA_DIR / "dispatches"

# Filename patterns
HEADLESS_REPORT_RE = re.compile(
    r"^(?P<date>\d{8})-(?P<time>\d{6})-HEADLESS-"
    r"(?P<gate>codex_gate|gemini_review)-pr-(?P<pr>\d+)\.md$"
)
WORKER_REPORT_RE = re.compile(
    r"^(?P<date>\d{8})(?:-(?P<time>\d{4,6}))?-(?P<track>[A-Z])-(?P<slug>.+)\.md$"
)

# Minimum word-overlap score to accept a dispatch directory correlation
_DISPATCH_CORR_MIN_SCORE = 3


def _build_pr_gate_index() -> Dict[Tuple[str, int], Dict[str, Any]]:
    """Build mapping (gate, pr_number) -> {status, branch, report_file} from result/request files."""
    index: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for src_dir in (RESULTS_DIR, REQUESTS_DIR):
        if not src_dir.exists():
            continue
        for path in src_dir.iterdir():
            if path.suffix != ".json":
                continue
            m = re.match(r"pr-?(\d+)-(codex_gate|gemini_review)", path.name)
            if not m:
                continue
            pr, gate = int(m.group(1)), m.group(2)
            key = (gate, pr)
            try:
                with path.open() as f:
                    data = json.load(f)
            except Exception:
                continue
            if key not in index:
                index[key] = {}
            entry = index[key]
            if "status" not in entry or src_dir == RESULTS_DIR:
                entry["status"] = data.get("status", "unknown")
            if "branch" not in entry or entry.get("branch") == "unknown":
                entry["branch"] = data.get("branch", "unknown")
            rp = data.get("report_path", "")
            if rp and "report_file" not in entry:
                entry["report_file"] = os.path.basename(rp)
    return index


def _build_dispatch_index() -> Dict[str, str]:
    """Build mapping of dispatch_directory_name -> dispatch_id from bundle files."""
    index: Dict[str, str] = {}
    if not DISPATCHES_DIR.exists():
        return index
    for d in DISPATCHES_DIR.iterdir():
        if not d.is_dir():
            continue
        bundle = d / "bundle.json"
        if not bundle.exists():
            continue
        try:
            with bundle.open() as f:
                data = json.load(f)
            index[d.name] = data["dispatch_id"]
        except Exception:
            pass
    return index


def _correlate_worker_report(
    report_file: str,
    dispatch_index: Dict[str, str],
) -> Optional[str]:
    """Try to find a dispatch_id by slug-matching report filename against dispatch dirs.

    Returns dispatch_id only when word-overlap score >= _DISPATCH_CORR_MIN_SCORE.
    """
    m = WORKER_REPORT_RE.match(report_file)
    if not m:
        return None
    date = m.group("date")
    track = m.group("track")
    slug = m.group("slug")
    slug_words = set(re.split(r"[-_]", slug.lower())) - {"", "review", "pr", "a", "b", "c"}

    best_id: Optional[str] = None
    best_score = 0
    for dir_name, dispatch_id in dispatch_index.items():
        if not dir_name.startswith(date):
            continue
        if not dir_name.endswith(f"-{track}"):
            continue
        dir_words = set(re.split(r"[-_]", dir_name.lower()))
        score = len(slug_words & dir_words)
        if score > best_score:
            best_score = score
            best_id = dispatch_id

    return best_id if best_score >= _DISPATCH_CORR_MIN_SCORE else None


def _parse_report_header(report_file: str) -> Dict[str, str]:
    """Extract structured fields from newer HEADLESS report markdown headers."""
    path = VNX_REPORTS_DIR / report_file
    if not path.exists():
        return {}
    try:
        content = path.read_text(encoding="utf-8", errors="replace")[:1500]
    except Exception:
        return {}
    result: Dict[str, str] = {}
    for label, key in [("PR", "pr"), ("Branch", "branch"), ("Gate", "gate"), ("Generated", "generated")]:
        hit = re.search(rf"\*\*{label}\*\*:\s*(\S+)", content)
        if hit:
            result[key] = hit.group(1)
    return result


def _derive_status_from_report(report_file: str) -> Optional[str]:
    """Extract gate status from report content when result file is unavailable."""
    path = VNX_REPORTS_DIR / report_file
    if not path.exists():
        return None
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    m = re.search(r'"verdict":\s*"(pass|fail|approve|reject)"', content, re.I)
    if m:
        return m.group(1).lower()
    if re.search(r"\bAPPROVE\b", content):
        return "approve"
    if re.search(r"\bREJECT\b", content):
        return "reject"
    return None


def _make_worker_synthetic_id(report_file: str, terminal: str) -> str:
    """Produce a stable synthetic dispatch_id for non-HEADLESS worker receipts."""
    stem = Path(report_file).stem[:40] if report_file else "unknown"
    clean = re.sub(r"[^a-z0-9-]", "-", stem.lower()).strip("-")
    t = terminal.lower() if terminal and terminal != "unknown" else "worker"
    return f"{t}-{clean}"


def _backfill_headless(
    receipt: Dict[str, Any],
    pr_gate_index: Dict[Tuple[str, int], Dict[str, Any]],
    report_file: str,
    m: re.Match,
) -> Dict[str, Any]:
    gate = m.group("gate")
    pr = int(m.group("pr"))
    synthetic_id = f"gate-{gate}-pr-{pr}"
    key = (gate, pr)
    idx = pr_gate_index.get(key, {})
    header = _parse_report_header(report_file)

    status = idx.get("status") or _derive_status_from_report(report_file) or "completed"
    if status == "unknown":
        status = _derive_status_from_report(report_file) or "completed"

    branch = header.get("branch") or idx.get("branch") or "unknown"

    updated = dict(receipt)
    updated.update(
        dispatch_id=synthetic_id,
        task_id=synthetic_id,
        terminal="HEADLESS",
        track="headless",
        type=gate.upper(),
        gate=gate,
        status=status,
        title=f"{gate} PR-{pr}",
        pr_number=pr,
    )
    if branch != "unknown":
        updated["branch"] = branch
    updated["missing_fields"] = [
        f for f in updated.get("missing_fields", [])
        if f not in ("dispatch_id", "task_id")
    ]
    return updated


def _backfill_worker(
    receipt: Dict[str, Any],
    dispatch_index: Dict[str, str],
    report_file: str,
) -> Dict[str, Any]:
    terminal = receipt.get("terminal", "unknown")
    corr_id = _correlate_worker_report(report_file, dispatch_index)
    synthetic_id = corr_id or _make_worker_synthetic_id(report_file, terminal)

    updated = dict(receipt)
    updated.update(
        dispatch_id=synthetic_id,
        task_id=synthetic_id,
    )
    updated["missing_fields"] = [
        f for f in updated.get("missing_fields", [])
        if f not in ("dispatch_id", "task_id")
    ]
    if corr_id:
        updated["dispatch_id_source"] = "dispatch_correlation"
    else:
        updated["dispatch_id_source"] = "synthetic_worker"
    return updated


def _backfill_receipt(
    receipt: Dict[str, Any],
    pr_gate_index: Dict[Tuple[str, int], Dict[str, Any]],
    dispatch_index: Dict[str, str],
) -> Tuple[bool, Dict[str, Any]]:
    """Return (was_modified, updated_receipt)."""
    if str(receipt.get("dispatch_id", "")).strip() not in ("unknown", "", "None", None):
        return False, receipt
    if receipt.get("backfilled"):
        return False, receipt

    report_file = receipt.get("report_file", "") or os.path.basename(
        receipt.get("report_path", "")
    )
    if not report_file:
        return False, receipt

    hm = HEADLESS_REPORT_RE.match(report_file)
    if hm:
        updated = _backfill_headless(receipt, pr_gate_index, report_file, hm)
    else:
        updated = _backfill_worker(receipt, dispatch_index, report_file)

    now = datetime.now(timezone.utc).isoformat()
    updated["backfilled"] = True
    updated["backfilled_at"] = now
    updated["backfilled_by"] = "backfill_headless_receipts.py"
    return True, updated


def _update_processed_receipts(
    pr_gate_index: Dict[Tuple[str, int], Dict[str, Any]],
    dispatch_index: Dict[str, str],
    dry_run: bool,
    verbose: bool,
) -> Tuple[int, int, int]:
    """Returns (patched_headless, patched_worker, skipped)."""
    ph = pw = skipped = 0
    if not RECEIPTS_PROCESSED_DIR.exists():
        return 0, 0, 0
    for path in sorted(RECEIPTS_PROCESSED_DIR.iterdir()):
        if path.suffix != ".json":
            continue
        try:
            with path.open() as f:
                receipt = json.load(f)
        except Exception:
            skipped += 1
            continue
        was_modified, updated = _backfill_receipt(receipt, pr_gate_index, dispatch_index)
        if not was_modified:
            skipped += 1
            continue
        src = updated.get("dispatch_id_source", "headless")
        if src == "headless" or src not in ("dispatch_correlation", "synthetic_worker"):
            ph += 1
        else:
            pw += 1
        if not dry_run:
            with path.open("w") as f:
                json.dump(updated, f, separators=(",", ":"))
        if verbose:
            mode = "[DRY] " if dry_run else ""
            print(f"  {mode}{path.name}: dispatch_id={updated['dispatch_id']} "
                  f"gate={updated.get('gate','worker')} status={updated.get('status','?')}")
    return ph, pw, skipped


def _update_ndjson(
    pr_gate_index: Dict[Tuple[str, int], Dict[str, Any]],
    dispatch_index: Dict[str, str],
    dry_run: bool,
) -> Tuple[int, int]:
    """Rewrite t0_receipts.ndjson. Returns (patched, skipped)."""
    if not T0_RECEIPTS_NDJSON.exists():
        return 0, 0
    patched = skipped = 0
    updated_lines: List[str] = []
    with T0_RECEIPTS_NDJSON.open() as f:
        for raw_line in f:
            raw_line = raw_line.rstrip("\n")
            if not raw_line.strip():
                updated_lines.append(raw_line)
                continue
            try:
                receipt = json.loads(raw_line)
            except json.JSONDecodeError:
                updated_lines.append(raw_line)
                skipped += 1
                continue
            was_modified, updated = _backfill_receipt(receipt, pr_gate_index, dispatch_index)
            if was_modified:
                patched += 1
                updated_lines.append(json.dumps(updated, separators=(",", ":")))
            else:
                skipped += 1
                updated_lines.append(raw_line)
    if not dry_run:
        tmp = T0_RECEIPTS_NDJSON.parent / (T0_RECEIPTS_NDJSON.name + ".backfill_tmp")
        try:
            with tmp.open("w") as f:
                f.write("\n".join(updated_lines))
                if updated_lines and updated_lines[-1]:
                    f.write("\n")
            tmp.replace(T0_RECEIPTS_NDJSON)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
    return patched, skipped


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill dispatch_id in headless gate receipts (OI-AT-4 phase 2)"
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--summary", action="store_true", help="Print totals only, no per-file output")
    args = parser.parse_args()

    verbose = not args.summary

    print("[backfill] Building PR/gate index from review_gates results/requests...")
    pr_gate_index = _build_pr_gate_index()
    print(f"[backfill] Index: {len(pr_gate_index)} (gate, pr) entries")

    print("[backfill] Building dispatch directory index...")
    dispatch_index = _build_dispatch_index()
    print(f"[backfill] Dispatch index: {len(dispatch_index)} entries")

    print("\n[backfill] Updating processed receipt JSON files...")
    ph, pw, js = _update_processed_receipts(pr_gate_index, dispatch_index, args.dry_run, verbose)
    print(f"[backfill] JSON: {ph} HEADLESS patched, {pw} worker patched, {js} skipped")

    print("\n[backfill] Updating t0_receipts.ndjson...")
    np, ns = _update_ndjson(pr_gate_index, dispatch_index, args.dry_run)
    print(f"[backfill] NDJSON: {np} patched, {ns} skipped")

    mode = "DRY RUN" if args.dry_run else "LIVE"
    total_patched = ph + pw
    print(f"\n[backfill] {mode} complete. JSON={total_patched} patched, NDJSON={np} patched")

    if args.dry_run:
        print("[backfill] Re-run without --dry-run to apply changes.")


if __name__ == "__main__":
    main()
