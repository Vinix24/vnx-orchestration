"""Session-id and model/provider resolution + token-usage extraction."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from .common import _emit, _utc_now_iso, facade

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
    mp = facade._resolve_model_provider(terminal, state_dir)
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
    mp = facade._resolve_model_provider(terminal, state_dir)
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

    Args:
        receipt: Receipt payload.
        state_dir: Resolved state directory (caller should supply to avoid
                   double-resolution). Falls back to resolve_state_dir(__file__).
    """
    metadata = receipt.get("metadata") if isinstance(receipt.get("metadata"), dict) else {}
    terminal = str(receipt.get("terminal") or "unknown").strip()

    for candidate in (metadata.get("session_id"), metadata.get("session"), receipt.get("session_id")):
        value = str(candidate or "").strip()
        if value and value not in {"unknown", "null", "None"}:
            return value

    if state_dir is None:
        state_dir = facade.resolve_state_dir(__file__)
    current_session_file = state_dir / f"current_session_{terminal}"
    if current_session_file.exists():
        try:
            value = current_session_file.read_text(encoding="utf-8").strip()
            if value and value not in {"unknown", "null", "None"}:
                return value
        except Exception:
            pass

    value = facade._rsi_check_env_session(terminal, state_dir, current_session_file)
    if value:
        return value

    value = facade._rsi_check_provider_files(terminal, state_dir)
    if value:
        return value

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
    model_provider = facade._resolve_model_provider(terminal, state_dir)
    session_id = facade._resolve_session_id(receipt, state_dir)
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
            token_usage = facade._extract_session_token_usage(session_id, terminal)
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
