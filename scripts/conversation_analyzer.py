#!/usr/bin/env python3
"""
VNX Conversation Analyzer — Nightly Session Mining Pipeline
Version: 1.0.0
Purpose: Extract actionable intelligence from Claude Code JSONL session logs.

4-phase pipeline:
  Phase 1: Parse JSONL → token/tool/message metrics (pure Python)
  Phase 2: Heuristic pattern detection (pure Python)
  Phase 3: Selective LLM deep analysis (Claude Max or Ollama fallback)
  Phase 4: Store in session_analytics + generate nightly digest
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
try:
    from vnx_paths import ensure_env
except Exception as exc:
    raise SystemExit(f"Failed to load vnx_paths: {exc}")

PATHS = ensure_env()
VNX_BASE = Path(PATHS["VNX_HOME"])
STATE_DIR = Path(PATHS["VNX_STATE_DIR"])
DB_PATH = STATE_DIR / "quality_intelligence.db"

CLAUDE_PROJECTS_DIR = Path(
    os.environ.get("CLAUDE_PROJECTS_DIR", str(Path.home() / ".claude" / "projects"))
)
ANALYZER_VERSION = "1.1.0"

# LLM strategy: auto | claude-only | ollama-only
LLM_STRATEGY = os.environ.get("VNX_ANALYZER_LLM", "auto")

# Ollama model for local inference
OLLAMA_MODEL = os.environ.get("VNX_OLLAMA_MODEL", "qwen2.5-coder:14b")

# Deep analysis trigger thresholds
DEEP_THRESHOLD_TOKENS = 100_000
DEEP_THRESHOLD_TOOLS = 100

# Terminal detection patterns
TERMINAL_PATTERNS = {
    "T-MANAGER": re.compile(r"T-MANAGER", re.IGNORECASE),
    "T0": re.compile(r"(?:^|-)T0(?:$|-)", re.IGNORECASE),
    "T1": re.compile(r"(?:^|-)T1(?:$|-)", re.IGNORECASE),
    "T2": re.compile(r"(?:^|-)T2(?:$|-)", re.IGNORECASE),
    "T3": re.compile(r"(?:^|-)T3(?:$|-)", re.IGNORECASE),
}

TOOL_NAMES = {"Read", "Edit", "Bash", "Grep", "Write", "Task", "Glob",
              "WebFetch", "WebSearch", "NotebookEdit", "MultiEdit"}

# Model normalization patterns
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
    """Normalize a raw model ID to a canonical family name.

    Examples:
        "claude-opus-4-1-20250805" → "claude-opus"
        "claude-sonnet-4-5-20250514" → "claude-sonnet"
        "codex-mini-latest" → "codex"
        "gemini-2.0-flash" → "gemini"
        "" → "unknown"
    """
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


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Phase 1: Session Parser
# ---------------------------------------------------------------------------

class SessionParser:
    """Parse a Claude Code JSONL session into structured metrics."""

    DISPATCH_TABLE_RE = re.compile(r'\|\s*\*\*Dispatch-ID\*\*\s*\|\s*([^\|]+?)\s*\|')
    DISPATCH_HEADER_RE = re.compile(r'Dispatch-ID:\s*(\S+)')

    @staticmethod
    def session_id_from_path(jsonl_path: Path) -> str:
        return jsonl_path.stem

    @staticmethod
    def project_path_from_dir(dir_name: str) -> str:
        decoded = dir_name.replace("-", "/")
        if decoded.startswith("/"):
            return decoded
        return "/" + decoded

    @staticmethod
    def detect_terminal(dir_name: str) -> str:
        for terminal, pattern in TERMINAL_PATTERNS.items():
            if pattern.search(dir_name):
                return terminal
        return "unknown"

    def parse_file(self, jsonl_path: Path) -> Tuple[SessionMetrics, List[dict]]:
        metrics = SessionMetrics()
        metrics.session_id = self.session_id_from_path(jsonl_path)
        metrics.file_size_bytes = jsonl_path.stat().st_size

        dir_name = jsonl_path.parent.name
        metrics.project_path = self.project_path_from_dir(dir_name)
        metrics.terminal = self.detect_terminal(dir_name)

        messages = []
        first_ts = None
        last_ts = None

        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_type = record.get("type", "")
                timestamp_str = record.get("timestamp", "")
                metrics.message_count += 1

                if timestamp_str:
                    ts = self._parse_timestamp(timestamp_str)
                    if ts:
                        if first_ts is None:
                            first_ts = ts
                        last_ts = ts

                if msg_type == "assistant":
                    metrics.assistant_message_count += 1
                    msg = record.get("message", {})
                    usage = msg.get("usage", {})
                    metrics.total_input_tokens += usage.get("input_tokens", 0)
                    metrics.total_output_tokens += usage.get("output_tokens", 0)
                    metrics.cache_creation_tokens += usage.get("cache_creation_input_tokens", 0)
                    metrics.cache_read_tokens += usage.get("cache_read_input_tokens", 0)

                    # Extract model from first assistant message
                    if not metrics.session_model:
                        raw_model = msg.get("model", "")
                        if raw_model:
                            metrics.session_model = normalize_model(raw_model)

                    for block in msg.get("content", []):
                        if block.get("type") == "tool_use":
                            tool_name = block.get("name", "")
                            metrics.tool_calls_total += 1
                            self._count_tool(metrics, tool_name)

                    messages.append(record)

                elif msg_type == "user":
                    metrics.user_message_count += 1
                    # Extract dispatch_id from first few user messages
                    if not metrics.dispatch_id and metrics.user_message_count <= 3:
                        content = record.get("message", {}).get("content", "")
                        text = content if isinstance(content, str) else " ".join(
                            b.get("text", "") for b in content if isinstance(b, dict)
                        ) if isinstance(content, list) else ""
                        m = self.DISPATCH_TABLE_RE.search(text)
                        if not m:
                            m = self.DISPATCH_HEADER_RE.search(text)
                        if m:
                            metrics.dispatch_id = m.group(1).strip()
                    messages.append(record)

                elif msg_type == "system":
                    messages.append(record)

        if first_ts and last_ts:
            delta = (last_ts - first_ts).total_seconds()
            metrics.duration_minutes = round(delta / 60.0, 1)

        if first_ts:
            metrics.session_date = first_ts.strftime("%Y-%m-%d")
        else:
            metrics.session_date = datetime.now().strftime("%Y-%m-%d")

        return metrics, messages

    @staticmethod
    def _parse_timestamp(ts_str: str) -> Optional[datetime]:
        try:
            clean = ts_str.replace("Z", "+00:00")
            return datetime.fromisoformat(clean)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _count_tool(metrics: SessionMetrics, tool_name: str):
        name_lower = tool_name.lower()
        if name_lower == "read":
            metrics.tool_read_count += 1
        elif name_lower in ("edit", "multiedit"):
            metrics.tool_edit_count += 1
        elif name_lower == "bash":
            metrics.tool_bash_count += 1
        elif name_lower in ("grep", "glob"):
            metrics.tool_grep_count += 1
        elif name_lower == "write":
            metrics.tool_write_count += 1
        elif name_lower == "task":
            metrics.tool_task_count += 1
        else:
            metrics.tool_other_count += 1


# ---------------------------------------------------------------------------
# Phase 2: Heuristic Detector
# ---------------------------------------------------------------------------

class HeuristicDetector:
    """Detect session patterns via heuristics — no LLM needed."""

    ERROR_KEYWORDS = {"error", "fail", "traceback", "exception", "errno",
                      "denied", "not found", "cannot", "fatal"}
    TEST_KEYWORDS = {"pytest", "test", "unittest", "-m pytest", "python3 -m pytest"}

    def detect_patterns(self, metrics: SessionMetrics,
                        messages: List[dict]) -> SessionFlags:
        flags = SessionFlags()

        flags.has_large_refactor = metrics.tool_edit_count > 10

        tool_sequence = self._extract_tool_sequence(messages)
        flags.has_error_recovery = self._detect_error_recovery(messages)
        flags.context_reset_count = self._count_context_resets(messages)
        flags.has_context_reset = flags.context_reset_count > 0
        flags.has_test_cycle = self._detect_test_cycle(tool_sequence, messages)
        flags.primary_activity = self._classify_activity(metrics)

        return flags

    def _extract_tool_sequence(self, messages: List[dict]) -> List[str]:
        sequence = []
        for record in messages:
            if record.get("type") != "assistant":
                continue
            for block in record.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    sequence.append(block.get("name", ""))
        return sequence

    def _detect_error_recovery(self, messages: List[dict]) -> bool:
        error_count = 0
        for record in messages:
            msg_type = record.get("type", "")
            if msg_type == "user":
                content = record.get("message", {}).get("content", "")
                text = self._extract_text(content)
                if any(kw in text.lower() for kw in self.ERROR_KEYWORDS):
                    error_count += 1
            elif msg_type == "assistant":
                for block in record.get("message", {}).get("content", []):
                    if block.get("type") == "tool_result":
                        result_text = self._extract_text(block.get("content", ""))
                        if any(kw in result_text.lower() for kw in self.ERROR_KEYWORDS):
                            error_count += 1
        return error_count >= 2

    def _count_context_resets(self, messages: List[dict]) -> int:
        """Count context compaction/rotation events in session."""
        count = 0
        for record in messages:
            if record.get("type") == "system":
                subtype = record.get("subtype", "")
                if "compaction" in subtype or "summary" in subtype:
                    count += 1
                    continue
                data = record.get("data", "")
                if isinstance(data, str) and "context" in data.lower():
                    count += 1
        return count

    def _detect_test_cycle(self, tool_sequence: List[str],
                           messages: List[dict]) -> bool:
        # Build a parallel list: for each tool in tool_sequence, record if it's
        # a Bash call with a test command
        is_test_bash = []
        tool_idx = 0
        for record in messages:
            if record.get("type") != "assistant":
                continue
            for block in record.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    name = block.get("name", "")
                    if name.lower() == "bash":
                        inp = block.get("input", {})
                        cmd = inp.get("command", "") if isinstance(inp, dict) else ""
                        is_test = any(kw in cmd.lower() for kw in self.TEST_KEYWORDS)
                        is_test_bash.append(is_test)
                    else:
                        is_test_bash.append(False)
                    tool_idx += 1

        # Find test-bash positions in the tool sequence
        test_positions = [i for i, v in enumerate(is_test_bash) if v]

        cycle_count = 0
        for i in range(len(test_positions) - 1):
            idx_a = test_positions[i]
            idx_b = test_positions[i + 1]
            between = tool_sequence[idx_a + 1:idx_b] if idx_b > idx_a + 1 else []
            if any(t.lower() in ("edit", "multiedit") for t in between):
                cycle_count += 1

        return cycle_count >= 2

    @staticmethod
    def _classify_activity(metrics: SessionMetrics) -> str:
        total = metrics.tool_calls_total or 1
        read_grep_ratio = (metrics.tool_read_count + metrics.tool_grep_count) / total
        edit_write_ratio = (metrics.tool_edit_count + metrics.tool_write_count) / total
        bash_ratio = metrics.tool_bash_count / total

        if read_grep_ratio > 0.50:
            return "research"
        if bash_ratio > 0.30:
            return "debugging"
        if edit_write_ratio > 0.40:
            if metrics.tool_edit_count > 10:
                return "refactoring"
            return "coding"
        return "mixed"

    @staticmethod
    def _extract_text(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    parts.append(item.get("text", ""))
                elif isinstance(item, str):
                    parts.append(item)
            return " ".join(parts)
        return ""


# ---------------------------------------------------------------------------
# Phase 3: Deep Analyzer (LLM-powered, selective)
# ---------------------------------------------------------------------------

class DeepAnalyzer:
    """LLM-based deep analysis of flagged sessions."""

    ANALYSIS_CATEGORIES = [
        "prompt", "hook", "template", "skill", "workflow", "architecture"
    ]

    SYSTEM_PROMPT = """You are a VNX orchestration system analyst. Analyze this Claude Code session summary and extract actionable improvement suggestions.

