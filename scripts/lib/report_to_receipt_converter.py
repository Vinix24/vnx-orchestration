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
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

from dispatch_identity import _IDENTITY_UNRESOLVED  # single canonical sentinel (dispatch-20260804-190000)

_LIB_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _LIB_DIR.parent  # scripts/ — append_receipt.py lives here
_WATERMARK_FILENAME = "report_to_receipt_processed.txt"
# OI-1102: the Bash receipt processor uses a separate watermark file
# (processed_receipts.txt). The Python converter must also write to it so
# the two processors share one dedup store — a report processed by the
# Python converter must be skipped by the Bash processor and vice versa.
_BASH_WATERMARK_FILENAME = "processed_receipts.txt"

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
# OI-1125: workers also write the colon INSIDE the closing markers
# (`**Dispatch-ID:** value`), not just after them (`**Dispatch-ID**: value`).
# Two alternatives, one per colon placement — Python's re has no branch-reset
# groups, so group 1/2 hold the outer-colon form and group 3/4 the
# inner-colon form; exactly one pair is non-None per match.
#
# The inner-colon alternative is anchored to line start (optional [-*]
# marker, mirroring _DISPATCH_PLAIN_RE) — unlike the pre-existing outer-colon
# alternative, which is a bare substring search with no anchor at all. This
# is not cosmetic: the unanchored form was measured against the real
# unified_reports/ corpus (4015 files) and produced a genuine false positive
# — a report's OWN changelog prose, `` `**Dispatch-ID:**` labels (closing
# `**` sits after the colon) ``, quoting the exact inner-colon shape as an
# example, matched as if it were a field declaration and its trailing prose
# became the "value", shadowing (via fields.setdefault order) the report's
# real bare Dispatch-ID line earlier in the same file. Anchoring closes this
# without touching the outer form's existing (unrelated, pre-#1445) lack of
# anchoring, which is out of scope here. Neither alternative matches bold
# text with no colon at all (e.g. `**note** text`).
_BOLD_KV_RE = re.compile(
    r"\*\*([^*]+)\*\*:\s*(.+)|^\s*(?:[-*]\s+)?\*\*([^*]+):\*\*\s*(.+)", re.MULTILINE
)
# OI-1120: the report contract tells workers to stamp the id "as a plain-text
# or bold field" — a markdown list item (`- Dispatch-ID: x`) is plainly within
# that instruction, so the optional leading marker must be tolerated. `+` is
# deliberately excluded from the marker class: it is also the unified-diff
# addition prefix, and reports routinely quote diffs in their body — e.g.
# `+        dispatch_id: str,` (a pasted function signature) matched with `+`
# included, which is not a real id stamp. `-`/`*` cover every genuine
# list-item occurrence observed in the corpus (grep across 4411 real reports:
# 5 genuine `- Dispatch-ID:` stamps, 0 false positives) with no such collision.
#
# OI-1120 round 2: `-` is ALSO the unified-diff REMOVAL prefix, and the first
# cut (leading `\s*` + `[-*]\s+`) treated it as symmetric with a genuine list
# item, so `-        dispatch_id: str,` (a pasted removed function signature)
# and `-    Dispatch-ID: old-value` (a pasted removed id stamp) both matched —
# the second one is dangerous: a report quoting a diff that REMOVES a
# Dispatch-ID line would adopt the OLD value as the receipt identity. Anchored
# at `^` with no leading `\s*`, and the marker is followed by exactly one
# literal space (`[-*] `, not `[-*]\s+`): a genuine list item is always
# "marker, single space, label" with nothing else preceding it on the line,
# while a diff-removal line has arbitrary reindentation whitespace after the
# `-` that no longer lines up with a single space. This also stops matching
# an indented line (the shape a fenced/indented code block produces),
# closing the "code block placeholder" gap the round-1 test recorded as a
# known pre-existing hole. Blast-radius re-measured across all 4015 reports
# in unified_reports/ (2026-08-10): 0 reports whose only Dispatch-ID form is
# indented — nothing regresses from dropping the leading `\s*`.
_DISPATCH_PLAIN_RE = re.compile(
    r"^(?:[-*] )?Dispatch-ID:\s*(\S+)\s*$", re.MULTILINE | re.IGNORECASE
)
_DISPATCH_ID_KEY_RE = re.compile(
    r"^(?:[-*] )?dispatch_id:\s*(\S+)\s*$", re.MULTILINE | re.IGNORECASE
)


# ---------------------------------------------------------------------------
# OI-1120: non-dispatch report classification — scope the register
# cross-check to dispatch reports only
# ---------------------------------------------------------------------------

# Producer-controlled: review_gate_manager.headless_reports_dir is set from
# VNX_HEADLESS_REPORTS_DIR, which defaults to <reports_dir>/headless
# (vnx_paths.py:543-544) and is the ONLY directory review_gate_manager writes
# HEADLESS-*.md reports into (review_gate_manager.py:61, :129-141). scan_and_
# convert() / main() already walk this directory as a dedicated scan target
# (this module, main()) alongside the top-level unified_reports/ — a report
# living there is, by construction, never a dispatch report.
_HEADLESS_REPORTS_DIRNAME = "headless"

