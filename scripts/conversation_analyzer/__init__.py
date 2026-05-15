"""VNX Conversation Analyzer package.

Backwards-compatible re-exports so existing code using
`from conversation_analyzer import X` continues to work.
"""

from .models import (
    SCRIPT_DIR,
    PATHS,
    VNX_BASE,
    STATE_DIR,
    DB_PATH,
    CLAUDE_PROJECTS_DIR,
    ANALYZER_VERSION,
    LLM_STRATEGY,
    OLLAMA_MODEL,
    DEEP_THRESHOLD_TOKENS,
    DEEP_THRESHOLD_TOOLS,
    TERMINAL_PATTERNS,
    TOOL_NAMES,
    MODEL_PATTERNS,
    normalize_model,
    Colors,
    log,
    SessionMetrics,
    SessionFlags,
    RunStats,
)
from .parser import SessionParser
from .detector import HeuristicDetector
from .deep_analyzer import DeepAnalyzer
from .generator import DigestGenerator
from .runner import ConversationAnalyzer

__all__ = [
    "SessionParser", "SessionMetrics", "SessionFlags",
    "HeuristicDetector", "DeepAnalyzer", "DigestGenerator",
    "ConversationAnalyzer", "RunStats", "normalize_model",
    "Colors", "log",
    "CLAUDE_PROJECTS_DIR", "SCRIPT_DIR", "PATHS",
    "VNX_BASE", "STATE_DIR", "DB_PATH", "ANALYZER_VERSION",
    "LLM_STRATEGY", "OLLAMA_MODEL",
    "DEEP_THRESHOLD_TOKENS", "DEEP_THRESHOLD_TOOLS",
    "TERMINAL_PATTERNS", "TOOL_NAMES", "MODEL_PATTERNS",
]
