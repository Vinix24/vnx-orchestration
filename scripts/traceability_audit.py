#!/usr/bin/env python3
"""Traceability audit — cross-reference PRs / commits / dispatches / receipts.

Re-runnable governance observability tool. Scans a VNX repo for the four
traceability chains and reports every gap with counts, examples, and a
percentage per category.

Usage:
    python3 scripts/traceability_audit.py [--since YYYY-MM-DD] [--until YYYY-MM-DD]
    python3 scripts/traceability_audit.py --since 2026-01-01 --repo /path/to/repo

Exit code 0 always — this is a reporting tool, not a gate.
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Path / project-root setup
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR / "lib"))

from project_root import resolve_project_root, resolve_data_dir  # noqa: E402


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class ReceiptRecord:
    dispatch_id: str
    pr_id: str          # internal "PR-N" or "none" or ""
    event_type: str
    status: str
    timestamp: str
    commit_hash: str    # commit_hash_after, may be ""
    terminal: str
    source_file: str    # which NDJSON file it came from
    raw: Dict           # full record


@dataclass
class DispatchRecord:
    dispatch_id: str    # filename stem
    state: str          # completed / failed / rejected / ...
    pr_id: str          # PR-ID header value, may be "none" or ""
    source_path: str    # file path


@dataclass
class PRRecord:
    number: int         # GitHub PR number (0 = unknown / git-only)
    title: str
    branch: str         # headRefName
    merged_at: str      # ISO timestamp
    sha: str            # merge commit SHA
    internal_pr_ids: List[str]  # "PR-N" refs extracted from commit message
    github_pr_refs: List[str]   # "#NNN" refs extracted


@dataclass
class GapReport:
    category: str
    total: int
    traced: int
    gap_count: int
    gap_pct: float
    examples: List[str]  # up to 10 gap examples
    notes: str = ""


# ---------------------------------------------------------------------------
# Receipt loader
# ---------------------------------------------------------------------------

_ISO_Z_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T")


def _parse_date(ts: str) -> Optional[datetime.date]:
    """Extract date from ISO-8601 timestamp."""
    if not ts:
        return None
    m = _ISO_Z_RE.match(ts)
    if m:
        try:
            return datetime.date.fromisoformat(m.group(1))
        except ValueError:
            return None
    return None


def _in_range(
    ts: str,
    since: Optional[datetime.date],
    until: Optional[datetime.date],
) -> bool:
    d = _parse_date(ts)
    if d is None:
        return True  # unknown date: include
    if since and d < since:
        return False
    if until and d > until:
        return False
    return True


def iter_receipts(
    ndjson_path: Path,
    since: Optional[datetime.date],
    until: Optional[datetime.date],
) -> Iterator[ReceiptRecord]:
    """Yield ReceiptRecords from an NDJSON file, filtered by date range."""
    if not ndjson_path.exists():
        return
    with ndjson_path.open("r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(r, dict):
                continue
            ts = str(r.get("timestamp", ""))
            if not _in_range(ts, since, until):
                continue
            yield ReceiptRecord(
                dispatch_id=str(r.get("dispatch_id") or ""),
                pr_id=str(r.get("pr_id") or ""),
                event_type=str(r.get("event_type") or r.get("event") or ""),
                status=str(r.get("status") or ""),
                timestamp=ts,
                commit_hash=str(r.get("commit_hash_after") or ""),
                terminal=str(r.get("terminal") or ""),
                source_file=str(ndjson_path),
                raw=r,
            )


def load_all_receipts(
    data_dir: Path,
    project_root: Path,
    since: Optional[datetime.date],
    until: Optional[datetime.date],
) -> List[ReceiptRecord]:
    """Load receipts from all known locations."""
    candidates: List[Path] = [
        data_dir / "state" / "t0_receipts.ndjson",
        project_root / ".vnx-intelligence" / "receipts" / "t0_receipts.ndjson",
        project_root / "receipts" / "t0_receipts.ndjson",
    ]
    # Also check central ~/.vnx-data
    home_central = Path.home() / ".vnx-data" / "state" / "t0_receipts.ndjson"
    if home_central not in candidates:
        candidates.append(home_central)

    seen_keys: Set[str] = set()
    records: List[ReceiptRecord] = []
    for path in candidates:
        for rec in iter_receipts(path, since, until):
            # Dedup by dispatch_id + event_type + timestamp
            key = f"{rec.dispatch_id}|{rec.event_type}|{rec.timestamp}"
            if key not in seen_keys:
                seen_keys.add(key)
                records.append(rec)
    return records


# ---------------------------------------------------------------------------
# Dispatch loader
# ---------------------------------------------------------------------------

_PR_ID_HEADER_RE = re.compile(r"^PR-ID:\s*(\S+)", re.MULTILINE)
_DISPATCH_ID_HEADER_RE = re.compile(r"^Dispatch-ID:\s*(\S+)", re.MULTILINE)


def _extract_pr_id_from_dispatch(content: str) -> str:
    m = _PR_ID_HEADER_RE.search(content)
    return m.group(1).strip() if m else ""


def _read_dispatch_dir(
    dispatch_dir: Path,
    state: str,
    since: Optional[datetime.date],
    until: Optional[datetime.date],
) -> List[DispatchRecord]:
    """Read all markdown dispatch files from one state dir."""
    records: List[DispatchRecord] = []
    if not dispatch_dir.exists():
        return records
    for f in dispatch_dir.glob("*.md"):
        dispatch_id = f.stem
        # Extract date from filename (format: YYYYMMDD-...)
        date_match = re.match(r"^(\d{4})(\d{2})(\d{2})-", dispatch_id)
        if date_match:
            try:
                d = datetime.date(
                    int(date_match.group(1)),
                    int(date_match.group(2)),
                    int(date_match.group(3)),
                )
                if since and d < since:
                    continue
                if until and d > until:
                    continue
            except ValueError:
                pass
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
            pr_id = _extract_pr_id_from_dispatch(content)
        except OSError:
            pr_id = ""
        records.append(DispatchRecord(
            dispatch_id=dispatch_id,
            state=state,
            pr_id=pr_id,
            source_path=str(f),
        ))
    return records


def load_all_dispatches(
    project_root: Path,
    since: Optional[datetime.date],
    until: Optional[datetime.date],
) -> List[DispatchRecord]:
    """Load dispatch records from both primary and intelligence locations."""
    dispatch_roots: List[Path] = [
        project_root / "dispatches",
        project_root / ".vnx-intelligence" / "dispatches",
    ]
    active_states = {"completed", "failed", "active", "pending"}
    records: List[DispatchRecord] = []
    seen_ids: Set[str] = set()

    for root in dispatch_roots:
        if not root.exists():
            continue
        for state_dir in root.iterdir():
            if not state_dir.is_dir():
                continue
            state = state_dir.name
            for rec in _read_dispatch_dir(state_dir, state, since, until):
                if rec.dispatch_id not in seen_ids:
                    seen_ids.add(rec.dispatch_id)
                    records.append(rec)

    return records


# ---------------------------------------------------------------------------
# Dispatch register NDJSON loader (optional — may not exist)
# ---------------------------------------------------------------------------

def load_dispatch_register(data_dir: Path) -> List[Dict]:
    """Load dispatch_register.ndjson events if present."""
    path = data_dir / "state" / "dispatch_register.ndjson"
    if not path.exists():
        return []
    records = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return records


# ---------------------------------------------------------------------------
# Runtime coordination DB loader
# ---------------------------------------------------------------------------

def load_db_dispatches(data_dir: Path) -> List[Dict]:
    """Load dispatches from runtime_coordination.db if accessible."""
    db_path = data_dir / "state" / "runtime_coordination.db"
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='dispatches'")
        if not c.fetchone():
            conn.close()
            return []
        c.execute("SELECT * FROM dispatches")
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows
    except Exception:
        return []


# ---------------------------------------------------------------------------
# GitHub / Git PR loader
# ---------------------------------------------------------------------------

_INTERNAL_PR_RE = re.compile(r"\bPR-(\d+)\b")
_GITHUB_PR_NUM_RE = re.compile(r"#(\d+)")


def _run_git(args: List[str], cwd: str) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception as exc:
        print(f"[traceability-audit] _run_git({args!r}) failed: {exc}", file=sys.stderr)
    return None


def _run_gh(args: List[str], cwd: str) -> Optional[str]:
    try:
        result = subprocess.run(
            ["gh"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception as exc:
        print(f"[traceability-audit] _run_gh({args!r}) failed: {exc}", file=sys.stderr)
    return None


def load_github_prs(
    project_root: Path,
    since: Optional[datetime.date],
    until: Optional[datetime.date],
) -> List[PRRecord]:
    """Load merged PRs via gh CLI (primary) or git log (fallback)."""
    cwd = str(project_root)
    records: List[PRRecord] = []

    # --- Try gh pr list ---
    gh_output = _run_gh(
        [
            "pr", "list",
            "--state", "merged",
            "--limit", "500",
            "--json",
            "number,title,headRefName,mergedAt,mergeCommit",
        ],
        cwd,
    )
    if gh_output:
        try:
            prs = json.loads(gh_output)
            for pr in prs:
                merged_at = str(pr.get("mergedAt") or "")
                d = _parse_date(merged_at)
                if since and d and d < since:
                    continue
                if until and d and d > until:
                    continue
                merge_commit = pr.get("mergeCommit") or {}
                sha = str(merge_commit.get("oid") or "")
                title = str(pr.get("title") or "")
                branch = str(pr.get("headRefName") or "")
                combined = f"{title} {branch}"
                internal_prs = [
                    f"PR-{m}" for m in _INTERNAL_PR_RE.findall(combined)
                ]
                github_refs = [
                    f"#{m}" for m in _GITHUB_PR_NUM_RE.findall(title)
                ]
                records.append(PRRecord(
                    number=int(pr.get("number") or 0),
                    title=title,
                    branch=branch,
                    merged_at=merged_at,
                    sha=sha,
                    internal_pr_ids=internal_prs,
                    github_pr_refs=github_refs,
                ))
            return records
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    # --- Fallback: git log --merges ---
    git_out = _run_git(
        ["log", "--merges", "--pretty=format:%H %ai %s"],
        cwd,
    )
    if not git_out:
        return records

    for line in git_out.splitlines():
        parts = line.split(" ", 3)
        if len(parts) < 4:
            continue
        sha, date_str, time_str, rest = parts[0], parts[1], parts[2], parts[3]
        tz_part = ""
        # git format: YYYY-MM-DD HH:MM:SS +0200
        msg_parts = rest.split(" ", 1)
        if len(msg_parts) == 2:
            tz_part, title = msg_parts
        else:
            title = rest

        try:
            d = datetime.date.fromisoformat(date_str)
        except ValueError:
            d = None
        if since and d and d < since:
            continue
        if until and d and d > until:
            continue

        internal_prs = [f"PR-{m}" for m in _INTERNAL_PR_RE.findall(title)]
        github_refs = [f"#{m}" for m in _GITHUB_PR_NUM_RE.findall(title)]
        records.append(PRRecord(
            number=0,  # unknown from git log
            title=title,
            branch="",
            merged_at=f"{date_str}T{time_str}",
            sha=sha,
            internal_pr_ids=internal_prs,
            github_pr_refs=github_refs,
        ))

    return records


# ---------------------------------------------------------------------------
# Gap analysis
# ---------------------------------------------------------------------------

COMPLETION_EVENTS = frozenset({
    "task_complete",
    "task_completed",
    "completion",
    "complete",
    "subprocess_completion",
})

_VALID_DISPATCH_ID_RE = re.compile(r"^\d{8}-")  # YYYYMMDD-... date-prefix format


def _is_valid_dispatch_id(dispatch_id: str) -> bool:
    """True when dispatch_id looks like a real structured dispatch (not free-form)."""
    if not dispatch_id:
        return False
    skip = {
        "unknown", "(none - direct instruction)", "(self-initiated)",
        "(user-requested)", "",
    }
    if dispatch_id in skip:
        return False
    return bool(_VALID_DISPATCH_ID_RE.match(dispatch_id))


def gap_dispatches_without_completion_receipt(
    dispatches: List[DispatchRecord],
    receipts: List[ReceiptRecord],
) -> GapReport:
    """Category A: dispatches in completed/ with no completion receipt."""
    completed = [d for d in dispatches if d.state == "completed"]
    completion_dispatch_ids: Set[str] = {
        r.dispatch_id
        for r in receipts
        if r.event_type in COMPLETION_EVENTS
    }

    gaps = [
        d.dispatch_id
        for d in completed
        if d.dispatch_id not in completion_dispatch_ids
    ]
    total = len(completed)
    gap_count = len(gaps)
    traced = total - gap_count

    return GapReport(
        category="A — Dispatches without completion receipt",
        total=total,
        traced=traced,
        gap_count=gap_count,
        gap_pct=round(gap_count / total * 100, 1) if total else 0.0,
        examples=gaps[:10],
        notes=(
            "Dispatch file in completed/ state but no task_complete/task_completed/"
            "subprocess_completion receipt with matching dispatch_id."
        ),
    )


def gap_receipts_without_dispatch(
    receipts: List[ReceiptRecord],
    dispatch_ids: Set[str],
) -> GapReport:
    """Category B: receipts whose dispatch_id is missing, unknown, or unresolvable."""
    # Only look at receipts that are supposed to carry a dispatch_id
    relevant_events = COMPLETION_EVENTS | {
        "task_started", "task_failed", "task_timeout", "task_blocked",
        "dispatch_sent", "dispatch_ack",
    }
    relevant = [r for r in receipts if r.event_type in relevant_events]

    gaps: List[str] = []
    for r in relevant:
        did = r.dispatch_id
        if not _is_valid_dispatch_id(did) or did not in dispatch_ids:
            label = f"{r.event_type}@{r.timestamp[:19]} dispatch_id={did!r:.60}"
            gaps.append(label)

    total = len(relevant)
    gap_count = len(gaps)
    traced = total - gap_count

    return GapReport(
        category="B — Receipts without traceable dispatch",
        total=total,
        traced=traced,
        gap_count=gap_count,
        gap_pct=round(gap_count / total * 100, 1) if total else 0.0,
        examples=gaps[:10],
        notes=(
            "Task/completion/ack receipts with a dispatch_id that is empty, "
            "'unknown', or does not match any known dispatch file."
        ),
    )


def gap_prs_without_receipt(
    prs: List[PRRecord],
    receipts: List[ReceiptRecord],
    dispatches: List[DispatchRecord],
) -> GapReport:
    """Category C: merged PRs that cannot be traced to any receipt or dispatch.

    Linkage strategies tried (in order):
    1. Receipt pr_id "PR-N" matches internal_pr_ids extracted from merge commit
    2. Receipt dispatch_id or pr_id contains the GitHub PR number (#NNN)
    3. Dispatch file pr_id "PR-N" matches internal_pr_ids from merge commit
    4. Branch name substring match against dispatch_ids (heuristic)
    """
    # Build lookup structures
    receipt_pr_ids: Set[str] = {
        r.pr_id for r in receipts if r.pr_id and r.pr_id.lower() not in ("none", "")
    }
    receipt_dispatch_ids: Set[str] = {
        r.dispatch_id for r in receipts if _is_valid_dispatch_id(r.dispatch_id)
    }
    dispatch_pr_ids: Dict[str, str] = {
        d.pr_id: d.dispatch_id
        for d in dispatches
        if d.pr_id and d.pr_id.lower() not in ("none", "", "n/a")
    }

    gaps: List[str] = []
    traced: int = 0

    for pr in prs:
        linked = False

        # Strategy 1: internal_pr_ids (PR-N from commit message) in receipt pr_ids
        for int_pr_id in pr.internal_pr_ids:
            if int_pr_id in receipt_pr_ids:
                linked = True
                break

        # Strategy 2: GitHub PR number appears in receipt dispatch_id or pr_id
        if not linked and pr.number:
            gh_ref = f"#{pr.number}"
            pr_num_str = str(pr.number)
            for r in receipts:
                if pr_num_str in (r.dispatch_id or "") or gh_ref in (r.pr_id or ""):
                    linked = True
                    break

        # Strategy 3: internal_pr_ids match dispatch pr_id header
        if not linked:
            for int_pr_id in pr.internal_pr_ids:
                if int_pr_id in dispatch_pr_ids:
                    linked = True
                    break

        # Strategy 4: significant branch tokens appear in dispatch_id (heuristic)
        # Split branch into tokens, require >=2 tokens of >=4 chars to match
        # within a receipt dispatch_id. E.g. "feat/my-feature" → ["feat", "my", "feature"]
        # → a dispatch_id containing "my-feature" or "feat-my" matches.
        if not linked and pr.branch:
            branch_raw = pr.branch.lower().replace("/", "-").replace("_", "-")
            branch_tokens = [t for t in re.split(r"[-]+", branch_raw) if len(t) >= 4]
            if branch_tokens:
                for did in receipt_dispatch_ids:
                    did_lower = did.lower()
                    matches = sum(1 for tok in branch_tokens if tok in did_lower)
                    if matches >= max(1, len(branch_tokens) // 2):
                        linked = True
                        break

        if linked:
            traced += 1
        else:
            num_str = f"#{pr.number}" if pr.number else "(git-only)"
            gaps.append(
                f"{num_str} [{pr.merged_at[:10]}] {pr.title[:60]}"
            )

    total = len(prs)
    gap_count = len(gaps)

    return GapReport(
        category="C — Merged PRs without receipt/dispatch linkage",
        total=total,
        traced=traced,
        gap_count=gap_count,
        gap_pct=round(gap_count / total * 100, 1) if total else 0.0,
        examples=gaps[:10],
        notes=(
            "Merged PR has no receipt with pr_id matching internal PR-N label, "
            "no receipt/dispatch referencing GitHub PR number, and no branch-slug "
            "match in any receipt dispatch_id. "
            "NOTE: recent PRs use GitHub numeric IDs (#600+) while receipts store "
            "internal PR-N labels (PR-0..PR-70) — cross-scheme gap is structural."
        ),
    )


def gap_receipts_without_pr_or_dispatch(
    receipts: List[ReceiptRecord],
    dispatch_ids: Set[str],
) -> GapReport:
    """Category D: completion receipts with no pr_id AND unknown/missing dispatch_id."""
    completion_receipts = [
        r for r in receipts if r.event_type in COMPLETION_EVENTS
    ]
    gaps: List[str] = []
    for r in completion_receipts:
        has_pr = r.pr_id and r.pr_id.lower() not in ("none", "")
        has_dispatch = _is_valid_dispatch_id(r.dispatch_id) and r.dispatch_id in dispatch_ids
        if not has_pr and not has_dispatch:
            label = (
                f"{r.event_type}@{r.timestamp[:19]} "
                f"dispatch_id={r.dispatch_id!r:.40}"
            )
            gaps.append(label)

    total = len(completion_receipts)
    gap_count = len(gaps)
    traced = total - gap_count

    return GapReport(
        category="D — Completion receipts with no PR and no dispatch linkage",
        total=total,
        traced=traced,
        gap_count=gap_count,
        gap_pct=round(gap_count / total * 100, 1) if total else 0.0,
        examples=gaps[:10],
        notes=(
            "Completion receipt (task_complete, subprocess_completion, etc.) "
            "has neither a valid pr_id nor a dispatch_id that resolves to a "
            "known dispatch file. Full audit trail broken for these events."
        ),
    )


# ---------------------------------------------------------------------------
# Report renderer
# ---------------------------------------------------------------------------

def render_markdown_report(
    gaps: List[GapReport],
    since: Optional[datetime.date],
    until: Optional[datetime.date],
    project_root: Path,
    receipt_count: int,
    dispatch_count: int,
    pr_count: int,
    run_ts: str,
    schema_notes: List[str],
) -> str:
    since_str = str(since) if since else "all time"
    until_str = str(until) if until else "today"
    overall_traced = sum(g.traced for g in gaps)
    overall_total = sum(g.total for g in gaps)
    overall_gap = overall_total - overall_traced
    overall_pct = round(overall_traced / overall_total * 100, 1) if overall_total else 0.0

    lines: List[str] = [
        f"# Traceability Audit — {run_ts[:10]}",
        "",
        f"**Range:** {since_str} → {until_str}",
        f"**Repo:** {project_root}",
        f"**Run at:** {run_ts}",
        "",
        "## Summary",
        "",
        f"| Metric | Value |",
        f"|---|---|",
        f"| Receipts loaded | {receipt_count} |",
        f"| Dispatch records loaded | {dispatch_count} |",
        f"| Merged PRs loaded | {pr_count} |",
        f"| **Overall traced events** | **{overall_traced} / {overall_total}** |",
        f"| **Overall gap** | **{overall_gap} ({100 - overall_pct:.1f}%)** |",
        "",
        "## Gap Categories",
        "",
    ]

    for g in gaps:
        traced_pct = round(g.traced / g.total * 100, 1) if g.total else 0.0
        lines += [
            f"### {g.category}",
            "",
            f"| Metric | Value |",
            f"|---|---|",
            f"| Total | {g.total} |",
            f"| Traced | {g.traced} ({traced_pct:.1f}%) |",
            f"| **Gap** | **{g.gap_count} ({g.gap_pct:.1f}%)** |",
            "",
            f"**What this means:** {g.notes}",
            "",
        ]
        if g.examples:
            lines.append("**Gap examples (first 10):**")
            lines.append("")
            for ex in g.examples:
                lines.append(f"- `{ex}`")
            lines.append("")
        else:
            lines.append("**No gaps detected.**")
            lines.append("")

    if schema_notes:
        lines += [
            "## Schema / structural notes",
            "",
        ]
        for note in schema_notes:
            lines.append(f"- {note}")
        lines.append("")

    lines += [
        "## Open Items",
        "",
        "_(Empty — all gaps are reported above. Operator action required to close structural "
        "gaps in PR↔receipt linkage before traceability reaches 100%.)_",
        "",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        help="Include only items from this date onwards (inclusive).",
    )
    p.add_argument(
        "--until",
        metavar="YYYY-MM-DD",
        help="Include only items up to this date (inclusive).",
    )
    p.add_argument(
        "--repo",
        metavar="PATH",
        help="Override project root (default: auto-detect via git).",
    )
    p.add_argument(
        "--output",
        metavar="PATH",
        help="Override output file path (default: claudedocs/traceability-audit-DATE.md).",
    )
    p.add_argument(
        "--no-write",
        action="store_true",
        help="Print report to stdout instead of writing to file.",
    )
    return p.parse_args()


def _parse_date_arg(value: Optional[str]) -> Optional[datetime.date]:
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        print(f"ERROR: invalid date format {value!r} — expected YYYY-MM-DD", file=sys.stderr)
        sys.exit(1)


def run_audit(
    since: Optional[datetime.date],
    until: Optional[datetime.date],
    project_root: Path,
) -> Tuple[List[GapReport], int, int, int, List[str]]:
    """Execute the audit and return (gap_reports, receipt_count, dispatch_count, pr_count, notes)."""
    data_dir = resolve_data_dir(str(project_root / "scripts" / "traceability_audit.py"))

    # --- Load data ---
    receipts = load_all_receipts(data_dir, project_root, since, until)
    dispatches = load_all_dispatches(project_root, since, until)
    prs = load_github_prs(project_root, since, until)

    # Index dispatch IDs
    all_dispatch_ids: Set[str] = {d.dispatch_id for d in dispatches}

    # Also collect dispatch IDs from register and DB (may not overlap)
    register_events = load_dispatch_register(data_dir)
    for ev in register_events:
        did = str(ev.get("dispatch_id") or "")
        if _is_valid_dispatch_id(did):
            all_dispatch_ids.add(did)

    db_dispatches = load_db_dispatches(data_dir)
    for row in db_dispatches:
        did = str(row.get("dispatch_id") or "")
        if _is_valid_dispatch_id(did):
            all_dispatch_ids.add(did)

    # --- Gap analysis ---
    gap_a = gap_dispatches_without_completion_receipt(dispatches, receipts)
    gap_b = gap_receipts_without_dispatch(receipts, all_dispatch_ids)
    gap_c = gap_prs_without_receipt(prs, receipts, dispatches)
    gap_d = gap_receipts_without_pr_or_dispatch(receipts, all_dispatch_ids)

    # --- Schema notes ---
    schema_notes: List[str] = []

    # Check if pr_number (GitHub numeric) appears in ANY receipt
    pr_number_in_receipts = any(
        r.raw.get("pr_number") is not None for r in receipts
    )
    if not pr_number_in_receipts:
        schema_notes.append(
            "No receipt in any source carries a `pr_number` field (GitHub numeric ID). "
            "Receipts use `pr_id` (internal PR-N labels, e.g. PR-0..PR-70). "
            "GitHub PR numbers (#600+) and internal PR-N labels are disjoint schemes — "
            "this is the root cause of Category C gaps for all recent PRs."
        )

    unique_pr_ids = {r.pr_id for r in receipts if r.pr_id and r.pr_id not in ("none", "")}
    if unique_pr_ids:
        schema_notes.append(
            f"Internal PR-N IDs present in receipts: {sorted(unique_pr_ids)[:20]}"
        )

    if not prs:
        schema_notes.append(
            "No merged PRs could be loaded — gh CLI may not be authenticated "
            "or no GitHub remote is configured. Category C shows 0/0."
        )

    return [gap_a, gap_b, gap_c, gap_d], len(receipts), len(dispatches), len(prs), schema_notes


def main() -> None:
    args = _parse_args()
    since = _parse_date_arg(args.since)
    until = _parse_date_arg(args.until)

    if args.repo:
        project_root = Path(args.repo).resolve()
    else:
        project_root = resolve_project_root(__file__)

    run_ts = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()

    print(f"[traceability-audit] project_root={project_root}", file=sys.stderr)
    print(f"[traceability-audit] range={since or 'all'} -> {until or 'now'}", file=sys.stderr)

    gaps, receipt_count, dispatch_count, pr_count, schema_notes = run_audit(
        since, until, project_root
    )

    report = render_markdown_report(
        gaps=gaps,
        since=since,
        until=until,
        project_root=project_root,
        receipt_count=receipt_count,
        dispatch_count=dispatch_count,
        pr_count=pr_count,
        run_ts=run_ts,
        schema_notes=schema_notes,
    )

    if args.no_write:
        print(report)
        return

    # Determine output path
    if args.output:
        out_path = Path(args.output)
    else:
        date_str = run_ts[:10]
        claudedocs = project_root / "claudedocs"
        claudedocs.mkdir(parents=True, exist_ok=True)
        out_path = claudedocs / f"traceability-audit-{date_str}.md"

    # Atomic write
    tmp_path = out_path.with_suffix(".tmp")
    tmp_path.write_text(report, encoding="utf-8")
    import os
    os.replace(tmp_path, out_path)

    print(f"[traceability-audit] report written to {out_path}", file=sys.stderr)

    # Print summary to stdout
    for g in gaps:
        traced_pct = round(g.traced / g.total * 100, 1) if g.total else 0.0
        print(
            f"  {g.category}: {g.gap_count}/{g.total} gaps "
            f"({g.gap_pct:.1f}% gap rate, {traced_pct:.1f}% traced)"
        )


if __name__ == "__main__":
    main()