# Producer-controlled: panel.py and worktree_release.py both write into the
# top-level unified_reports/ directory (same directory real dispatch reports
# land in), so directory placement cannot distinguish them. Each prefix below
# is the literal filename template the writer hardcodes for every report it
# produces — not a guessed shape:
#   scripts/panel.py:101           f"panel-{args.mode}-{uuid4().hex[:8]}.md"
#   scripts/lib/worktree_release.py:632  f"worktree-release-{ts}.md"
# Neither writer ever emits a Dispatch-ID/dispatch_id field or the dispatch
# report body-contract headings (## Summary/## Changes/## Verification/
# ## Open Items) — both are tool-output reports, not dispatch reports, and a
# filename prefix owned by the producer is the only signal available to key
# on. Measured against the live dead-letter directory (2026-08-10, 221
# files): 3 panel-*.md + 5 worktree-release-*.md, both prefixes fully
# disjoint from the 15 real dispatch report filenames present in that corpus.
_NON_DISPATCH_FILENAME_PREFIXES = (
    "panel-",
    "worktree-release-",
)


def _classify_non_dispatch_report(report_path: Path) -> Optional[str]:
    """Return a skip reason when *report_path* is a known non-dispatch report.

    Returns None when the report is a dispatch-report candidate and must go
    through the normal pipeline (including the register cross-check below).
    A non-None return means: this file was never a dispatch report to begin
    with, so it must never be judged against the dispatch register and must
    never be dead-lettered as ``unknown_dispatch``.
    """
    if report_path.parent.name == _HEADLESS_REPORTS_DIRNAME:
        return "headless_gate_report"
    name = report_path.name
    for prefix in _NON_DISPATCH_FILENAME_PREFIXES:
        if name.startswith(prefix):
            return "non_dispatch_tool_output"
    return None


# ---------------------------------------------------------------------------
# OI-1102: dispatch-register cross-check + dead-letter quarantine
# ---------------------------------------------------------------------------

def _is_known_dispatch(dispatch_id: str, state_dir: Optional[Path] = None) -> bool:
    """Return True when *dispatch_id* exists in the dispatch register.

    Parses each line of the NDJSON dispatch register
    (``dispatch_register.ndjson``) as a JSON object and compares the
    ``dispatch_id`` field.  This is robust against ANY JSON formatting
    difference — spacing, key order, extra fields — unlike a raw substring
    search that ties the match to a specific serialisation.

    Fail-open: returns True when the register file is missing or unreadable,
    so a transient I/O error never causes a healthy report to be dead-lettered.

    OI-1105: the dispatch register is sparse as of 2026-08-09.  The last
    entry is dated 2026-08-08T21:41Z; only ``backfill_pr_merged_receipts.py``
    still writes to it.  No dispatch lane adds entries; the four lane types
    (tmux-spawn, provider, envelope, and the subprocess adapter) are all
    absent.  Of the nine dispatches that went through the door on 9 August,
    zero appear in the register.

    The register cross-check is only reached when the report carries no
    content-side dispatch_id (``not content_id_valid``), which is already a
    contract violation.  A false negative here dead-letters the report into
    ``receipt_deadletter/`` — quarantine, not deletion — so the direction is
    safe.  The fail-open paths (missing/unreadable file) are structurally
    permissive; when the register file exists and is readable, the check is
    structural — it confirms or denies the dispatch_id against every parsed
    record — and a reader should not assume the old fail-open label still
    applies to the match path.
    """
    if state_dir is None:
        try:
            state_dir = _resolve_state_dir()
        except Exception:
            return True  # fail-open
    register_path = state_dir / "dispatch_register.ndjson"
    if not register_path.exists():
        return True  # fail-open: no register means no cross-check is possible
    try:
        with register_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("dispatch_id") == dispatch_id:
                    return True
        return False
    except OSError:
        return True  # fail-open


