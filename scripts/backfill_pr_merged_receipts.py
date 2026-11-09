#!/usr/bin/env python3
"""Backfill pr_merged receipts for GitHub PRs that have been merged without an audit trail.

Queries GitHub for all merged PRs in the current repository, compares against
t0_receipts.ndjson, and emits a pr_merged receipt for any PR that is missing one.

Usage:
    python3 scripts/backfill_pr_merged_receipts.py          # live backfill
    python3 scripts/backfill_pr_merged_receipts.py --dry-run  # preview, no writes
    python3 scripts/backfill_pr_merged_receipts.py --limit 50  # cap at 50 PRs
    python3 scripts/backfill_pr_merged_receipts.py --since 2026-01-01  # only newer PRs
    python3 scripts/backfill_pr_merged_receipts.py --json   # structured output

    python3 scripts/backfill_pr_merged_receipts.py --report-only  # audit, never heals

Idempotent: already-receipted PR numbers are skipped.

--report-only (door audit, deur D7a): the same comparison, but it reports instead
of healing. The window is 7 days (--since overrides) and is proven by paginating
`pulls?state=closed&sort=updated&direction=desc` until the oldest `updated_at` on
a page is older than `since`. Both ledgers are read (state/t0_receipts.ndjson and
events/pr_merged.ndjson). Every merged PR lands in one bucket: `door`, `backfilled`
or `missing`. Exit 0 = complete read, no gaps ("0 van <n>"); exit 1 = at least one
missing; exit 2 = unmeasurable (failed read, page limit, unreadable ledger).

--all-projects (deur D7b): the fleet runner. It audits the union of ~/.vnx/projects.json and the
installed com.vnx.ledger-health.* labels, one `--report-only --json` subprocess per project
(that project's cwd and VNX_* paths). A project in one list only is a finding, one without
path or repo or with a failing child is `unmeasurable`. `--project <id>` narrows; an unknown
id is exit 2 (`onbekend project: <id>`). Exit = maximum severity; an empty enumeration is 2.

--reconcile-open-permits (deur D7c): reads every OPEN PR (not the merged ones) and the
`vnx-gate/door` runs on its head. A run of the door App that is older than --permit-ttl
(default 30 minutes) or whose permit_id (`<app_id>:<head>:<run id>`) is in no pr_merged and no
pr_merge_refused line of either ledger is revoked with D3's revoke_door (PATCH to failure, three
attempts, no DELETE) and gets one open item (warn when revoked, blocker when not). A run of
another App is reported (`foreign_door_check`) and never touched. Exit 0 = nothing found or all
revoked; 1 = a revocation failed or a foreign door check exists; 2 = unreadable. --dry-run patches
nothing. The --all-projects child runs it after --report-only in the same process.

BILLING SAFETY: No Anthropic SDK. No direct API calls.
"""

from __future__ import annotations

import argparse
import datetime
import fcntl
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

log = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent
LIB_DIR = SCRIPT_DIR / "lib"
sys.path.insert(0, str(LIB_DIR))
sys.path.insert(0, str(SCRIPT_DIR))

from vnx_paths import ensure_env
from governance_receipts import emit_governance_receipt

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_UNKNOWN = 2

AUDIT_WINDOW_DAYS = 7
AUDIT_PAGE_SIZE = 100
AUDIT_PAGE_LIMIT = 50
AUDIT_BEACON = "door_audit"
DOOR_SOURCE = "pr_merge"

_PR_LABEL_RE = re.compile(r"\bPR-([A-Z0-9]+(?:-[A-Z0-9]+)*)\b", re.IGNORECASE)


def _extract_pr_id_from_subject(subject: str) -> Optional[str]:
    """Extract internal PR-N label from commit subject or PR title."""
    m = _PR_LABEL_RE.search(subject)
    return f"PR-{m.group(1).upper()}" if m else None


def _lookup_dispatch_id_for_pr(
    pr_number: int,
    branch: str,
    register_events: List[Dict[str, Any]],
) -> str:
    """Find dispatch_id from dispatch_register events by pr_number or branch-slug match.

    Priority: pr_number exact match > branch-slug heuristic > '' (not found).
    """
    # 1. Exact pr_number match
    for ev in reversed(register_events):
        if ev.get("pr_number") == pr_number and ev.get("dispatch_id"):
            return str(ev["dispatch_id"])

    # 2. Branch-slug heuristic: tokenise branch and match against dispatch_id strings
    if branch:
        branch_raw = branch.lower().replace("/", "-").replace("_", "-")
        tokens = [t for t in re.split(r"[-]+", branch_raw) if len(t) >= 4]
        if tokens:
            for ev in reversed(register_events):
                did = str(ev.get("dispatch_id") or "")
                if not did:
                    continue
                did_lower = did.lower()
                matches = sum(1 for tok in tokens if tok in did_lower)
                if matches >= max(1, len(tokens) // 2):
                    return did

    return ""


def _resolve_receipts_path() -> Path:
    paths = ensure_env()
    return Path(paths["VNX_STATE_DIR"]) / "t0_receipts.ndjson"


def _resolve_events_pr_merged_path() -> Path:
    """Return path for ADR-005 events ledger: .vnx-data/events/pr_merged.ndjson."""
    paths = ensure_env()
    events_dir = Path(paths["VNX_STATE_DIR"]).parent / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    return events_dir / "pr_merged.ndjson"


def _append_locked(path: Path, record: Dict[str, Any]) -> None:
    """Append a JSON record to an NDJSON file under an exclusive lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _load_receipted_pr_numbers(
    receipts_path: Path,
    events_path: Optional[Path] = None,
) -> Set[int]:
    """Return set of pr_number values already receipted as pr_merged.

    Scans both t0_receipts.ndjson and events/pr_merged.ndjson (ADR-005 ledger)
    so idempotency works regardless of which path prior runs wrote to.

    events_path: explicit override. When None, derived as receipts_path/../events/pr_merged.ndjson
    (no env-var lookup, so test isolation is preserved).
    """
    result: Set[int] = set()
    paths_to_scan: List[Path] = [receipts_path]

    # Derive events path from receipts_path structure (state/ → events/pr_merged.ndjson)
    if events_path is None:
        candidate = receipts_path.parent.parent / "events" / "pr_merged.ndjson"
        if candidate.resolve() != receipts_path.resolve():
            events_path = candidate

    if events_path is not None and events_path not in paths_to_scan:
        paths_to_scan.append(events_path)

    for path in paths_to_scan:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            event = rec.get("event_type") or rec.get("event") or ""
            if event == "pr_merged":
                pn = rec.get("pr_number")
                if pn is not None:
                    try:
                        result.add(int(pn))
                    except (TypeError, ValueError):
                        pass
    return result


def _gh_list_merged_prs(limit: int, since: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return list of merged PRs from GitHub via gh CLI."""
    args = [
        "gh", "pr", "list",
        "--state", "merged",
        "--limit", str(max(limit, 1)),
        "--json", "number,title,headRefName,mergedAt,baseRefName,mergeCommit",
    ]
    # gh does not support --search date filtering directly, handled post-fetch
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=60, check=False,
        )
        if result.returncode != 0:
            print(f"ERROR: gh pr list failed: {result.stderr.strip()}", file=sys.stderr)
            return []
        return json.loads(result.stdout or "[]")
    except Exception as exc:
        print(f"ERROR: gh pr list raised: {exc}", file=sys.stderr)
        return []


