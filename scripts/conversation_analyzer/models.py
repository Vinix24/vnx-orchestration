"""Shared constants, data classes, logging helpers, and model normalization."""

import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

# scripts/ directory (one level above this package)
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS_DIR / "lib"))
try:
    from vnx_paths import ensure_env
except Exception as exc:
    raise SystemExit(f"Failed to load vnx_paths: {exc}")

PATHS = ensure_env()
SCRIPT_DIR = _SCRIPTS_DIR
VNX_BASE = Path(PATHS["VNX_HOME"])
STATE_DIR = Path(PATHS["VNX_STATE_DIR"])
DB_PATH = STATE_DIR / "quality_intelligence.db"

CLAUDE_PROJECTS_DIR = Path(
    os.environ.get("CLAUDE_PROJECTS_DIR", str(Path.home() / ".claude" / "projects"))
)
ANALYZER_VERSION = "1.1.0"

LLM_STRATEGY = os.environ.get("VNX_ANALYZER_LLM", "auto")
OLLAMA_MODEL = os.environ.get("VNX_OLLAMA_MODEL", "qwen2.5-coder:14b")

DEEP_THRESHOLD_TOKENS = 100_000
DEEP_THRESHOLD_TOOLS = 100

TERMINAL_PATTERNS = {
    "T-MANAGER": re.compile(r"T-MANAGER", re.IGNORECASE),
    "T0": re.compile(r"(?:^|-)T0(?:$|-)", re.IGNORECASE),
    "T1": re.compile(r"(?:^|-)T1(?:$|-)", re.IGNORECASE),
    "T2": re.compile(r"(?:^|-)T2(?:$|-)", re.IGNORECASE),
    "T3": re.compile(r"(?:^|-)T3(?:$|-)", re.IGNORECASE),
}

TOOL_NAMES = {"Read", "Edit", "Bash", "Grep", "Write", "Task", "Glob",
              "WebFetch", "WebSearch", "NotebookEdit", "MultiEdit"}

MODEL_PATTERNS = [
    (re.compile(r"claude-opus", re.IGNORECASE), "claude-opus"),
    (re.compile(r"claude-sonnet", re.IGNORECASE), "claude-sonnet"),
    (re.compile(r"claude-haiku", re.IGNORECASE), "claude-haiku"),
    (re.compile(r"codex", re.IGNORECASE), "codex"),
    (re.compile(r"gemini", re.IGNORECASE), "gemini"),
    (re.compile(r"gpt-?4", re.IGNORECASE), "gpt-4"),
    (re.compile(r"o[134]-", re.IGNORECASE), "openai-reasoning"),
]


def normalize_model(raw_model: str) -> str:
    if not raw_model:
        return "unknown"
    for pattern, family in MODEL_PATTERNS:
        if pattern.search(raw_model):
            return family
    return "unknown"


class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    RESET = '\033[0m'


def log(level: str, message: str):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    color_map = {
        'INFO': Colors.BLUE, 'SUCCESS': Colors.GREEN,
        'WARNING': Colors.YELLOW, 'ERROR': Colors.RED,
        'ANALYZE': Colors.CYAN,
    }
    color = color_map.get(level, Colors.RESET)
    print(f"[{timestamp}] {color}[{level}]{Colors.RESET} {message}")


@dataclass
class SessionMetrics:
    session_id: str = ""
    project_path: str = ""
    terminal: str = "unknown"
    session_date: str = ""
    dispatch_id: str = ""
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    tool_calls_total: int = 0
    tool_read_count: int = 0
    tool_edit_count: int = 0
    tool_bash_count: int = 0
    tool_grep_count: int = 0
    tool_write_count: int = 0
    tool_task_count: int = 0
    tool_other_count: int = 0
    message_count: int = 0
    user_message_count: int = 0
    assistant_message_count: int = 0
    duration_minutes: float = 0.0
    file_size_bytes: int = 0
    session_model: str = ""


@dataclass
class SessionFlags:
    has_error_recovery: bool = False
    has_context_reset: bool = False
    context_reset_count: int = 0
    has_large_refactor: bool = False
    has_test_cycle: bool = False
    primary_activity: str = "unknown"


@dataclass
class RunStats:
    sessions_analyzed: int = 0
    sessions_deep: int = 0
    total_tokens: int = 0
    errors: int = 0
    skipped: int = 0
    suggestions: List[dict] = field(default_factory=list)
