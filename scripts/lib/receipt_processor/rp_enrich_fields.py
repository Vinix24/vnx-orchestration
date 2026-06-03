"""rp_enrich_fields.py — v2 receipt field enrichment.

Provides enrich_fields() used by rp_extract.sh (via CLI) and the test suite.

Called from rp_extract.sh:
  python3 rp_enrich_fields.py <receipt_json> [report_path] [gate_dir]
Outputs shell-sourceable _rf_KEY='value' lines.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional

_VERIFY_SECTION_RE = re.compile(
    r"## (?:Verification|Test Results|Evidence|Tests)\b(.*?)(?=\n## |\Z)",
    re.DOTALL | re.IGNORECASE,
)
_PYTEST_RE = re.compile(r"pytest[^\n]*?(\d+)\s+passed", re.IGNORECASE)
_TESTS_PASSED_RE = re.compile(r"(\d+)\s+(?:tests?\s+)?passed", re.IGNORECASE)


def _parse_tests_passed(report_text: str) -> str:
    """Extract passed-test count from the Verification section. Returns '' if absent."""
    m_sec = _VERIFY_SECTION_RE.search(report_text)
    section = m_sec.group(1) if m_sec else report_text

    m = _PYTEST_RE.search(section)
    if m:
        return m.group(1)
    m = _TESTS_PASSED_RE.search(section)
    if m:
        return m.group(1)
    return ""


def _load_gate_result(pr_id: str, gate_dir: Path) -> Optional[Dict[str, Any]]:
    """Load newest gate result JSON for pr_id. Returns None when unavailable."""
    if not pr_id or pr_id.lower() in ("none", ""):
        return None
    if not gate_dir or not gate_dir.is_dir():
        return None
    candidates = (
        list(gate_dir.glob(f"{pr_id}-*.json"))
        + list(gate_dir.glob(f"pr-{pr_id}-*.json"))
    )
    if not candidates:
        return None
    try:
        newest = max(candidates, key=lambda p: p.stat().st_mtime)
        with newest.open() as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _derive_next_action(
    status: str,
    pr_id: str,
    gate_blockers: int,
    gate_status: str,
    exit_code: Optional[int],
) -> str:
    """Derive next action per dispatch rules (exact precedence order)."""
    no_pr = not pr_id or pr_id.lower() in ("none", "")

    if (exit_code is not None and exit_code != 0) or status in ("failed", "error", "contract_invalid"):
        return "fix needed"
    if status == "done" and no_pr:
        return "T0 to open PR"
    if gate_blockers > 0:
        return "fix-forward needed"
    if gate_status in ("requested", "queued", "running"):
        return "gate pending"
    if gate_blockers == 0 and gate_status in ("completed", "done", "not_configured"):
        return "ready for merge"
    if not gate_status or gate_status in ("", "pending"):
        return "request codex_gate"
    return "verify"


def _isolation_mode(lane: str, pool_id: str) -> str:
    """Derive human-readable isolation mode from pool_id or lane."""
    if pool_id and pool_id not in ("", "?"):
        return pool_id
    for suffix in ("_interactive", "_headless", "_cheap", "_subprocess", "_tmux"):
        if lane.endswith(suffix):
            return suffix.lstrip("_")
    return lane


def _pr_title_slug(receipt: Dict[str, Any]) -> str:
    """Derive PR title slug from receipt or dispatch_id."""
    slug = receipt.get("pr_title_slug") or ""
    if slug:
        return str(slug)
    dispatch_id = receipt.get("dispatch_id") or ""
    if dispatch_id:
        parts = dispatch_id.split("-", 2)
        if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit():
            return parts[2]
        return dispatch_id
    return "?"


def enrich_fields(
    receipt: Dict[str, Any],
    report_text: Optional[str],
    gate_dir: Optional[Path],
) -> Dict[str, str]:
    """Build v2 enrichment dict from receipt JSON + optional report body + gate dir."""
    provider = str(receipt.get("provider") or "?")
    model = str(receipt.get("model") or "?")
    lane = str(receipt.get("lane") or "?")
    pool_id = str(receipt.get("pool_id") or "")
    smart_context = "✓" if receipt.get("smart_context") else ""

    pr_id_raw = receipt.get("pr_id") or receipt.get("pr_number") or ""
    pr_id = str(pr_id_raw) if str(pr_id_raw).lower() not in ("none", "null", "") else ""

    exit_code_raw = receipt.get("exit_code")
    exit_code: Optional[int]
    try:
        exit_code = int(exit_code_raw) if exit_code_raw is not None else None
    except (ValueError, TypeError):
        exit_code = None

    diff = (receipt.get("provenance") or {}).get("diff_summary") or {}
    files_changed = str(diff["files_changed"]) if diff.get("files_changed") is not None else ""
    insertions = str(diff["insertions"]) if diff.get("insertions") is not None else ""
    deletions = str(diff["deletions"]) if diff.get("deletions") is not None else ""

    tests_passed = _parse_tests_passed(report_text) if report_text else ""

    gate_result = _load_gate_result(pr_id, gate_dir)
    gate_name = gate_blockers_count = gate_advisories_count = 0
    gate_top_advisory = gate_status_str = ""
    gate_name_str = ""

    if gate_result:
        gate_name_str = gate_result.get("gate") or ""
        gate_blockers_count = int(gate_result.get("blocking_count") or 0)
        gate_advisories_count = int(gate_result.get("advisory_count") or 0)
        advisories = gate_result.get("advisory_findings") or []
        if advisories:
            gate_top_advisory = ((advisories[0].get("message") or "")[:60])
        state = gate_result.get("state") or gate_result.get("result_status") or ""
        gate_status_str = state.lower() if state else "completed"

    status = receipt.get("status") or ""
    next_action = _derive_next_action(
        status=status,
        pr_id=pr_id,
        gate_blockers=gate_blockers_count,
        gate_status=gate_status_str,
        exit_code=exit_code,
    )

    return {
        "provider": provider,
        "model": model,
        "lane": lane,
        "isolation_mode": _isolation_mode(lane, pool_id),
        "files_changed": files_changed,
        "insertions": insertions,
        "deletions": deletions,
        "tests_passed": tests_passed,
        "smart_context": smart_context,
        "gate_name": gate_name_str,
        "gate_blockers": str(gate_blockers_count),
        "gate_advisories": str(gate_advisories_count),
        "gate_top_advisory": gate_top_advisory,
        "next_action": next_action,
        "pr_title_slug": _pr_title_slug(receipt),
        "exit_code": str(exit_code) if exit_code is not None else "",
    }


def main() -> int:
    """CLI for rp_extract.sh. Args: receipt_json [report_path] [gate_dir]."""
    if len(sys.argv) < 2:
        print("Usage: rp_enrich_fields.py <receipt_json> [report_path] [gate_dir]", file=sys.stderr)
        return 1

    try:
        receipt = json.loads(sys.argv[1])
    except json.JSONDecodeError as exc:
        print(f"rp_enrich_fields: invalid JSON: {exc}", file=sys.stderr)
        return 1

    report_path_str = sys.argv[2] if len(sys.argv) > 2 else ""
    gate_dir_str = sys.argv[3] if len(sys.argv) > 3 else ""

    report_text: Optional[str] = None
    if report_path_str:
        try:
            report_text = Path(report_path_str).read_text(encoding="utf-8")
        except OSError:
            pass

    gate_dir = Path(gate_dir_str) if gate_dir_str else None

    fields = enrich_fields(receipt, report_text, gate_dir)
    for key, val in fields.items():
        safe_val = val.replace("'", "'\\''")
        print(f"_rf_{key}='{safe_val}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