def _load_dispatch_register_events() -> List[Dict[str, Any]]:
    """Load dispatch_register.ndjson events. Best-effort — returns [] on graceful miss."""
    try:
        from dispatch_register import read_events
        return read_events()
    except (ImportError, FileNotFoundError):
        return []
    except Exception as e:
        log.warning("dispatch_register load failed: %s", e, exc_info=True)
        return []


def _emit_backfill_receipt(
    pr: Dict[str, Any],
    *,
    pr_id: str = "",
    dispatch_id: str = "",
    events_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Emit a pr_merged receipt for a single PR to events/pr_merged.ndjson (ADR-005).

    Dual-scheme: pr_number (GitHub numeric) + pr_id (internal PR-N/PR-LABEL).
    pr_id_resolution='unmatched' when no internal label could be derived.
    Uses the merge commit timestamp for accurate historical placement.
    """
    pr_number = int(pr["number"])
    merged_at = pr.get("mergedAt", "")

    # Use merge commit timestamp if available for accurate historical placement
    merge_commit = pr.get("mergeCommit") or {}
    commit_ts = str(merge_commit.get("committedDate") or merge_commit.get("authoredDate") or merged_at)

    receipt: Dict[str, Any] = {
        "timestamp": commit_ts or merged_at or datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "event_type": "pr_merged",
        "status": "success",
        "terminal": "T0",
        "source": "backfill_pr_merged_receipts",
        "pr_number": pr_number,
        "conclusion": "merged",
        "merge_method": "unknown",
        "pr_title": pr.get("title", ""),
        "branch": pr.get("headRefName", ""),
        "merged_at": merged_at,
        "backfilled": True,
    }
    if pr_id:
        receipt["pr_id"] = pr_id
    else:
        receipt["pr_id_resolution"] = "unmatched"
    if dispatch_id:
        receipt["dispatch_id"] = dispatch_id

    target_path = events_path if events_path is not None else _resolve_events_pr_merged_path()
    _append_locked(target_path, receipt)
    return receipt


def _emit_register_event(pr_number: int) -> bool:
    """Write pr_merged event to dispatch_register.ndjson. Best-effort."""
    try:
        from dispatch_register import append_event
        return append_event(
            "pr_merged",
            pr_number=pr_number,
            terminal="T0",
            extra={"backfilled": True, "conclusion": "merged"},
        )
    except Exception:
        return False


def backfill(
    *,
    limit: int = 500,
    since: Optional[str] = None,
    dry_run: bool = False,
    receipts_file: Optional[str] = None,
    events_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Run the backfill and return a summary dict.

    Writes new pr_merged receipts to events/pr_merged.ndjson (ADR-005 ledger).
    The receipts_file parameter is kept for backwards compatibility but is only
    used to seed the already-receipted set (idempotency check).
    events_path overrides the default events/pr_merged.ndjson destination.
    """
    receipts_path = Path(receipts_file) if receipts_file else _resolve_receipts_path()
    resolved_events_path = events_path if events_path is not None else _resolve_events_pr_merged_path()
    already_receipted = _load_receipted_pr_numbers(receipts_path, events_path=resolved_events_path)

    merged_prs = _gh_list_merged_prs(limit=limit, since=since)

    # Filter by --since date when provided (gh does not support this flag natively)
    if since:
        filtered = []
        for pr in merged_prs:
            merged_at = pr.get("mergedAt", "")
            if merged_at and merged_at[:10] >= since[:10]:
                filtered.append(pr)
        merged_prs = filtered

    missing: List[Dict[str, Any]] = [
        pr for pr in merged_prs
        if pr.get("number") is not None and int(pr["number"]) not in already_receipted
    ]

    # Load dispatch_register events once for dispatch_id lookup
    register_events = _load_dispatch_register_events()

    summary: Dict[str, Any] = {
        "total_merged_prs": len(merged_prs),
        "already_receipted": len(already_receipted),
        "missing_receipt_count": len(missing),
        "backfilled": 0,
        "errors": 0,
        "dry_run": dry_run,
        "details": [],
    }

    for pr in missing:
        pr_number = int(pr["number"])
        detail: Dict[str, Any] = {
            "pr_number": pr_number,
            "pr_title": pr.get("title", ""),
            "merged_at": pr.get("mergedAt", ""),
        }

        # Derive dual-scheme fields
        subject = pr.get("title", "") or pr.get("headRefName", "")
        pr_id = _extract_pr_id_from_subject(subject)
        dispatch_id = _lookup_dispatch_id_for_pr(pr_number, pr.get("headRefName", ""), register_events)
        detail["pr_id"] = pr_id or ""
        detail["dispatch_id"] = dispatch_id

        if dry_run:
            detail["action"] = "would_backfill"
        else:
            try:
                receipt = _emit_backfill_receipt(
                    pr,
                    pr_id=pr_id or "",
                    dispatch_id=dispatch_id,
                    events_path=events_path,
                )
                detail["action"] = "backfilled"
                detail["register_ok"] = _emit_register_event(pr_number)
                summary["backfilled"] += 1
            except Exception as exc:
                detail["action"] = "error"
                detail["error"] = str(exc)
                summary["errors"] += 1

        summary["details"].append(detail)

    return summary


# ---------------------------------------------------------------------------
# --report-only: fail-closed door audit (deur D7a)
# ---------------------------------------------------------------------------

class AuditUnmeasurable(Exception):
    """The audit could not prove its window or read its evidence: outcome `unknown`, exit 2."""


def _parse_ts(value: Any, what: str) -> datetime.datetime:
    """Parse an ISO date/timestamp as UTC. Anything else is unmeasurable, never a default."""
    if not isinstance(value, str) or not value.strip():
        raise AuditUnmeasurable(f"{what} missing or not a string: {value!r}")
    text = value.strip()
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise AuditUnmeasurable(f"{what} is not a timestamp: {value!r}") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def _audit_since(since: Optional[str]) -> datetime.datetime:
    if since:
        return _parse_ts(since, "--since")
    return datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=AUDIT_WINDOW_DAYS)


