#!/usr/bin/env python3
"""Canonical receipt append helper with lock, validation, and idempotency guard."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

try:
    from vnx_paths import ensure_env
    from project_root import resolve_state_dir
    from quality_advisory import generate_quality_advisory, get_changed_files
    from terminal_snapshot import collect_terminal_snapshot
    from cqs_calculator import calculate_cqs
    from receipt_provenance import enrich_receipt_provenance, validate_receipt_provenance
    from ghost_receipt_filter import should_route_to_gate_stream, gate_events_file
except Exception as exc:  # pragma: no cover - hard fail on bootstrap issue
    raise SystemExit(f"Failed to load vnx_paths: {exc}")

# Lazy import for open_items_manager (only needed for quality registration)
_open_items_manager = None

def _get_open_items_manager():
    global _open_items_manager
    if _open_items_manager is None:
        sys.path.insert(0, str(SCRIPT_DIR))
        import open_items_manager as _oim
        _open_items_manager = _oim
    return _open_items_manager

EXIT_OK = 0
EXIT_INVALID_INPUT = 10
EXIT_VALIDATION_ERROR = 11
EXIT_IO_ERROR = 12
EXIT_LOCK_ERROR = 13
EXIT_UNEXPECTED_ERROR = 20

DISPATCH_REQUIRED_EVENTS = {
    "task_started",
    "task_complete",
    "task_failed",
    "task_timeout",
    "task_blocked",
    "dispatch_sent",
    "dispatch_ack",
    "ack",
}

STATE_MUTATION_EVENTS = {"state_mutation"}

_REPO_ROOT = SCRIPT_DIR.parent
_REBUILD_THROTTLE_SECONDS = 30

_warned_review_gate_no_dispatch_id = False


IDEMPOTENCY_FIELDS = (
    "dispatch_id",
    "task_id",
    "pr_number",  # prevents review_gate_request fan-out collision per gate
    "gate",       # multiple gates per dispatch_id must not collide
    "terminal",
    "event_type",
    "event",
    "report_path",
    "source",
    "file",
    "trigger",
    "section",
)


def is_headless_t0() -> bool:
    """Return True when T0 is configured to run via subprocess adapter.

    Set VNX_ADAPTER_T0=subprocess to enable headless T0 mode.  When True,
    the receipt processor annotates the T0 terminal snapshot entry with
    adapter/headless metadata and skips tmux-dependent probes for T0.
    """
    return os.environ.get("VNX_ADAPTER_T0", "tmux").lower() == "subprocess"


@dataclass(frozen=True)
class AppendResult:
    status: str
    receipts_file: Path
    idempotency_key: str


class AppendReceiptError(RuntimeError):
    def __init__(self, code: str, exit_code: int, message: str):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code
        self.message = message


def _emit(level: str, code: str, **fields: Any) -> None:
    payload = {
        "level": level,
        "code": code,
        "timestamp": int(time.time()),
    }
    payload.update(fields)
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True), file=sys.stderr)


def _resolve_receipts_file(receipts_file: Optional[str] = None) -> Path:
    if receipts_file:
        return Path(receipts_file).expanduser()

    paths = ensure_env()
    return Path(paths["VNX_STATE_DIR"]) / "t0_receipts.ndjson"


def _lock_file_for(receipts_path: Path) -> Path:
    return receipts_path.parent / "append_receipt.lock"


def _cache_file_for(receipts_path: Path) -> Path:
    return receipts_path.parent / "receipt_idempotency_recent.ndjson"


def _requires_dispatch_id(receipt: Dict[str, Any], event_name: str) -> bool:
    if event_name in DISPATCH_REQUIRED_EVENTS:
        return True
    if event_name.startswith("task_"):
        return True
    if receipt.get("task_id"):
        return True
    return False


def _warn_if_review_gate_missing_dispatch_id(event_name: str, receipt: Dict[str, Any]) -> None:
    global _warned_review_gate_no_dispatch_id
    if _warned_review_gate_no_dispatch_id:
        return
    if event_name == "review_gate_request":
        if not str(receipt.get("dispatch_id", "")).strip():
            _warned_review_gate_no_dispatch_id = True
            _emit(
                "WARN",
                "review_gate_request_missing_dispatch_id",
                message="review_gate_request receipt has no dispatch_id — receipt-to-gate audit linkage severed",
            )


def _validate_receipt(receipt: Dict[str, Any]) -> str:
    timestamp = str(receipt.get("timestamp", "")).strip()
    if not timestamp:
        raise AppendReceiptError(
            "missing_required_key",
            EXIT_VALIDATION_ERROR,
            "Missing required key: timestamp",
        )

    event_name = str(receipt.get("event_type") or receipt.get("event") or "").strip()
    if not event_name:
        raise AppendReceiptError(
            "missing_required_key",
            EXIT_VALIDATION_ERROR,
            "Missing required key: event_type or event",
        )

    if _requires_dispatch_id(receipt, event_name):
        dispatch_id = str(receipt.get("dispatch_id", "")).strip()
        if not dispatch_id:
            raise AppendReceiptError(
                "missing_required_key",
                EXIT_VALIDATION_ERROR,
                "Missing required key: dispatch_id",
            )

    _warn_if_review_gate_missing_dispatch_id(event_name, receipt)

    return event_name


def _compute_idempotency_key(receipt: Dict[str, Any], event_name: str) -> str:
    digest_fields: Dict[str, Any] = {}

    for field in IDEMPOTENCY_FIELDS:
        value = receipt.get(field)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        digest_fields[field] = value

    if "event_type" not in digest_fields and "event" not in digest_fields:
        digest_fields["event_type"] = event_name

    # For receipts without stable identity fields, include timestamp to avoid
    # collapsing distinct events in the short dedupe window.
    if (
        "dispatch_id" not in digest_fields
        and "task_id" not in digest_fields
        and "report_path" not in digest_fields
    ):
        digest_fields["timestamp"] = receipt.get("timestamp")

    if not digest_fields:
        digest_fields = receipt

    payload = json.dumps(digest_fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_cache(cache_file: Path, min_epoch: float) -> List[Dict[str, Any]]:
    if not cache_file.exists():
        return []

    entries: List[Dict[str, Any]] = []
    try:
        with cache_file.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = float(parsed.get("ts", 0))
                key = str(parsed.get("key", "")).strip()
                if key and ts >= min_epoch:
                    entries.append({"ts": ts, "key": key})
    except OSError as exc:
        raise AppendReceiptError("cache_read_failed", EXIT_IO_ERROR, f"Failed to read idempotency cache: {exc}") from exc

    return entries


def _write_cache(cache_file: Path, entries: List[Dict[str, Any]], max_entries: int = 2048) -> None:
    entries = entries[-max_entries:]
    tmp_file = cache_file.with_name(f"{cache_file.name}.{os.getpid()}.tmp")

    try:
        with tmp_file.open("w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry, sort_keys=True, separators=(",", ":")))
                handle.write("\n")
        os.replace(tmp_file, cache_file)
    except OSError as exc:
        raise AppendReceiptError("cache_write_failed", EXIT_IO_ERROR, f"Failed to write idempotency cache: {exc}") from exc
    finally:
        try:
            if tmp_file.exists():
                tmp_file.unlink()
        except OSError:
            pass


def _is_completion_event(receipt: Dict[str, Any]) -> bool:
    """Check if receipt is a completion event."""
    event_type = receipt.get("event_type") or receipt.get("event") or ""
    return event_type in ("task_complete", "task_completed", "completion", "complete")


def _safe_subprocess(cmd: List[str], cwd: Optional[Path] = None, timeout: int = 5) -> Optional[str]:
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip()


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _extract_shortstat_value(shortstat: str, token: str) -> int:
    # Example: "12 files changed, 342 insertions(+), 87 deletions(-)"
    for part in shortstat.split(","):
        chunk = part.strip().lower()
        if token in chunk:
            digits = "".join(ch for ch in chunk if ch.isdigit())
            if digits:
                try:
                    return int(digits)
                except ValueError:
                    return 0
    return 0


def _build_git_provenance(repo_root: Path) -> Dict[str, Any]:
    # Resolve the PROJECT root, not the vnx-system root.
    # CLAUDE_PROJECT_DIR points to the actual project worktree.
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if project_dir:
        probe = _safe_subprocess(["git", "rev-parse", "--show-toplevel"], cwd=Path(project_dir))
        if probe:
            repo_root = Path(project_dir)

    git_root_raw = _safe_subprocess(["git", "rev-parse", "--show-toplevel"], cwd=repo_root)
    captured_at = _utc_now_iso()

    if not git_root_raw:
        return {
            "git_ref": "not_a_repo",
            "branch": "unknown",
            "is_dirty": False,
            "dirty_files": 0,
            "diff_summary": None,
            "captured_at": captured_at,
            "captured_by": "append_receipt",
        }

    git_root = Path(git_root_raw)
    git_ref = _safe_subprocess(["git", "rev-parse", "HEAD"], cwd=git_root) or "unknown"
    branch = _safe_subprocess(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=git_root) or "unknown"
    status_raw = _safe_subprocess(["git", "status", "--porcelain"], cwd=git_root) or ""
    dirty_files = len([line for line in status_raw.splitlines() if line.strip()])
    is_dirty = dirty_files > 0

    diff_summary = None
    if is_dirty:
        shortstat = _safe_subprocess(["git", "diff", "--shortstat"], cwd=git_root) or ""
        if shortstat:
            diff_summary = {
                "files_changed": _extract_shortstat_value(shortstat, "file"),
                "insertions": _extract_shortstat_value(shortstat, "insertion"),
                "deletions": _extract_shortstat_value(shortstat, "deletion"),
            }

    # Detect if running inside a git worktree
    git_dir = _safe_subprocess(["git", "rev-parse", "--git-dir"], cwd=git_root) or ""
    git_common_dir = _safe_subprocess(["git", "rev-parse", "--git-common-dir"], cwd=git_root) or ""
    in_worktree = bool(git_dir and git_common_dir and git_dir != git_common_dir)

    provenance = {
        "git_ref": git_ref,
        "branch": branch,
        "is_dirty": is_dirty,
        "dirty_files": dirty_files,
        "diff_summary": diff_summary,
        "in_worktree": in_worktree,
        "captured_at": captured_at,
        "captured_by": "append_receipt",
    }
    if in_worktree:
        provenance["worktree_path"] = str(git_root)
    return provenance


def _resolve_session_id(receipt: Dict[str, Any]) -> str:
    """Resolve session_id with deterministic priority chain (parallel-terminal safe).

    Priority chain (matches session_resolver.sh):
    1. Report-provided session_id (explicit in metadata)
    2. Per-terminal current_session files (deterministic, parallel-safe)
    3. Environment variables (with auto-create of per-terminal files)
    4. Provider "current" files (global session files)
    5. Fallback: "unknown"
    """
    metadata = receipt.get("metadata") if isinstance(receipt.get("metadata"), dict) else {}
    terminal = str(receipt.get("terminal") or "unknown").strip()

    # Priority 1: Report-provided session_id (explicit - RECOMMENDED)
    report_values = (
        metadata.get("session_id"),
        metadata.get("session"),
        receipt.get("session_id"),
    )
    for candidate in report_values:
        value = str(candidate or "").strip()
        if value and value not in {"unknown", "null", "None"}:
            return value

    # Priority 2: Per-terminal current_session file (DETERMINISTIC)
    state_dir = resolve_state_dir(__file__)
    current_session_file = state_dir / f"current_session_{terminal}"
    if current_session_file.exists():
        try:
            value = current_session_file.read_text(encoding="utf-8").strip()
            if value and value not in {"unknown", "null", "None"}:
                return value
        except Exception:
            pass

    # Priority 3: Environment variables (provider-specific) with auto-create
    env_mapping = {
        "T0": "CLAUDE_SESSION_ID",
        "T1": "CLAUDE_SESSION_ID",
        "T2": "CLAUDE_SESSION_ID",
        "T3": "CLAUDE_SESSION_ID",
        "T-MANAGER": "CLAUDE_SESSION_ID",
    }

    # Also check for provider-specific patterns in terminal name
    if terminal.upper().startswith(("GEMINI", "GEM-")):
        env_var = "GEMINI_SESSION_ID"
    elif terminal.upper().startswith(("CODEX", "CODE-")):
        env_var = "CODEX_SESSION_ID"
    else:
        env_var = env_mapping.get(terminal, "CLAUDE_SESSION_ID")

    env_value = os.environ.get(env_var)
    if env_value:
        value = env_value.strip()
        if value and value not in {"unknown", "null", "None"}:
            # Auto-create per-terminal current_session file for next time
            try:
                state_dir.mkdir(parents=True, exist_ok=True)
                current_session_file.write_text(value, encoding="utf-8")
            except Exception:
                pass  # Non-fatal, just optimization for future lookups
            return value

    # Priority 4: Provider "current" session files (global)
    provider_current_files = (
        Path.home() / ".codex" / "sessions" / "current",
        Path.home() / ".gemini" / "sessions" / "current",
        Path.home() / ".claude" / "sessions" / "current",
    )
    for current_file in provider_current_files:
        try:
            if current_file.exists():
                value = current_file.read_text(encoding="utf-8").strip()
                if value and value not in {"unknown", "null", "None"}:
                    return value
        except Exception:
            continue

    # Priority 5: Fallback to "unknown"
    return "unknown"


def _resolve_model_provider(terminal: str, state_dir: Path) -> Dict[str, str]:
    """Resolve model and provider with panes.json priority (matches session_resolver.sh).

    Priority:
    1. panes.json mapping (if exists)
    2. Terminal naming convention heuristic (fallback)

    Default models for Claude terminals:
    - T0: claude-opus-4.6
    - T1/T2/T3/T-MANAGER: claude-sonnet-4.5
    """
    model = "unknown"
    provider = "unknown"

    # Priority 1: panes.json mapping (if exists)
    panes_json = state_dir / "panes.json"
    if panes_json.exists():
        try:
            payload = json.loads(panes_json.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                term_lower = terminal.lower()
                entry = payload.get(terminal) or payload.get(term_lower)
                if isinstance(entry, dict):
                    model = str(entry.get("model") or "unknown").strip() or "unknown"
                    provider = str(entry.get("provider") or "unknown").strip().lower() or "unknown"
        except Exception:
            pass

    # Priority 2: Terminal naming convention heuristic (fallback)
    # Model is NOT guessed — it must come from panes.json or remain "unknown".
    # Hardcoding model defaults leads to incorrect receipt metadata when
    # terminals run a different model than the startup default.
    if provider == "unknown":
        upper = terminal.upper()
        if upper in ("T0", "T1", "T2", "T3", "T-MANAGER"):
            provider = "claude_code"
        elif "GEMINI" in upper or upper.startswith("GEM-"):
            provider = "gemini_cli"
            if model == "unknown":
                model = "gemini-pro"
        elif "CODEX" in upper or upper.startswith("CODE-"):
            provider = "codex_cli"
            if model == "unknown":
                model = "gpt-5.2-codex"
        elif "KIMI" in upper:
            provider = "kimi_cli"

    return {"model": model, "provider": provider}


def _extract_session_token_usage(session_id: str, terminal: str) -> Optional[Dict[str, int]]:
    """Extract cumulative token usage from Claude Code JSONL session log.

    Scans the session JSONL for message.usage fields and sums all token counters.
    Returns None if the session file cannot be found or parsed.
    """
    if not session_id or session_id == "unknown":
        return None

    # Claude Code stores sessions under ~/.claude/projects/<project-key>/<session-id>.jsonl
    claude_projects = Path.home() / ".claude" / "projects"
    if not claude_projects.is_dir():
        return None

    # Search for the session JSONL across all project dirs
    session_file = None
    for project_dir in claude_projects.iterdir():
        if not project_dir.is_dir():
            continue
        candidate = project_dir / f"{session_id}.jsonl"
        if candidate.is_file():
            session_file = candidate
            break

    if not session_file:
        return None

    totals: Dict[str, int] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    try:
        with open(session_file, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    usage = (msg.get("message") or {}).get("usage")
                    if isinstance(usage, dict):
                        for key in totals:
                            totals[key] += usage.get(key, 0)
                except (json.JSONDecodeError, AttributeError):
                    continue
    except (OSError, IOError):
        return None

    # Only return if we actually found token data
    if totals["input_tokens"] == 0 and totals["output_tokens"] == 0:
        return None
    return totals


def _build_session_metadata(receipt: Dict[str, Any], state_dir: Path) -> Dict[str, Any]:
    terminal = str(receipt.get("terminal") or "unknown").strip() or "unknown"
    model_provider = _resolve_model_provider(terminal, state_dir)
    session_id = _resolve_session_id(receipt)
    metadata: Dict[str, Any] = {
        "session_id": session_id,
        "terminal": terminal,
        "model": model_provider["model"],
        "provider": model_provider["provider"],
        "captured_at": _utc_now_iso(),
    }

    # Inject token usage from session JSONL (best-effort)
    try:
        token_usage = _extract_session_token_usage(session_id, terminal)
        if token_usage:
            metadata["token_usage"] = token_usage
    except Exception:
        pass

    return metadata


def _enrich_completion_receipt(receipt: Dict[str, Any], repo_root: Optional[Path] = None) -> Dict[str, Any]:
    """Enrich completion receipts with quality advisory and terminal snapshot.

    This is best-effort - failures will result in status="unavailable" markers
    rather than crashing the receipt append flow.

    Args:
        receipt: Receipt payload to enrich
        repo_root: Repository root path for git operations

    Returns:
        Enriched receipt with quality_advisory and terminal_snapshot fields
    """
    # Only enrich completion receipts
    if not _is_completion_event(receipt):
        return receipt

    enriched = receipt.copy()
    paths = ensure_env()
    state_dir = Path(paths.get("VNX_STATE_DIR", ".")).resolve()

    # Inject git provenance metadata (best-effort).
    if "provenance" not in enriched:
        try:
            resolved_repo_root = repo_root or Path(paths.get("PROJECT_ROOT", Path.cwd())).resolve()
            enriched["provenance"] = _build_git_provenance(resolved_repo_root)
        except Exception as exc:
            _emit("WARN", "provenance_capture_failed", error=str(exc))
            enriched["provenance"] = {
                "git_ref": "unknown",
                "branch": "unknown",
                "is_dirty": False,
                "dirty_files": 0,
                "diff_summary": None,
                "captured_at": _utc_now_iso(),
                "captured_by": "append_receipt",
                "status": "unavailable",
                "error": str(exc),
            }

    # Inject session metadata for usage correlation (best-effort).
    existing_session = enriched.get("session")
    if isinstance(existing_session, dict):
        merged_session = dict(existing_session)
        try:
            defaults = _build_session_metadata(enriched, state_dir)
            for key, value in defaults.items():
                merged_session.setdefault(key, value)
        except Exception as exc:
            _emit("WARN", "session_metadata_failed", error=str(exc))
        enriched["session"] = merged_session
    else:
        try:
            enriched["session"] = _build_session_metadata(enriched, state_dir)
        except Exception as exc:
            _emit("WARN", "session_metadata_failed", error=str(exc))
            enriched["session"] = {
                "session_id": "unknown",
                "terminal": str(enriched.get("terminal") or "unknown"),
                "model": "unknown",
                "provider": "unknown",
                "captured_at": _utc_now_iso(),
                "status": "unavailable",
                "error": str(exc),
            }

    # Enrich with provenance linkage fields (PR-2: dispatch_id, trace_token, etc.)
    try:
        enrich_receipt_provenance(enriched)
        # Validate provenance and log gaps (best-effort)
        prov_validation = validate_receipt_provenance(enriched)
        if prov_validation.gaps:
            gap_summaries = [g.to_dict() for g in prov_validation.gaps]
            enriched.setdefault("provenance_validation", {
                "chain_status": prov_validation.chain_status,
                "gaps": gap_summaries,
            })
            for gap in prov_validation.gaps:
                if gap.severity in ("warning", "error"):
                    _emit("WARN", "provenance_gap_detected",
                           gap_type=gap.gap_type, entity_id=gap.entity_id,
                           description=gap.description)
    except Exception as exc:
        _emit("WARN", "provenance_enrichment_failed", error=str(exc))

    # Collect terminal snapshot (best-effort)
    try:
        snapshot = collect_terminal_snapshot(state_dir)
        enriched["terminal_snapshot"] = snapshot.to_dict()
    except Exception as exc:
        _emit("WARN", "terminal_snapshot_failed", error=str(exc))
        enriched["terminal_snapshot"] = {
            "status": "unavailable",
            "error": str(exc),
        }

    # Annotate T0 terminal snapshot entry when T0 runs via subprocess adapter.
    # Skips tmux-based probes for T0 and marks the entry so downstream readers
    # know T0 was headless at receipt time.
    if is_headless_t0():
        snapshot_data = enriched.get("terminal_snapshot") or {}
        terminals = snapshot_data.get("terminals") or {}
        t0_entry = dict(terminals.get("T0") or {})
        t0_entry["adapter"] = "subprocess"
        t0_entry["headless"] = True
        terminals["T0"] = t0_entry
        snapshot_data["terminals"] = terminals
        enriched["terminal_snapshot"] = snapshot_data

    # Generate quality advisory (best-effort)
    try:
        if repo_root is None:
            # Try to detect repo root from receipt context or use current directory
            repo_root = Path.cwd()

        changed_files = get_changed_files(repo_root)

        # Fallback: parse report for "Files Modified" when git diff is empty.
        if not changed_files:
            report_path = str(receipt.get("report_path") or "")
            if report_path:
                changed_files = _extract_changed_files_from_report(Path(report_path), repo_root)

        # Only run quality checks if there are changed files
        if changed_files:
            advisory = generate_quality_advisory(changed_files, repo_root)
            enriched["quality_advisory"] = advisory.to_dict()
        else:
            # No changed files - generate minimal advisory
            enriched["quality_advisory"] = {
                "version": "1.0",
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "scope": [],
                "checks": [],
                "summary": {
                    "warning_count": 0,
                    "blocking_count": 0,
                    "risk_score": 0,
                },
                "t0_recommendation": {
                    "decision": "approve",
                    "reason": "No changed files detected",
                    "suggested_dispatches": [],
                    "open_items": [],
                },
            }
    except Exception as exc:
        _emit("WARN", "quality_advisory_failed", error=str(exc))
        enriched["quality_advisory"] = {
            "status": "unavailable",
            "error": str(exc),
        }

    # Enrich with open items delta for CQS (best-effort)
    try:
        dispatch_id_for_oi = enriched.get("dispatch_id") or enriched.get("metadata", {}).get("dispatch_id")
        if dispatch_id_for_oi:
            oim = _get_open_items_manager()
            resolved_count = oim.count_items_closed_by_dispatch(dispatch_id_for_oi)
            enriched["open_items_resolved"] = resolved_count

            # Load target_open_items from dispatch_metadata
            db_path_oi = state_dir / "quality_intelligence.db"
            if db_path_oi.exists():
                import sqlite3 as _sqlite3_oi
                conn_oi = _sqlite3_oi.connect(str(db_path_oi))
                row = conn_oi.execute(
                    "SELECT target_open_items FROM dispatch_metadata WHERE dispatch_id=?",
                    (dispatch_id_for_oi,),
                ).fetchone()
                conn_oi.close()
                if row and row[0]:
                    try:
                        enriched["target_open_items"] = json.loads(row[0])
                    except (json.JSONDecodeError, TypeError):
                        pass
    except Exception as exc:
        _emit("WARN", "oi_delta_enrichment_failed", error=str(exc))

    # Compute CQS and persist to dispatch_metadata (best-effort)
    # CQS computed at receipt time may be later overwritten by update_dispatch_cqs.py
    # with a stripped payload that omits quality_advisory. Tracked as OI-1175.
    # Out of scope for this PR (PR-4b3 only adds dispatch_register emit).
    try:
        db_path = state_dir / "quality_intelligence.db"
        if db_path.exists():
            dispatch_id = enriched.get("dispatch_id") or enriched.get("metadata", {}).get("dispatch_id")
            if dispatch_id:
                cqs_result = calculate_cqs(enriched, None, db_path, dispatch_id)
                enriched["cqs"] = cqs_result
                import sqlite3
                conn = sqlite3.connect(str(db_path))
                conn.execute(
                    """UPDATE dispatch_metadata
                       SET cqs=?, normalized_status=?, cqs_components=?,
                           open_items_created=?, open_items_resolved=?
                       WHERE dispatch_id=?""",
                    (
                        cqs_result["cqs"],
                        cqs_result["normalized_status"],
                        json.dumps(cqs_result["components"]),
                        enriched.get("open_items_created", 0),
                        enriched.get("open_items_resolved", 0),
                        dispatch_id,
                    ),
                )
                conn.commit()
                conn.close()
    except Exception as exc:
        _emit("WARN", "cqs_calculation_failed", error=str(exc))

    return enriched


def _extract_changed_files_from_report(report_path: Path, repo_root: Path) -> List[Path]:
    """Best-effort: parse 'Files Modified' section from report markdown.

    Supports two formats:
    1. Bullet list:  - `path/to/file.py` — description
    2. Markdown table:  | `path/to/file.py` | Type | Description |
    """
    if not report_path.exists():
        return []

    try:
        content = report_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []

    # Find the "Files Modified" section (## or ### heading).
    pattern = re.compile(
        r"^#{2,3}\s+Files\s+Modified(?:/Created)?\s*$", re.MULTILINE
    )
    match = pattern.search(content)
    if not match:
        return []

    section = content[match.end():]
    next_heading = re.search(r"^##+\s+", section, re.MULTILINE)
    if next_heading:
        section = section[:next_heading.start()]

    files: List[Path] = []
    for line in section.splitlines():
        line = line.strip()

        # Skip table header separators (|---|---|)
        if re.match(r"^\|[\s\-:|]+\|$", line):
            continue

        # Extract backtick paths from any format (bullets, tables, plain)
        backtick = re.search(r"`([^`]+\.\w+)`", line)
        if backtick:
            raw_path = backtick.group(1).strip()
        elif line.startswith("-"):
            # Fallback: "- path/to/file.py: description"
            raw_path = line.lstrip("-").strip().split(":", 1)[0].strip()
        elif line.startswith("|"):
            # Table row without backticks: | path/to/file.py | ...
            cells = [c.strip() for c in line.split("|") if c.strip()]
            raw_path = cells[0] if cells else ""
        else:
            continue

        if not raw_path or not re.search(r"\.\w+$", raw_path):
            continue

        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = (repo_root / candidate).resolve()
        if candidate.exists():
            files.append(candidate)

    return files


_SEVERITY_MAP = {
    "blocking": "blocker",
    "warning": "warn",
    "info": "info",
}


def _emit_dispatch_register(receipt: dict) -> bool:
    """Emit dispatch_register event for codex_gate-relevant receipts.

    SCOPE: codex_gate only. gemini_review and claude_github_optional are
    deferred until proper findings parsers exist (separate PR).

    Returns True on success, False on any failure (best-effort, never raises).
    """
    try:
        sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))
        from dispatch_register import append_event

        event_type = str(receipt.get("event_type") or receipt.get("event") or "").lower()
        status = str(receipt.get("status", "")).lower()
        gate = str(receipt.get("gate", "")).lower()
        dispatch_id = str(receipt.get("dispatch_id", ""))
        terminal = str(receipt.get("terminal", ""))
        feature_id = str(receipt.get("feature_id", ""))
        pr_number = receipt.get("pr_number")
        if pr_number is None:
            pr_number = receipt.get("metadata", {}).get("pr_number") if isinstance(receipt.get("metadata"), dict) else None
        try:
            pr_number = int(pr_number) if pr_number is not None else None
        except (ValueError, TypeError):
            pr_number = None

        SUCCESS_STATUSES = {"success", "completed", "complete", "ok", ""}
        FAILURE_STATUSES = {"failed", "failure", "error", "blocked"}

        register_event = None
        if event_type in ("task_complete", "task_completed"):
            if status in FAILURE_STATUSES:
                register_event = "dispatch_failed"
            elif status in SUCCESS_STATUSES:
                register_event = "dispatch_completed"
            else:
                return False
        elif event_type == "task_failed":
            register_event = "dispatch_failed"
        elif event_type == "task_timeout":
            register_event = "dispatch_failed"
        elif event_type in ("task_started", "task_start", "dispatch_start"):
            register_event = "dispatch_started"
        elif event_type == "review_gate_request":
            if gate != "codex_gate":
                return False
            register_event = "gate_requested"
        else:
            return False

        return append_event(
            register_event,
            dispatch_id=dispatch_id,
            pr_number=pr_number,
            feature_id=feature_id,
            terminal=terminal,
            gate=gate,
        )
    except Exception:
        return False


def _count_quality_violations(receipt: dict) -> int:
    """Count violations that _register_quality_open_items WILL create.

    Reads the same data path as _register_quality_open_items so the
    pre-computed count is consistent with what will be registered.
    Returns 0 when quality_advisory or t0_recommendation is absent.
    """
    advisory = receipt.get("quality_advisory") or {}
    rec = advisory.get("t0_recommendation") or {}
    open_items = rec.get("open_items") or []
    return len(open_items)


def _register_quality_open_items(receipt: Dict[str, Any]) -> int:
    """Best-effort: register quality advisory violations as tracked open items.

    Reads t0_recommendation.open_items[] from the enriched receipt, creates
    open items with dedup keys, and ALWAYS writes a sidecar summary for the
    receipt processor to include in T0 notifications (even when clean).

    Returns:
        Count of newly created open items.
    """
    try:
        advisory = receipt.get("quality_advisory")
        if not isinstance(advisory, dict):
            return 0

        rec = advisory.get("t0_recommendation")
        if not isinstance(rec, dict):
            return 0

        dispatch_id = str(receipt.get("dispatch_id") or "unknown")
        report_path = str(receipt.get("report_path") or "")
        pr_id = str(receipt.get("pr_id") or "")

        new_ids: List[str] = []
        counts = {"blocker": 0, "warn": 0, "info": 0}

        open_items = rec.get("open_items") or []

        if open_items:
            oim = _get_open_items_manager()

            for item in open_items:
                try:
                    check_id = str(item.get("check_id", "unknown"))
                    file_path = str(item.get("file", ""))
                    symbol = str(item.get("symbol") or "")
                    raw_severity = str(item.get("severity", "info"))
                    mapped_severity = _SEVERITY_MAP.get(raw_severity, "info")
                    title = str(item.get("item", ""))
                    file_basename = Path(file_path).name if file_path else "unknown"

                    # Build dedup key: qa:{check_id}:{file_basename}:{symbol}
                    # Dedup key uses basename+symbol; collisions across directories tracked as OI-1176.
                    # Out of scope for PR-4b3.
                    dedup_key = f"qa:{check_id}:{file_basename}:{symbol}"

                    item_id, created = oim.add_item_programmatic(
                        title=title,
                        severity=mapped_severity,
                        dispatch_id=dispatch_id,
                        report_path=report_path,
                        pr_id=pr_id,
                        details=f"file={file_path}, symbol={symbol}" if symbol else f"file={file_path}",
                        dedup_key=dedup_key,
                        source="quality_advisory",
                    )

                    counts[mapped_severity] = counts.get(mapped_severity, 0) + 1
                    if created:
                        new_ids.append(item_id)
                except Exception as exc:
                    _emit("WARN", "quality_oi_item_failed", error=str(exc))


        return len(new_ids)

    except Exception as exc:
        _emit("WARN", "quality_oi_registration_failed", error=str(exc))
        return 0


def append_receipt_payload(
    receipt: Dict[str, Any],
    *,
    receipts_file: Optional[str] = None,
    cache_window_seconds: int = 300,
    skip_enrichment: bool = False,
) -> AppendResult:
    if not isinstance(receipt, dict):
        raise AppendReceiptError("invalid_receipt_type", EXIT_INVALID_INPUT, "Receipt payload must be a JSON object")

    # Enrich completion receipts with quality advisory and terminal snapshot (best-effort)
    if not skip_enrichment:
        receipt = _enrich_completion_receipt(receipt)

    # Pre-compute open_items_created BEFORE the lock-protected ndjson write so the
    # embedded count is non-zero when the advisory has violations.  Without this,
    # open_items_created persisted as 0 and _score_open_items_delta() lost the penalty.
    receipt["open_items_created"] = _count_quality_violations(receipt)

    # Route ghost gate receipts (dispatch_id="unknown" + gate event) to gate_events.ndjson.
    # review_gate_request with empty/missing dispatch_id is redirected here (pre-existing
    # behaviour from PR #255, see ghost_receipt_filter.py). With PR-2 fix, callers supply
    # a real dispatch_id so is_ghost_dispatch_id() returns False and receipts land normally
    # in t0_receipts.ndjson. This is documented intent, not a regression.
    if receipts_file is None and should_route_to_gate_stream(receipt):
        try:
            paths = ensure_env()
            state_dir = Path(paths["VNX_STATE_DIR"])
            receipts_file = str(gate_events_file(state_dir))
            _emit("INFO", "ghost_receipt_rerouted",
                  gate=str(receipt.get("gate") or ""),
                  pr_id=str(receipt.get("pr_id") or ""),
                  destination=receipts_file)
        except Exception as exc:
            _emit("WARN", "ghost_receipt_reroute_failed", error=str(exc))

    event_name = _validate_receipt(receipt)
    idempotency_key = _compute_idempotency_key(receipt, event_name)

    receipt_path = _resolve_receipts_file(receipts_file).expanduser().resolve()
    receipt_path.parent.mkdir(parents=True, exist_ok=True)

    lock_path = _lock_file_for(receipt_path)
    cache_path = _cache_file_for(receipt_path)

    min_epoch = time.time() - max(1, int(cache_window_seconds))

    result: Optional[AppendResult] = None

    try:
        with lock_path.open("a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)

            cache_entries = _load_cache(cache_path, min_epoch)
            recent_keys = {entry["key"] for entry in cache_entries}

            if idempotency_key in recent_keys:
                _write_cache(cache_path, cache_entries)
                result = AppendResult(
                    status="duplicate",
                    receipts_file=receipt_path,
                    idempotency_key=idempotency_key,
                )
            else:
                try:
                    with receipt_path.open("a", encoding="utf-8") as receipts_handle:
                        receipts_handle.write(json.dumps(receipt, separators=(",", ":"), sort_keys=False))
                        receipts_handle.write("\n")
                except OSError as exc:
                    raise AppendReceiptError("receipt_write_failed", EXIT_IO_ERROR, f"Failed to append receipt: {exc}") from exc

                cache_entries.append({"ts": time.time(), "key": idempotency_key})
                _write_cache(cache_path, cache_entries)

                result = AppendResult(
                    status="appended",
                    receipts_file=receipt_path,
                    idempotency_key=idempotency_key,
                )
    except AppendReceiptError:
        raise
    except OSError as exc:
        raise AppendReceiptError("lock_failed", EXIT_LOCK_ERROR, f"Failed to acquire append lock: {exc}") from exc

    # Best-effort post-append hooks (skipped for skip_enrichment=True lightweight events)
    if result is not None and result.status == "appended" and not skip_enrichment:
        # DB registration runs outside lock (existing constraint preserved).
        # Count is already embedded in the receipt above; do not overwrite.
        _register_quality_open_items(receipt)

        _update_confidence_from_receipt(receipt)

        _emit_dispatch_register(receipt)

        _maybe_trigger_state_rebuild(receipt)

    return result


def _update_confidence_from_receipt(receipt: Dict[str, Any]) -> None:
    """Wire dispatch outcome into pattern confidence scores (best-effort)."""
    try:
        event_type = str(receipt.get("event_type") or receipt.get("status") or "")
        if event_type not in ("task_complete", "task_failed"):
            return

        dispatch_id = str(receipt.get("dispatch_id") or "")
        terminal = str(receipt.get("terminal") or "")
        if not dispatch_id:
            return

        outcome = "success" if event_type == "task_complete" else "failure"

        state_dir = resolve_state_dir(__file__)

        db_path = state_dir / "quality_intelligence.db"
        if not db_path.exists():
            return

        sys.path.insert(0, str(SCRIPT_DIR / "lib"))
        from intelligence_persist import update_confidence_from_outcome
        update_confidence_from_outcome(db_path, dispatch_id, terminal, outcome)
    except Exception as exc:
        _emit("WARN", "confidence_update_failed", error=str(exc))


def _maybe_trigger_state_rebuild(receipt: Dict[str, Any]) -> None:
    """Fire non-blocking rebuild of t0_state.json after qualifying events. Best-effort."""
    try:
        event_type = str(receipt.get("event_type") or receipt.get("event") or "")
        if not (_is_completion_event(receipt) or event_type in ("dispatch_promoted", "dispatch_started")):
            return

        state_dir = resolve_state_dir(__file__)
        throttle_file = state_dir / ".last_state_rebuild_ts"

        try:
            if throttle_file.exists():
                last_ts = float(throttle_file.read_text(encoding="utf-8").strip())
                if time.time() - last_ts < _REBUILD_THROTTLE_SECONDS:
                    return
        except Exception:
            pass

        subprocess.Popen(
            ["python3", "scripts/build_t0_state.py"],
            cwd=str(_REPO_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        # Write throttle marker only after Popen succeeds; a failed launch
        # should not suppress the next rebuild attempt for 30s.
        try:
            tmp = throttle_file.with_name(throttle_file.name + ".tmp")
            tmp.write_text(str(time.time()), encoding="utf-8")
            os.replace(str(tmp), str(throttle_file))
        except Exception:
            pass
    except Exception:
        pass


def _parse_input(receipt_json: Optional[str], receipt_file: Optional[str]) -> Dict[str, Any]:
    raw = ""

    if receipt_json is not None:
        raw = receipt_json
    elif receipt_file:
        try:
            raw = Path(receipt_file).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            raise AppendReceiptError("input_read_failed", EXIT_IO_ERROR, f"Failed to read receipt file: {exc}") from exc
    else:
        raw = sys.stdin.read()

    if not raw or not raw.strip():
        raise AppendReceiptError("empty_input", EXIT_INVALID_INPUT, "No receipt JSON input provided")

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AppendReceiptError("invalid_json", EXIT_INVALID_INPUT, f"Malformed receipt JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise AppendReceiptError("invalid_receipt_type", EXIT_INVALID_INPUT, "Receipt payload must be a JSON object")

    return parsed


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Canonical receipt append helper")
    parser.add_argument("--receipt", help="Raw receipt JSON payload", default=None)
    parser.add_argument("--receipt-file", help="Path to file containing a single receipt JSON payload", default=None)
    parser.add_argument("--receipts-file", help="Override canonical receipts file path", default=None)
    parser.add_argument("--cache-window-seconds", type=int, default=300, help="Recent idempotency window in seconds")
    parser.add_argument("--skip-enrichment", action="store_true", default=False, help="Skip quality advisory and provenance enrichment (for state-mutation events)")
    args = parser.parse_args(argv)

    try:
        receipt = _parse_input(args.receipt, args.receipt_file)
        result = append_receipt_payload(
            receipt,
            receipts_file=args.receipts_file,
            cache_window_seconds=args.cache_window_seconds,
            skip_enrichment=args.skip_enrichment,
        )
    except AppendReceiptError as exc:
        _emit("ERROR", exc.code, message=exc.message)
        return exc.exit_code
    except Exception as exc:  # pragma: no cover - safety net
        _emit("ERROR", "unexpected_error", message=str(exc))
        return EXIT_UNEXPECTED_ERROR

    if result.status == "duplicate":
        _emit(
            "INFO",
            "duplicate_receipt_skipped",
            idempotency_key=result.idempotency_key,
            receipts_file=str(result.receipts_file),
        )
    else:
        _emit(
            "INFO",
            "receipt_appended",
            idempotency_key=result.idempotency_key,
            receipts_file=str(result.receipts_file),
        )

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
