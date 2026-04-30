#!/usr/bin/env python3
"""receipt_git_session.py — Git provenance and session metadata helpers.

Extracted from append_receipt.py to keep the main module under 500 lines.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from vnx_paths import ensure_env
from project_root import resolve_state_dir


def _emit(level: str, code: str, **fields: Any) -> None:
    payload = {"level": level, "code": code, "timestamp": int(time.time())}
    payload.update(fields)
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True), file=sys.stderr)


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


_PROVIDER_ENV_VAR: Dict[str, str] = {
    "claude_code": "CLAUDE_SESSION_ID",
    "gemini_cli": "GEMINI_SESSION_ID",
    "codex_cli": "CODEX_SESSION_ID",
    "kimi_cli": "KIMI_SESSION_ID",
}

_PROVIDER_SESSION_FILES: Dict[str, Path] = {
    "claude_code": Path.home() / ".claude" / "sessions" / "current",
    "gemini_cli": Path.home() / ".gemini" / "sessions" / "current",
    "codex_cli": Path.home() / ".codex" / "sessions" / "current",
    "kimi_cli": Path.home() / ".kimi" / "sessions" / "current",
}


def _rsi_check_env_session(
    terminal: str, state_dir: Path, current_session_file: Path
) -> Optional[str]:
    """Priority 3: check provider env var; auto-create per-terminal file if found."""
    mp = _resolve_model_provider(terminal, state_dir)
    provider = mp.get("provider", "unknown")
    env_var = _PROVIDER_ENV_VAR.get(provider, "CLAUDE_SESSION_ID")
    env_value = os.environ.get(env_var)
    if env_value:
        value = env_value.strip()
        if value and value not in {"unknown", "null", "None"}:
            try:
                state_dir.mkdir(parents=True, exist_ok=True)
                current_session_file.write_text(value, encoding="utf-8")
            except Exception:
                pass
            return value
    return None


def _rsi_check_provider_files(terminal: str, state_dir: Path) -> Optional[str]:
    """Priority 4: check provider session files in provider-priority order."""
    mp = _resolve_model_provider(terminal, state_dir)
    provider = mp.get("provider", "unknown")
    preferred_file = _PROVIDER_SESSION_FILES.get(provider)
    other_files = [f for f in _PROVIDER_SESSION_FILES.values() if f != preferred_file]
    files = ([preferred_file] + other_files) if preferred_file else list(_PROVIDER_SESSION_FILES.values())
    for current_file in files:
        try:
            if current_file.exists():
                value = current_file.read_text(encoding="utf-8").strip()
                if value and value not in {"unknown", "null", "None"}:
                    return value
        except Exception:
            continue
    return None


def _resolve_session_id(receipt: Dict[str, Any], state_dir: Optional[Path] = None) -> str:
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

    for candidate in (metadata.get("session_id"), metadata.get("session"), receipt.get("session_id")):
        value = str(candidate or "").strip()
        if value and value not in {"unknown", "null", "None"}:
            return value

    if state_dir is None:
        state_dir = resolve_state_dir(__file__)
    current_session_file = state_dir / f"current_session_{terminal}"
    if current_session_file.exists():
        try:
            value = current_session_file.read_text(encoding="utf-8").strip()
            if value and value not in {"unknown", "null", "None"}:
                return value
        except Exception:
            pass

    value = _rsi_check_env_session(terminal, state_dir, current_session_file)
    if value:
        return value

    value = _rsi_check_provider_files(terminal, state_dir)
    if value:
        return value

    return "unknown"


def _resolve_model_provider(terminal: str, state_dir: Path) -> Dict[str, str]:
    """Resolve model and provider with panes.json priority (matches session_resolver.sh).

    Priority:
    1. panes.json mapping (if exists)
    2. Terminal naming convention heuristic (fallback)
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
    """Extract cumulative token usage from Claude Code JSONL session log."""
    if not session_id or session_id == "unknown":
        return None

    claude_projects = Path.home() / ".claude" / "projects"
    if not claude_projects.is_dir():
        return None

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

    if totals["input_tokens"] == 0 and totals["output_tokens"] == 0:
        return None
    return totals


def _build_session_metadata(receipt: Dict[str, Any], state_dir: Path) -> Dict[str, Any]:
    terminal = str(receipt.get("terminal") or "unknown").strip() or "unknown"
    model_provider = _resolve_model_provider(terminal, state_dir)
    session_id = _resolve_session_id(receipt, state_dir)
    metadata: Dict[str, Any] = {
        "session_id": session_id,
        "terminal": terminal,
        "model": model_provider["model"],
        "provider": model_provider["provider"],
        "captured_at": _utc_now_iso(),
    }

    provider = model_provider["provider"]
    try:
        if provider == "codex_cli":
            from adapters.codex_adapter import CodexAdapter  # noqa: PLC0415
            token_usage = CodexAdapter.get_token_usage(terminal, state_dir)
        elif provider in ("gemini_cli", "gemini"):
            from adapters.gemini_adapter import GeminiAdapter  # noqa: PLC0415
            token_usage = GeminiAdapter.get_token_usage(terminal, state_dir)
        else:
            token_usage = _extract_session_token_usage(session_id, terminal)
        if token_usage:
            metadata["token_usage"] = token_usage
    except Exception:
        pass

    manifest_path = receipt.get("manifest_path")
    if manifest_path:
        try:
            manifest_data = json.loads(Path(manifest_path).read_text())
            sha = manifest_data.get("instruction_sha256")
            if sha:
                metadata["instruction_sha256"] = sha
        except (OSError, IOError, json.JSONDecodeError) as exc:
            _emit("WARN", "manifest_sha256_read_failed", manifest_path=str(manifest_path), error=str(exc))

    return metadata