def _gh_api_pulls_page(page: int) -> Any:
    """One page of closed PRs, most recently updated first. Raises AuditUnmeasurable on any failure."""
    endpoint = (
        "repos/{owner}/{repo}/pulls?state=closed&sort=updated&direction=desc"
        f"&per_page={AUDIT_PAGE_SIZE}&page={page}"
    )
    try:
        result = subprocess.run(
            ["gh", "api", endpoint], capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AuditUnmeasurable(f"gh api page {page} could not run: {exc}") from None
    if result.returncode != 0:
        raise AuditUnmeasurable(f"gh api page {page} failed (rc={result.returncode}): {result.stderr.strip()}")
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise AuditUnmeasurable(f"gh api page {page} returned unparseable JSON") from None


def _page_entry(raw: Any, page: int) -> Dict[str, Any]:
    """Validate one PR row of a page; a malformed row makes the window unprovable."""
    if not isinstance(raw, dict) or isinstance(raw.get("number"), bool) or not isinstance(raw.get("number"), int):
        raise AuditUnmeasurable(f"gh api page {page} has a row without an integer number")
    updated = _parse_ts(raw.get("updated_at"), f"PR #{raw['number']} updated_at")
    merged_raw = raw.get("merged_at")
    merged = None if merged_raw is None else _parse_ts(merged_raw, f"PR #{raw['number']} merged_at")
    head = raw.get("head")
    branch = head.get("ref", "") if isinstance(head, dict) else ""
    return {
        "number": raw["number"],
        "title": str(raw.get("title") or ""),
        "branch": str(branch or ""),
        "merged_at": merged,
        "updated_at": updated,
    }


def list_merged_in_window(since: datetime.datetime) -> List[Dict[str, Any]]:
    """Every PR merged at/after `since`, with the window proven by the updated_at stop condition.

    `updated_at >= merged_at` for a merged PR, so once a page holds a PR updated
    before `since`, no later page can hold a merge inside the window. Reaching the
    page limit before that point, or any failed/malformed read, raises AuditUnmeasurable.
    """
    merged: List[Dict[str, Any]] = []
    for page in range(1, AUDIT_PAGE_LIMIT + 1):
        raw_page = _gh_api_pulls_page(page)
        if not isinstance(raw_page, list):
            raise AuditUnmeasurable(f"gh api page {page} is not a JSON list")
        rows = [_page_entry(r, page) for r in raw_page]
        merged.extend(r for r in rows if r["merged_at"] is not None and r["merged_at"] >= since)
        if not rows or len(rows) < AUDIT_PAGE_SIZE:
            return merged
        if min(r["updated_at"] for r in rows) < since:
            return merged
    raise AuditUnmeasurable(
        f"page limit {AUDIT_PAGE_LIMIT} reached before the stop condition (updated_at < since): window not proven"
    )


def _audit_ledger_paths() -> List[Path]:
    """Both ledgers, resolved from the store the script already uses (no mkdir: read-only)."""
    receipts = _resolve_receipts_path()
    return [receipts, receipts.parent.parent / "events" / "pr_merged.ndjson"]


def _is_door_evidence(rec: Dict[str, Any]) -> bool:
    """Door evidence needs source exactly `pr_merge` and no backfilled marker at all (not even a falsy non-False)."""
    return rec.get("source") == DOOR_SOURCE and ("backfilled" not in rec or rec["backfilled"] is False)


def _classify_ledger_line(rec: Any, door: Set[int], backfilled: Set[int]) -> None:
    if not isinstance(rec, dict):
        return
    if (rec.get("event_type") or rec.get("event") or "") != "pr_merged":
        return
    raw = rec.get("pr_number")
    if isinstance(raw, bool):
        return
    try:
        number = int(raw)
    except (TypeError, ValueError):
        return
    (door if _is_door_evidence(rec) else backfilled).add(number)


def read_provenance(paths: List[Path]) -> Dict[str, Any]:
    """Read every ledger file; a missing t0_receipts file or an unreadable file is unmeasurable.

    Malformed lines are skipped and counted: a line that cannot be parsed can
    only ever fail to vouch for a PR, so skipping stays on the blocking side.
    """
    door: Set[int] = set()
    backfilled: Set[int] = set()
    malformed = 0
    for index, path in enumerate(paths):
        if not path.exists():
            if index == 0:
                raise AuditUnmeasurable(f"ledger not found: {path}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise AuditUnmeasurable(f"ledger unreadable: {path}: {exc}") from None
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                malformed += 1
                continue
            _classify_ledger_line(rec, door, backfilled)
    return {"door": door, "backfilled": backfilled, "malformed_lines": malformed,
            "ledgers": [str(p) for p in paths]}


def _bucket_prs(merged: List[Dict[str, Any]], prov: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    buckets: Dict[str, List[Dict[str, Any]]] = {"door": [], "backfilled": [], "missing": []}
    for pr in sorted(merged, key=lambda p: p["number"]):
        row = {"pr_number": pr["number"], "pr_title": pr["title"],
               "merged_at": pr["merged_at"].isoformat().replace("+00:00", "Z")}
        if pr["number"] in prov["door"]:
            buckets["door"].append(row)
        elif pr["number"] in prov["backfilled"]:
            buckets["backfilled"].append(row)
        else:
            buckets["missing"].append(row)
    return buckets


def run_report_only(since: Optional[str] = None) -> Dict[str, Any]:
    """Compare merged PRs with both ledgers. Never writes, never reaches _emit_backfill_receipt."""
    report: Dict[str, Any] = {"mode": "report-only", "outcome": "unknown", "exit_code": EXIT_UNKNOWN,
                              "reason": "", "since": "", "total": 0,
                              "door": [], "backfilled": [], "missing": []}
    try:
        since_dt = _audit_since(since)
        report["since"] = since_dt.isoformat().replace("+00:00", "Z")
        merged = list_merged_in_window(since_dt)
        prov = read_provenance(_audit_ledger_paths())
    except AuditUnmeasurable as exc:
        report["reason"] = str(exc)
        return report
    report.update(_bucket_prs(merged, prov))
    report["total"] = len(merged)
    report["malformed_ledger_lines"] = prov["malformed_lines"]
    report["ledgers"] = prov["ledgers"]
    if report["missing"]:
        report.update(outcome="gaps", exit_code=EXIT_ERROR, reason=f"{len(report['missing'])} merge(s) without a pr_merged receipt")
    else:
        report.update(outcome="clean", exit_code=EXIT_OK)
    return report


def _audit_project_id(state_dir: Path) -> str:
    from vnx_paths import project_id_from_state_dir
    from project_scope import current_project_id
    return project_id_from_state_dir(state_dir) or current_project_id()


def _file_open_item(pr: Dict[str, Any], project_id: str) -> Tuple[str, bool]:
    from open_items_manager import add_item_programmatic
    number = pr["pr_number"]
    return add_item_programmatic(
        title=f"PR #{number} merged without a pr_merged receipt ({pr['pr_title'][:60]})",
        severity="warn",
        dispatch_id="door-audit",
        details=f"merged_at {pr['merged_at']}; found by backfill_pr_merged_receipts --report-only",
        dedup_key=f"door-audit:{project_id}:{number}",
        source="door_audit",
    )


def _heartbeat_audit(state_dir: Path, status: str, details: Dict[str, Any]) -> None:
    from health_beacon import HealthBeacon
    HealthBeacon(state_dir.parent, AUDIT_BEACON, expected_interval_seconds=2 * 86400).heartbeat_strict(
        status=status, details=details)


def _beacon_guard(doc: Dict[str, Any], state_dir: Path, details: Dict[str, Any], errors: List[str]) -> None:
    """Write the beacon. A failure is a publication error: clean becomes unknown (exit 2), a finding keeps its exit."""
    try:
        _heartbeat_audit(state_dir, "ok" if doc["outcome"] == "clean" else "fail", details)
    except Exception as exc:
        errors.append(f"beacon: {exc}")
        print(f"WRITE ERROR: beacon: {exc}", file=sys.stderr)
        doc["publication_errors"] = int(doc.get("publication_errors") or 0) + 1
        if doc["outcome"] == "clean":
            doc.update(outcome="unknown", exit_code=EXIT_UNKNOWN, reason="beacon not written: control invisible")
        else:
            doc["reason"] = f"{doc.get('reason') or ''}; beacon not written".lstrip("; ")


def publish_report(report: Dict[str, Any]) -> None:
    """File one open item per missing PR and write the beacon. Failures are recorded on the report.

    A gap whose open item cannot be written stays exit 1. A clean or unknown read whose
    beacon cannot be written becomes unknown (exit 2): a control nobody can see proves nothing.
    """
    state_dir = _audit_ledger_paths()[0].parent
    errors: List[str] = []
    project_id = _audit_project_id(state_dir)
    report["project_id"] = project_id
    report["open_items"] = []
    for pr in report["missing"]:
        try:
            item_id, created = _file_open_item(pr, project_id)
            report["open_items"].append({"pr_number": pr["pr_number"], "item_id": item_id, "created": created})
        except Exception as exc:
            errors.append(f"open item for PR #{pr['pr_number']}: {exc}")
    details = {"outcome": report["outcome"], "since": report["since"], "total": report["total"],
               "door": len(report["door"]), "backfilled": len(report["backfilled"]),
               "missing": [p["pr_number"] for p in report["missing"]], "reason": report["reason"]}
    if "reconcile" in report:
        details["reconcile"] = report["reconcile"]["counts"]
    _beacon_guard(report, state_dir, details, errors)
    report["write_errors"] = errors


def _print_report(report: Dict[str, Any]) -> None:
    print(f"Door audit (report-only) since {report['since'] or '?'}: {report['outcome']}")
    if report["outcome"] == "unknown":
        print(f"  UNMEASURABLE: {report['reason']}")
        return
    print(f"  {len(report['missing'])} van {report['total']} merges zonder pr_merged receipt "
          f"(door {len(report['door'])}, backfilled {len(report['backfilled'])})")
    for pr in report["missing"]:
        print(f"  MISSING PR #{pr['pr_number']}  {pr['merged_at']}  {pr['pr_title'][:60]}")
    for err in report.get("write_errors", []):
        print(f"  WRITE ERROR: {err}", file=sys.stderr)


# ---------------------------------------------------------------------------
# --reconcile-open-permits: revoke a stale or orphaned door permit (deur D7c)
# ---------------------------------------------------------------------------

PERMIT_TTL_MINUTES = 30
_PERMIT_EVENTS = ("pr_merged", "pr_merge_refused")
_COUNT_KEYS = ("checked", "stale", "orphaned", "revoked", "revoke_failed", "foreign")


def _gh_api_json(endpoint: str, what: str) -> Any:
    """One `gh api` read. Raises AuditUnmeasurable on any failure."""
    try:
        result = subprocess.run(["gh", "api", endpoint], capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AuditUnmeasurable(f"{what} could not run: {exc}") from None
    if result.returncode != 0:
        raise AuditUnmeasurable(f"{what} failed (rc={result.returncode}): {result.stderr.strip()}")
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise AuditUnmeasurable(f"{what} returned unparseable JSON") from None


def _open_pr_row(raw: Any, page: int) -> Dict[str, Any]:
    if not isinstance(raw, dict) or isinstance(raw.get("number"), bool) or not isinstance(raw.get("number"), int):
        raise AuditUnmeasurable(f"open pulls page {page} has a row without an integer number")
    head = raw.get("head")
    sha = head.get("sha") if isinstance(head, dict) else None
    if not isinstance(sha, str) or not sha.strip():
        raise AuditUnmeasurable(f"open PR #{raw['number']} has no head sha")
    return {"number": raw["number"], "sha": sha.strip()}


def list_open_prs() -> List[Dict[str, Any]]:
    """Every open PR with its head sha, read until a short page. A failed or unbounded read is unmeasurable."""
    prs: List[Dict[str, Any]] = []
    for page in range(1, AUDIT_PAGE_LIMIT + 1):
        raw_page = _gh_api_json(
            f"repos/{{owner}}/{{repo}}/pulls?state=open&per_page={AUDIT_PAGE_SIZE}&page={page}",
            f"gh api open pulls page {page}",
        )
        if not isinstance(raw_page, list):
            raise AuditUnmeasurable(f"gh api open pulls page {page} is not a JSON list")
        prs.extend(_open_pr_row(r, page) for r in raw_page)
        if len(raw_page) < AUDIT_PAGE_SIZE:
            return prs
    raise AuditUnmeasurable(f"page limit {AUDIT_PAGE_LIMIT} reached before the last open PR page")


def _read_ledger_text(path: Path, required: bool) -> str:
    if not path.exists():
        if required:
            raise AuditUnmeasurable(f"ledger not found: {path}")
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise AuditUnmeasurable(f"ledger unreadable: {path}: {exc}") from None


def read_vouched_permits(paths: List[Path]) -> Set[str]:
    """The `permit_id` of every `door_check` block on a pr_merged or pr_merge_refused line of either ledger.

    A line that cannot be parsed vouches for nothing: skipping it keeps its permit orphaned (blocking side).
    """
    ids: Set[str] = set()
    for index, path in enumerate(paths):
        for line in _read_ledger_text(path, required=index == 0).splitlines():
            try:
                rec = json.loads(line) if line.strip() else None
            except ValueError:
                continue
            if not isinstance(rec, dict) or (rec.get("event_type") or rec.get("event")) not in _PERMIT_EVENTS:
                continue
            block = rec.get("door_check")
            permit_id = block.get("permit_id") if isinstance(block, dict) else None
            if isinstance(permit_id, str) and permit_id:
                ids.add(permit_id)
    return ids


def _reconcile_root() -> Path:
    return Path(os.environ.get("VNX_PROJECT_ROOT") or os.getcwd())


def _permit_kind(run: Dict[str, Any], permit_id: str, now: datetime.datetime,
                 ttl: datetime.timedelta, vouched: Set[str]) -> Optional[str]:
    """`stale`, `orphaned` or None. Only a run exactly on `failure` is no finding."""
    from forge_gate_publisher import CONCLUSION_FAILURE
    if run.get("conclusion") == CONCLUSION_FAILURE:
        return None
    try:
        stamped = _parse_ts(run.get("completed_at") or run.get("started_at"), "door run timestamp")
    except AuditUnmeasurable:
        return "stale"  # an age that cannot be read cannot be shown to be inside the TTL
    if now - stamped > ttl:
        return "stale"
    return None if permit_id in vouched else "orphaned"


def _head_findings(pr: Dict[str, Any], root: Path, now: datetime.datetime, ttl: datetime.timedelta,
                   vouched: Set[str], counts: Dict[str, int]) -> List[Dict[str, Any]]:
    import forge_gate_publisher
    head = forge_gate_publisher.door_runs_on_head(pr["sha"], project_root=root)
    findings: List[Dict[str, Any]] = []
    base = {"pr_number": pr["number"], "head_sha": pr["sha"]}
    for run in head.foreign:
        counts["foreign"] += 1
        findings.append(dict(base, kind="foreign_door_check", check_run_id=run.get("id"), permit_id=None))
    for run in head.own:
        counts["checked"] += 1
        permit_id = f"{head.app_id}:{pr['sha']}:{run.get('id')}"
        kind = _permit_kind(run, permit_id, now, ttl, vouched)
        if kind:
            counts[kind] += 1
            findings.append(dict(base, kind=kind, check_run_id=run.get("id"), permit_id=permit_id))
    return findings


def _revoke_finding(finding: Dict[str, Any], root: Path, receipts_file: str) -> bool:
    """D3's `revoke_door` (three attempts, PATCH to failure, no DELETE). True only when GitHub shows `failure`."""
    import forge_gate_publisher as fgp
    reason = f"{finding['kind']} {finding['permit_id']}"[:200]
    try:
        result = fgp.revoke_door(finding["check_run_id"], reason, sha=finding["head_sha"],
                                 pr=finding["pr_number"], project_root=root, receipts_file=receipts_file)
    except fgp.ForgeCheckRunError as exc:
        log.warning("revoking door run %s failed: %s", finding["check_run_id"], exc)
        return False
    return result == fgp.REVOKED


def _reconcile_exit(rec: Dict[str, Any]) -> None:
    counts = rec["counts"]
    stuck = counts["revoke_failed"] or counts["foreign"] or (rec["dry_run"] and (counts["stale"] or counts["orphaned"]))
    rec.update(outcome="findings" if stuck else "clean", exit_code=EXIT_ERROR if stuck else EXIT_OK)
    rec["reason"] = ("door permit findings: " + ", ".join(f"{k}={counts[k]}" for k in _COUNT_KEYS)) if stuck else ""


def run_reconcile(ttl_minutes: int, dry_run: bool = False,
                  now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """Read every open PR, revoke each stale or orphaned `vnx-gate/door` run. A failed read is `unknown`."""
    from forge_gate_publisher import ForgeCheckRunError
    rec: Dict[str, Any] = {"mode": "reconcile-open-permits", "outcome": "unknown", "exit_code": EXIT_UNKNOWN,
                           "reason": "", "dry_run": dry_run, "permit_ttl_minutes": ttl_minutes,
                           "counts": {k: 0 for k in _COUNT_KEYS}, "findings": []}
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    ttl = datetime.timedelta(minutes=ttl_minutes)
    try:
        root, ledgers = _reconcile_root(), _audit_ledger_paths()
        prs, vouched = list_open_prs(), read_vouched_permits(ledgers)
        for pr in prs:
            rec["findings"] += _head_findings(pr, root, moment, ttl, vouched, rec["counts"])
    except (AuditUnmeasurable, ForgeCheckRunError) as exc:
        rec["reason"] = str(exc)
        return rec
    for finding in rec["findings"]:
        if finding["kind"] != "foreign_door_check" and not dry_run:
            finding["revoked"] = _revoke_finding(finding, root, str(ledgers[0]))
            rec["counts"]["revoked" if finding["revoked"] else "revoke_failed"] += 1
    _reconcile_exit(rec)
    return rec


def _file_permit_item(finding: Dict[str, Any], project_id: str) -> Tuple[str, bool]:
    from open_items_manager import add_item_programmatic
    number, run_id, kind = finding["pr_number"], finding["check_run_id"], finding["kind"]
    revoked = finding.get("revoked") is True
    details = f"vnx-gate/door run {run_id} on PR #{number} (head {finding['head_sha']}): {kind}"
    if kind == "foreign_door_check":
        details += "; a door check from another App, reported and not touched"
    elif revoked:
        details += "; revoked with a PATCH to failure by backfill_pr_merged_receipts --reconcile-open-permits"
    else:
        details += f"; NOT revoked and may still read success. Recovery: python3 scripts/pr_merge.py --pr {number}"
    return add_item_programmatic(
        title=f"vnx-gate/door {kind} on PR #{number} ({'revoked' if revoked else 'open'})",
        severity="warn" if revoked or kind == "foreign_door_check" else "blocker",
        dispatch_id="door-permit", details=details,
        dedup_key=f"door-permit:{project_id}:{number}:{run_id}", source="door_reconcile",
    )


def publish_reconcile(rec: Dict[str, Any]) -> None:
    """One open item per finding (deduped on project, PR and run). A failed write is recorded, never hidden."""
    project_id = _audit_project_id(_audit_ledger_paths()[0].parent)
    rec["project_id"], rec["open_items"], rec["write_errors"] = project_id, [], []
    if rec["dry_run"]:
        return
    for finding in rec["findings"]:
        try:
            item_id, created = _file_permit_item(finding, project_id)
            rec["open_items"].append({"pr_number": finding["pr_number"], "item_id": item_id, "created": created})
        except Exception as exc:
            rec["write_errors"].append(f"open item for PR #{finding['pr_number']}: {exc}")


def _fold_publication(doc: Dict[str, Any], errors: List[str]) -> None:
    """A failed open-item write makes the doc unclean (exit 2), before its beacon is written. Each error goes to stderr."""
    if not errors:
        return
    for err in errors:
        print(f"WRITE ERROR: {err}", file=sys.stderr)
    doc.update(outcome="unknown", exit_code=EXIT_UNKNOWN, publication_errors=len(errors),
               reason=f"publication failed: {len(errors)} write error(s): " + "; ".join(errors))


def _merge_reconcile(report: Dict[str, Any], rec: Dict[str, Any]) -> None:
    """Fold the reconcile result into the audit report: exit = maximum, outcome follows the worse side."""
    report["reconcile"] = rec
    if rec["exit_code"] > report["exit_code"]:
        report.update(outcome="unknown" if rec["exit_code"] == EXIT_UNKNOWN else "gaps",
                      exit_code=rec["exit_code"], reason=rec["reason"])


def _print_reconcile(rec: Dict[str, Any]) -> None:
    if rec["outcome"] == "unknown":
        print(f"Door permits (reconcile): UNMEASURABLE: {rec['reason']}")
        return
    print("Door permits (reconcile): " + "  ".join(f"{k}={rec['counts'][k]}" for k in _COUNT_KEYS)
          + (" [dry-run]" if rec["dry_run"] else ""))
    for f in rec["findings"]:
        print(f"  {f['kind'].upper()} PR #{f['pr_number']} run {f['check_run_id']} revoked={f.get('revoked', False)}")


def _main_reconcile(args: argparse.Namespace) -> int:
    rec = run_reconcile(args.permit_ttl, dry_run=args.dry_run)
    if not args.no_write:
        publish_reconcile(rec)
        _fold_publication(rec, rec["write_errors"])
        _beacon_guard(rec, _audit_ledger_paths()[0].parent,
                      {"outcome": rec["outcome"], "reconcile": rec["counts"], "reason": rec["reason"]},
                      rec["write_errors"])
    if args.json:
        print(json.dumps(rec, indent=2, default=str))
    else:
        _print_reconcile(rec)
    return int(rec["exit_code"])


def _main_report_only(args: argparse.Namespace) -> int:
    report = run_report_only(since=args.since)
    if args.reconcile_open_permits:
        _merge_reconcile(report, run_reconcile(args.permit_ttl, dry_run=args.dry_run))
    if not args.no_write:
        if args.reconcile_open_permits:
            publish_reconcile(report["reconcile"])
            _fold_publication(report, report["reconcile"]["write_errors"])
        publish_report(report)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_report(report)
        if args.reconcile_open_permits:
            _print_reconcile(report["reconcile"])
    return int(report["exit_code"])


# ---------------------------------------------------------------------------
# --all-projects: the fleet runner (deur D7b)
# ---------------------------------------------------------------------------

FLEET_CHILD_TIMEOUT_SECONDS = 900
LEDGER_HEALTH_LABEL_PREFIX = "com.vnx.ledger-health."
_CHILD_PATH_ENV = ("VNX_DISPATCH_DIR", "VNX_LOGS_DIR", "VNX_PIDS_DIR", "VNX_LOCKS_DIR", "VNX_SOCKETS_DIR",
                   "VNX_REPORTS_DIR", "VNX_DB_DIR", "VNX_HEADLESS_REPORTS_DIR", "VNX_INTELLIGENCE_DIR")


def _installed_ledger_health_ids() -> Set[str]:
    """Project ids of the installed com.vnx.ledger-health.<id> labels (one `launchctl list` call)."""
    try:
        proc = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AuditUnmeasurable(f"launchctl list failed: {exc}") from exc
    if proc.returncode != 0:
        raise AuditUnmeasurable(f"launchctl list exited {proc.returncode}: {(proc.stderr or '').strip()[:200]}")
    ids: Set[str] = set()
    for line in (proc.stdout or "").splitlines():
        label = line.split()[-1] if line.split() else ""
        if label.startswith(LEDGER_HEALTH_LABEL_PREFIX) and len(label) > len(LEDGER_HEALTH_LABEL_PREFIX):
            ids.add(label[len(LEDGER_HEALTH_LABEL_PREFIX):])
    return ids


def enumerate_projects(registry_path: Path) -> List[Dict[str, Any]]:
    """Union of the registry and the ledger-health labels; a one-sided project carries a `finding`."""
    from fleet_role_drift import load_registry
    try:
        entries = load_registry(registry_path)
    except (OSError, ValueError) as exc:
        raise AuditUnmeasurable(f"project registry unreadable ({registry_path}): {exc}") from exc
    labelled = _installed_ledger_health_ids()
    projects: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        entry = entry if isinstance(entry, dict) else {}
        pid = str(entry.get("project_id") or entry.get("name") or "?")
        projects[pid] = {"project_id": pid, "path": entry.get("path") or "",
                         "finding": "" if pid in labelled else "job_missing",
                         "registry_ok": bool(entry.get("project_id"))}
    for pid in sorted(labelled - set(projects)):
        projects[pid] = {"project_id": pid, "path": "", "finding": "unregistered_project", "registry_ok": True}
    return list(projects.values())


def _project_data_dir(project_id: str, root: Path) -> Path:
    """The store of ONE project. An inherited explicit data dir would point every project at the same store."""
    from vnx_paths import resolve_project_data_root
    saved = {k: os.environ.pop(k) for k in ("VNX_DATA_DIR_EXPLICIT", "VNX_DATA_DIR", "VNX_STATE_DIR") if k in os.environ}
    try:
        return resolve_project_data_root(project_id, root)
    finally:
        os.environ.update(saved)


def _child_env(project_id: str, root: Path, data_dir: Path) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _CHILD_PATH_ENV}
    env.update({"VNX_PROJECT_ID": project_id, "VNX_PROJECT_ROOT": str(root), "PROJECT_ROOT": str(root),
                "VNX_DATA_DIR": str(data_dir), "VNX_DATA_DIR_EXPLICIT": "1",
                "VNX_STATE_DIR": str(data_dir / "state")})
    return env


def _project_row(project: Dict[str, Any], outcome: str, exit_code: int, gaps: int = 0, reason: str = "") -> Dict[str, Any]:
    finding = project["finding"]
    if finding and outcome == "clean":
        outcome, exit_code = "finding", max(exit_code, EXIT_ERROR)
    return {"project_id": project["project_id"], "outcome": outcome, "gaps": gaps,
            "exit_code": exit_code, "finding": finding, "reason": reason}


def _run_child(project: Dict[str, Any], since: Optional[str], permit_ttl: int = PERMIT_TTL_MINUTES,
               dry_run: bool = False) -> Tuple[str, int, int, str]:
    """Run `--report-only --reconcile-open-permits --json` for one project. Returns (outcome, exit, gaps, reason); never raises."""
    pid, raw_path = project["project_id"], project["path"]
    if not project["registry_ok"]:
        return "unmeasurable", EXIT_UNKNOWN, 0, "registry entry has no project_id"
    root = Path(raw_path).expanduser() if raw_path else None
    if root is None or not root.is_dir():
        return "unmeasurable", EXIT_UNKNOWN, 0, f"no project path ({raw_path or 'none registered'})"
    if not (root / ".git").exists():
        return "unmeasurable", EXIT_UNKNOWN, 0, f"no git repo at {root}"
    try:
        data_dir = _project_data_dir(pid, root)
        cmd = [sys.executable, str(Path(__file__).resolve()), "--report-only", "--reconcile-open-permits",
               "--permit-ttl", str(permit_ttl), "--json"]
        cmd += ["--since", since] if since else []
        cmd += ["--dry-run"] if dry_run else []
        proc = subprocess.run(cmd, cwd=str(root), env=_child_env(pid, root.resolve(), data_dir),
                              capture_output=True, text=True, timeout=FLEET_CHILD_TIMEOUT_SECONDS)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return "unmeasurable", EXIT_UNKNOWN, 0, f"child failed: {exc}"
    return _read_child_report(proc)


def _read_child_report(proc: Any) -> Tuple[str, int, int, str]:
    wanted = {0: "clean", 1: "gaps", 2: "unknown"}
    try:
        report = json.loads(proc.stdout or "")
        rc = proc.returncode
        if wanted.get(rc) != report.get("outcome") or report.get("exit_code") != rc:
            raise ValueError(f"exit {rc} does not match report outcome {report.get('outcome')!r}")
        gaps = len(report["missing"])
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        tail = (proc.stderr or "").strip()[-200:]
        return "unmeasurable", EXIT_UNKNOWN, 0, f"child output unusable: {exc} {tail}".strip()
    if rc == EXIT_UNKNOWN and report.get("publication_errors"):
        return "publication_failed", rc, gaps, str(report.get("reason") or "")
    if rc == EXIT_UNKNOWN:
        return "unmeasurable", EXIT_UNKNOWN, 0, str(report.get("reason") or "child reported unknown")
    return wanted[rc], rc, gaps, str(report.get("reason") or "")


class UnknownProject(Exception):
    pass


def run_fleet(selected: List[str], registry_path: Path, since: Optional[str],
              permit_ttl: int = PERMIT_TTL_MINUTES, dry_run: bool = False) -> Dict[str, Any]:
    """Run the audit per project. Raises AuditUnmeasurable on an empty enumeration or an unknown id."""
    projects = enumerate_projects(registry_path)
    if not projects:
        raise AuditUnmeasurable("no projects enumerated (registry and ledger-health labels are both empty)")
    known = {p["project_id"] for p in projects}
    for pid in selected:
        if pid not in known:
            raise UnknownProject(pid)
    rows = [_project_row(p, *_run_child(p, since, permit_ttl, dry_run)) for p in projects if not selected or p["project_id"] in selected]
    exit_code = max(r["exit_code"] for r in rows)
    return {"mode": "all-projects", "rows": rows, "exit_code": exit_code}


def _print_fleet(result: Dict[str, Any]) -> None:
    for r in result["rows"]:
        extra = "  ".join(x for x in (r["finding"], r["reason"]) if x)
        print(f"{r['project_id']:<24} {r['outcome']:<12} gaps={r['gaps']}  exit={r['exit_code']}  {extra}".rstrip())
    rows = result["rows"]
    print(f"Door audit fleet: {len(rows)} project(s), {sum(r['gaps'] for r in rows)} gap(s), exit {result['exit_code']}")


def _main_fleet(args: argparse.Namespace) -> int:
    registry = Path(args.registry).expanduser() if args.registry else Path.home() / ".vnx" / "projects.json"
    try:
        result = run_fleet(args.project or [], registry, args.since, args.permit_ttl, args.dry_run)
    except UnknownProject as exc:
        print(f"onbekend project: {exc}", file=sys.stderr)
        return EXIT_UNKNOWN
    except AuditUnmeasurable as exc:
        print(f"Door audit fleet: UNMEASURABLE: {exc}")
        return EXIT_UNKNOWN
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        _print_fleet(result)
    return int(result["exit_code"])


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill pr_merged receipts for already-merged PRs",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no writes")
    parser.add_argument("--limit", type=int, default=500, help="Max merged PRs to fetch (default 500)")
    parser.add_argument("--since", default=None, help="Only PRs merged on/after this date (YYYY-MM-DD)")
    parser.add_argument("--json", action="store_true", help="Output summary as JSON")
    parser.add_argument("--report-only", action="store_true",
                        help="Door audit: compare, report, never heal (exit 0 clean, 1 gaps, 2 unmeasurable)")
    parser.add_argument("--no-write", action="store_true",
                        help="With --report-only: write no open items and no beacon")
    parser.add_argument("--all-projects", action="store_true",
                        help="Door audit for every enumerated project, one subprocess each (exit = max severity)")
    parser.add_argument("--project", action="append", metavar="ID",
                        help="With --all-projects: only this project id (repeatable); unknown id is exit 2")
    parser.add_argument("--registry", default=None, help="With --all-projects: project registry (default ~/.vnx/projects.json)")
    parser.add_argument("--reconcile-open-permits", action="store_true",
                        help="Door permits: read every open PR, revoke a stale or orphaned vnx-gate/door run (PATCH to "
                             "failure; --dry-run patches nothing; --no-write skips the open items, not the revocation)")
    parser.add_argument("--permit-ttl", type=int, default=PERMIT_TTL_MINUTES, metavar="MINUTES",
                        help="Door permit lifetime in minutes (default 30)")
    args = parser.parse_args(argv)

    if args.project and not args.all_projects:
        parser.error("--project needs --all-projects")
    if args.permit_ttl <= 0:
        parser.error("--permit-ttl must be a positive number of minutes")
    if args.all_projects:
        return _main_fleet(args)
    if args.report_only:
        return _main_report_only(args)
    if args.reconcile_open_permits:
        return _main_reconcile(args)

    summary = backfill(
        limit=args.limit,
        since=args.since,
        dry_run=args.dry_run,
    )

    if args.json:
        print(json.dumps(summary, indent=2))
        return EXIT_OK

    dry_label = " [dry-run]" if args.dry_run else ""
    print(f"Backfill pr_merged receipts{dry_label}")
    print(f"  merged PRs found    : {summary['total_merged_prs']}")
    print(f"  already receipted   : {summary['already_receipted']}")
    print(f"  missing receipt     : {summary['missing_receipt_count']}")
    if args.dry_run:
        print(f"  would backfill      : {summary['missing_receipt_count']}")
    else:
        print(f"  backfilled          : {summary['backfilled']}")
        if summary["errors"]:
            print(f"  errors              : {summary['errors']}")

    if summary["details"] and not args.json:
        print()
        for d in summary["details"][:20]:
            action = d.get("action", "?")
            print(f"  PR #{d['pr_number']:>4}  {action:<14}  {d.get('pr_title','')[:60]}")
        if len(summary["details"]) > 20:
            print(f"  ... and {len(summary['details']) - 20} more")

    return EXIT_OK if summary["errors"] == 0 else EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