For each suggestion, specify:
- category: one of "prompt", "hook", "template", "skill", "workflow", "architecture"
- component: the specific VNX component (e.g. "dispatcher_v8", "receipt_processor", "gather_intelligence")
- current_behavior: what happens now
- suggested_improvement: what should change
- evidence: concrete evidence from the session
- priority: "critical", "high", "medium", or "low"

Respond with valid JSON:
{
  "patterns": ["list of successful patterns observed"],
  "bottlenecks": ["list of bottlenecks or inefficiencies"],
  "suggestions": [
    {
      "category": "prompt",
      "component": "component_name",
      "current_behavior": "...",
      "suggested_improvement": "...",
      "evidence": "...",
      "priority": "medium"
    }
  ]
}"""

    def should_deep_analyze(self, metrics: SessionMetrics,
                            flags: SessionFlags) -> bool:
        if flags.has_error_recovery:
            return True
        if metrics.total_output_tokens > DEEP_THRESHOLD_TOKENS:
            return True
        if flags.has_context_reset:
            return True
        if metrics.tool_calls_total > DEEP_THRESHOLD_TOOLS:
            return True
        return False

    def analyze_session(self, jsonl_path: Path,
                        metrics: SessionMetrics,
                        flags: SessionFlags) -> Optional[dict]:
        summary = self._build_session_summary(jsonl_path, metrics, flags)
        prompt = f"{self.SYSTEM_PROMPT}\n\n## Session Summary\n\n{summary}"

        result_text = None

        if LLM_STRATEGY in ("auto", "claude-only"):
            result_text = self._try_claude_max(prompt)

        if result_text is None and LLM_STRATEGY in ("auto", "ollama-only"):
            result_text = self._try_ollama(prompt)

        if result_text is None:
            log("WARNING", "No LLM available for deep analysis")
            return None

        return self._parse_response(result_text)

    def _build_session_summary(self, jsonl_path: Path,
                               metrics: SessionMetrics,
                               flags: SessionFlags) -> str:
        lines = [
            f"Session: {metrics.session_id}",
            f"Model: {metrics.session_model or 'unknown'}",
            f"Terminal: {metrics.terminal}",
            f"Project: {metrics.project_path}",
            f"Date: {metrics.session_date}",
            f"Duration: {metrics.duration_minutes} min",
            f"Tokens: {metrics.total_input_tokens:,} in / {metrics.total_output_tokens:,} out",
            f"Cache: {metrics.cache_read_tokens:,} read / {metrics.cache_creation_tokens:,} create",
            f"Tools: {metrics.tool_calls_total} total (Read={metrics.tool_read_count}, "
            f"Edit={metrics.tool_edit_count}, Bash={metrics.tool_bash_count}, "
            f"Grep={metrics.tool_grep_count}, Write={metrics.tool_write_count}, "
            f"Task={metrics.tool_task_count})",
            f"Activity: {flags.primary_activity}",
            f"Flags: error_recovery={flags.has_error_recovery}, "
            f"context_reset={flags.has_context_reset}, "
            f"large_refactor={flags.has_large_refactor}, "
            f"test_cycle={flags.has_test_cycle}",
            "",
            "## Key Messages (first/last 20 user messages):",
        ]

        user_messages = []
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    record = json.loads(line.strip())
                    if record.get("type") == "user":
                        content = record.get("message", {}).get("content", "")
                        text = HeuristicDetector._extract_text(content)
                        if text.strip():
                            user_messages.append(text[:200])
                except (json.JSONDecodeError, KeyError):
                    continue

        selected = user_messages[:20] + user_messages[-20:]
        for i, msg in enumerate(selected, 1):
            lines.append(f"  {i}. {msg}")

        return "\n".join(lines)

    @staticmethod
    def _try_claude_max(prompt: str) -> Optional[str]:
        try:
            result = subprocess.run(
                ["claude", "-p", "--output-format", "json", "--max-turns", "1"],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=90,
            )
            if result.returncode != 0:
                log("WARNING", f"Claude CLI failed: {result.stderr[:200]}")
                return None
            try:
                output = json.loads(result.stdout)
                return output.get("result", result.stdout)
            except json.JSONDecodeError:
                return result.stdout
        except FileNotFoundError:
            log("INFO", "Claude CLI not found")
            return None
        except subprocess.TimeoutExpired:
            log("WARNING", "Claude CLI timed out")
            return None
        except Exception as e:
            log("WARNING", f"Claude CLI error: {e}")
            return None

    @staticmethod
    def _try_ollama(prompt: str) -> Optional[str]:
        try:
            import urllib.request
            payload = json.dumps({
                "model": OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.3, "num_predict": 2048},
            }).encode("utf-8")
            req = urllib.request.Request(
                "http://localhost:11434/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return body.get("response", "")
        except Exception as e:
            log("INFO", f"Ollama not available: {e}")
            return None

    @staticmethod
    def _parse_response(text: str) -> Optional[dict]:
        json_match = re.search(r'\{[\s\S]*\}', text)
        if not json_match:
            return None
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError:
            return None


# ---------------------------------------------------------------------------
# Digest Generator
# ---------------------------------------------------------------------------

class DigestGenerator:
    """Generate a human-readable nightly digest markdown report."""

    def generate(self, run_date: str, run_stats: RunStats,
                 session_rows: List[dict], db_path: Path) -> str:
        lines = [
            f"# VNX Nightly Digest — {run_date}",
            "",
            "## Samenvatting",
            f"- **{run_stats.sessions_analyzed}** sessies geanalyseerd",
            f"- **{run_stats.sessions_deep}** sessies diep geanalyseerd (LLM)",
            f"- **{len(run_stats.suggestions)}** nieuwe verbeter-suggesties",
            f"- **{run_stats.total_tokens:,}** output tokens totaal",
            "",
        ]

        # Token overview per terminal
        terminal_stats: Dict[str, dict] = {}
        for row in session_rows:
            t = row.get("terminal", "unknown")
            if t not in terminal_stats:
                terminal_stats[t] = {"sessions": 0, "input": 0, "output": 0,
                                     "cache_read": 0, "cache_create": 0}
            s = terminal_stats[t]
            s["sessions"] += 1
            s["input"] += row.get("total_input_tokens", 0)
            s["output"] += row.get("total_output_tokens", 0)
            s["cache_read"] += row.get("cache_read_tokens", 0)
            s["cache_create"] += row.get("cache_creation_tokens", 0)

        if terminal_stats:
            lines.append("## Token Overzicht")
            lines.append("")
            lines.append("| Terminal | Sessies | Input | Output | Cache Hit% |")
            lines.append("|----------|---------|-------|--------|------------|")
            for t in sorted(terminal_stats.keys()):
                s = terminal_stats[t]
                total_cache = s["cache_read"] + s["cache_create"]
                cache_pct = (s["cache_read"] / total_cache * 100) if total_cache > 0 else 0
                lines.append(
                    f"| {t} | {s['sessions']} | "
                    f"{self._fmt_tokens(s['input'])} | "
                    f"{self._fmt_tokens(s['output'])} | "
                    f"{cache_pct:.0f}% |"
                )
            lines.append("")

        # Suggestions
        if run_stats.suggestions:
            lines.append(f"## Verbeter Suggesties ({len(run_stats.suggestions)} nieuw)")
            lines.append("")
            for sg in sorted(run_stats.suggestions,
                             key=lambda x: {"critical": 0, "high": 1, "medium": 2, "low": 3}
                             .get(x.get("priority", "medium"), 2)):
                prio = sg.get("priority", "medium").upper()
                cat = sg.get("category", "workflow")
                comp = sg.get("component", "")
                lines.append(f"### [{prio}] {cat.capitalize()}: {comp}")
                lines.append(f"**Huidige situatie**: {sg.get('current_behavior', 'N/A')}")
                lines.append(f"**Suggestie**: {sg.get('suggested_improvement', 'N/A')}")
                lines.append(f"**Bewijs**: {sg.get('evidence', 'N/A')}")
                lines.append("")

        # Trends (last 7 days from DB)
        trends = self._get_trends(db_path, run_date)
        if trends:
            lines.append("## Trends (laatste 7 dagen)")
            lines.append("")
            for trend_line in trends:
                lines.append(f"- {trend_line}")
            lines.append("")

        lines.append("---")
        lines.append(f"*Generated by VNX Conversation Analyzer v{ANALYZER_VERSION}*")
        return "\n".join(lines)

    def write_digest(self, markdown: str, run_date: str) -> Path:
        digest_dir = VNX_BASE / "reports" / "nightly"
        digest_dir.mkdir(parents=True, exist_ok=True)
        digest_path = digest_dir / f"digest_{run_date}.md"
        digest_path.write_text(markdown, encoding="utf-8")
        return digest_path

    @staticmethod
    def _fmt_tokens(n: int) -> str:
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n / 1_000:.0f}K"
        return str(n)

    @staticmethod
    def _get_trends(db_path: Path, run_date: str) -> List[str]:
        trends = []
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            week_ago = (datetime.strptime(run_date, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
            prev_week = (datetime.strptime(run_date, "%Y-%m-%d") - timedelta(days=14)).strftime("%Y-%m-%d")

            # This week tokens
            cur.execute(
                "SELECT SUM(total_output_tokens) as total "
                "FROM session_analytics WHERE session_date >= ?", (week_ago,))
            this_week = (cur.fetchone() or {}).get("total", 0) or 0

            cur.execute(
                "SELECT SUM(total_output_tokens) as total "
                "FROM session_analytics WHERE session_date >= ? AND session_date < ?",
                (prev_week, week_ago))
            last_week = (cur.fetchone() or {}).get("total", 0) or 0

            if last_week > 0:
                change = ((this_week - last_week) / last_week) * 100
                arrow = "+" if change > 0 else ""
                trends.append(f"Token verbruik: {arrow}{change:.0f}% vs vorige week")

            # Error recovery sessions
            cur.execute(
                "SELECT COUNT(*) as cnt FROM session_analytics "
                "WHERE session_date >= ? AND has_error_recovery = 1", (week_ago,))
            err_count = (cur.fetchone() or {}).get("cnt", 0) or 0
            trends.append(f"Error recovery sessies: {err_count} (laatste 7 dagen)")

            # Most common activity
            cur.execute(
                "SELECT primary_activity, COUNT(*) as cnt "
                "FROM session_analytics WHERE session_date >= ? "
                "GROUP BY primary_activity ORDER BY cnt DESC LIMIT 1", (week_ago,))
            row = cur.fetchone()
            if row:
                trends.append(f"Meest voorkomende activiteit: {row['primary_activity']} ({row['cnt']}x)")

            conn.close()
        except Exception:
            pass
        return trends


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class ConversationAnalyzer:
    """Orchestrate the full 4-phase pipeline."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.parser = SessionParser()
        self.detector = HeuristicDetector()
        self.deep = DeepAnalyzer()
        self.digest_gen = DigestGenerator()
        self.conn: Optional[sqlite3.Connection] = None

    def connect(self):
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row

    def close(self):
        if self.conn:
            self.conn.close()

    def find_unanalyzed_sessions(self, project_filter: Optional[str] = None,
                                  terminal_filter: Optional[str] = None,
                                  diagnostics: bool = False) -> List[Path]:
        if not CLAUDE_PROJECTS_DIR.exists():
            log("WARNING", f"Claude projects dir not found: {CLAUDE_PROJECTS_DIR}")
            if diagnostics:
                log("INFO", f"  Set CLAUDE_PROJECTS_DIR env var to override")
            return []

        if diagnostics:
            log("INFO", f"Session source: {CLAUDE_PROJECTS_DIR}")

        known_ids = set()
        if self.conn:
            cur = self.conn.cursor()
            cur.execute("SELECT session_id FROM session_analytics")
            known_ids = {row[0] for row in cur.fetchall()}

        if diagnostics:
            log("INFO", f"Already imported: {len(known_ids)} sessions")

        candidates = []
        project_counts: Dict[str, int] = {}
        for project_dir in CLAUDE_PROJECTS_DIR.iterdir():
            if not project_dir.is_dir():
                continue

            dir_name = project_dir.name
            if project_filter and project_filter not in dir_name:
                continue
            if terminal_filter:
                terminal = self.parser.detect_terminal(dir_name)
                if terminal != terminal_filter:
                    continue

            dir_candidates = []
            for jsonl_file in project_dir.glob("*.jsonl"):
                session_id = jsonl_file.stem
                if session_id in known_ids:
                    continue
                dir_candidates.append(jsonl_file)

            if dir_candidates:
                project_counts[dir_name] = len(dir_candidates)
                candidates.extend(dir_candidates)

        if diagnostics and project_counts:
            log("INFO", f"New sessions by project directory:")
            for dir_name, count in sorted(project_counts.items(), key=lambda x: -x[1])[:15]:
                terminal = self.parser.detect_terminal(dir_name)
                log("INFO", f"  [{terminal:>9}] {count:>4} sessions  {dir_name}")
            if len(project_counts) > 15:
                log("INFO", f"  ... and {len(project_counts) - 15} more project directories")

        candidates.sort(key=lambda p: p.stat().st_size, reverse=True)
        return candidates

    def analyze_session(self, jsonl_path: Path,
                        deep_allowed: bool = True) -> Tuple[Optional[dict], List[dict]]:
        log("ANALYZE", f"Parsing: {jsonl_path.name} ({jsonl_path.stat().st_size // 1024}KB)")

        # Phase 1: Parse
        metrics, messages = self.parser.parse_file(jsonl_path)

        # Phase 2: Heuristic detection
        flags = self.detector.detect_patterns(metrics, messages)
        log("INFO", f"  Activity={flags.primary_activity} "
                     f"err={flags.has_error_recovery} ctx={flags.has_context_reset} "
                     f"refactor={flags.has_large_refactor} test={flags.has_test_cycle}")

        # Phase 3: Deep analysis (selective)
        deep_result = None
        suggestions = []
        if deep_allowed and self.deep.should_deep_analyze(metrics, flags):
            log("ANALYZE", f"  Deep analyzing (flagged)...")
            deep_result = self.deep.analyze_session(jsonl_path, metrics, flags)
            if deep_result and "suggestions" in deep_result:
                for sg in deep_result["suggestions"]:
                    sg["session_id"] = metrics.session_id
                suggestions = deep_result.get("suggestions", [])

        # Phase 4: Store
        self._store_session(metrics, flags, deep_result)
        log("SUCCESS", f"  Stored: {metrics.session_id[:8]}... "
                        f"tokens={metrics.total_output_tokens:,}")

        row = {
            "terminal": metrics.terminal,
            "total_input_tokens": metrics.total_input_tokens,
            "total_output_tokens": metrics.total_output_tokens,
            "cache_read_tokens": metrics.cache_read_tokens,
            "cache_creation_tokens": metrics.cache_creation_tokens,
        }
        return row, suggestions

    def _store_session(self, metrics: SessionMetrics, flags: SessionFlags,
                       deep_result: Optional[dict]):
        cur = self.conn.cursor()
        cur.execute("""
            INSERT OR REPLACE INTO session_analytics (
                session_id, project_path, terminal, session_date,
                total_input_tokens, total_output_tokens,
                cache_creation_tokens, cache_read_tokens,
                tool_calls_total, tool_read_count, tool_edit_count,
                tool_bash_count, tool_grep_count, tool_write_count,
                tool_task_count, tool_other_count,
                message_count, user_message_count, assistant_message_count,
                duration_minutes,
                has_error_recovery, has_context_reset, context_reset_count,
                has_large_refactor, has_test_cycle, primary_activity,
                deep_analysis_json, deep_analysis_model, deep_analysis_at,
                file_size_bytes, analyzer_version, session_model, dispatch_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            metrics.session_id, metrics.project_path, metrics.terminal,
            metrics.session_date,
            metrics.total_input_tokens, metrics.total_output_tokens,
            metrics.cache_creation_tokens, metrics.cache_read_tokens,
            metrics.tool_calls_total, metrics.tool_read_count,
            metrics.tool_edit_count, metrics.tool_bash_count,
            metrics.tool_grep_count, metrics.tool_write_count,
            metrics.tool_task_count, metrics.tool_other_count,
            metrics.message_count, metrics.user_message_count,
            metrics.assistant_message_count, metrics.duration_minutes,
            flags.has_error_recovery, flags.has_context_reset,
            flags.context_reset_count,
            flags.has_large_refactor, flags.has_test_cycle,
            flags.primary_activity,
            json.dumps(deep_result) if deep_result else None,
            (deep_result.get("_model", "unknown") if deep_result else None),
            (datetime.now().isoformat() if deep_result else None),
            metrics.file_size_bytes, ANALYZER_VERSION,
            metrics.session_model or "unknown",
            metrics.dispatch_id or None,
        ))
        self.conn.commit()

    def _store_suggestions(self, suggestions: List[dict], digest_id: str):
        if not suggestions:
            return
        cur = self.conn.cursor()
        for sg in suggestions:
            cur.execute("""
                INSERT INTO improvement_suggestions (
                    session_id, category, component,
                    current_behavior, suggested_improvement,
                    evidence, priority, digest_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                sg.get("session_id", ""),
                sg.get("category", "workflow"),
                sg.get("component", ""),
                sg.get("current_behavior", ""),
                sg.get("suggested_improvement", ""),
                sg.get("evidence", ""),
                sg.get("priority", "medium"),
                digest_id,
            ))
        self.conn.commit()

    def _store_digest(self, run_date: str, stats: RunStats,
                      markdown: str, digest_path: Path):
        cur = self.conn.cursor()
        cur.execute("""
            INSERT OR REPLACE INTO nightly_digests (
                digest_date, sessions_analyzed, deep_analyzed,
                new_suggestions, total_tokens_used,
                digest_markdown, digest_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            run_date, stats.sessions_analyzed, stats.sessions_deep,
            len(stats.suggestions), stats.total_tokens,
            markdown, str(digest_path),
        ))
        self.conn.commit()

    def run(self, max_sessions: int = 50, deep_budget: int = 20,
            dry_run: bool = False,
            project_filter: Optional[str] = None,
            terminal_filter: Optional[str] = None):
        log("INFO", "Starting conversation analysis pipeline...")
        run_date = datetime.now().strftime("%Y-%m-%d")
        stats = RunStats()

        sessions = self.find_unanalyzed_sessions(project_filter, terminal_filter, diagnostics=dry_run)
        log("INFO", f"Found {len(sessions)} unanalyzed sessions")

        if not sessions:
            log("INFO", "Nothing to analyze")
            return stats

        sessions = sessions[:max_sessions]
        deep_remaining = deep_budget
        session_rows = []

        for i, jsonl_path in enumerate(sessions, 1):
            log("ANALYZE", f"[{i}/{len(sessions)}] {jsonl_path.parent.name}/{jsonl_path.name}")

            if dry_run:
                metrics, _ = self.parser.parse_file(jsonl_path)
                log("INFO", f"  [DRY RUN] tokens={metrics.total_output_tokens:,} "
                            f"tools={metrics.tool_calls_total}")
                stats.sessions_analyzed += 1
                stats.total_tokens += metrics.total_output_tokens
                continue

            try:
                deep_allowed = deep_remaining > 0
                row, suggestions = self.analyze_session(jsonl_path, deep_allowed)

                stats.sessions_analyzed += 1
                stats.total_tokens += row.get("total_output_tokens", 0)
                session_rows.append(row)

                if suggestions:
                    stats.sessions_deep += 1
                    deep_remaining -= 1
                    stats.suggestions.extend(suggestions)

            except Exception as e:
                log("ERROR", f"  Failed: {e}")
                stats.errors += 1

        if not dry_run:
            # Store suggestions
            digest_id = f"digest_{run_date}"
            self._store_suggestions(stats.suggestions, digest_id)

            # Generate and write digest
            markdown = self.digest_gen.generate(
                run_date, stats, session_rows, self.db_path)
            digest_path = self.digest_gen.write_digest(markdown, run_date)
            self._store_digest(run_date, stats, markdown, digest_path)

            log("SUCCESS", f"Digest written to: {digest_path}")

        # Summary
        print(f"\n{Colors.GREEN}{'=' * 70}")
        print("Conversation Analysis Complete!")
        print(f"{'=' * 70}{Colors.RESET}\n")
        print(f"Sessions Analyzed: {stats.sessions_analyzed}")
        print(f"Deep Analyzed:     {stats.sessions_deep}")
        print(f"Suggestions:       {len(stats.suggestions)}")
        print(f"Total Tokens:      {stats.total_tokens:,}")
        print(f"Errors:            {stats.errors}")

        if not dry_run and stats.sessions_analyzed > 0:
            digest_path = VNX_BASE / "reports" / "nightly" / f"digest_{run_date}.md"
            print(f"\nDigest: {digest_path}")

        return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="VNX Conversation Analyzer — Nightly Session Mining Pipeline")
    parser.add_argument("--max-sessions", type=int, default=50,
                        help="Max sessions to analyze per run")
    parser.add_argument("--deep-budget", type=int, default=20,
                        help="Max LLM deep analysis calls per run")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse sessions without storing or LLM calls")
    parser.add_argument("--project-filter",
                        help="Only analyze sessions from project matching this string")
    parser.add_argument("--terminal-filter",
                        help="Only analyze sessions from this terminal (T-MANAGER, T1, T2, T3)")
    args = parser.parse_args()

    print(f"\n{Colors.BLUE}{'=' * 70}")
    print(f"VNX Conversation Analyzer")
    print(f"Version: {ANALYZER_VERSION}")
    print(f"{'=' * 70}{Colors.RESET}\n")

    if not DB_PATH.exists():
        log("ERROR", f"Quality database not found: {DB_PATH}")
        log("INFO", "Run quality_db_init.py first")
        return 1

    analyzer = ConversationAnalyzer(DB_PATH)
    analyzer.connect()

    try:
        analyzer.run(
            max_sessions=args.max_sessions,
            deep_budget=args.deep_budget,
            dry_run=args.dry_run,
            project_filter=args.project_filter,
            terminal_filter=args.terminal_filter,
        )
        return 0
    except Exception as e:
        log("ERROR", f"Analysis failed: {e}")
        return 1
    finally:
        analyzer.close()


if __name__ == "__main__":
    sys.exit(main())
