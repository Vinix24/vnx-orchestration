"""Phase 3: LLM-powered deep analysis of flagged sessions."""

import json
import re
import subprocess
from pathlib import Path
from typing import Optional

from .models import (
    SessionMetrics, SessionFlags,
    LLM_STRATEGY, OLLAMA_MODEL,
    DEEP_THRESHOLD_TOKENS, DEEP_THRESHOLD_TOOLS,
    log,
)
from .detector import HeuristicDetector


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