def _deadletter_report(
    report_path: Path,
    reason_code: str,
    state_dir: Path,
) -> None:
    """Quarantine *report_path* into the dead-letter directory.

    Uses the same directory layout as ``rp_deadletter.sh`` so both the Bash
    and Python lanes share one quarantine store.  The report file is moved to
    ``<state_dir>/receipt_deadletter/<filename>`` and an entry is appended to
    ``INDEX.txt``.  Best-effort: a failed move logs a warning and leaves the
    file in place (the next scan retries it).

    Also records the file's SHA-256 hash in the Bash watermark so the Bash
    receipt processor skips the quarantined file.
    """
    deadletter_dir = state_dir / "receipt_deadletter"
    try:
        deadletter_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "report_to_receipt_converter: cannot create dead-letter dir %s: %s",
            deadletter_dir, exc,
        )
        return

    report_name = report_path.name
    target = deadletter_dir / report_name
    if target.exists():
        file_hash = _compute_sha256(report_path)
        target = deadletter_dir / f"{report_path.stem}.{file_hash[:8]}.md"

    try:
        shutil.move(str(report_path), str(target))
    except OSError as exc:
        logger.warning(
            "report_to_receipt_converter: cannot dead-letter %s: %s",
            report_path.name, exc,
        )
        return

    # Append to INDEX.txt (same format as rp_deadletter.sh).
    try:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        file_hash = _compute_sha256(target)
        index_line = f"{ts} {file_hash} {reason_code} {report_name}\n"
        with (deadletter_dir / "INDEX.txt").open("a", encoding="utf-8") as fh:
            fh.write(index_line)
    except OSError as exc:
        logger.warning(
            "report_to_receipt_converter: cannot write dead-letter INDEX: %s", exc,
        )

    # Record hash in the Bash watermark so the Bash processor also skips it.
    bash_watermark = state_dir / _BASH_WATERMARK_FILENAME
    try:
        with bash_watermark.open("a", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.write(file_hash + "\n")
            fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError as exc:
        logger.warning(
            "report_to_receipt_converter: cannot update Bash watermark for "
            "dead-lettered %s: %s", report_name, exc,
        )

    logger.warning(
        "report_to_receipt_converter: DEAD-LETTERED %s (reason=%s) -> %s",
        report_name, reason_code, target,
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

    Values that were quoted by a YAML emitter (e.g. timestamp-like strings)
    have one layer of surrounding matching quotes stripped so the caller
    receives the bare value.
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
        # Strip one layer of surrounding matching quotes (yaml.safe_dump
        # quotes timestamp-like strings to avoid YAML-native date parsing).
        if len(val) >= 2:
            if (val[0] == '"' and val[-1] == '"') or (val[0] == "'" and val[-1] == "'"):
                val = val[1:-1]
        if key and val:
            fm[key] = val
    return fm


def _extract_body_fields(text: str) -> Dict[str, Any]:
    """Extract **Key**: value fields + plain-text Dispatch-ID fallback from body."""
    fields: Dict[str, Any] = {}
    for m in _BOLD_KV_RE.finditer(text[:3000]):
        raw_key, raw_val = (
            (m.group(1), m.group(2)) if m.group(1) is not None else (m.group(3), m.group(4))
        )
        key = raw_key.strip().lower().replace("-", "_").replace(" ", "_")
        # A bold key whose value is whitespace-only (e.g. truncated at the
        # text[:3000] scan boundary, or genuinely empty in the source report)
        # strips down to "" — splitlines() on "" is [], so index [0] would
        # raise IndexError. Guard it: no non-empty first line means no value.
        value_lines = raw_val.strip().splitlines()
        val = value_lines[0].strip() if value_lines else ""
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


def _resolve_report_role(
    dispatch_id: str, merged: Dict[str, Any], state_dir: Optional[Path]
) -> str:
    """Resolve the dispatch's real role for the converter receipt.

    W7 fix: the shared resolver prefers the report's OWN stamped ``role``
    (frontmatter/body — the author's resolved identity) over the
    dispatch_metadata join, matching every other emit path. The metadata join
    remains the fallback for reports whose author never stamped a role.

    FAIL-OPEN contract (mirrors PR-1/PR-2): any resolution error, missing DB,
    missing row, null/empty role, or the fake ``backend-developer`` default
    degrades to ``identity_unresolved`` — never ``unknown``, never the fake
    literal, never raises.
    """
    role: Optional[str] = None
    try:
        from dispatch_identity import resolve_effective_role  # noqa: PLC0415
        project_id = merged.get("project_id") or None
        if not project_id:
            from dispatch_cli import _resolve_project_id  # noqa: PLC0415
            project_id = _resolve_project_id()
        role = resolve_effective_role(
            merged.get("role"), dispatch_id, project_id, state_dir=state_dir,
        )
    except Exception:  # noqa: BLE001 — identity join is fail-open
        logger.debug(
            "report_to_receipt_converter: role resolution failed open dispatch=%s",
            dispatch_id,
            exc_info=True,
        )
        role = None
    return role or _IDENTITY_UNRESOLVED


# ---------------------------------------------------------------------------
# Provider string normalization (OI-1111)
# ---------------------------------------------------------------------------

# Canonical provider names as used by dispatch lanes. Every variant found in
# the ledger (deepseek-harness, deepseek, litellm:deepseek, kimi-k3, kimi,
# kimi-code/k3, glm-harness) normalises to one of these.
_CANONICAL_PROVIDER_LANE = {
    "deepseek-harness",
    "deepseek",
    "kimi",
    "glm-harness",
    "claude",
    "codex",
    "gemini",
}


def _normalise_provider(provider_raw: str) -> str:
    """Normalise a provider string to its canonical lane value.

    Variants observed in the ledger as of 2026-08-09:
      deepseek-harness (294), deepseek (27), ``deepseek (harness, key-auth)``
      (one self-report), litellm:deepseek (43), kimi-k3, kimi-code/k3.

    Without normalisation every cost aggregation over provider counts these as
    separate providers. The canonical lane values are the short forms the
    dispatch door uses.
    """
    if not provider_raw or not provider_raw.strip():
        return "unknown"
    p = provider_raw.strip()
    pl = p.lower()

    # Already a canonical lane value — pass through.
    if pl in _CANONICAL_PROVIDER_LANE:
        return pl

    # DeepSeek variants → deepseek-harness
    if "deepseek" in pl:
        return "deepseek-harness"

    # Kimi variants (kimi-k3, kimi-code/k3, kimi) → kimi
    if "kimi" in pl:
        return "kimi"

    # GLM variants → glm-harness.  parse_route_model_id maps glm-* model IDs
    # to litellm:zai (the litellm provider flag for Zhipu AI); normalise that
    # flag back to the canonical lane name.
    if "glm" in pl or pl == "litellm:zai":
        return "glm-harness"

    # Unknown — return as-is rather than invent a value.
    return p


def _resolve_report_provider_model(
    dispatch_id: str, merged: Dict[str, Any], state_dir: Optional[Path]
) -> Tuple[str, str]:
    """Resolve provider and model from the lane, falling back to the body.

    OI-1111: on harness lanes (GLM, DeepSeek) the worker introspects as
    sonnet/claude because the CLI around it thinks that is what it is.  The
    body therefore carries a false identity the worker cannot know is wrong.
    For model and provider the LANE wins — opposite precedence from
    ``_resolve_report_role``, where the body wins because the author's own
    role stamp is the authoritative source.

    Resolution order:
      1. Route decision JSON (``state_dir/route_decisions/<dispatch_id>.json``)
         — the lane's own record of which model was selected.
      2. Report body/frontmatter fields — fallback when no route decision
         exists (e.g. plan-gate seats that do not go through the smart router).
    """
    provider: Optional[str] = None
    model: Optional[str] = None

    # Lane identity first: the route decision knows which model ran.
    if state_dir and dispatch_id:
        route_dec = _load_route_decision(dispatch_id, state_dir)
        if route_dec and route_dec.get("selected_model"):
            model_id = route_dec["selected_model"]
            try:
                from smart_router import parse_route_model_id  # noqa: PLC0415
                lane_provider, lane_model = parse_route_model_id(model_id)
                provider = _normalise_provider(lane_provider)
                model = lane_model
            except Exception:
                logger.debug(
                    "report_to_receipt_converter: provider/model lane resolution "
                    "failed for dispatch=%s model_id=%s",
                    dispatch_id, model_id,
                    exc_info=True,
                )

    # Body fallback: only used when lane resolution produced nothing.
    if not provider:
        provider = _normalise_provider(merged.get("provider", "unknown"))
    if not model:
        model = (merged.get("model") or "").strip()

    return provider, model


# Terminal success statuses that trigger fail-closed validation (OI-1035 et al.).
# Any report claiming one of these statuses must pass all three fail-closed
# checks before a success receipt is written.  Reports with other statuses
# (e.g. "unknown", absent) keep the pre-existing contract-invalid semantics.
_TERMINAL_SUCCESS_STATUSES = frozenset({"success", "done", "complete", "completed"})


def _check_branch_on_origin(dispatch_id: str) -> bool:
    """Return True when ``dispatch/<dispatch_id>`` exists on origin.

    Uses ``git ls-remote --heads`` so the check is a real measurement against
    the remote, not a local-ref assumption.  A branch that was committed
    locally but never pushed returns False — this is the OI-1011 signal.
    """
    branch = f"dispatch/{dispatch_id}"
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", "origin", branch],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except Exception:
        return False


def _run_fail_closed_checks(
    text: str, dispatch_id: str, body_result: Any,
) -> List[str]:
    """Run the three fail-closed validation checks for terminal-success receipts.

    Returns a list of violation strings.  An empty list means all checks passed
    and the receipt may carry its claimed success status.

    Checks (OI-1035, OI-1011, OI-1002, OI-659, OI-1017):
      1. Frontmatter validates against ``schemas/unified_report_v1.json``.
      2. The four mandatory headings are present (body contract).
      3. The dispatch branch exists on origin (``git ls-remote``).
    """
    violations: List[str] = []

    # Check 1: v1 frontmatter validation
    try:
        from unified_report_schema import validate_frontmatter, SchemaViolation
        validate_frontmatter(text)
    except SchemaViolation as exc:
        violations.append(f"frontmatter_v1: {exc}")
    except ImportError:
        pass  # schema module not available — skip check rather than crash

    # Check 2: body contract headings (re-verify via the existing validator)
    if not body_result.valid:
        violations.append(
            f"body_contract: missing required sections — "
            f"{', '.join(body_result.missing)}"
        )
        if body_result.placeholder:
            violations.append("body_contract: placeholder_summary")

    # Check 3: branch exists on origin (real git ls-remote, never mocked)
    if not _check_branch_on_origin(dispatch_id):
        violations.append(
            f"branch_not_on_origin: dispatch/{dispatch_id} not found on origin"
        )

    return violations


def build_receipt_from_report(
    report_path: Path, text: str, *, state_dir: Optional[Path] = None
) -> Optional[Dict[str, Any]]:
    """Build a minimal governed receipt dict from report content.

    Returns:
    - event_type="task_complete" when the report passes all validation gates.
    - event_type="task_failed" with status="failure" when the report claims
      a terminal success status but fails one or more fail-closed checks
      (v1 frontmatter, body contract, branch on origin).
    - event_type="report_contract_invalid" when a dispatch_id is resolvable
      but the report fails the body contract or lacks a content-side
      dispatch_id AND does NOT claim a terminal success status.
      Filename-only dispatch_id is a contract violation.
    - None when no dispatch_id can be determined at all (warning logged).

    Never raises.
    """
    sys.path.insert(0, str(_LIB_DIR))
    from report_body_contract import validate_body
    from dispatch_spec import _ID_RE  # OI-1122: single shape definition, shared with staging
    from datetime import datetime, timezone

    fm = parse_frontmatter(text)
    body = _extract_body_fields(text)
    # Frontmatter takes priority over body fields
    merged: Dict[str, Any] = {**body, **fm}

    # Check if dispatch_id comes from report content (frontmatter or body fields).
    # A filename-derived dispatch_id is NOT authoritative and is treated as a
    # contract violation — it must not produce a clean task_complete receipt.
    content_dispatch_id: Optional[str] = merged.get("dispatch_id") or None
    # OI-1122: a growing deny-list ("unknown"/"none"/"null") never covers a
    # template placeholder echoed back verbatim (`Dispatch-ID: <dispatch_id>`)
    # — the literal string isn't in the list, so it was adopted as the real
    # receipt identity, starving the actual dispatch of a closing receipt.
    # Validate the shape instead: dispatch_spec._ID_RE is the SAME rule
    # staging already enforces for a legal id, so a value that fails it can
    # never have been a real dispatch_id to begin with. A shape failure is
    # treated identically to a missing id — it falls through to the filename
    # fallback and the existing OI-1102 register cross-check below, not a
    # new rejection path of its own.
    content_id_valid = bool(
        content_dispatch_id
        and content_dispatch_id.lower() not in ("unknown", "none", "null")
        and _ID_RE.match(content_dispatch_id)
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

    # OI-1102: when the dispatch_id comes from the filename alone (not from
    # report content), cross-check it against the dispatch register.  A
    # filename with a prefix (e.g. "dispatch-<id>.md") produces a phantom
    # dispatch identity that never existed — the register won't know it,
    # and it must not enter the grootboek.  Dead-letter the report so no
    # lane retries it.
    if not content_id_valid and state_dir is not None:
        if not _is_known_dispatch(dispatch_id, state_dir):
            logger.warning(
                "report_to_receipt_converter: dispatch_id=%r (from filename %s) "
                "not found in dispatch register — dead-lettering",
                dispatch_id, report_path.name,
            )
            _deadletter_report(report_path, "unknown_dispatch", state_dir)
            return None

    timestamp = (
        merged.get("timestamp")
        or merged.get("recorded_at")
        or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    # Use "unknown" for task_id so the idempotency key aligns with what
    # report_parser.py produces (it defaults task_id to "unknown").  This lets
    # append_receipt_payload()'s rolling cache deduplicate same-cycle runs.

    # OI-989/OI-993: resolve the report path via the shared resolver instead
    # of blindly using the scanner's current path.  When the worker wrote
    # multiple files (e.g. <id>.md AND dispatch-<id>.md), the resolver picks
    # the canonical form and flags ambiguity — the receipt carries the
    # canonical path and logs the ambiguity for T0.
    from report_path import resolve_report_path
    resolved = resolve_report_path(dispatch_id)
    canonical_report_path = str(resolved.path) if resolved is not None else str(report_path)
    ambiguous_report = resolved.ambiguous if resolved is not None else False

    # OI-1111: provider and model come from the LANE (route decision), not
    # from the body. On harness lanes (GLM, DeepSeek) the worker introspects
    # as sonnet/claude; the body is a false identity the worker cannot know
    # is wrong. The lane's route decision holds the authoritative identity.
    # Provider strings are normalised to canonical lane values so every cost
    # aggregation counts one provider, not four variants of the same lane.
    _resolved_provider, _resolved_model = _resolve_report_provider_model(
        dispatch_id, merged, state_dir,
    )

    base: Dict[str, Any] = {
        "dispatch_id": dispatch_id,
        # Receipt-quality PR-3: converter receipts are dispatch-lane outcomes
        # (closed-set receipt_kind; the emit-time lint raises on untagged).
        "receipt_kind": "dispatch",
        # Receipt-quality PR-4: real role from dispatch_metadata via the
        # resolver; fail-open to identity_unresolved (never unknown / fake).
        "role": _resolve_report_role(dispatch_id, merged, state_dir),
        "task_id": merged.get("task_id", "unknown"),
        "terminal": merged.get("terminal", "unknown"),
        "provider": _resolved_provider,
        "model": _resolved_model,
        "timestamp": timestamp,
        "report_path": canonical_report_path,
    }

    # OI-1035 fail-closed gate: when the report claims a terminal success
    # status, all three validation checks must pass before a success receipt
    # is written.  Failure on any check produces an explicit failure receipt
    # with a queryable reason — never a silent skip and never the pre-existing
    # "contract_invalid" bucket (which received 96 hits in 7 days and is
    # invisible to any alarm).
    status_raw = (merged.get("status") or "").strip().lower()
    is_terminal_success = status_raw in _TERMINAL_SUCCESS_STATUSES

    if is_terminal_success:
        fail_closed_violations = _run_fail_closed_checks(
            text, dispatch_id, body_result,
        )
        if fail_closed_violations:
            logger.warning(
                "report_to_receipt_converter: fail-closed REJECTION dispatch=%s "
                "status=%s violations=%s",
                dispatch_id, status_raw, fail_closed_violations,
            )
            receipt_out: Dict[str, Any] = {
                **base,
                "event_type": "task_failed",
                "status": "failure",
                "fail_closed_violations": fail_closed_violations,
            }
            if ambiguous_report:
                receipt_out["ambiguous_report_path"] = True
                receipt_out["report_path_candidates"] = [
                    {"path": str(c), "size": resolved.candidate_sizes[str(c)]}
                    for c in resolved.candidates_found
                ] if resolved is not None else []
            return receipt_out

    if contract_violations:
        logger.warning(
            "report_to_receipt_converter: contract violations in %s: %s"
            " — emitting as report_contract_invalid",
            report_path.name, contract_violations,
        )
        receipt_out: Dict[str, Any] = {
            **base,
            "event_type": "report_contract_invalid",
            "status": "contract_invalid",
            "contract_violations": contract_violations,
        }
        if ambiguous_report:
            receipt_out["ambiguous_report_path"] = True
            receipt_out["report_path_candidates"] = [
                {"path": str(c), "size": resolved.candidate_sizes[str(c)]}
                for c in resolved.candidates_found
            ] if resolved is not None else []
        return receipt_out

    receipt: Dict[str, Any] = {
        **base,
        "event_type": "task_complete",
        "status": status_raw,
    }
    if ambiguous_report:
        receipt["ambiguous_report_path"] = True
        receipt["report_path_candidates"] = [
            {"path": str(c), "size": resolved.candidate_sizes[str(c)]}
            for c in resolved.candidates_found
        ] if resolved is not None else []
    if state_dir and dispatch_id:
        route_dec = _load_route_decision(dispatch_id, state_dir)
        if route_dec:
            receipt["route_decision"] = route_dec
    return receipt


# ---------------------------------------------------------------------------
# Single-report converter
# ---------------------------------------------------------------------------

# Outcome tags returned by _convert_one_detailed(), consumed by
# scan_and_convert() for per-scan counting (OI-998):
#   "appended"  — new receipt written.
#   "duplicate" — idempotent re-send, no new receipt.
#   "rejected"  — fail-closed refusal by append_receipt_payload's own
#                 validation (e.g. missing Model — see AppendReceiptError
#                 code "missing_model" in append_receipt_internals/validation.py).
#                 A WARNING is logged here with dispatch_id + reason so the
#                 refusal is loud, not silent.
#   "malformed" — file unreadable, or no dispatch_id resolvable at all.
#   "error"     — anything else: a crash while parsing/building the receipt,
#                 or an append failure other than the fail-closed rejection.
#   "skipped_non_dispatch" — OI-1120: report classified as a known non-dispatch
#                 producer (HEADLESS gate report, panel output, worktree-release
#                 output) — never a receipt candidate, never dead-lettered.

def _convert_one_detailed(
    report_path: Path,
    *,
    receipts_file: Optional[str] = None,
    cache_window_seconds: int = 300,
) -> Tuple[Optional[Any], str]:  # (Optional[AppendResult], outcome_tag)
    """Convert one report file to a governed receipt; classify the outcome.

    Never raises — every failure mode (unreadable file, unparseable body,
    fail-closed model rejection, append failure) is caught here and turned
    into a (None, outcome_tag) result so a single poisoned report can never
    propagate an exception into a caller's batch loop.
    """
    # OI-1120: classify BEFORE any receipt-building work. A known non-dispatch
    # report (HEADLESS gate output, panel output, worktree-release output) is
    # never a receipt candidate — it must not reach the register cross-check
    # in build_receipt_from_report() and must not be dead-lettered.
    skip_reason = _classify_non_dispatch_report(report_path)
    if skip_reason is not None:
        logger.info(
            "report_to_receipt_converter: skipping non-dispatch report %s (reason=%s)",
            report_path.name, skip_reason,
        )
        return None, "skipped_non_dispatch"

    # Import through append_receipt.py (scripts/ root) so the facade is
    # registered before append_receipt_payload() is called.
    sys.path.insert(0, str(_SCRIPTS_DIR))
    sys.path.insert(0, str(_LIB_DIR))
    try:
        import append_receipt  # registers facade as side effect
        append_receipt_payload = append_receipt.append_receipt_payload
        AppendReceiptError = append_receipt.AppendReceiptError
        AppendResult = append_receipt.AppendResult
    except Exception as exc:
        logger.warning("report_to_receipt_converter: cannot import append_receipt: %s", exc)
        return None, "error"

    try:
        text = report_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("report_to_receipt_converter: cannot read %s: %s", report_path.name, exc)
        return None, "malformed"

    state_dir_for_route = Path(receipts_file).parent if receipts_file else None
    try:
        receipt = build_receipt_from_report(report_path, text, state_dir=state_dir_for_route)
    except Exception as exc:
        # build_receipt_from_report() is documented "never raises" but a
        # single poisoned report must never be trusted to honor that on its
        # own (OI-997): one crashed report must not end the batch.
        logger.error(
            "report_to_receipt_converter: build_receipt_from_report crashed for %s: %s: %s",
            report_path.name, type(exc).__name__, exc,
        )
        return None, "error"
    if receipt is None:
        return None, "malformed"

    # OI-1017/OI-1048 guard: when the envelope hot-path already wrote a
    # receipt for this dispatch (binding body-contract enforcement landed
    # before the converter's periodic sweep), skip the append to avoid
    # redundant work.  The idempotency cache in append_receipt_payload would
    # catch the duplicate anyway, but this short-circuit saves reading/
    # parsing/hashing the mapper file and keeps the ScanStats.duplicate_count
    # honest (the converter didn't build this receipt — the hot-path did).
    #
    # The guard fires for ANY existing receipt (not only contract_invalid):
    # the hot-path's emit_dispatch_receipt writes richer data (token_usage,
    # cost_usd, session_id) than the converter can reconstruct from the
    # report, so the hot-path's receipt should always win.  Checking only
    # for contract_invalid would miss cases where the converter's
    # fail-closed checks produce a different status (e.g. failure when the
    # hot-path wrote contract_invalid).
    _guard_did = receipt.get("dispatch_id")
    if (
        _guard_did
        and receipts_file
        and Path(receipts_file).exists()
    ):
        try:
            _text = Path(receipts_file).read_text(encoding="utf-8", errors="replace")
            # Parse NDJSON lines: compare the parsed dispatch_id field
            # instead of substring-matching against a specific JSON
            # serialisation (which differs between compact and
            # pretty-printed formats — OI-1110 needle-fix).
            for _line in _text.splitlines():
                _line = _line.strip()
                if not _line:
                    continue
                try:
                    _rec = json.loads(_line)
                except json.JSONDecodeError:
                    continue
                if _rec.get("dispatch_id") == _guard_did:
                    logger.info(
                        "report_to_receipt_converter: skipping dispatch=%s — "
                        "receipt already exists (hot-path wrote first)",
                        _guard_did,
                    )
                    return AppendResult(
                        status="duplicate",
                        receipts_file=Path(receipts_file),
                        idempotency_key=_guard_did,
                    ), "duplicate"
        except OSError:
            pass  # can't read NDJSON — fall through to normal append (idempotency will catch it)

    try:
        result = append_receipt_payload(
            receipt,
            receipts_file=receipts_file,
            cache_window_seconds=cache_window_seconds,
            skip_enrichment=True,  # converter receipts skip quality advisory
        )
        return result, result.status
    except AppendReceiptError as exc:
        if exc.code == "missing_model":
            logger.warning(
                "report_to_receipt_converter: REJECTED (fail-closed) dispatch=%s file=%s reason=%s",
                receipt.get("dispatch_id"), report_path.name, exc.message,
            )
            return None, "rejected"
        logger.warning(
            "report_to_receipt_converter: append failed for %s: %s",
            report_path.name, exc,
        )
        return None, "error"
    except Exception as exc:
        logger.warning(
            "report_to_receipt_converter: append failed for %s: %s",
            report_path.name, exc,
        )
        return None, "error"


def convert_report_to_receipt(
    report_path: Path,
    *,
    receipts_file: Optional[str] = None,
    cache_window_seconds: int = 300,
) -> Optional[Any]:  # Optional[AppendResult]
    """Convert one report file to a governed receipt.

    Returns AppendResult (status="appended" | "duplicate") on success,
    or None on unreadable / malformed / rejected / errored input (warning
    or error logged, no crash). Thin wrapper over _convert_one_detailed()
    that preserves this exact return contract for existing callers
    (scripts/hooks/tmux_signal_stop_receipt.sh and direct-call tests) —
    scan_and_convert() calls _convert_one_detailed() directly for its richer
    per-outcome counts.
    """
    result, _outcome = _convert_one_detailed(
        report_path,
        receipts_file=receipts_file,
        cache_window_seconds=cache_window_seconds,
    )
    return result


# ---------------------------------------------------------------------------
# Directory scanner
# ---------------------------------------------------------------------------

_HEALTH_COMPONENT = "report_to_receipt_converter"
_HEALTH_EXPECTED_INTERVAL_SECONDS = 3600


@dataclass(frozen=True)
class ScanStats:
    """Per-scan outcome counts, returned by scan_and_convert() (OI-998).

    A scan that only tracked ``new_count`` could not distinguish "nothing to
    do" from "reports piled up and every single one was rejected" — both
    read as 0. The separate counters make that distinction visible to any
    caller, and scan_and_convert() also surfaces them via a HealthBeacon
    (see _write_scan_heartbeat) so "zero receipts" stops reading as healthy
    when reports were actually scanned and refused.
    """

    new_count: int = 0
    duplicate_count: int = 0
    rejected_count: int = 0
    malformed_count: int = 0
    error_count: int = 0
    # OI-1120: reports classified as known non-dispatch producer output
    # (HEADLESS gate, panel, worktree-release) — deliberately excluded from
    # attempted_count below. A scan cycle can legitimately see ONLY
    # non-dispatch reports (the headless/ directory is scanned every cycle),
    # and that must read as healthy, not as "attempted with zero success".
    skipped_non_dispatch_count: int = 0

    @property
    def attempted_count(self) -> int:
        return (
            self.new_count
            + self.duplicate_count
            + self.rejected_count
            + self.malformed_count
            + self.error_count
        )


def _write_scan_heartbeat(state_dir: Path, stats: ScanStats) -> None:
    """Surface this scan's counts via the existing HealthBeacon channel.

    Writes <state_dir>/health/report_to_receipt_converter.json — the same
    mechanism producer_freshness_monitor.py uses for its own
    health/producer_freshness_monitor.json heartbeat (scripts/lib/health_beacon.py),
    auto-discovered by health_beacon.all_beacons() / beacon_summary() (consumed
    by scripts/health_check.py, dashboard/api_health.py, vnx_cli subsystems).
    Reusing this channel means no new monitoring surface is invented (OI-998).

    status="fail" specifically for the case this dispatch closes: reports
    were scanned this cycle (attempted_count > 0) but NONE resulted in a
    receipt landing (new_count == duplicate_count == 0) — "zero receipts"
    while work was actually attempted. A quiet scan with nothing new to do
    (attempted_count == 0) stays "ok" — that is the healthy, common case.
    Best-effort: heartbeat write failures never raise into the caller.
    """
    try:
        from health_beacon import HealthBeacon  # noqa: PLC0415
    except Exception as exc:
        logger.warning("report_to_receipt_converter: cannot import health_beacon: %s", exc)
        return

    status = "ok"
    if stats.attempted_count > 0 and stats.new_count == 0 and stats.duplicate_count == 0:
        status = "fail"

    beacon = HealthBeacon(
        state_dir, _HEALTH_COMPONENT, expected_interval_seconds=_HEALTH_EXPECTED_INTERVAL_SECONDS,
    )
    beacon.heartbeat(  # best-effort: swallows OSError internally
        status=status,
        details={
            "new_count": stats.new_count,
            "duplicate_count": stats.duplicate_count,
            "rejected_count": stats.rejected_count,
            "malformed_count": stats.malformed_count,
            "error_count": stats.error_count,
            "skipped_non_dispatch_count": stats.skipped_non_dispatch_count,
        },
    )


def scan_and_convert(
    reports_dirs: List[Path],
    state_dir: Optional[Path] = None,
    *,
    cache_window_seconds: int = 300,
) -> ScanStats:
    """Scan report directories and convert unprocessed reports.

    Deduplication uses both the converter's own watermark file
    (report_to_receipt_processed.txt) AND the Bash receipt processor's
    watermark (processed_receipts.txt) — OI-1102 cross-processor dedup
    so a report processed by either lane is skipped by the other.

    Returns a ScanStats with per-outcome counts (new/duplicate/rejected/
    malformed/error/skipped_non_dispatch) — see ScanStats and
    _convert_one_detailed()'s outcome tags. Newly-appended, duplicate, and
    skipped_non_dispatch reports are marked processed (the classification
    that produced skipped_non_dispatch is permanent — a panel-*.md report
    never becomes a dispatch report on a later scan); rejected, malformed,
    and errored reports are NOT marked processed, so they are retried on the
    next scan once their cause is fixed.

    A single poisoned report can never abort the scan (OI-997): each
    report's conversion is wrapped in try/except here, in addition to
    _convert_one_detailed()'s own internal guards, so an exception from
    ANY stage of parsing/building/appending is caught, logged once, and the
    scan continues with the next report.
    """
    if state_dir is None:
        try:
            state_dir = _resolve_state_dir()
        except Exception as exc:
            logger.error("report_to_receipt_converter: cannot resolve state_dir: %s", exc)
            return ScanStats()

    state_dir.mkdir(parents=True, exist_ok=True)
    receipts_file = str(state_dir / "t0_receipts.ndjson")
    watermark_path = state_dir / _WATERMARK_FILENAME
    # OI-1102: also load the Bash processor's watermark so a report processed
    # by the Bash lane is skipped here, and vice versa (cross-processor dedup).
    bash_watermark_path = state_dir / _BASH_WATERMARK_FILENAME

    watermark = _load_watermark(watermark_path)
    bash_watermark = _load_watermark(bash_watermark_path)

    new_count = 0
    duplicate_count = 0
    rejected_count = 0
    malformed_count = 0
    error_count = 0
    skipped_non_dispatch_count = 0

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

            # Skip if already in either watermark (cross-processor dedup: OI-1102).
            if file_hash in watermark or file_hash in bash_watermark:
                continue

            try:
                result, outcome = _convert_one_detailed(
                    report_path,
                    receipts_file=receipts_file,
                    cache_window_seconds=cache_window_seconds,
                )
            except Exception as exc:
                # Belt-and-suspenders: _convert_one_detailed() already catches
                # its own failure modes, but ANY report must be able to crash
                # here without ending the scan for every report after it —
                # that is the exact 2026-08-03 18:36 incident this closes.
                # One ERROR line per file per scan; NOT marked processed so
                # it is retried once the cause is fixed.
                logger.error(
                    "report_to_receipt_converter: unhandled exception converting %s: %s: %s",
                    report_path.name, type(exc).__name__, exc,
                )
                error_count += 1
                continue

            if outcome in ("appended", "duplicate", "skipped_non_dispatch"):
                # Mark as processed in BOTH watermarks — no point re-scanning
                # a report that's already in the system (OI-1102 cross-processor
                # dedup: the Bash processor must also skip it) or that will
                # never become a dispatch report (OI-1120 non-dispatch skip).
                _mark_processed(file_hash, watermark_path)
                watermark.add(file_hash)
                _mark_processed(file_hash, bash_watermark_path)
                bash_watermark.add(file_hash)
                if outcome == "appended":
                    new_count += 1
                    logger.info(
                        "report_to_receipt_converter: receipt emitted dispatch=%s file=%s",
                        result.idempotency_key[:20],
                        report_path.name,
                    )
                elif outcome == "duplicate":
                    duplicate_count += 1
                else:  # "skipped_non_dispatch"
                    skipped_non_dispatch_count += 1
            elif outcome == "rejected":
                rejected_count += 1
            elif outcome == "malformed":
                malformed_count += 1
            else:  # "error"
                error_count += 1
            # rejected / malformed / error reports are NOT marked processed:
            # retried on the next scan once the cause is fixed.

    stats = ScanStats(
        new_count=new_count,
        duplicate_count=duplicate_count,
        rejected_count=rejected_count,
        malformed_count=malformed_count,
        error_count=error_count,
        skipped_non_dispatch_count=skipped_non_dispatch_count,
    )
    _write_scan_heartbeat(state_dir, stats)
    return stats


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

    stats = scan_and_convert(dirs, state_dir, cache_window_seconds=300)
    if stats.new_count:
        logger.info("report_to_receipt_converter: %d new receipt(s) emitted", stats.new_count)
    if stats.skipped_non_dispatch_count:
        logger.info(
            "report_to_receipt_converter: %d non-dispatch report(s) skipped this scan",
            stats.skipped_non_dispatch_count,
        )
    if stats.rejected_count or stats.malformed_count or stats.error_count:
        logger.warning(
            "report_to_receipt_converter: %d rejected, %d malformed, %d error(s) this scan",
            stats.rejected_count, stats.malformed_count, stats.error_count,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
